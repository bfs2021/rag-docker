"""Every API error answers in the JSON envelope; dependency failures are 503 (issue #373).

Runs with the API's dependencies, in the built API image with no network
(`01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_error_envelope.py

Each request goes through `main.app` with FastAPI's TestClient, without the
lifespan. Weaviate and Ollama are mocked; nothing reaches a backend.
"""
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx
from fastapi.testclient import TestClient

from config import settings
from main import app
from services import metrics, ollama_client, packager
from services import weaviate_client as wc

# Text that stands for a backend's own error message: it must be logged, never sent.
CAUSE = "SENTINEL backend detail /private/path"

QUESTION = {"question": "who approves overtime?", "collection": "Coll", "top_k": 3}


def ollama_request():
    return httpx.Request("POST", "http://ollama:11434/api/chat")


def logged(logs):
    """What the captured records put in the log, tracebacks and their causes included."""
    return "\n".join(logging.Formatter().format(record) for record in logs.records)


class EnvelopeTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        upload_dir = patch.object(settings, "upload_dir", temp.name)
        upload_dir.start()
        self.addCleanup(upload_dir.stop)
        # An unhandled error must come back as a response, as a real client sees it.
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def assert_error(self, response, status, code, message=None, detail=None):
        self.assertEqual(response.status_code, status, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/json"),
                        response.headers["content-type"])
        error = response.json()["error"]
        self.assertEqual(error["code"], code)
        if message is not None:
            self.assertEqual(error["message"], message)
        self.assertTrue(error["message"])
        self.assertEqual(error["detail"], detail)
        self.assertNotIn("SENTINEL", response.text)
        return error


class RoutingErrorTests(EnvelopeTestCase):
    def test_an_unknown_route_is_404_not_found(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = self.client.request(method, "/no-such-route")
                self.assert_error(response, 404, "NOT_FOUND", "No API route matches this path.")

    def test_a_wrong_method_is_405_method_not_allowed_with_its_allow_header(self):
        response = self.client.put("/query", json=QUESTION)
        self.assert_error(response, 405, "METHOD_NOT_ALLOWED",
                          "This API route does not accept this method.")
        self.assertEqual(response.headers["allow"], "POST")

    def test_a_route_404_keeps_its_own_code(self):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=False)):
            response = self.client.post("/query", json=QUESTION)
        self.assert_error(response, 404, "COLLECTION_NOT_FOUND")

    def test_other_http_errors_keep_fastapi_answer(self):
        # A body FastAPI can't parse is its own 400, outside the routing envelope.
        response = self.client.post("/ingest/upload", content=b"not multipart",
                                    headers={"content-type": "multipart/form-data"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertIn("detail", response.json())
        self.assertNotIn("error", response.json())

    def test_request_validation_is_still_422_invalid_parameter(self):
        response = self.client.post("/query", json={**QUESTION, "top_k": 0})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "INVALID_PARAMETER")


class UnhandledErrorTests(EnvelopeTestCase):
    def test_an_unhandled_exception_is_500_internal_error_logged_not_sent(self):
        # A malformed Ollama answer is not a transport error: nothing handles it.
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(ollama_client, "chat", AsyncMock(side_effect=KeyError(CAUSE))), \
                patch.object(metrics, "record", AsyncMock()) as record, \
                self.assertLogs("main", "ERROR") as logs:
            response = self.client.post("/query", json=QUESTION)
        self.assert_error(response, 500, "INTERNAL_ERROR",
                          "The API hit an unexpected error. The cause is in the API log.")
        self.assertIn("POST /query", logged(logs))
        self.assertIn("KeyError", logged(logs))
        self.assertIn(CAUSE, logged(logs))
        record.assert_not_awaited()


class CollectionListTests(EnvelopeTestCase):
    MESSAGE = ("Cannot list the collections: Weaviate could not be read. "
               "Try again when Weaviate is available.")

    def test_weaviate_unreachable_or_timing_out_is_503(self):
        for cause in (ConnectionError(CAUSE), TimeoutError(CAUSE)):
            with self.subTest(cause=type(cause).__name__), \
                    patch.object(wc, "get_collections", AsyncMock(side_effect=cause)), \
                    self.assertLogs("routers.collections", "ERROR") as logs:
                response = self.client.get("/collections")
                self.assert_error(response, 503, "SERVICE_UNAVAILABLE", self.MESSAGE)
                self.assertIn(CAUSE, logged(logs))

    def test_a_healthy_list_is_unchanged(self):
        listed = [{"name": "Coll", "object_count": 2, "index_type": "hnsw",
                   "distance_metric": "cosine", "hnsw_config": None}]
        with patch.object(wc, "get_collections", AsyncMock(return_value=listed)):
            response = self.client.get("/collections")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["collections"][0]["name"], "Coll")


