"""Ingest, query, retrieval-config, metrics and health API paths no test covered (issue #399).

Each class names the feature-review items it covers (CI:I6, QR:2, PL:B1, ...). A check
that finds a bug is an expectedFailure naming its item and #399; nothing is fixed here.

Runs with the API's dependencies, in the built API image with no network
(`16_api_paths.sh`, run by `01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_api_paths.py

Requests go through `main.app` with FastAPI's TestClient, without the lifespan. Weaviate
and Ollama are mocked; nothing reaches a backend. Each test points UPLOAD_DIR at a fresh
temporary folder and starts with empty metrics and retrieval-config state.
"""
import asyncio
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx
from fastapi.testclient import TestClient

from config import settings
from main import app
from services import collection_writes, ingest_pipeline, metrics, ollama_client
from services import rag_pipeline, retrieval_config, system_info
from services import weaviate_client as wc

ROOT = Path(__file__).resolve().parents[2]
REAL_ASYNC_CLIENT = httpx.AsyncClient
REAL_OPEN = open

ANSWER = {"answer": "Managers approve overtime.", "citations": None,
          "retrieval_latency_ms": 12, "llm_latency_ms": 34, "chunks_retrieved": 3}
CHUNKS = [
    {"content": "Overtime needs a manager's approval.", "source_file": "policies.txt",
     "chunk_index": 4, "score": 0.91},
    {"content": "Requests go through the HR portal.", "source_file": "hr/guide.md",
     "chunk_index": 0, "score": 0.83},
]


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.upload_dir = Path(temp.name)
        for target, attribute, value in (
                (settings, "upload_dir", temp.name),
                # Module state a fresh API process starts with; both cache their paths.
                (metrics, "_ring", deque(maxlen=metrics.MAX_RECORDS)),
                (metrics, "_METRICS_FILE", None),
                (metrics, "_file_lines", 0),
                (retrieval_config, "_DIR", None)):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def assert_error(self, response, status, code, message=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/json"),
                        response.headers["content-type"])
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        if message is not None:
            self.assertEqual(error["message"], message)
        return error


# ── ingest (CI:I6, CI:I29) ───────────────────────────────────────────────────

def zip_of(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class IngestPathTests(ApiTestCase):
    def test_an_upload_with_no_supported_file_is_400_no_supported_files_listing_the_skipped(self):
        files = [("files", ("notes.xyz", b"plain text", "application/octet-stream")),
                 ("files", ("docs.zip", zip_of({"data.bin": b"\x00\x01"}), "application/zip"))]
        jobs_before = len(ingest_pipeline._jobs)
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)):
            response = self.client.post("/ingest/upload", data={"collection": "Coll"}, files=files)
        error = self.assert_error(
            response, 400, "NO_SUPPORTED_FILES",
            "No supported files found in upload. Skipped: notes.xyz (unsupported type '.xyz'); "
            "docs.zip:data.bin (unsupported type '.bin')")
        self.assertIsNone(error["detail"])
        # The whole request is refused: no job, no staging left, no registration.
        self.assertEqual(len(ingest_pipeline._jobs), jobs_before)
        self.assertEqual(sorted(p.name for p in self.upload_dir.glob("ingest-*")), [])
        self.assertEqual(collection_writes.active_jobs("Coll"), [])

    def test_an_unknown_job_id_is_404_job_not_found(self):
        response = self.client.get("/ingest/job/nosuchjob")
        error = self.assert_error(response, 404, "JOB_NOT_FOUND", "Job 'nosuchjob' not found.")
        self.assertIsNone(error["detail"])


# ── /query defaults, prompts and its metric (QR:2, QR:8, QR:12, QR:13, QR:19) ──

