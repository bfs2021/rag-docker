"""Job records that survive an API restart (issue #23).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_jobstore.py

Disposable directories only; nothing reaches a backend. The lifespan case runs
in a child process, so the module stores of this process stay unbound.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from config import settings
from services import collection_writes, exporter, importer, ingest_pipeline, jobstore, tuning

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


class PruneTests(StoreTestCase):
    """Retention while the API runs (#309)."""

    def setUp(self):
        super().setUp()
        change = patch.object(jobstore, "RETENTION", 3)
        change.start()
        self.addCleanup(change.stop)

    def path(self, kind, job_id):
        return self.root / "jobs" / kind / f"{job_id}.json"

    def add(self, store, job_id, status, saved=None):
        store[job_id] = {"job_id": job_id, "status": status}
        if saved is not None:
            store[job_id]._saved = saved
        return store[job_id]

    def test_the_finished_records_saved_longest_ago_go_from_memory_and_disk(self):
        store = self.bound()
        for n, job_id in enumerate(("r2", "r1", "r3")):
            self.add(store, job_id, "completed", saved=[200.0, 100.0, 300.0][n])
        with self.assertLogs("services.jobstore", "INFO") as logs:
            self.add(store, "r4", "queued")
        self.assertEqual(set(store), {"r2", "r3", "r4"})
        self.assertFalse(self.path("export", "r1").exists())
        self.assertTrue(all(self.path("export", i).exists() for i in ("r2", "r3", "r4")))
        self.assertTrue(any("Removed old export job record r1" in line for line in logs.output))
        self.assertIsNone(store.get("r1"))                    # the endpoint's 404

    def test_queued_and_running_records_are_never_removed(self):
        store = self.bound("import")
        self.add(store, "a1", "running", saved=1.0)
        self.add(store, "a2", "queued", saved=2.0)
        self.add(store, "a3", "running", saved=3.0)
        self.add(store, "a4", "queued")
        self.add(store, "a5", "failed")                       # the record just stored stays
        self.assertEqual(len(store), 5)
        store["a1"]["status"] = "completed"
        store["a1"]._saved = 1.0
        store["a5"]._saved = 5.0
        self.add(store, "a6", "partial")
        self.assertEqual(set(store), {"a2", "a3", "a4", "a6"})
        self.assertFalse(self.path("import", "a1").exists())
        self.assertFalse(self.path("import", "a5").exists())

    def test_records_from_startup_are_ordered_by_their_file_time(self):
        self.put("tune", "l1", {"job_id": "l1", "status": "completed"}, age=200)
        self.put("tune", "l2", {"job_id": "l2", "status": "failed"}, age=300)
        self.put("tune", "l3", {"job_id": "l3", "status": "completed"}, age=100)
        store = jobstore.JobStore("tune")
        store.load()
        self.add(store, "n1", "queued")
        self.assertEqual(set(store), {"l1", "l3", "n1"})
        self.assertFalse(self.path("tune", "l2").exists())

    def test_a_removed_record_cancels_its_pending_write(self):
        store = self.bound()
        for job_id, saved in (("t1", 1.0), ("t2", 2.0), ("t3", 3.0)):
            self.add(store, job_id, "completed", saved=saved)
        timer = threading.Timer(60, lambda: None)
        store["t1"]._timer = timer
        timer.start()
        self.add(store, "t4", "running")
        self.assertNotIn("t1", store)
        self.assertFalse(timer.is_alive() and not timer.finished.is_set())

    def test_an_unbound_store_never_prunes(self):
        store = jobstore.JobStore("export")
        for n in range(5):
            store[f"u{n}"] = {"status": "completed"}
        self.assertEqual(len(store), 5)

    def test_a_failed_file_removal_is_logged_and_the_record_still_dropped(self):
        store = self.bound()
        for job_id, saved in (("e1", 1.0), ("e2", 2.0), ("e3", 3.0)):
            self.add(store, job_id, "completed", saved=saved)
        with patch.object(Path, "unlink", side_effect=OSError("read-only")), \
             self.assertLogs("services.jobstore", "ERROR"):
            self.add(store, "e4", "queued")                  # no exception
        self.assertEqual(set(store), {"e2", "e3", "e4"})
        self.assertTrue(self.path("export", "e1").exists())   # startup retention removes it

    def test_the_order_follows_each_record_s_own_last_write(self):
        # No stamps set by hand: the order comes from the writes themselves.
        store = self.bound()
        for job_id in ("w1", "w2", "w3"):
            self.add(store, job_id, "running")
        for job_id in ("w2", "w1", "w3"):
            time.sleep(0.02)
            store[job_id]["status"] = "completed"     # a status change is written at once
        self.add(store, "w4", "queued")
        self.assertEqual(set(store), {"w1", "w3", "w4"})
        self.assertFalse(self.path("export", "w2").exists())

    def test_replacing_a_record_prunes_nothing(self):
        store = self.bound()
        for job_id in ("p1", "p2", "p3", "p4"):
            self.add(store, job_id, "running")
        store["p1"]["status"] = "completed"
        store["p2"] = {"job_id": "p2", "status": "running"}
        self.assertEqual(set(store), {"p1", "p2", "p3", "p4"})


class Recording(jobstore.JobStore):
    """A bound store that notes which thread stores each record, and can hold
    that write until released."""

    def __init__(self, kind, hold=False):
        super().__init__(kind)
        self.threads, self.ids = [], []
        self.entered, self.release = threading.Event(), threading.Event()
        self.stored = threading.Event()
        if not hold:
            self.release.set()

    def __setitem__(self, job_id, doc):
        self.threads.append(threading.get_ident())
        self.ids.append(job_id)
        self.entered.set()
        self.release.wait(10)
        super().__setitem__(job_id, doc)
        self.stored.set()


class Failing(Recording):
    """A Recording whose held write raises instead of storing."""

    def __setitem__(self, job_id, doc):
        self.ids.append(job_id)
        self.entered.set()
        self.release.wait(10)
        raise OSError("disk gone")


class FirstHeld(Recording):
    """A Recording that holds only its first write, which then stores, or raises
    if fail is set; later writes go straight through."""

    def __init__(self, kind, fail=False):
        super().__init__(kind, hold=True)
        self.fail = fail

    def __setitem__(self, job_id, doc):
        if self.ids:
            self.ids.append(job_id)
            jobstore.JobStore.__setitem__(self, job_id, doc)
            return
        self.ids.append(job_id)
        self.entered.set()
        self.release.wait(10)
        if self.fail:
            raise OSError("disk gone")
        jobstore.JobStore.__setitem__(self, job_id, doc)
        self.stored.set()


class FailThenHold(Recording):
    """A Recording whose first write is held and then raises, and whose second
    write is held until second_release, then stores."""

    def __init__(self, kind):
        super().__init__(kind, hold=True)
        self.second_release = threading.Event()

    def __setitem__(self, job_id, doc):
        first = not self.ids
        self.ids.append(job_id)
        if first:
            self.entered.set()
            self.release.wait(10)
            raise OSError("disk gone")
        self.second_release.wait(10)
        jobstore.JobStore.__setitem__(self, job_id, doc)
        self.stored.set()


class StartTests(StoreTestCase):
    """The four job starts store their first record off the event loop, and a
    start cancelled during that write leaves nothing behind (#309). A second
    export of a collection waits for that write before it names the first (#326),
    for at most START_WAIT (#338)."""

    KINDS = {"export": (exporter, "_run"), "import": (importer, "_run"),
             "tune": (tuning, "_run"), "ingest": (ingest_pipeline, "_process_job_sync")}

    def harness(self, kind, hold=False):
        module, worker = self.KINDS[kind]
        store = Recording(kind, hold)
        self.assertEqual(store.load(), [])
        calls = []
        for change in (patch.object(module, "_jobs", store),
                       patch.object(module, worker, side_effect=lambda *a, **k: calls.append(a))):
            change.start()
            self.addCleanup(change.stop)
        return module, store, calls

    @staticmethod
    def start(kind, name):
        if kind == "export":
            return exporter.start_export_job(name)
        if kind == "import":
            return importer.start_import_job(name, "abort")
        if kind == "tune":
            return tuning.start_tune_job(name, "reindex", {})
        upload = type("Upload", (), {"filename": "a.txt", "file": io.BytesIO(b"hello world")})()
        return ingest_pipeline.start_ingest_job([upload], name, "fixed", 300, 0, 0.85, 50)

    def guards_free(self, kind, module, name):
        key = collection_writes.canonical(name) if kind == "tune" else name
        active = key in module._active if kind != "ingest" else False
        return not active and collection_writes.active_jobs(name) == []

    def test_each_start_stores_its_record_off_the_event_loop(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                module, store, calls = self.harness(kind)
                name = f"Vfy309{kind.capitalize()}"

                async def run():
                    loop_thread = threading.get_ident()
                    job_id = await self.start(kind, name)
                    stored = job_id in store and self.path_exists(kind, job_id)
                    for _ in range(500):
                        if calls:
                            break
                        await asyncio.sleep(0.01)
                    return loop_thread, job_id, stored

                loop_thread, job_id, stored = asyncio.run(run())
                self.assertEqual(store.ids, [job_id])
                self.assertNotEqual(store.threads, [loop_thread])
                self.assertTrue(stored)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store[job_id]["status"], "queued")
                # the stub worker never ran, so release what it would have
                collection_writes.end_job(kind, job_id)
                if kind == "export":
                    module._active.pop(name, None)
                elif kind == "tune":
                    module._active.discard(collection_writes.canonical(name))
                elif kind == "import":
                    module._active.discard(name)

    def path_exists(self, kind, job_id):
        return (self.root / "jobs" / kind / f"{job_id}.json").exists()

    def test_a_start_cancelled_during_its_write_leaves_nothing_behind(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                module, store, calls = self.harness(kind, hold=True)
                name = f"Vfy309Cancel{kind.capitalize()}"

                async def run():
                    task = asyncio.ensure_future(self.start(kind, name))
                    entered = await asyncio.get_running_loop().run_in_executor(
                        None, store.entered.wait, 10)
                    self.assertTrue(entered)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    freed = self.guards_free(kind, module, name)
                    store.release.set()
                    job_id = store.ids[0]
                    # the abandoned write still stores the record; then it goes
                    self.assertTrue(await asyncio.get_running_loop().run_in_executor(
                        None, store.stored.wait, 10))
                    for _ in range(500):
                        if job_id not in store and not self.path_exists(kind, job_id):
                            break
                        await asyncio.sleep(0.01)
                    return freed, job_id

                freed, job_id = asyncio.run(run())
                self.assertTrue(freed)
                self.assertNotIn(job_id, store)
                self.assertFalse(self.path_exists(kind, job_id))
                self.assertEqual(calls, [])
                if kind == "ingest":
                    self.assertEqual([p.name for p in self.root.iterdir()], ["jobs"])


    def release_export(self, name, job_id):
        # the stub worker never ran, so release what it would have
        collection_writes.end_job("export", job_id)
        exporter._active.pop(name, None)

    @staticmethod
    async def second_start(name):
        """The second start's job id, or the id its RuntimeError (the 409) names
        and whether that job could be read when it was raised."""
        try:
            return await exporter.start_export_job(name), None
        except RuntimeError as exc:
            return str(exc), exporter.get_job(str(exc)) is not None

    def test_a_second_export_waits_until_the_first_has_stored_its_record(self):
        module, store, calls = self.harness("export", hold=True)
        name = "Vfy326Wait"

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job(name))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            second = asyncio.ensure_future(self.second_start(name))
            await asyncio.sleep(0.2)
            pending = not second.done()
            store.release.set()
            return pending, await first, await second

        pending, job_id, (named, readable) = asyncio.run(run())
        self.release_export(name, job_id)
        self.assertTrue(pending)                      # it waited for the write
        self.assertEqual(named, job_id)               # then answered the 409
        self.assertTrue(readable)                     # naming a job that reads 200
        self.assertEqual(store.ids, [job_id])

    def test_an_export_waiting_on_a_cancelled_start_starts_its_own(self):
        module, store, calls = self.harness("export", hold=True)
        name = "Vfy326Cancel"

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job(name))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            second = asyncio.ensure_future(self.second_start(name))
            await asyncio.sleep(0.2)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            store.release.set()
            outcome = await second
            for _ in range(500):      # the abandoned record goes; the second's worker runs
                if store.ids[0] not in store and calls:
                    break
                await asyncio.sleep(0.01)
            return outcome

        second_id, refused = asyncio.run(run())
        first_id = store.ids[0]
        self.assertIsNone(refused)                    # no 409 naming the cancelled job
        self.assertNotEqual(second_id, first_id)
        self.assertEqual(exporter._active.get(name), second_id)
        self.assertEqual(store[second_id]["status"], "queued")
        self.assertNotIn(first_id, store)
        self.assertEqual(len(calls), 1)
        self.release_export(name, second_id)

    def test_an_abandoned_write_that_raises_is_logged_and_leaves_nothing(self):
        store = Failing("export", hold=True)
        self.assertEqual(store.load(), [])

        async def run(logs):
            loop = asyncio.get_running_loop()
            task = asyncio.ensure_future(store.create("abcd0326", {"status": "queued"}))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            store.release.set()
            for _ in range(500):
                if any("abandoned" in line for line in logs.output):
                    break
                await asyncio.sleep(0.01)

        with self.assertLogs("services.jobstore", "ERROR") as logs:
            asyncio.run(run(logs))
        self.assertIn("Could not store abandoned export job abcd0326: disk gone",
                      "\n".join(logs.output))
        self.assertNotIn("abcd0326", store)
        self.assertFalse(self.path_exists("export", "abcd0326"))

    # Added by the testing reviewer (#326): the 409 at once, a failed first
    # write, a cancelled waiter and other collections.
    def first_held(self, fail=False):
        store = FirstHeld("export", fail)
        self.assertEqual(store.load(), [])
        calls = []
        for change in (patch.object(exporter, "_jobs", store),
                       patch.object(exporter, "_run", side_effect=lambda *a, **k: calls.append(a))):
            change.start()
            self.addCleanup(change.stop)
        return store, calls

    def test_a_second_export_answers_409_at_once_once_the_record_is_stored(self):
        module, store, calls = self.harness("export")
        name = "Vfy326Stored"

        async def run():
            first = await exporter.start_export_job(name)
            self.assertNotIn(first, exporter._starting)
            return first, await asyncio.wait_for(self.second_start(name), 1)

        job_id, (named, readable) = asyncio.run(run())
        self.release_export(name, job_id)
        self.assertEqual((named, readable), (job_id, True))
        self.assertEqual(store.ids, [job_id])
        self.assertEqual(exporter._starting, {})

    def test_an_export_waiting_on_a_start_whose_write_fails_starts_its_own(self):
        store, calls = self.first_held(fail=True)
        name = "Vfy326Failed"

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job(name))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            second = asyncio.ensure_future(self.second_start(name))
            await asyncio.sleep(0.2)
            pending = not second.done()
            store.release.set()
            with self.assertRaises(OSError):
                await first
            outcome = await second
            for _ in range(500):
                if calls:
                    break
                await asyncio.sleep(0.01)
            return pending, outcome

        pending, (second_id, refused) = asyncio.run(run())
        first_id = store.ids[0]
        self.assertTrue(pending)                      # it waited for the write
        self.assertIsNone(refused)                    # no 409 naming the failed job
        self.assertNotEqual(second_id, first_id)
        self.assertEqual(exporter._active.get(name), second_id)
        self.assertNotIn(first_id, store)
        self.assertEqual(store[second_id]["status"], "queued")
        self.assertEqual(len(calls), 1)
        self.assertEqual(exporter._starting, {})
        self.release_export(name, second_id)

    def test_a_waiting_export_cancelled_holds_nothing_and_the_first_goes_on(self):
        module, store, calls = self.harness("export", hold=True)
        name = "Vfy326Waiter"

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job(name))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            second = asyncio.ensure_future(exporter.start_export_job(name))
            await asyncio.sleep(0.2)
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            starting = list(exporter._starting)
            store.release.set()
            job_id = await first
            for _ in range(500):
                if calls:
                    break
                await asyncio.sleep(0.01)
            return starting, job_id

        starting, job_id = asyncio.run(run())
        self.assertEqual(starting, [job_id])          # the waiter added nothing
        self.assertEqual(store.ids, [job_id])         # and never wrote
        self.assertEqual(exporter._active.get(name), job_id)
        self.assertEqual(store[job_id]["status"], "queued")
        self.assertEqual(len(calls), 1)
        self.assertEqual(exporter._starting, {})
        self.release_export(name, job_id)

    def test_exports_of_different_collections_never_wait_on_each_other(self):
        store, calls = self.first_held()

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job("Vfy326A"))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            other = await asyncio.wait_for(exporter.start_export_job("Vfy326B"), 5)
            first_done = first.done()
            store.release.set()
            return first_done, await first, other

        first_done, first_id, other_id = asyncio.run(run())
        self.release_export("Vfy326A", first_id)
        self.release_export("Vfy326B", other_id)
        self.assertFalse(first_done)                  # B finished while A was held
        self.assertNotEqual(first_id, other_id)
        self.assertEqual(store[other_id]["status"], "queued")
        self.assertEqual(exporter._starting, {})

    def test_a_second_export_gives_up_after_the_bound_and_holds_nothing(self):
        module, store, calls = self.harness("export", hold=True)
        name = "Vfy338Bound"
        change = patch.object(exporter, "START_WAIT", 0.2)
        change.start()
        self.addCleanup(change.stop)

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job(name))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            with self.assertRaises(exporter.ExportStartingError):
                await asyncio.wait_for(exporter.start_export_job(name), 5)
            first_done, starting = first.done(), list(exporter._starting)
            store.release.set()
            job_id = await first
            for _ in range(500):
                if calls:
                    break
                await asyncio.sleep(0.01)
            return first_done, starting, job_id

        first_done, starting, job_id = asyncio.run(run())
        self.release_export(name, job_id)
        self.assertFalse(first_done)                  # it gave up while the write was held
        self.assertEqual(starting, [job_id])          # and added nothing
        self.assertEqual(store.ids, [job_id])         # nor wrote
        self.assertEqual(store[job_id]["status"], "queued")
        self.assertEqual(len(calls), 1)
        self.assertEqual(exporter._starting, {})

    # Added by the testing reviewer (#338): the bound covers all of a start's
    # waits together, counted from its first wait, not each wait afresh.
    def test_the_bound_counts_from_the_first_wait_across_later_waits(self):
        store = FailThenHold("export")
        self.assertEqual(store.load(), [])
        calls = []
        for change in (patch.object(exporter, "_jobs", store),
                       patch.object(exporter, "_run", side_effect=lambda *a, **k: calls.append(a)),
                       patch.object(exporter, "START_WAIT", 2.0)):
            change.start()
            self.addCleanup(change.stop)
        self.addCleanup(store.release.set)
        self.addCleanup(store.second_release.set)
        name = "Vfy338Total"

        async def timed():
            began = asyncio.get_running_loop().time()
            try:
                return await exporter.start_export_job(name), None
            except exporter.ExportStartingError as exc:
                return exc, asyncio.get_running_loop().time() - began

        async def run():
            loop = asyncio.get_running_loop()
            first = asyncio.ensure_future(exporter.start_export_job(name))
            self.assertTrue(await loop.run_in_executor(None, store.entered.wait, 10))
            waiters = [asyncio.ensure_future(timed()) for _ in range(2)]
            await asyncio.sleep(1.5)             # both wait on the first start
            store.release.set()                  # its write fails; one waiter takes the slot
            with self.assertRaises(OSError):
                await first
            # the other waits again, on the winner, whose write is held
            done, _ = await asyncio.wait(waiters, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            gave_up = [w.result() for w in done]
            store.second_release.set()
            results = [await w for w in waiters]
            for _ in range(500):
                if calls:
                    break
                await asyncio.sleep(0.01)
            return gave_up, results

        gave_up, results = asyncio.run(run())
        refused = [elapsed for value, elapsed in results if elapsed is not None]
        started = [value for value, elapsed in results if elapsed is None]
        self.assertEqual(len(refused), 1, results)
        self.assertEqual(len(started), 1, results)
        # 2.0 s from its first wait, not 1.5 + 2.0 s from its second
        self.assertGreaterEqual(refused[0], 1.9)
        self.assertLess(refused[0], 3.0)
        # the waiter done while the winner's write was held is the one that gave up
        self.assertEqual(gave_up, [(value, elapsed) for value, elapsed in results
                                   if elapsed is not None])
        self.assertIsInstance(gave_up[0][0], exporter.ExportStartingError)
        self.assertEqual(len(store.ids), 2)           # the first start's failed write, the winner's
        self.assertEqual(store.ids[1], started[0])    # the waiter that gave up never wrote
        self.assertEqual(len(calls), 1)               # only the winner's worker ran
        self.assertEqual(exporter._active.get(name), started[0])
        self.assertEqual(store[started[0]]["status"], "queued")
        self.assertEqual(exporter._starting, {})
        self.release_export(name, started[0])

    def test_the_route_answers_export_starting_with_no_job_id(self):
        from unittest.mock import AsyncMock
        from models.schemas import ExportRequest
        from routers import transfer as route
        with patch.object(route.wc, "collection_exists", new=AsyncMock(return_value=True)), \
             patch.object(exporter, "start_export_job",
                          new=AsyncMock(side_effect=exporter.ExportStartingError("Vfy338"))):
            response = asyncio.run(route.start_export(ExportRequest(collection="Vfy338")))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["error"], {
            "code": "EXPORT_STARTING",
            "message": "An export of 'Vfy338' is starting; retry shortly.",
            "detail": None,
        })


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