class QueryPipelineTests(EnvelopeTestCase):
    def setUp(self):
        super().setUp()
        exists = patch.object(wc, "collection_exists", AsyncMock(return_value=True))
        record = patch.object(metrics, "record", AsyncMock())
        exists.start()
        self.addCleanup(exists.stop)
        self.record = record.start()
        self.addCleanup(record.stop)

    def message(self, service):
        return (f"The question was not answered: {service} could not be reached, timed out "
                f"or returned an error. Try again when {service} is available.")

    WEAVIATE_OR_EMBEDDING = (
        "The question was not answered: Weaviate could not be reached, timed out or returned "
        "an error, or Ollama could not embed the question for it. Try again when Weaviate and "
        "Ollama are available.")

    def ask(self, mode="hnsw", chat=None, embed=None, near_vector=None, hybrid=None,
            near_text=None):
        chunk = {"content": "Managers approve overtime.", "source_file": "a.txt",
                 "chunk_index": 0, "score": 0.1}
        mocks = {
            (ollama_client, "chat"): chat or AsyncMock(return_value="an answer"),
            (ollama_client, "embed"): embed or AsyncMock(return_value=[0.0, 1.0]),
            (wc, "near_vector_query"): near_vector or AsyncMock(return_value=[chunk]),
            (wc, "hybrid_query"): hybrid or AsyncMock(return_value=[chunk]),
            (wc, "near_text_query"): near_text or AsyncMock(return_value=[chunk]),
        }
        patches = [patch.object(module, name, mock) for (module, name), mock in mocks.items()]
        for p in patches:
            p.start()
        try:
            return self.client.post("/query", json={**QUESTION, "retrieval_mode": mode})
        finally:
            for p in patches:
                p.stop()

    def assert_unavailable(self, response, service, logs, message=None):
        self.assert_error(response, 503, "SERVICE_UNAVAILABLE", message or self.message(service),
                          {"collection": "Coll"})
        self.assertIn(CAUSE, logged(logs))
        self.record.assert_not_awaited()

    def test_ollama_unreachable_timing_out_or_failing_is_503(self):
        failures = {
            "connect error": httpx.ConnectError(CAUSE, request=ollama_request()),
            "timeout": httpx.ReadTimeout(CAUSE, request=ollama_request()),
            "error status": httpx.HTTPStatusError(
                CAUSE, request=ollama_request(),
                response=httpx.Response(500, request=ollama_request())),
        }
        for case, failure in failures.items():
            with self.subTest(case=case), self.assertLogs("routers.query", "ERROR") as logs:
                response = self.ask(chat=AsyncMock(side_effect=failure))
                self.assert_unavailable(response, "Ollama", logs)

    def test_each_ollama_call_in_the_pipeline_is_covered(self):
        down = httpx.ConnectError(CAUSE, request=ollama_request())
        cases = {
            "query embedding": dict(embed=AsyncMock(side_effect=down)),
            "answer synthesis": dict(chat=AsyncMock(side_effect=["a search query", down])),
        }
        for case, mocks in cases.items():
            with self.subTest(case=case), self.assertLogs("routers.query", "ERROR") as logs:
                response = self.ask(**mocks)
                self.assert_unavailable(response, "Ollama", logs)

    def test_the_engineer_answer_synthesis_is_covered(self):
        down = httpx.ConnectError(CAUSE, request=ollama_request())
        chat = AsyncMock(side_effect=["a search query", down])
        with patch.object(ollama_client, "chat", chat), \
                patch.object(ollama_client, "embed", AsyncMock(return_value=[0.0, 1.0])), \
                patch.object(wc, "near_vector_query", AsyncMock(return_value=[])), \
                self.assertLogs("routers.query", "ERROR") as logs:
            response = self.client.post("/query", json={**QUESTION,
                                                         "response_format": "engineer"})
        self.assertEqual(chat.await_count, 2)
        self.assert_unavailable(response, "Ollama", logs)

    def test_a_weaviate_query_failing_is_503_in_every_retrieval_mode(self):
        # In hybrid and semantic modes Weaviate embeds the question through Ollama, so
        # an Ollama failure there comes back as a Weaviate error: both are named (#411).
        cases = {"hnsw": ("near_vector", None), "flat": ("near_vector", None),
                 "hybrid": ("hybrid", self.WEAVIATE_OR_EMBEDDING),
                 "semantic": ("near_text", self.WEAVIATE_OR_EMBEDDING)}
        for mode, (call, message) in cases.items():
            with self.subTest(mode=mode), self.assertLogs("routers.query", "ERROR") as logs:
                response = self.ask(mode, **{call: AsyncMock(side_effect=ConnectionError(CAUSE))})
                self.assert_unavailable(response, "Weaviate", logs, message)
                named = "Weaviate (or Ollama, embedding the question for it) did not answer"
                if message:
                    self.assertIn(named, logged(logs))
                else:
                    self.assertIn("Weaviate did not answer", logged(logs))

    def test_a_healthy_query_is_unchanged(self):
        response = self.ask()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["answer"], "an answer")
        self.record.assert_awaited_once()


