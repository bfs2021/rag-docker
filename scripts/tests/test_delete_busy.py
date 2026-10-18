"""Deleting a collection while a job is active for it (issue #40).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_delete_busy.py

Weaviate is mocked; nothing reaches a backend. Orderings are arranged with
events and the guard's own waiter count, not with sleeps.
"""
import asyncio
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx
from fastapi import UploadFile

from config import settings
from main import app
from routers import collections as collection_routes
from services import collection_writes as writes
from services import exporter, goldstandard, importer, ingest_pipeline, tuning
from services import weaviate_client as wc

DELETED = "collection 'OwnedBusy' was deleted while this job was running"


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.005)


class Backend:
    """The collections that exist, and the backend deletions the route made."""

    def __init__(self, *names):
        self.names = set(names)
        self.deleted = []

    def exists(self, name):
        return writes.canonical(name) in self.names

    def delete(self, name):
        self.deleted.append(name)
        self.names.discard(writes.canonical(name))
        return 2


class BusyTestCase(unittest.TestCase):
    def has_chunks(self, collection, source_file):
        if not self.backend.exists(collection):
            raise RuntimeError(f"could not find class {collection} in schema")
        return False

    def setUp(self):
        self.addCleanup(writes._active_jobs.clear)
        writes._active_jobs.clear()
        self.backend = Backend("OwnedBusy")
        self.registry = {}
        stack = ExitStack()
        self.addCleanup(stack.close)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for change in [
            patch.object(wc, "_collection_exists_sync", side_effect=self.backend.exists),
            # No name has earlier chunks, so nothing here replaces (#22); a
            # deleted collection makes the lookup fail, as the backend does.
            patch.object(wc, "_source_has_chunks_sync", side_effect=self.has_chunks),
            patch.object(wc, "_delete_collection_sync", side_effect=self.backend.delete),
            patch.object(collection_routes, "_load_registry", side_effect=lambda: dict(self.registry)),
            patch.object(collection_routes, "_save_registry", side_effect=self.save_registry),
            patch.object(settings, "upload_dir", str(self.root)),
        ]:
            stack.enter_context(change)

    def save_registry(self, registry):
        self.registry.clear(); self.registry.update(registry)

    def delete(self, name="OwnedBusy"):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://owned") as api:
                return await api.delete(f"/collections/{name}?confirm=true")
        return asyncio.run(run())

    def assert_busy(self, response, kind, job_id, phrase):
        self.assertEqual(response.status_code, 409, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "COLLECTION_BUSY")
        self.assertTrue(error["message"].startswith(phrase), error["message"])
        self.assertIn(job_id, error["message"])
        self.assertEqual(error["detail"]["job_kind"], kind)
        self.assertEqual(error["detail"]["job_id"], job_id)
        self.assertEqual(error["detail"]["jobs"][0], {"job_kind": kind, "job_id": job_id})
        self.assertEqual(self.backend.deleted, [])
        self.assertIn("OwnedBusy", self.backend.names)


class RegistryTests(BusyTestCase):
    def test_alias_order_and_repeatable_end(self):
        writes.register_job("ownedBusy", "ingest", "a")
        writes.register_job("OwnedBusy", "export", "b")
        writes.register_job("Other", "ingest", "c")
        self.assertEqual(writes.active_jobs("OwnedBusy"), [("ingest", "a"), ("export", "b")])
        self.assertEqual(writes.active_jobs("ownedBusy"), [("ingest", "a"), ("export", "b")])
        writes.end_job("ingest", "a")
        writes.end_job("ingest", "a")
        writes.end_job("tune", "never-registered")
        self.assertEqual(writes.active_jobs("OwnedBusy"), [("export", "b")])
        writes.end_job("export", "b")
        self.assertEqual(writes.active_jobs("OwnedBusy"), [])
        self.assertNotIn("OwnedBusy", writes._active_jobs)
        self.assertEqual(writes.active_jobs("Other"), [("ingest", "c")])


