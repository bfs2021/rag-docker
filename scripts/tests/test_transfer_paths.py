"""Transfer and tuning routes with no test before (issue #387).

`on_conflict` validation, `IMPORT_IN_PROGRESS`, unknown job ids, export off the event loop,
`GET /packages`, the help page's dimension fallback and `HELP_RENDER_FAILED`, the
`TUNE_IN_PROGRESS` answer and a re-chunk that produces no chunks.

Runs with the API's dependencies, inside the verify project's API container
(`15_transfer_paths.sh`):

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_transfer_paths.py

Requests go through `main.app` without the lifespan. Weaviate, Ollama and the query pipeline are
mocked; nothing reaches a backend. Orderings are arranged with events, not timing.
"""
import asyncio
import json
import logging
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx
from fastapi.testclient import TestClient

from config import settings
from main import app
from services import collection_writes, exporter, importer, metrics, ollama_client, packager
from services import rag_pipeline, sources, tuning
from services import weaviate_client as wc

# Long enough for any ordering here to finish; a hang fails instead of blocking the suite.
WAIT = 10.0


def asgi_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://owned")


async def until(condition, what):
    deadline = time.monotonic() + WAIT
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for change in (patch.object(settings, "upload_dir", str(self.root / "uploads")),
                       patch.object(settings, "exports_dir", str(self.root / "exports")),
                       patch.object(settings, "sources_dir", str(self.root / "sources")),
                       patch.dict(importer._jobs, {}, clear=True),
                       patch.dict(exporter._jobs, {}, clear=True),
                       patch.dict(tuning._jobs, {}, clear=True)):
            stack.enter_context(change)
        (self.root / "uploads").mkdir()
        self.stack = stack
        self.client = TestClient(app, raise_server_exceptions=False)    # no lifespan
        self.addCleanup(self.client.close)

    def assert_error(self, response, status, code, message=None, detail=None):
        self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], code, error)
        if message is not None:
            self.assertEqual(error["message"], message)
        if detail is not None:
            self.assertEqual(error["detail"], detail)
        return error


class ImportRequestTests(RouteTestCase):
    def test_on_conflict_missing_null_or_unknown_is_422_and_starts_nothing(self):
        start = AsyncMock(side_effect=AssertionError("no import may start"))
        with patch.object(importer, "start_import_job", start):
            for body in ({"filename": "ragpkg-x.tar.gz"},
                         {"filename": "ragpkg-x.tar.gz", "on_conflict": None},
                         {"filename": "ragpkg-x.tar.gz", "on_conflict": "merge"},
                         {"filename": "ragpkg-x.tar.gz", "on_conflict": "ABORT"}):
                with self.subTest(body=body):
                    error = self.assert_error(self.client.post("/import", json=body), 422,
                                              "INVALID_PARAMETER", "Request parameters are invalid.")
                    self.assertEqual([item["loc"] for item in error["detail"]],
                                     [["body", "on_conflict"]])
        start.assert_not_called()
        self.assertEqual(dict(importer._jobs), {})

    def test_a_running_import_of_the_same_file_is_409_import_in_progress(self):
        release = threading.Event()
        ran = []

        def run(job_id, filename, on_conflict):
            # Stands in for the worker; frees the filename as the real one does.
            ran.append(job_id)
            release.wait(WAIT)
            importer._jobs[job_id]["status"] = "failed"
            with importer._lock:
                importer._active.discard(filename)

        async def scenario():
            async with asgi_client() as api:
                first = await api.post("/import", json={"filename": "ragpkg-a.tar.gz",
                                                         "on_conflict": "rename"})
                self.assertEqual(first.status_code, 202, first.text)
                await until(lambda: ran, "the first import to start")
                second = await api.post("/import", json={"filename": "ragpkg-a.tar.gz",
                                                          "on_conflict": "replace"})
                jobs_while_running = set(importer._jobs)
                release.set()
                await until(lambda: "ragpkg-a.tar.gz" not in importer._active, "the slot to free")
                third = await api.post("/import", json={"filename": "ragpkg-a.tar.gz",
                                                         "on_conflict": "abort"})
                await until(lambda: len(ran) == 2, "the third import to start")
                await until(lambda: not importer._active, "the third import to end")
                return first, second, jobs_while_running, third

        with patch.object(importer, "_run", run):
            first, second, jobs_while_running, third = asyncio.run(scenario())
        self.assert_error(second, 409, "IMPORT_IN_PROGRESS",
                          "An import of 'ragpkg-a.tar.gz' is already running.",
                          {"filename": "ragpkg-a.tar.gz"})
        self.assertEqual(jobs_while_running, {first.json()["job_id"]})
        self.assertEqual(third.status_code, 202, third.text)
        self.assertNotEqual(third.json()["job_id"], first.json()["job_id"])


