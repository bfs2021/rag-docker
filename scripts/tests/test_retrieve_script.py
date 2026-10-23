"""The packaged retrieve.py runs, takes its flags and exits as specified (E8-E10, issue #387).

RAG_EXPORT_SPECIFICATIONS.md section 5.1: standard library only, baked defaults overridden by flags,
exit codes 0 ok, 2 bad usage, 3 unreachable (a gateway status and a timeout too), 4 collection
absent (only the enveloped COLLECTION_NOT_FOUND), 5 API error (a bare 404 too), each failure one
sentence and a hint, never a traceback.

The script is rendered by the exporter's own `packager._render_retrieve` and run with
`python -I -S` (no site-packages) against a stub API on 127.0.0.1. Runs with the API's
dependencies, inside the verify project's API container (`15_transfer_paths.sh`):

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_retrieve_script.py
"""
import ast
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from services import packager

BAKED = {"collection": "Policies", "retrieval_mode": "hybrid", "top_k": 6, "alpha": 0.5,
         "ef": None, "response_format": "engineer"}
ANSWER = {"answer": "Managers approve overtime.",
          "citations": [{"source_file": "policies.txt", "chunk_index": 2, "score": 0.9123,
                         "excerpt": "Overtime must be approved by a manager."}],
          "retrieval_latency_ms": 12, "llm_latency_ms": 345, "chunks_retrieved": 1}
STDLIB = {"argparse", "json", "sys", "urllib.error", "urllib.request"}