class RouteTests(BusyTestCase):
    def test_idle_collection_deletes_and_missing_is_404(self):
        self.registry.update({"OwnedBusy": "2026-10-17T00:00:00+00:00"})
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"name": "OwnedBusy", "objects_deleted": 2})
        self.assertEqual(self.backend.deleted, ["OwnedBusy"])
        self.assertNotIn("OwnedBusy", self.registry)
        missing = self.delete("OwnedMissing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "COLLECTION_NOT_FOUND")

    def test_every_job_is_named_and_alias_spelling_is_refused(self):
        writes.register_job("OwnedBusy", "tune", "t1")
        writes.register_job("OwnedBusy", "ingest", "i1")
        response = self.delete("ownedBusy")
        self.assert_busy(response, "tune", "t1", "A tuning job is running for this collection")
        self.assertEqual(response.json()["error"]["detail"]["jobs"],
                         [{"job_kind": "tune", "job_id": "t1"}, {"job_kind": "ingest", "job_id": "i1"}])
        self.assertEqual(response.json()["error"]["detail"]["collection"], "ownedBusy")


class OrderingTests(BusyTestCase):
    def test_a_registered_job_refuses_without_waiting_for_the_guard(self):
        writes.register_job("OwnedBusy", "ingest", "j1")
        held = threading.Event(); release = threading.Event()
        def holder():
            with writes.guard("OwnedBusy"):
                held.set(); release.wait(5)
        with ThreadPoolExecutor() as pool:
            task = pool.submit(holder); self.assertTrue(held.wait(2))
            started = time.monotonic()
            response = self.delete()
            elapsed = time.monotonic() - started
            release.set(); task.result(timeout=2)
        self.assert_busy(response, "ingest", "j1", "An ingest job is running for this collection")
        self.assertLess(elapsed, 0.5)

    def test_a_job_registered_while_the_delete_waits_refuses_promptly(self):
        held = threading.Event(); release = threading.Event()
        def holder():
            with writes.guard("OwnedBusy"):
                held.set(); release.wait(10)
        with ThreadPoolExecutor() as pool:
            task = pool.submit(holder); self.assertTrue(held.wait(2))
            deleting = pool.submit(self.delete)
            # The delete is waiting once the guard counts a second party.
            wait_until(lambda: writes._registry.get("OwnedBusy", [None, 0])[1] >= 2)
            writes.register_job("OwnedBusy", "ingest", "late")
            started = time.monotonic()
            response = deleting.result(timeout=2)
            elapsed = time.monotonic() - started
            self.assertFalse(release.is_set())          # the guard was still held
            release.set(); task.result(timeout=2)
        self.assert_busy(response, "ingest", "late", "An ingest job is running for this collection")
        self.assertLess(elapsed, 1.0)

    def test_a_job_registered_once_the_delete_holds_the_guard_refuses(self):
        real_guard = writes.guard
        @contextmanager
        def guard_then_register(collection, timeout=None):
            with real_guard(collection, timeout=timeout):
                writes.register_job(collection, "export", "e-late")
                yield
        with patch.object(writes, "guard", guard_then_register):
            response = self.delete()
        self.assert_busy(response, "export", "e-late", "An export job is running for this collection")

    def test_a_job_admitted_after_the_delete_fails_truthfully_at_its_first_store(self):
        self.assertEqual(self.delete().status_code, 200)
        path = self.root / "small.txt"; path.write_text("Owned text")
        job_id = "after-delete"
        job = {"status": "queued", "files_total": 1, "files_completed": 0, "files_failed": 0,
               "chunks_stored": 0, "errors": []}
        writes.register_job("OwnedBusy", "ingest", job_id)
        rejected = RuntimeError("Weaviate rejected 1 batch object(s): class 'OwnedBusy' not present in schema")
        with patch.dict(ingest_pipeline._jobs, {job_id: job}), \
             patch.object(ingest_pipeline, "_parse_file", return_value=("Owned text", [])), \
             patch.object(ingest_pipeline, "do_chunk", return_value=["Owned text"]), \
             patch.object(wc, "_insert_chunks_sync", side_effect=rejected), \
             patch.object(ingest_pipeline.sources, "store") as store:
            ingest_pipeline._process_job_sync(job_id, [path], self.root, "OwnedBusy",
                                              "fixed", 300, 0, 0.85, 0)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"],
                          job["chunks_stored"], job["errors"]), ("failed", 0, 1, 0, [DELETED]))
        store.assert_not_called()
        self.assertEqual(writes.active_jobs("OwnedBusy"), [])
        self.assertEqual(self.backend.names, set())


