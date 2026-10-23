"""Per-collection state is keyed by the canonical collection name (issue #372).

Weaviate stores a collection under its name with the first character
upper-cased (`myDocs` becomes `MyDocs`). Every route that takes a collection
name now replaces it with that canonical name before using it, so the registry,
retained originals, saved settings, sessions, metrics and job guards all use
one spelling, whichever spelling the caller sent.

Runs with the API's dependencies, in the built API image with no network
(`02_ingest.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_collection_spelling.py

Each request goes through `main.app` with FastAPI's TestClient, without the
lifespan. Weaviate and the job workers are mocked; nothing reaches a backend.
"""
import collections
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from routers import collections as collections_router
from services import (exporter, goldstandard, ingest_config, ingest_pipeline, metrics,
                      rag_pipeline, retrieval_config, sources, tuning)
from services import weaviate_client as wc

TYPED, STORED = "myDocs", "MyDocs"


def listed(name):
    return {"name": name, "object_count": 0, "index_type": "hnsw",
            "distance_metric": "cosine", "hnsw_config": None}


class SpellingTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        # The settings, and the paths the modules cache on first use.
        for target, name, value in (
                (settings, "upload_dir", str(root / "uploads")),
                (settings, "sources_dir", str(root / "sources")),
                (collections_router, "_REGISTRY_FILE", None),
                (ingest_config, "_DIR", None),
                (retrieval_config, "_DIR", None),
                (metrics, "_METRICS_FILE", None),
                (metrics, "_ring", collections.deque(maxlen=metrics.MAX_RECORDS)),
                (wc, "collection_exists", AsyncMock(return_value=True))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def mock(self, target, name, **kwargs):
        patcher = patch.object(target, name, AsyncMock(**kwargs))
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked


class CollectionTests(SpellingTestCase):
    def test_create_registers_and_answers_the_stored_name(self):
        self.mock(wc, "collection_exists", return_value=False)
        create = self.mock(wc, "create_collection")
        self.mock(wc, "get_collections", return_value=[listed(STORED)])
        response = self.client.post("/collections", json={"name": TYPED})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"name": STORED, "status": "created"})
        self.assertEqual(create.await_args.kwargs["name"], STORED)
        self.assertEqual(list(collections_router._load_registry()), [STORED])
        listing = self.client.get("/collections").json()["collections"]
        self.assertIsNotNone(listing[0]["created_at"], listing)

    def test_delete_answers_the_stored_name(self):
        delete = self.mock(wc, "delete_collection_when_idle", return_value=3)
        response = self.client.delete(f"/collections/{TYPED}", params={"confirm": "true"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"name": STORED, "objects_deleted": 3})
        delete.assert_awaited_once_with(STORED)

    def test_a_missing_collection_is_named_as_stored(self):
        self.mock(wc, "collection_exists", return_value=False)
        response = self.client.post("/query", json={"question": "q", "collection": TYPED})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["message"], f"Collection '{STORED}' not found.")


class IngestTests(SpellingTestCase):
    def test_upload_starts_the_job_under_the_stored_name(self):
        start = self.mock(ingest_pipeline, "start_ingest_job", return_value="job-1")
        response = self.client.post("/ingest/upload", data={"collection": TYPED},
                                    files=[("files", ("a.txt", b"Some text.", "text/plain"))])
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["collection"], STORED)
        self.assertEqual(start.await_args.kwargs["collection"], STORED)

    def test_ingest_settings_have_one_file_read_under_either_spelling(self):
        body = {"collection": TYPED, "chunking_strategy": "fixed", "chunk_size": 300}
        saved = self.client.post("/ingest/config", json=body)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["collection"], STORED)
        for spelling in (TYPED, STORED):
            with self.subTest(spelling=spelling):
                read = self.client.get(f"/ingest/config/{spelling}").json()
                self.assertEqual((read["collection"], read["is_default"], read["chunk_size"]),
                                 (STORED, False, 300))
        self.assertEqual(sorted(p.name for p in ingest_config._dir().iterdir()), [f"{STORED}.json"])

    def test_retrieval_settings_have_one_file_read_under_either_spelling(self):
        saved = self.client.post("/retrieval/config", json={"collection": TYPED, "top_k": 7})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["collection"], STORED)
        for spelling in (TYPED, STORED):
            with self.subTest(spelling=spelling):
                read = self.client.get(f"/retrieval/config/{spelling}").json()
                self.assertEqual((read["collection"], read["is_default"], read["top_k"]),
                                 (STORED, False, 7))
        self.assertEqual(sorted(p.name for p in retrieval_config._dir().iterdir()), [f"{STORED}.json"])


class QueryMetricsTests(SpellingTestCase):
    def test_a_query_is_recorded_and_filtered_under_the_stored_name(self):
        run = self.mock(rag_pipeline, "run_query", return_value={
            "answer": "a", "citations": None, "retrieval_latency_ms": 5,
            "llm_latency_ms": 7, "chunks_retrieved": 1})
        response = self.client.post("/query", json={"question": "q", "collection": TYPED})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(run.await_args.kwargs["collection"], STORED)
        self.assertEqual([r["collection"] for r in metrics._ring], [STORED])
        for spelling in (TYPED, STORED):
            with self.subTest(spelling=spelling):
                summary = self.client.get("/metrics/latency", params={"collection": spelling}).json()
                self.assertEqual(summary["total_records"], 1, summary)
                self.assertEqual(summary["history"][0]["collection"], STORED)


class JobTests(SpellingTestCase):
    def test_export_starts_under_the_stored_name(self):
        # One export per collection is keyed by the name it is started with, so
        # both spellings now meet the same guard.
        start = self.mock(exporter, "start_export_job", return_value="exp-1")
        response = self.client.post("/export", json={"collection": TYPED})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["collection"], STORED)
        start.assert_awaited_once_with(STORED, False)

    def test_tune_starts_under_the_stored_name(self):
        start = self.mock(tuning, "start_tune_job", return_value="tune-1")
        for operation, extra in (("rechunk", {"chunking_strategy": "fixed"}),
                                 ("reembed", {}), ("reindex", {"index_type": "flat"})):
            with self.subTest(operation=operation):
                response = self.client.post(f"/tune/{operation}", json={"collection": TYPED, **extra})
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["collection"], STORED)
                self.assertEqual(start.await_args.args[:2], (STORED, operation))

    def test_tune_options_find_originals_retained_under_the_stored_name(self):
        sources.store(STORED, "a.txt", b"Some text.", "text/plain")
        with patch.object(tuning, "rechunk_refusal", return_value=None):
            response = self.client.get(f"/tune/{TYPED}")
        self.assertEqual(response.status_code, 200, response.text)
        options = response.json()
        self.assertEqual((options["collection"], options["fidelity"], options["can_rechunk"]),
                         (STORED, "with-sources", True))

    def test_generation_starts_under_the_stored_name(self):
        start = self.mock(goldstandard, "start_generation", return_value={
            "session_id": "gs_1", "status": "generating", "pairs_total": 1, "pairs_completed": 0})
        response = self.client.post("/goldstandard/generate", json={"collection": TYPED, "sample_size": 1})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(start.await_args.kwargs["collection"], STORED)


if __name__ == "__main__":
    unittest.main()