class QueryDefaultsTests(ApiTestCase):
    """QR:2: a body with only the question and collection runs with the documented defaults."""

    BODY = {"question": "who approves overtime?", "collection": "Coll"}

    def test_the_request_defaults_reach_the_pipeline(self):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(rag_pipeline, "run_query", AsyncMock(return_value=ANSWER)) as run:
            response = self.client.post("/query", json=self.BODY)
        self.assertEqual(response.status_code, 200, response.text)
        run.assert_awaited_once_with(
            question="who approves overtime?", collection="Coll", retrieval_mode="hnsw",
            top_k=5, alpha=0.75, include_citations=False, response_format="end_user")

    def test_the_defaults_drive_the_real_pipeline(self):
        chat = AsyncMock(side_effect=["overtime approval", "Managers approve it."])
        embed = AsyncMock(return_value=[0.1] * 768)
        search = AsyncMock(return_value=CHUNKS)
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(ollama_client, "chat", chat), \
                patch.object(ollama_client, "embed", embed), \
                patch.object(wc, "near_vector_query", search):
            response = self.client.post("/query", json=self.BODY)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["citations"])
        self.assertEqual(body["chunks_retrieved"], 2)
        # hnsw: an embedded vector search for the top 5.
        embed.assert_awaited_once_with("overtime approval")
        search.assert_awaited_once_with("Coll", [0.1] * 768, 5)
        # end_user: the plain-language synthesis prompt.
        self.assertEqual(chat.await_args_list[1].args[0], rag_pipeline.SYNTHESIS_END_USER_SYSTEM)


def spec_prompts(section):
    """The System and User code blocks under SPECIFICATIONS.md's `### <section>` heading."""
    text = (ROOT / "SPECIFICATIONS.md").read_text()
    start = text.index(f"\n### {section} ")
    end = text.index("\n### ", start + 1)
    part = text[start:end]
    blocks = {}
    for label in ("System", "User"):
        match = re.search(r"\*\*" + label + r":\*\*\n```\n(.*?)\n```", part, re.S)
        blocks[label] = match.group(1)
    return blocks["System"], blocks["User"]


class PromptTests(unittest.TestCase):
    """QR:8, QR:12, QR:13: the §6.1-§6.3 prompts and context formats, word for word."""

    QUESTION = "Who approves overtime?"

    def run_pipeline(self, response_format):
        chat = AsyncMock(side_effect=["overtime approval policy", "Managers do."])
        embed = AsyncMock(return_value=[0.2] * 768)
        with patch.object(ollama_client, "chat", chat), \
                patch.object(ollama_client, "embed", embed), \
                patch.object(wc, "near_vector_query", AsyncMock(return_value=CHUNKS)):
            asyncio.run(rag_pipeline.run_query(
                question=self.QUESTION, collection="Coll", retrieval_mode="hnsw", top_k=2,
                alpha=0.75, include_citations=False, response_format=response_format))
        self.assertEqual(chat.await_count, 2)
        # The search uses the rewritten question.
        embed.assert_awaited_once_with("overtime approval policy")
        return [call.args for call in chat.await_args_list]

    def test_the_reformulation_prompt_is_the_spec_text(self):
        system, user = spec_prompts("6.1")
        self.assertEqual(rag_pipeline.REFORMULATE_SYSTEM, system)
        for response_format in ("end_user", "engineer"):
            with self.subTest(response_format=response_format):
                reformulate = self.run_pipeline(response_format)[0]
                self.assertEqual(reformulate, (system, user.replace("{user_question}", self.QUESTION)))

    def test_the_end_user_prompt_and_context_are_the_spec_format(self):
        system, user = spec_prompts("6.2")
        self.assertEqual(rag_pipeline.SYNTHESIS_END_USER_SYSTEM, system)
        want = (user.replace("...\n\n", "")
                .replace("{chunk_1_text}", CHUNKS[0]["content"])
                .replace("{chunk_2_text}", CHUNKS[1]["content"])
                .replace("{original_question}", self.QUESTION))
        self.assertEqual(self.run_pipeline("end_user")[1], (system, want))

    def test_the_engineer_prompt_and_context_are_the_spec_format(self):
        system, user = spec_prompts("6.3")
        self.assertEqual(rag_pipeline.SYNTHESIS_ENGINEER_SYSTEM, system)
        want = user.replace("...\n\n", "").replace("{original_question}", self.QUESTION)
        for i, chunk in enumerate(CHUNKS, 1):
            want = (want.replace(f"{{source_file_{i}}}", chunk["source_file"])
                    .replace(f"{{chunk_index_{i}}}", str(chunk["chunk_index"]))
                    .replace(f"{{chunk_{i}_text}}", chunk["content"]))
        self.assertNotIn("{", want)
        self.assertEqual(self.run_pipeline("engineer")[1], (system, want))