class IngestOutcomeTests(BusyTestCase):
    def run_job(self, names, insert, parse=None):
        paths = []
        for name in names:
            path = self.root / name; path.write_text(name); paths.append(path)
        job_id = "outcome"
        job = {"status": "queued", "files_total": len(paths), "files_completed": 0,
               "files_failed": 0, "chunks_stored": 0, "errors": []}
        parsed = []
        def default_parse(path):
            parsed.append(path.name); return path.name, []
        with patch.dict(ingest_pipeline._jobs, {job_id: job}), \
             patch.object(ingest_pipeline, "_parse_file", side_effect=parse or default_parse), \
             patch.object(ingest_pipeline, "do_chunk", side_effect=lambda text, **_: [text]), \
             patch.object(wc, "_insert_chunks_sync", side_effect=insert), \
             patch.object(ingest_pipeline.sources, "store") as store:
            ingest_pipeline._process_job_sync(job_id, paths, self.root, "OwnedBusy",
                                              "fixed", 300, 0, 0.85, 0)
        return job, parsed, store

    def test_a_collection_deleted_mid_job_stops_the_job(self):
        def insert(collection, chunks):
            if chunks[0]["source_file"] == "two.txt":
                self.backend.names.discard("OwnedBusy")
                raise RuntimeError("Weaviate rejected 1 batch object(s)")
        job, parsed, store = self.run_job(["one.txt", "two.txt", "three.txt"], insert)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"],
                          job["chunks_stored"], job["errors"]), ("failed", 1, 2, 1, [DELETED]))
        self.assertEqual(parsed, ["one.txt", "two.txt"])
        self.assertEqual(store.call_count, 1)

    def test_a_store_failure_with_the_collection_present_is_reported_as_before(self):
        def insert(collection, chunks):
            raise RuntimeError("Owned backend failure")
        job, _, _ = self.run_job(["one.txt"], insert)
        self.assertEqual((job["status"], job["errors"]), ("failed", ["one.txt: Owned backend failure"]))

    def test_a_parse_failure_does_not_consult_existence(self):
        def parse(path):
            raise ValueError("Owned parse failure")
        wc._collection_exists_sync.reset_mock()
        job, _, _ = self.run_job(["one.txt"], lambda *a: None, parse=parse)
        wc._collection_exists_sync.assert_not_called()
        self.assertEqual((job["status"], job["errors"]), ("failed", ["one.txt: Owned parse failure"]))

    def test_a_failed_existence_check_keeps_the_original_error(self):
        wc._collection_exists_sync.side_effect = OSError("Owned check failure")
        def insert(collection, chunks):
            raise RuntimeError("Owned backend failure")
        job, _, _ = self.run_job(["one.txt"], insert)
        self.assertEqual(job["errors"], ["one.txt: Owned backend failure"])


