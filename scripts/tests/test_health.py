"""GET /health answers 503 with the per-service body when a dependency is down (issue #375).

The Health page reads that body to show the failing services in red, so the body is
checked here: a model missing from Ollama, Ollama unreachable, Weaviate unreachable,
and Weaviate and Ollama down together. Memory below the recommendation, or unreadable,
is advisory: still 200 `ok` (#415). `/health` itself is unchanged by #375 and #415;
this pins what the page relies on.

Runs with the API's dependencies, in the built API image with no network
(`01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_health.py

Each request goes through `main.app` with FastAPI's TestClient, without the lifespan.
Ollama's `/api/tags` is answered by an httpx mock transport, so `ollama_client.check_health`
runs unchanged; the Weaviate client check is mocked, and so is the memory reading when
a case sets it. Nothing reaches a backend.
"""
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx
from fastapi.testclient import TestClient

from config import settings
from main import app
from services import system_info
from services import weaviate_client as wc

REAL_ASYNC_CLIENT = httpx.AsyncClient
UNSET = object()


def tagged(model):
    """A model name as Ollama lists it, with a tag."""
    return model if ":" in model else model + ":latest"


class HealthTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def get_health(self, *, models=None, ollama_down=False, weaviate=True, memory_gb=UNSET):
        """GET /health with Ollama listing `models` (or refusing connections),
        Weaviate's client check returning, or raising, `weaviate`, and, when given,
        `memory_gb` as the Docker VM's memory (None: unreadable)."""
        if models is None:
            models = [settings.llm_model, settings.embed_model]

        def ollama(request):
            if ollama_down:
                raise httpx.ConnectError("connection refused", request=request)
            if request.url.path != "/api/tags":
                return httpx.Response(404)
            return httpx.Response(200, json={"models": [{"name": tagged(m)} for m in models]})

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(ollama)
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        check = (AsyncMock(side_effect=weaviate) if isinstance(weaviate, BaseException)
                 else AsyncMock(return_value=weaviate))
        memory = (patch.object(system_info, "_read_mem_total_gb", return_value=memory_gb)
                  if memory_gb is not UNSET else contextlib.nullcontext())
        with patch.object(httpx, "AsyncClient", client_factory), \
                patch.object(wc, "check_health", check), memory:
            return self.client.get("/health")

    def assert_body(self, response, status_code, overall, weaviate, llm, embed):
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/json"),
                        response.headers["content-type"])
        body = response.json()
        self.assertEqual(body["status"], overall)
        services = body["services"]
        self.assertEqual(services["weaviate"]["status"], weaviate)
        self.assertIsInstance(services["weaviate"]["latency_ms"], int)
        for key, want, model in (("llm", llm, settings.llm_model), ("embed", embed, settings.embed_model)):
            service = services["ollama"][key]
            self.assertEqual(service["status"], want, key)
            self.assertEqual(service["model"], model)
            self.assertIsInstance(service["latency_ms"], int)
        # Advisory resources are still reported when degraded.
        self.assertIn("memory", body["resources"])
        return body


class HealthBodyTests(HealthTestCase):
    def test_all_services_ok_is_200_ok(self):
        self.assert_body(self.get_health(), 200, "ok", "ok", "ok", "ok")

    def test_a_missing_llm_model_is_503_degraded_naming_it(self):
        response = self.get_health(models=[settings.embed_model])
        self.assert_body(response, 503, "degraded", "ok", "error", "ok")

    def test_a_missing_embedding_model_is_503_degraded_naming_it(self):
        response = self.get_health(models=[settings.llm_model])
        self.assert_body(response, 503, "degraded", "ok", "ok", "error")

    def test_ollama_unreachable_is_503_degraded_with_both_models_in_error(self):
        response = self.get_health(ollama_down=True)
        self.assert_body(response, 503, "degraded", "ok", "error", "error")

    def test_weaviate_not_ready_is_503_degraded(self):
        response = self.get_health(weaviate=False)
        self.assert_body(response, 503, "degraded", "error", "ok", "ok")

    def test_weaviate_unreachable_is_503_degraded(self):
        for cause in (ConnectionError("connection refused"), TimeoutError("connect timed out")):
            with self.subTest(cause=type(cause).__name__):
                response = self.get_health(weaviate=cause)
                self.assert_body(response, 503, "degraded", "error", "ok", "ok")

    def test_weaviate_and_ollama_down_together_is_503_with_every_service_in_error(self):
        response = self.get_health(ollama_down=True, weaviate=ConnectionError("connection refused"))
        self.assert_body(response, 503, "degraded", "error", "error", "error")


class HealthMemoryTests(HealthTestCase):
    """Memory is advisory: a resource that isn't ok never degrades /health (#415)."""

    def test_memory_below_recommended_is_200_ok_and_reported(self):
        body = self.assert_body(self.get_health(memory_gb=1.0), 200, "ok", "ok", "ok", "ok")
        memory = body["resources"]["memory"]
        self.assertEqual(memory["status"], "below_recommended")
        self.assertEqual(memory["allocated_gb"], 1.0)
        self.assertEqual(memory["recommended_minimum_gb"], round(settings.recommended_memory_gb, 1))
        self.assertIn("note", memory)

    def test_memory_unknown_is_200_ok_and_reported(self):
        body = self.assert_body(self.get_health(memory_gb=None), 200, "ok", "ok", "ok", "ok")
        memory = body["resources"]["memory"]
        self.assertEqual(memory["status"], "unknown")
        self.assertIsNone(memory["allocated_gb"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
