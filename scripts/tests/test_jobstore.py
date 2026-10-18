"""Job records that survive an API restart (issue #23).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_jobstore.py

Disposable directories only; nothing reaches a backend. The lifespan case runs
in a child process, so the module stores of this process stay unbound.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from config import settings
from services import ingest_pipeline, jobstore

INGEST_MESSAGE = ("Interrupted: the API restarted while this job was running. "
                  "Re-upload the files to try again.")


def wait_until(condition, timeout):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        change = patch.object(settings, "upload_dir", str(self.root))
        change.start()
        self.addCleanup(change.stop)

    def bound(self, kind="export"):
        store = jobstore.JobStore(kind)
        self.assertEqual(store.load(), [])
        return store

    def on_disk(self, kind, job_id):
        return json.loads((self.root / "jobs" / kind / f"{job_id}.json").read_text())

    def put(self, kind, job_id, record, age=0.0):
        directory = self.root / "jobs" / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{job_id}.json"
        path.write_text(json.dumps(record) if not isinstance(record, str) else record)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path


class UnboundTests(StoreTestCase):
    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            jobstore.JobStore("goldstandard")

    def test_unbound_store_is_a_plain_dict_that_writes_nothing(self):
        store = jobstore.JobStore("ingest")
        doc = {"status": "queued", "errors": []}
        store["owned"] = doc
        self.assertIs(store["owned"], doc)          # helpers assert on their own dict
        store["owned"]["status"] = "running"
        self.assertEqual(doc["status"], "running")
        self.assertEqual(store.get("missing"), None)
        self.assertIn("owned", store)
        with patch.dict(store, {"other": {"status": "queued"}}):
            self.assertEqual(set(store), {"owned", "other"})
        self.assertEqual(set(store), {"owned"})
        self.assertIs(store.pop("owned"), doc)
        self.assertEqual(store, {})
        self.assertFalse((self.root / "jobs").exists())


class BoundTests(StoreTestCase):
    def test_new_record_is_written_at_once_owner_only(self):
        store = self.bound()
        doc = {"job_id": "0c5fd445", "status": "queued", "chunks_written": 0}
        store["0c5fd445"] = doc
        job = store["0c5fd445"]
        self.assertIsNot(job, doc)
        self.assertEqual(job, doc)
        path = self.root / "jobs" / "export" / "0c5fd445.json"
        self.assertEqual(json.loads(path.read_text()), doc)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_status_changes_and_changes_after_a_terminal_status_are_written_at_once(self):
        store = self.bound("import")
        store["a1"] = {"status": "queued", "error": None, "error_detail": None}
        job = store["a1"]
        job["status"] = "running"
        self.assertEqual(self.on_disk("import", "a1")["status"], "running")
        job.update(status="failed", error="boom")
        self.assertEqual(self.on_disk("import", "a1")["error"], "boom")
        # import names its recovery copy after finish() made the job terminal
        job["error_detail"] = {"recovered_as": "Docs__importing_x"}
        self.assertEqual(self.on_disk("import", "a1")["error_detail"],
                         {"recovered_as": "Docs__importing_x"})

    def test_progress_is_throttled_but_reaches_the_disk(self):
        store = self.bound()
        store["p1"] = {"status": "running", "chunks_written": 0, "warnings": []}
        job = store["p1"]
        writes = []
        real = store._write
        with patch.object(jobstore, "THROTTLE_S", 0.2), \
             patch.object(store, "_write", side_effect=lambda *a, **k: (writes.append(1), real(*a, **k))):
            started = time.monotonic()
            for n in range(10000):
                job["chunks_written"] = n
            elapsed = time.monotonic() - started
            self.assertLessEqual(len(writes), int(elapsed / 0.2) + 2, (len(writes), elapsed))
            # the last change, with nothing after it, still reaches the disk
            self.assertTrue(wait_until(
                lambda: self.on_disk("export", "p1")["chunks_written"] == 9999, 0.6))
            job["warnings"].append("in place")                # not seen by itself
            job["status"] = "completed"                      # the next write carries it
        self.assertEqual(self.on_disk("export", "p1")["warnings"], ["in place"])

    def test_a_failed_write_raises_nothing_and_keeps_the_previous_file(self):
        store = self.bound()
        store["w1"] = {"status": "queued"}
        before = (self.root / "jobs" / "export" / "w1.json").read_bytes()
        job = store["w1"]
        with patch.object(jobstore.os, "replace", side_effect=OSError("disk full")), \
             self.assertLogs("services.jobstore", "ERROR"):
            job["status"] = "running"                         # no exception
        self.assertEqual(job["status"], "running")
        self.assertEqual((self.root / "jobs" / "export" / "w1.json").read_bytes(), before)
        self.assertEqual(list((self.root / "jobs" / "export").glob(".*.tmp")), [])
        job["status"] = "completed"
        self.assertEqual(self.on_disk("export", "w1")["status"], "completed")

    def test_job_ids_never_name_another_path(self):
        store = self.bound()
        with self.assertRaises(ValueError):
            store["../escape"] = {"status": "queued"}
        self.assertIsNone(store.get("../x"))
        self.assertNotIn("a.b", store)
        self.assertFalse((self.root / "jobs" / "escape.json").exists())

    def test_delete_removes_the_file_and_a_pending_write(self):
        store = self.bound()
        store["d1"] = {"status": "running", "chunks_written": 0}
        with patch.object(jobstore, "THROTTLE_S", 0.2):
            store["d1"]["chunks_written"] = 1
            store["d1"]["chunks_written"] = 2                 # a timer is pending
            del store["d1"]
            time.sleep(0.4)
        self.assertFalse((self.root / "jobs" / "export" / "d1.json").exists())


class LoadTests(StoreTestCase):
    def test_interrupted_ingest_is_partial_or_failed_with_counts_kept(self):
        self.put("ingest", "aaaa0001", {"job_id": "aaaa0001", "status": "running", "files_total": 2,
                                       "files_completed": 1, "files_failed": 0,
                                       "chunks_stored": 3, "errors": []})
        self.put("ingest", "aaaa0002", {"job_id": "aaaa0002", "status": "running", "files_total": 2,
                                       "files_completed": 0, "files_failed": 0,
                                       "chunks_stored": 0, "errors": ["x: bad"]})
        self.put("ingest", "aaaa0003", {"job_id": "aaaa0003", "status": "queued", "files_total": 1,
                                       "files_completed": 0, "files_failed": 0,
                                       "chunks_stored": 0, "errors": []})
        store = jobstore.JobStore("ingest")
        self.assertEqual(sorted(store.load()), ["aaaa0001", "aaaa0002", "aaaa0003"])
        partial, failed, queued = (self.on_disk("ingest", i)
                                   for i in ("aaaa0001", "aaaa0002", "aaaa0003"))
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["errors"], [INGEST_MESSAGE])
        self.assertEqual((partial["files_completed"], partial["files_failed"],
                          partial["chunks_stored"]), (1, 0, 3))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errors"], ["x: bad", INGEST_MESSAGE])
        self.assertEqual(queued["status"], "failed")
        self.assertEqual(store["aaaa0001"], partial)

    def test_interrupted_export_import_and_tune_fail_with_interrupted(self):
        for kind, remedy in (("export", "Start the export again."),
                             ("import", "Start the import again."),
                             ("tune", "Start the operation again.")):
            with self.subTest(kind=kind):
                self.put(kind, "bbbb0001", {"job_id": "bbbb0001", "status": "running",
                                            "chunks_written": 40, "error": None,
                                            "error_code": None, "error_detail": {"keep": 1}})
                store = jobstore.JobStore(kind)
                self.assertEqual(store.load(), ["bbbb0001"])
                record = self.on_disk(kind, "bbbb0001")
                self.assertEqual((record["status"], record["error_code"]), ("failed", "INTERRUPTED"))
                self.assertEqual(record["error"], "Interrupted: the API restarted while this "
                                                  f"job was running. {remedy}")
                self.assertEqual((record["chunks_written"], record["error_detail"]), (40, {"keep": 1}))

    def test_terminal_records_load_unchanged(self):
        path = self.put("export", "cccc0001", {"job_id": "cccc0001", "status": "completed",
                                               "filename": "ragpkg-x.tar.gz"})
        before = path.read_bytes()
        store = jobstore.JobStore("export")
        self.assertEqual(store.load(), [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.get("cccc0001")["filename"], "ragpkg-x.tar.gz")

    def test_corrupt_records_are_kept_and_logged_never_loaded(self):
        outside = self.root / "jobs" / "keep.txt"
        cases = {"dddd0001": "{not json", "dddd0002": "[1, 2]", "dddd0003": '{"job_id": "dddd0003"}',
                 "dddd0004": '{"job_id": "other", "status": "running"}'}
        paths = {i: self.put("export", i, text) for i, text in cases.items()}
        target = self.put("export", "eeee0001", {"status": "running"})
        link = self.root / "jobs" / "export" / "dddd0005.json"
        link.symlink_to(target)
        outside.write_text("not ours")
        before = {i: p.read_bytes() for i, p in paths.items()}
        store = jobstore.JobStore("export")
        with self.assertLogs("services.jobstore", "WARNING") as logs:
            marked = store.load()
        self.assertEqual(marked, ["eeee0001"])
        for job_id, path in paths.items():
            self.assertEqual(path.read_bytes(), before[job_id])
            self.assertNotIn(job_id, store)
        self.assertTrue(link.is_symlink())
        self.assertNotIn("dddd0005", store)
        self.assertEqual(sum("kept as it is" in line for line in logs.output), 5)
        self.assertEqual(outside.read_text(), "not ours")

    def test_retention_keeps_the_newest_and_removes_leftover_temp_files(self):
        directory = self.root / "jobs" / "tune"
        for n in range(205):
            self.put("tune", f"f{n:07d}", {"status": "completed"} if n != 3 else "{corrupt",
                     age=10000 - n)                # f0000000 is the oldest
        (directory / ".f0000204.json.abc.tmp").write_text("{")
        store = jobstore.JobStore("tune")
        store.load()
        names = sorted(p.name for p in directory.iterdir())
        self.assertEqual(len(names), 200)
        self.assertEqual(names[0], "f0000005.json")
        self.assertEqual(len(store), 200)
        self.assertFalse(any(name.endswith(".tmp") for name in names))

    def test_lifespan_marks_a_stored_running_job(self):
        self.put("export", "abcd1234", {"job_id": "abcd1234", "status": "running",
                                        "collection": "Docs", "chunks_written": 5,
                                        "filename": None, "size_bytes": None,
                                        "source_document_count": None, "fidelity": None,
                                        "models_bundled": None, "retrieve_script": None,
                                        "warnings": [], "error": None, "error_code": None,
                                        "error_detail": None})
        script = """