class KindRegistrationTests(BusyTestCase):
    """Each kind refuses a delete while active, and is gone once its status is terminal."""

    def assert_released_by_terminal_status(self, read_status, kind, job_id):
        wait_until(lambda: read_status() in ("completed", "failed", "partial", "cancelled"))
        # The registration ends before the status turns terminal.
        self.assertNotIn((kind, job_id), writes.active_jobs("OwnedBusy"))

    def upload(self, name, data):
        return UploadFile(file=io.BytesIO(data), filename=name)

    def test_ingest(self):
        gate = threading.Event(); parsing = threading.Event()
        def parse(path):
            parsing.set(); assert gate.wait(5); return "Owned text", []
        async def run():
            job_id = await ingest_pipeline.start_ingest_job(
                [self.upload("one.txt", b"Owned text")], "OwnedBusy", "fixed", 300, 0, 0.85, 0)
            await asyncio.to_thread(parsing.wait, 5)
            response = await asyncio.to_thread(self.delete)
            gate.set()
            await asyncio.to_thread(self.assert_released_by_terminal_status,
                                    lambda: ingest_pipeline._jobs[job_id]["status"], "ingest", job_id)
            return job_id, response
        with patch.object(ingest_pipeline, "_parse_file", side_effect=parse), \
             patch.object(ingest_pipeline, "do_chunk", return_value=["Owned text"]), \
             patch.object(wc, "_insert_chunks_sync"), \
             patch.object(ingest_pipeline.sources, "store"):
            job_id, response = asyncio.run(run())
        self.assert_busy(response, "ingest", job_id, "An ingest job is running for this collection")
        self.assertEqual(ingest_pipeline._jobs[job_id]["status"], "completed")

    def test_ingest_admission_failures_leave_no_registration(self):
        async def run(files):
            return await ingest_pipeline.start_ingest_job(files, "OwnedBusy", "fixed", 300, 0, 0.85, 0)
        with self.assertRaises(ValueError):
            asyncio.run(run([self.upload("notes.xyz", b"unsupported")]))
        self.assertEqual(writes.active_jobs("OwnedBusy"), [])
        with self.assertRaises(ingest_pipeline.UploadRejected):
            asyncio.run(run([self.upload("broken.zip", b"PK\x03\x04 not a zip")]))
        self.assertEqual(writes.active_jobs("OwnedBusy"), [])

    def test_export(self):
        gate = threading.Event(); building = threading.Event()
        def build(collection, **_):
            building.set(); assert gate.wait(5)
            return {"filename": "x", "size_bytes": 1, "chunk_count": 1, "source_document_count": 0,
                    "fidelity": "chunks-only", "models_bundled": False, "retrieve_script": None,
                    "warnings": []}
        async def run():
            job_id = await exporter.start_export_job("OwnedBusy")
            await asyncio.to_thread(building.wait, 5)
            response = await asyncio.to_thread(self.delete)
            gate.set()
            await asyncio.to_thread(self.assert_released_by_terminal_status,
                                    lambda: exporter.get_job(job_id)["status"], "export", job_id)
            return job_id, response
        with patch.object(exporter.packager, "build", side_effect=build):
            job_id, response = asyncio.run(run())
        self.assert_busy(response, "export", job_id, "An export job is running for this collection")
        self.assertEqual(exporter.get_job(job_id)["status"], "completed")

    def test_tune(self):
        gate = threading.Event(); reading = threading.Event()
        def existing(collection):
            reading.set(); assert gate.wait(5); return [{"content": "Owned"}]
        async def run():
            job_id = await tuning.start_tune_job("OwnedBusy", "reembed", {"chunking": None})
            await asyncio.to_thread(reading.wait, 5)
            response = await asyncio.to_thread(self.delete)
            gate.set()
            await asyncio.to_thread(self.assert_released_by_terminal_status,
                                    lambda: tuning.get_job(job_id)["status"], "tune", job_id)
            return job_id, response
        with patch.object(tuning, "_existing_chunks", side_effect=existing), \
             patch.object(tuning, "_rebuild", return_value=1):
            job_id, response = asyncio.run(run())
        self.assert_busy(response, "tune", job_id, "A tuning job is running for this collection")
        self.assertEqual(tuning.get_job(job_id)["status"], "completed")

    def test_import(self):
        gate = threading.Event(); building = threading.Event()
        pkg = self.root / "pkg"; pkg.mkdir(); (pkg / "manifest.json").write_text("{}")
        manifest = {"collection": {"name": "OwnedBusy", "chunk_count": 1}, "fidelity": "chunks-only"}
        def build(name, *args):
            building.set(); assert gate.wait(5); return 1
        # The package's target is new: the route's existence check sees it once built.
        self.backend.names.discard("OwnedBusy")
        def visible(name):
            return building.is_set() or self.backend.exists(name)
        async def run():
            job_id = await importer.start_import_job("owned.tar.gz", "abort")
            await asyncio.to_thread(building.wait, 5)
            self.backend.names.add("OwnedBusy")
            response = await asyncio.to_thread(self.delete)
            gate.set()
            await asyncio.to_thread(self.assert_released_by_terminal_status,
                                    lambda: importer.get_job(job_id)["status"], "import", job_id)
            return job_id, response
        with patch.object(importer.packager, "exports_dir", return_value=self.root), \
             patch.object(importer.packager, "open_package", return_value=(pkg, manifest)), \
             patch.object(importer.packager, "verify_digests"), \
             patch.object(importer, "_check_embedding"), \
             patch.object(importer, "_ensure_models", return_value=[]), \
             patch.object(wc, "_collection_exists_sync", side_effect=visible), \
             patch.object(importer, "_mark_started", return_value="instance"), \
             patch.object(importer, "_mark_finished"), \
             patch.object(importer, "_build", side_effect=build), \
             patch.object(importer, "_restore_sidecars", return_value=[]):
            job_id, response = asyncio.run(run())
        self.assert_busy(response, "import", job_id, "An import job is running for this collection")
        self.assertEqual(importer.get_job(job_id)["status"], "completed", importer.get_job(job_id))

    def test_goldstandard(self):
        sessions = {}
        def store(session):
            session = {**session, "session_id": "gs_ownedbusy"}; sessions[session["session_id"]] = session
            return session
        def update(session_id, change):
            return change(sessions[session_id])
        async def run():
            gate = asyncio.Event(); generating = asyncio.Event()
            async def pair(chunk):
                generating.set(); await gate.wait(); return {"pair_id": "p1"}
            with patch.object(goldstandard, "_generate_pair", side_effect=pair):
                result = await goldstandard.start_generation("OwnedBusy", 1, None)
                await generating.wait()
                response = await asyncio.to_thread(self.delete)
                gate.set()
                while goldstandard._tasks:
                    await asyncio.sleep(0.01)
            return result["session_id"], response
        with patch.object(wc, "_sample_chunks_sync", return_value=[{"content": "Owned"}]), \
             patch.object(goldstandard, "_store_generated_session", side_effect=store), \
             patch.object(goldstandard, "_update_session_sync", side_effect=update):
            session_id, response = asyncio.run(run())
        self.assert_busy(response, "goldstandard", session_id,
                         "A gold-standard generation job is running for this collection")
        self.assertEqual(sessions[session_id]["status"], "completed")
        self.assertEqual(writes.active_jobs("OwnedBusy"), [])