class HelpTests(EnvelopeTestCase):
    def test_a_missing_help_template_is_help_render_failed_logged_not_sent(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        with patch.object(packager, "_TEMPLATE_DIR", Path(empty.name)), \
                patch.object(ollama_client, "embed", AsyncMock(return_value=[0.0] * 3)), \
                self.assertLogs("routers.help", "ERROR") as logs:
            response = self.client.get("/help/transfer")
        self.assert_error(response, 500, "HELP_RENDER_FAILED",
                          "The help page could not be rendered: a help template file could "
                          "not be read.")
        self.assertNotIn(empty.name, response.text)
        self.assertIn(empty.name, logged(logs))

    def test_a_template_runtime_error_keeps_its_own_message(self):
        with patch.object(packager, "render_help",
                          side_effect=RuntimeError("Template has an unknown placeholder.")), \
                patch.object(ollama_client, "embed", AsyncMock(return_value=[0.0] * 3)):
            response = self.client.get("/help/transfer")
        self.assert_error(response, 500, "HELP_RENDER_FAILED",
                          "Template has an unknown placeholder.")


class CreateDeleteTests(EnvelopeTestCase):
    def test_a_failed_create_sends_a_fixed_message_and_logs_the_cause(self):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=False)), \
                patch.object(wc, "create_collection", AsyncMock(side_effect=RuntimeError(CAUSE))), \
                self.assertLogs("routers.collections", "ERROR") as logs:
            response = self.client.post("/collections", json={"name": "Coll"})
        self.assert_error(response, 500, "CREATE_FAILED", "Failed to create collection.")
        self.assertIn(CAUSE, logged(logs))
        self.assertIn("'Coll'", logged(logs))

    def test_a_failed_delete_sends_a_fixed_message_and_logs_the_cause(self):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(wc, "delete_collection_when_idle",
                             AsyncMock(side_effect=RuntimeError(CAUSE))), \
                self.assertLogs("routers.collections", "ERROR") as logs:
            response = self.client.delete("/collections/Coll?confirm=true")
        self.assert_error(response, 500, "DELETE_FAILED", "Failed to delete collection.")
        self.assertIn(CAUSE, logged(logs))
        self.assertIn("'Coll'", logged(logs))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