import asyncio, json, sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[2])
from config import settings
settings.upload_dir = sys.argv[1]
from main import app, lifespan
from services import exporter, importer, weaviate_client as wc
async def run():
    with patch.object(wc, 'sweep_staging', new=AsyncMock(return_value=[])), \\
         patch.object(importer, 'sweep_interrupted_imports', return_value=[]), \\
         patch.object(importer, 'sweep_stale_workdirs', return_value=[]), \\
         patch.object(wc, 'close_client'):
        async with lifespan(app):
            print(json.dumps(exporter.get_job('abcd1234')))
asyncio.run(run())
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), API_DIR],
                                capture_output=True, text=True, timeout=120, cwd=API_DIR)
        self.assertEqual(result.returncode, 0, result.stderr)
        job = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual((job["status"], job["error_code"]), ("failed", "INTERRUPTED"))
        self.assertIn("Marked 1 job(s) interrupted by the restart: export abcd1234", result.stderr)


class IngestFallbackTests(StoreTestCase):
    def test_an_error_that_escapes_the_worker_is_saved_with_the_status(self):
        store = self.bound("ingest")
        with patch.object(ingest_pipeline, "_jobs", store):
            store["ffff0001"] = {"job_id": "ffff0001", "status": "running", "files_total": 1,
                                 "files_completed": 0, "files_failed": 0, "chunks_stored": 0,
                                 "errors": [], "skipped": []}
            source = Path(ingest_pipeline.__file__).read_text()
            start = source.index("    def _on_done(future")
            end = source.index("    future = asyncio.get_running_loop()", start)
            namespace = {"collection_writes": ingest_pipeline.collection_writes,
                         "_log": ingest_pipeline._log, "_jobs": store, "job_id": "ffff0001",
                         "asyncio": asyncio, "telemetry": ingest_pipeline.telemetry}
            exec("\n".join(line[4:] for line in source[start:end].splitlines()), namespace)
            loop = asyncio.new_event_loop()
            future = loop.create_future()
            future.set_exception(RuntimeError("worker crashed"))
            namespace["_on_done"](future)
            loop.close()
        record = self.on_disk("ingest", "ffff0001")
        self.assertEqual((record["status"], record["errors"]), ("failed", ["worker crashed"]))


if __name__ == "__main__":
    if not os.environ.get("RAG_TEST_LOG_LEVEL"):
        import logging
        logging.getLogger().setLevel(logging.CRITICAL)
    unittest.main()