class Stub(BaseHTTPRequestHandler):
    """Answers every POST with the server's configured reply and records the request."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append((self.path, json.loads(body.decode("utf-8"))))
        status, payload, delay = self.server.reply
        if delay and self.server.release.wait(delay):
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RetrieveScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.script = Path(temp.name) / "retrieve.py"
        cls.source = packager._render_retrieve("Policies", dict(BAKED), {
            "package_filename": "ragpkg-policies-20261021T000000Z-0123abcd.tar.gz",
            "id8": "0123abcd", "created_at": "2026-10-21T00:00:00Z",
            "embed_model": "nomic-embed-text", "embed_dimensions": 768})
        cls.script.write_text(cls.source)

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        self.server.requests = []
        self.server.release = threading.Event()
        self.server.daemon_threads = True
        self.reply(200, ANSWER)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.release.set)
        self.api = "http://127.0.0.1:%d/api" % self.server.server_address[1]

    def reply(self, status, payload, delay=0):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.server.reply = (status, data, delay)

    def run_script(self, *args, api=True):
        argv = [sys.executable, "-I", "-S", str(self.script)]
        argv += (["--api-url", self.api] if api else []) + list(args)
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)

    def assert_failure(self, result, code, sentence):
        self.assertEqual(result.returncode, code, result.stderr)
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 2, result.stderr)
        self.assertTrue(lines[0].startswith("error: "), lines)
        self.assertIn(sentence, lines[0])
        self.assertTrue(lines[1].startswith("       ") and lines[1].strip(), lines)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.stdout, "")

    # -- E8: it runs on the standard library alone -----------------------------

    def test_the_script_imports_only_the_standard_library(self):
        imported = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, STDLIB)

    def test_a_question_is_answered_with_the_baked_defaults(self):
        result = self.run_script("who approves overtime?")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Managers approve overtime.\n")
        self.assertIn("retrieved in 12ms, generated in 345ms", result.stderr)
        self.assertEqual(self.server.requests, [("/api/query", {
            "question": "who approves overtime?", "collection": "Policies",
            "retrieval_mode": "hybrid", "top_k": 6, "alpha": 0.5, "include_citations": False,
            "response_format": "engineer"})])

    # -- E9: every flag overrides its baked default ----------------------------

    def test_flags_override_the_baked_defaults(self):
        self.api += "/"            # a trailing slash is not doubled
        result = self.run_script("--top-k", "10", "--mode", "flat", "--alpha", "0.25",
                                 "--format", "end_user", "--collection", "Imported",
                                 "--citations", "--timeout", "30", "who approves overtime?")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.requests, [("/api/query", {
            "question": "who approves overtime?", "collection": "Imported",
            "retrieval_mode": "flat", "top_k": 10, "alpha": 0.25, "include_citations": True,
            "response_format": "end_user"})])
        self.assertIn("Sources:\n  - policies.txt (chunk 2, score 0.912)", result.stdout)
        self.assertIn("Overtime must be approved by a manager.", result.stdout)

    def test_json_prints_the_raw_response(self):
        result = self.run_script("--json", "who approves overtime?")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ANSWER)

    def test_help_shows_the_baked_and_fixed_defaults(self):
        result = self.run_script("--help", api=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = " ".join(result.stdout.split())
        for default in ("(default: http://localhost:8080/api)", "(default: Policies)",
                        "(default: hybrid)", "(default: 6)", "(default: 0.5)",
                        "(default: engineer)", "(default: 600)"):
            self.assertIn(default, text)

    # -- E10: exit codes -------------------------------------------------------

    def test_bad_usage_exits_2_without_a_request(self):
        for args, sentence in ((["   "], "The question is empty."),
                               (["--top-k", "0", "q"], "--top-k must be between 1 and 50, got 0."),
                               (["--top-k", "51", "q"], "--top-k must be between 1 and 50, got 51."),
                               (["--alpha", "1.5", "q"], "--alpha must be between 0.0 and 1.0"),
                               (["--timeout", "0", "q"], "--timeout must be greater than 0")):
            with self.subTest(args=args):
                self.assert_failure(self.run_script(*args), 2, sentence)
        for args in (["--mode", "fuzzy", "q"], []):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.server.requests, [])

    def test_an_unreachable_api_exits_3(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed = "http://127.0.0.1:%d/api" % sock.getsockname()[1]
        result = self.run_script("--api-url", closed, "q", api=False)
        self.assert_failure(result, 3, "Cannot reach the API at %s" % closed)

    def test_a_gateway_status_exits_3_whatever_the_body(self):
        for status in (502, 503, 504):
            for body in (b"<html>Bad Gateway</html>",
                         {"error": {"code": "SERVICE_UNAVAILABLE", "message": "down"}}):
                with self.subTest(status=status, body=body):
                    self.reply(status, body)
                    self.assert_failure(self.run_script("q"), 3,
                                        "(HTTP %d)" % status)

    def test_a_timeout_exits_3_with_its_own_message(self):
        self.reply(200, ANSWER, delay=5)
        result = self.run_script("--timeout", "0.5", "q")
        self.assert_failure(result, 3, "The API did not answer within 0.5 seconds.")

    def test_an_enveloped_collection_not_found_exits_4(self):
        self.reply(404, {"error": {"code": "COLLECTION_NOT_FOUND",
                                   "message": "Collection 'Policies' not found.", "detail": None}})
        self.assert_failure(self.run_script("q"), 4, 'The API has no collection named "Policies".')

    def test_a_bare_404_exits_5_not_4(self):
        for body in (b"Not Found", {"detail": "Not Found"}):
            with self.subTest(body=body):
                self.reply(404, body)
                self.assert_failure(self.run_script("q"), 5, "No rag-docker API answered at")

    def test_another_error_status_exits_5_quoting_the_api(self):
        self.reply(422, {"error": {"code": "INVALID_PARAMETER",
                                   "message": "Request parameters are invalid.", "detail": []}})
        self.assert_failure(self.run_script("q"), 5,
                            "The API rejected the query: Request parameters are invalid.")
        self.reply(500, b"Internal Server Error")
        self.assert_failure(self.run_script("q"), 5, "The API rejected the query:")

    def test_a_response_that_is_not_json_exits_5(self):
        self.reply(200, b"<html>not json</html>")
        self.assert_failure(self.run_script("q"), 5, "The API returned a response that is not JSON.")


if __name__ == "__main__":
    unittest.main()