class UnknownJobTests(RouteTestCase):
    def test_unknown_export_import_and_tune_jobs_are_404_job_not_found(self):
        for path in ("/export/job/nosuchjob", "/import/job/nosuchjob", "/tune/job/nosuchjob"):
            with self.subTest(path=path):
                self.assert_error(self.client.get(path), 404, "JOB_NOT_FOUND",
                                  "Job 'nosuchjob' not found.")


class ExportOffLoopTests(RouteTestCase):
    def test_a_query_is_answered_while_an_export_build_runs(self):
        release, started = threading.Event(), threading.Event()
        threads = {}

        def build(collection, include_models=False, progress=None):
            threads["build"] = threading.get_ident()
            started.set()
            if not release.wait(WAIT):
                raise AssertionError("the build was never released")
            return {"filename": "ragpkg-coll-x.tar.gz", "size_bytes": 1, "chunk_count": 0,
                    "source_document_count": 0, "fidelity": "chunks-only",
                    "models_bundled": False, "retrieve_script": False, "warnings": []}

        answer = {"answer": "Managers approve overtime.", "citations": None,
                  "retrieval_latency_ms": 1, "llm_latency_ms": 1, "chunks_retrieved": 1}

        async def scenario():
            threads["loop"] = threading.get_ident()
            async with asgi_client() as api:
                start = await api.post("/export", json={"collection": "Coll"})
                self.assertEqual(start.status_code, 202, start.text)
                job_id = start.json()["job_id"]
                await until(started.is_set, "the build to start")
                query = await asyncio.wait_for(
                    api.post("/query", json={"question": "who approves overtime?",
                                             "collection": "Other"}), 5)
                during = (await api.get(f"/export/job/{job_id}")).json()["status"]
                release.set()
                await until(lambda: exporter.get_job(job_id)["status"] == "completed",
                            "the export to complete")
                return query, during

        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(packager, "build", build), \
                patch.object(rag_pipeline, "run_query", AsyncMock(return_value=answer)), \
                patch.object(metrics, "record", AsyncMock()):
            try:
                query, during = asyncio.run(scenario())
            finally:
                release.set()
        self.assertEqual(query.status_code, 200, query.text)
        self.assertEqual(query.json()["answer"], answer["answer"])
        self.assertEqual(during, "running")
        self.assertNotEqual(threads["build"], threads["loop"])


class PackageListTests(RouteTestCase):
    def test_packages_lists_readable_and_unreadable_packages_only(self):
        exports = self.root / "exports"
        stage = self.root / "stage" / "ragpkg-a"
        stage.mkdir(parents=True)
        manifest = {"package_format": 1, "created_at": "2026-10-21T08:00:00Z",
                    "collection": {"name": "Corpus", "chunk_count": 12},
                    "fidelity": "with-sources"}
        (stage / "manifest.json").write_text(json.dumps(manifest))
        exports.mkdir()
        good = exports / "ragpkg-a-20261021T080000Z-0123abcd.tar.gz"
        with tarfile.open(good, "w:gz") as tar:
            tar.add(stage, arcname=stage.name)
        junk = exports / "ragpkg-b-junk.tar.gz"
        junk.write_text("not a package\n")
        for name in ("other.tar.gz", "notes.txt", ".ragpkg-c-x.tar.gz.partial"):
            (exports / name).write_text("ignored\n")
        response = self.client.get("/packages")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["packages"], [
            {"filename": good.name, "size_bytes": good.stat().st_size, "collection": "Corpus",
             "chunk_count": 12, "fidelity": "with-sources",
             "created_at": "2026-10-21T08:00:00Z", "readable": True},
            {"filename": junk.name, "size_bytes": junk.stat().st_size, "collection": None,
             "chunk_count": None, "fidelity": None, "created_at": None, "readable": False},
        ])