class InternalDeletionTests(BusyTestCase):
    def test_import_replace_internal_delete_is_not_refused_by_its_own_registration(self):
        writes.register_job("OwnedBusy", "import", "replacing")
        client = MagicMock()
        client.collections.get.return_value.config.get.return_value = SimpleNamespace(name="OwnedBusy")
        client.collections.get.return_value.aggregate.over_all.return_value = SimpleNamespace(total_count=2)
        from services import collection_recovery, ingest_config, retrieval_config, sources
        with patch.object(wc, "_delete_collection_sync", new=_REAL_DELETE), \
             patch.object(wc, "get_client", return_value=client), \
             patch.object(collection_recovery, "retire_deleted"), \
             patch.object(sources, "delete"), patch.object(retrieval_config, "delete"), \
             patch.object(ingest_config, "delete"), patch.object(goldstandard, "mark_orphaned"):
            self.assertEqual(wc._delete_collection_sync("OwnedBusy"), 2)
        client.collections.delete.assert_called_once_with("OwnedBusy")
        self.assertEqual(writes.active_jobs("OwnedBusy"), [("import", "replacing")])


_REAL_DELETE = wc._delete_collection_sync


if __name__ == "__main__":
    logging_level = os.environ.get("RAG_TEST_LOG_LEVEL")
    if not logging_level:
        import logging
        logging.disable(logging.CRITICAL)
    unittest.main()