class QueryMetricsTests(ApiTestCase):
    """QR:19: a successful query records one latency sample."""

    def test_a_successful_query_records_a_metric(self):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(rag_pipeline, "run_query", AsyncMock(return_value=ANSWER)):
            response = self.client.post("/query", json={"question": "q?", "collection": "Coll",
                                                        "retrieval_mode": "hybrid"})
        self.assertEqual(response.status_code, 200, response.text)
        summary = self.client.get("/metrics/latency").json()
        self.assertEqual(summary["total_records"], 1)
        self.assertEqual(len(summary["history"]), 1)
        record = summary["history"][0]
        self.assertEqual({k: record[k] for k in ("collection", "retrieval_mode", "retrieval_ms",
                                                 "llm_ms", "total_ms")},
                         {"collection": "Coll", "retrieval_mode": "hybrid", "retrieval_ms": 12,
                          "llm_ms": 34, "total_ms": 46})
        self.assertTrue(record["timestamp"])
        lines = (self.upload_dir / "metrics.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["total_ms"], 46)


# ── retrieval config file (QR:24, QR:25) ──────────────────────────────────────

DEFAULTS = {"collection": "Coll", "retrieval_mode": "hnsw", "top_k": 5, "alpha": 0.75,
            "ef": None, "response_format": "end_user"}


class RetrievalConfigFileTests(ApiTestCase):
    def saved_file(self):
        return retrieval_config._dir() / "Coll.json"

    def get_config(self):
        response = self.client.get("/retrieval/config/Coll")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_an_older_file_missing_keys_is_filled_from_the_defaults(self):
        # QR:24: saved before `ef` and `response_format` existed.
        self.saved_file().write_text(json.dumps(
            {"collection": "Coll", "retrieval_mode": "hybrid", "top_k": 9, "alpha": 0.4}))
        self.assertEqual(self.get_config(), {**DEFAULTS, "retrieval_mode": "hybrid", "top_k": 9,
                                             "alpha": 0.4, "is_default": False})

    def assert_falls_back(self):
        with self.assertLogs("services.retrieval_config", "WARNING") as logs:
            body = self.get_config()
        self.assertEqual(body, {**DEFAULTS, "is_default": True})
        self.assertIn("'Coll'", "\n".join(logs.output))

    def test_a_file_that_is_not_json_falls_back_to_the_defaults(self):
        # QR:25
        self.saved_file().write_text('{"retrieval_mode": "hybrid", ')
        self.assert_falls_back()

    def test_a_file_that_cannot_be_read_falls_back_to_the_defaults(self):
        # QR:25: a directory where the file belongs can't be read as one.
        self.saved_file().mkdir()
        self.assert_falls_back()

    def test_a_file_of_json_that_is_not_an_object_falls_back_to_the_defaults(self):
        # QR:25, found by #399 and fixed by #439: a saved JSON array made resolve() raise
        # TypeError and the route answer 500 INTERNAL_ERROR.
        self.saved_file().write_text("[1, 2]")
        self.assert_falls_back()

    def test_other_json_that_is_not_an_object_falls_back_to_the_defaults(self):
        # #439: a number, a string or a boolean, like the array above.
        for text in ("3", '"settings"', "true"):
            with self.subTest(saved=text):
                self.saved_file().write_text(text)
                self.assert_falls_back()

    def test_a_field_of_the_wrong_type_falls_back_to_the_defaults(self):
        # #439: a value the GET response can't carry made the route answer 500.
        saved = {"collection": "Coll", "retrieval_mode": "hybrid", "top_k": 9, "alpha": 0.4,
                 "ef": None, "response_format": "engineer"}
        for field, value in (("top_k", "x"), ("retrieval_mode", None), ("alpha", "x"),
                             ("ef", "x"), ("response_format", ["engineer"])):
            with self.subTest(field=field, value=value):
                self.saved_file().write_text(json.dumps({**saved, field: value}))
                self.assert_falls_back()

    def test_coercible_and_out_of_range_values_are_still_served_as_saved(self):
        # #439 falls back only for what can't be served; these were served before it.
        self.saved_file().write_text(json.dumps({"top_k": "7"}))
        self.assertEqual(self.get_config(), {**DEFAULTS, "top_k": 7, "is_default": False})
        self.saved_file().write_text(json.dumps({"top_k": 0, "ef": 513}))
        self.assertEqual(self.get_config(), {**DEFAULTS, "top_k": 0, "ef": 513,
                                             "is_default": False})

    def test_a_fractional_or_nested_value_falls_back_to_the_defaults(self):
        # #439: other values the GET response can't carry; each answered 500 before it.
        for value in (1.5, {"a": 1}, [7]):
            with self.subTest(top_k=value):
                self.saved_file().write_text(json.dumps({"top_k": value}))
                self.assert_falls_back()

    def test_a_saved_is_default_key_falls_back_to_the_defaults(self):
        # #477: a hand-edited file holding the response-only key `is_default` passed
        # resolve()'s check, then the router's RetrievalConfigResponse(is_default=...,
        # **cfg) got the keyword twice and the route answered 500 INTERNAL_ERROR.
        for value in (True, False):
            with self.subTest(is_default=value):
                self.saved_file().write_text(json.dumps({"top_k": 9, "is_default": value}))
                self.assert_falls_back()

    def test_a_non_finite_alpha_falls_back_to_the_defaults(self):
        # #477: JSON has no NaN or Infinity, so the response sent `"alpha": null` with
        # `is_default: false`; a finite alpha out of range is still served as saved.
        for text in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(alpha=text):
                self.saved_file().write_text('{"alpha": %s}' % text)
                self.assert_falls_back()
        self.saved_file().write_text(json.dumps({"alpha": 1.5}))
        self.assertEqual(self.get_config(), {**DEFAULTS, "alpha": 1.5, "is_default": False})


# ── GET /metrics/latency (PL:B1, PL:B2, PL:B3) ───────────────────────────────

def put_records(records):
    """Records as metrics.record builds them, oldest first."""
    for n, (collection, mode, retrieval_ms, llm_ms) in enumerate(records):
        metrics._ring.append({
            "ts": f"2026-10-21T00:{n // 60:02d}:{n % 60:02d}.000000+00:00",
            "collection": collection, "retrieval_mode": mode, "retrieval_ms": retrieval_ms,
            "llm_ms": llm_ms, "total_ms": retrieval_ms + llm_ms, "chunks_retrieved": 3})


ZERO = {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "count": 0}


class MetricsRouteTests(ApiTestCase):
    def latency(self, **params):
        response = self.client.get("/metrics/latency", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_no_records_is_all_zeros(self):
        self.assertEqual(self.latency(), {"total_records": 0, "retrieval_latency": ZERO,
                                          "llm_latency": ZERO, "total_latency": ZERO,
                                          "history": []})

    def test_the_statistics(self):
        put_records([("Coll", "hnsw", 10, 100), ("Coll", "hnsw", 20, 200),
                     ("Coll", "hnsw", 30, 300), ("Coll", "hnsw", 40, 400)])
        body = self.latency()
        self.assertEqual(body["total_records"], 4)
        # Linear interpolation between the closest ranks: position (n - 1) * p / 100.
        # p50 at 1.5, p95 at 2.85, p99 at 2.97, of values sorted ascending.
        self.assertEqual(body["retrieval_latency"],
                         {"p50": 25.0, "p95": 38.5, "p99": 39.7, "mean": 25.0, "count": 4})
        self.assertEqual(body["llm_latency"],
                         {"p50": 250.0, "p95": 385.0, "p99": 397.0, "mean": 250.0, "count": 4})
        self.assertEqual(body["total_latency"],
                         {"p50": 275.0, "p95": 423.5, "p99": 436.7, "mean": 275.0, "count": 4})
        self.assertEqual([h["total_ms"] for h in body["history"]], [110, 220, 330, 440])

    def test_one_record(self):
        put_records([("Coll", "flat", 7, 70)])
        body = self.latency()
        self.assertEqual(body["retrieval_latency"],
                         {"p50": 7.0, "p95": 7.0, "p99": 7.0, "mean": 7.0, "count": 1})

    def test_the_collection_and_mode_filters_match_exactly(self):
        put_records([("Alpha", "hnsw", 10, 1), ("Alpha", "hybrid", 20, 1),
                     ("Beta", "hnsw", 30, 1), ("AlphaTwo", "hnsw", 40, 1)])
        for params, want in (({"collection": "Alpha"}, [10, 20]),
                             ({"retrieval_mode": "hnsw"}, [10, 30, 40]),
                             ({"collection": "Alpha", "retrieval_mode": "hnsw"}, [10]),
                             ({"collection": "Gamma"}, []),
                             ({"retrieval_mode": "hyb"}, [])):
            with self.subTest(params=params):
                body = self.latency(**params)
                self.assertEqual([h["retrieval_ms"] for h in body["history"]], want)
                self.assertEqual(body["total_records"], len(want))
                self.assertEqual(body["retrieval_latency"]["count"], len(want))

    def test_history_limit(self):
        put_records([("Coll", "hnsw", n, 1) for n in range(150)])
        body = self.latency()
        # Default 100: the newest 100, oldest first.
        self.assertEqual([h["retrieval_ms"] for h in body["history"]], list(range(50, 150)))
        timestamps = [h["timestamp"] for h in body["history"]]
        self.assertEqual(timestamps, sorted(timestamps))
        # The statistics still cover every record.
        self.assertEqual(body["total_records"], 150)
        self.assertEqual(body["retrieval_latency"]["count"], 150)
        body = self.latency(history_limit=0)
        self.assertEqual(body["history"], [])
        self.assertEqual(body["total_records"], 150)
        self.assertEqual([h["retrieval_ms"] for h in self.latency(history_limit=3)["history"]],
                         [147, 148, 149])

    def test_history_limit_500_returns_the_whole_ring(self):
        put_records([("Coll", "hnsw", n, 1) for n in range(600)])
        body = self.latency(history_limit=500)
        self.assertEqual([h["retrieval_ms"] for h in body["history"]], list(range(100, 600)))
        self.assertEqual(body["total_records"], 500)

    def test_history_limit_over_500_is_422(self):
        response = self.client.get("/metrics/latency", params={"history_limit": 501})
        self.assert_error(response, 422, "INVALID_PARAMETER")


# ── /health probes (PL:A3, PL:A5) ────────────────────────────────────────────

class HealthProbeTests(ApiTestCase):
    def ollama_ok(self):
        """Ollama's /api/tags, answered by a mock transport, listing both models."""
        def tags(request):
            names = [settings.llm_model, settings.embed_model]
            return httpx.Response(200, json={"models": [{"name": n + ":latest"} for n in names]})

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(tags)
            return REAL_ASYNC_CLIENT(*args, **kwargs)
        return patch.object(httpx, "AsyncClient", client_factory)

    def test_a_hung_weaviate_check_is_cut_off_at_4_seconds(self):
        # PL:A3: the real wc.check_health, with the client call itself hanging.
        # The hung call is released only once /health has moved on to the Ollama probe,
        # so the probe can't have waited for it. (Released, the worker thread lets the
        # TestClient's event loop close; a real server's loop doesn't wait for it.)
        release = threading.Event()
        hung = []
        real_ollama_check = ollama_client.check_health

        def hang():
            hung.append(time.monotonic())
            release.wait(60)
            return True

        async def ollama_check():
            hung.append(time.monotonic())
            release.set()
            return await real_ollama_check()
        # Runs before the client closes (cleanups run last-in first-out), freeing the thread.
        self.addCleanup(release.set)
        with self.ollama_ok(), patch.object(wc, "_check_health_sync", hang), \
                patch.object(ollama_client, "check_health", ollama_check):
            started = time.monotonic()
            response = self.client.get("/health")
            elapsed = time.monotonic() - started
        self.assertEqual(len(hung), 2, "the Weaviate check never hung, or Ollama wasn't probed")
        # The Weaviate probe gave up about 4 s after the hung call began.
        self.assertGreaterEqual(hung[1] - hung[0], 3.9)
        self.assertLess(hung[1] - hung[0], 6.0)
        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["services"]["weaviate"]["status"], "error")
        self.assertGreaterEqual(elapsed, 3.9)
        self.assertLess(elapsed, 6.0)
        self.assertGreaterEqual(body["services"]["weaviate"]["latency_ms"], 3900)
        self.assertLess(body["services"]["weaviate"]["latency_ms"], 6000)
        self.assertEqual(body["services"]["ollama"]["llm"]["status"], "ok")

    def meminfo(self, content=None, error=None):
        """`open` as system_info sees it, with /proc/meminfo replaced."""
        def fake_open(path, *args, **kwargs):
            if str(path) != "/proc/meminfo":
                return REAL_OPEN(path, *args, **kwargs)
            if error is not None:
                raise error
            return io.StringIO(content)
        return patch.object(system_info, "open", fake_open, create=True)

    UNREADABLE = {
        "cannot be opened": {"error": PermissionError(13, "Permission denied")},
        "is missing": {"error": FileNotFoundError(2, "No such file or directory")},
        "has no MemTotal line": {"content": "MemFree: 1024 kB\nBuffers: 2 kB\n"},
        "has a malformed MemTotal": {"content": "MemTotal: lots kB\n"},
        "has an empty MemTotal": {"content": "MemTotal:\n"},
    }

    def test_unreadable_meminfo_reports_unknown_memory_with_a_note(self):
        # PL:A5
        for case, kwargs in self.UNREADABLE.items():
            with self.subTest(case=case), self.meminfo(**kwargs):
                self.assertEqual(system_info.memory_info(12.0), {
                    "status": "unknown", "allocated_gb": None, "recommended_minimum_gb": 12.0,
                    "note": "Could not read /proc/meminfo to determine allocated memory."})

    def test_unknown_memory_never_fails_health(self):
        # PL:A5: the memory report is advisory.
        with self.meminfo(error=PermissionError(13, "Permission denied")), self.ollama_ok(), \
                patch.object(wc, "check_health", AsyncMock(return_value=True)):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["resources"]["memory"]["status"], "unknown")
        self.assertIsNone(body["resources"]["memory"]["allocated_gb"])
        self.assertTrue(body["resources"]["memory"]["note"])

    def test_a_readable_meminfo_is_still_read(self):
        # The fake reaches the real parser: 12 GiB in kB is ok against 12.0.
        with self.meminfo(content="MemTotal:       12582912 kB\nMemFree: 1 kB\n"):
            self.assertEqual(system_info.memory_info(12.0),
                             {"status": "ok", "allocated_gb": 12.0, "recommended_minimum_gb": 12.0})


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