class HelpTests(RouteTestCase):
    def test_the_page_falls_back_to_words_when_the_embedding_probe_fails(self):
        probe = AsyncMock(side_effect=httpx.ConnectError("ollama is down"))
        with patch.object(ollama_client, "embed", probe), \
                self.assertLogs("routers.help", logging.WARNING) as logs:
            response = self.client.get("/help/transfer")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(f"`{settings.embed_model}` at its own number of dimensions.",
                      response.json()["markdown"])
        self.assertIn("Could not probe embedding dimensions", "\n".join(logs.output))

    def test_the_page_quotes_the_probed_width(self):
        with patch.object(ollama_client, "embed", AsyncMock(return_value=[0.0] * 768)):
            response = self.client.get("/help/transfer")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(f"`{settings.embed_model}` at 768 dimensions.", response.json()["markdown"])

    def test_a_template_that_cannot_render_is_help_render_failed(self):
        templates = self.root / "templates"
        (templates / "partials").mkdir(parents=True)
        cases = (("@@INCLUDE:missing_partial@@\n", "no such template partial: missing_partial"),
                 ("Width @@NOT_A_VALUE@@\n",
                  "help_transfer.md.tmpl: unsubstituted placeholder @@NOT_A_VALUE@@"))
        with patch.object(packager, "_TEMPLATE_DIR", templates), \
                patch.object(ollama_client, "embed", AsyncMock(return_value=[0.0] * 4)):
            for text, message in cases:
                with self.subTest(template=text):
                    (templates / "help_transfer.md.tmpl").write_text(text)
                    self.assert_error(self.client.get("/help/transfer"), 500,
                                      "HELP_RENDER_FAILED", message)


class TuneInProgressTests(RouteTestCase):
    def test_a_second_start_while_tuning_is_409_tune_in_progress(self):
        release = threading.Event()
        ran = []

        def run(job_id, collection, operation, params, *, source_collection=None):
            # Stands in for the worker; ends as the real one does.
            ran.append(job_id)
            release.wait(WAIT)
            collection_writes.end_job("tune", job_id)
            with tuning._lock:
                tuning._active.discard(collection)

        async def scenario():
            async with asgi_client() as api:
                first = await api.post("/tune/reindex", json={"collection": "Coll"})
                self.assertEqual(first.status_code, 202, first.text)
                await until(lambda: ran, "the first tune to start")
                refused = [await api.post("/tune/reindex", json={"collection": "coll"}),
                           await api.post("/tune/reembed", json={"collection": "Coll"})]
                jobs_while_running = set(tuning._jobs)
                release.set()
                await until(lambda: not tuning._active, "the first tune to end")
                return first, refused, jobs_while_running

        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(tuning, "_run", run):
            try:
                first, refused, jobs_while_running = asyncio.run(scenario())
            finally:
                release.set()
        for response in refused:
            self.assert_error(response, 409, "TUNE_IN_PROGRESS", "'Coll' is already being tuned.",
                              {"collection": "Coll"})
        self.assertEqual(jobs_while_running, {first.json()["job_id"]})
        self.assertEqual(collection_writes.active_jobs("Coll"), [])


class RechunkNoChunksTests(RouteTestCase):
    def test_a_rechunk_that_produces_no_chunks_fails_sources_required_before_rebuilding(self):
        sources.store("Coll", "blank.txt", b"   \n\n   \n", replace=True)
        stored = [SimpleNamespace(properties={"source_file": "blank.txt", "chunk_index": 0})]
        coll = SimpleNamespace(iterator=lambda **_: iter(stored))
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        job_id = "tunenone"
        tuning._jobs[job_id] = {"job_id": job_id, "status": "queued", "collection": "Coll",
                                "operation": "rechunk", "chunks_total": 0, "chunks_written": 0,
                                "notes": [], "error": None, "error_code": None,
                                "error_detail": None}
        chunking = {"strategy": "fixed", "chunk_size": 80, "chunk_overlap": 0,
                    "similarity_threshold": 0.85, "min_chunk_size": 0}
        with patch.object(wc, "get_client", return_value=client), \
                patch.object(tuning, "_rebuild", side_effect=AssertionError("nothing may be rebuilt")), \
                patch.object(tuning, "_log"):
            tuning._run(job_id, "Coll", "rechunk", {"chunking": chunking})
        job = tuning._jobs[job_id]
        self.assertEqual((job["status"], job["error_code"]), ("failed", "SOURCES_REQUIRED"),
                         job["error"])
        self.assertEqual(job["error"],
                         "Re-chunking 'Coll' produced no chunks; check the chunking parameters.")
        self.assertEqual(job["error_detail"], {"collection": "Coll"})
        self.assertEqual(sorted(p.name for p in (self.root / "uploads").glob("rechunk-*")), [])


if __name__ == "__main__":
    unittest.main()
