"""A failed metrics write never fails a query, and metrics.jsonl is bounded (issue #374).

Runs with the API's dependencies, in the built API image with no network
(`03_query.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_metrics.py

Each test points UPLOAD_DIR at a fresh temporary folder. The query case goes
through `main.app` with FastAPI's TestClient, without the lifespan, with
Weaviate and the pipeline mocked; nothing reaches a backend.
"""
import asyncio
import errno
import json
import logging
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from services import metrics, rag_pipeline
from services import weaviate_client as wc

ROOT = Path(__file__).resolve().parents[2]
RING = 500          # the ring size, unchanged
LIMIT = 1000        # the most lines metrics.jsonl may hold
QUESTION = {"question": "who approves overtime?", "collection": "Coll", "top_k": 3}
ANSWER = {"answer": "Managers approve overtime.", "citations": None,
          "retrieval_latency_ms": 12, "llm_latency_ms": 34, "chunks_retrieved": 3}


def logged(logs):
    """What the captured records put in the log, tracebacks included."""
    return "\n".join(logging.Formatter().format(record) for record in logs.records)


def entry(n):
    return {"ts": f"2026-10-21T00:00:00.{n:06d}+00:00", "collection": "Coll",
            "retrieval_mode": "hnsw", "retrieval_ms": n, "llm_ms": 1, "total_ms": n + 1,
            "chunks_retrieved": 3}


def record(n):
    return metrics.record(collection="Coll", retrieval_mode="hnsw", retrieval_ms=n,
                          llm_ms=1, chunks_retrieved=3)


def record_all(numbers, after_each=None):
    async def run():
        for n in numbers:
            await record(n)
            if after_each:
                after_each()
    asyncio.run(run())


class MetricsTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / "metrics.jsonl"
        upload_dir = patch.object(settings, "upload_dir", temp.name)
        upload_dir.start()
        self.addCleanup(upload_dir.stop)
        self.restart()
        self.addCleanup(self.restart)

    def restart(self):
        """The module state a fresh API process starts with."""
        metrics._METRICS_FILE = None
        metrics._ring.clear()
        if hasattr(metrics, "_file_lines"):
            metrics._file_lines = 0

    def lines(self):
        return self.path.read_bytes().decode("utf-8").splitlines()

    def file_numbers(self):
        return [json.loads(line)["retrieval_ms"] for line in self.lines()]

    def ring_numbers(self):
        return [r["retrieval_ms"] for r in metrics._ring]

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name != "metrics.jsonl")

    def unmakeable_upload_dir(self):
        """Point UPLOAD_DIR below a regular file, so creating it fails; the metrics
        file's path there."""
        blocker = self.dir / "blocker"
        blocker.write_text("")
        upload_dir = patch.object(settings, "upload_dir", str(blocker / "uploads"))
        upload_dir.start()
        self.addCleanup(upload_dir.stop)
        return blocker / "uploads" / "metrics.jsonl"


class FailedWriteTests(MetricsTestCase):
    """S1: the query keeps its answer when its metrics record can't be written."""

    def ask(self):
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        # Capture the log without assertLogs, so that on code that doesn't log the
        # failure the status assertion below still runs and shows the 500.
        class Capture(logging.Handler):
            def __init__(self):
                super().__init__(logging.WARNING)
                self.records = []

            def emit(self, record):
                self.records.append(record)
        logs = Capture()
        logger = logging.getLogger("services.metrics")
        logger.addHandler(logs)
        self.addCleanup(logger.removeHandler, logs)
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        self.addCleanup(setattr, logger, "propagate", logger.propagate)
        logger.propagate = False        # as assertLogs does: keep the traceback off stderr
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(rag_pipeline, "run_query", AsyncMock(return_value=dict(ANSWER))):
            response = client.post("/query", json=QUESTION)
        return response, logged(logs)

    def assert_answered(self, response, log):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), ANSWER)
        self.assertTrue(log, "the failed write is logged")
        self.assertIn("metrics.jsonl", log)
        self.assertNotIn(QUESTION["question"], log)
        self.assertNotIn(ANSWER["answer"], log)
        # The record still counts in the ring and in GET /metrics/latency.
        self.assertEqual(len(metrics._ring), 1)
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        latency = client.get("/metrics/latency", params={"collection": QUESTION["collection"]})
        self.assertEqual(latency.status_code, 200, latency.text)
        self.assertEqual(latency.json()["total_records"], 1)
        self.assertEqual(latency.json()["history"][0]["retrieval_ms"],
                         ANSWER["retrieval_latency_ms"])

    def test_an_unusable_metrics_file_still_returns_the_answer(self):
        self.path.mkdir()           # opening it to append raises IsADirectoryError
        response, log = self.ask()
        self.assert_answered(response, log)
        self.assertIn("IsADirectoryError", log)

    def test_a_full_volume_still_returns_the_answer(self):
        def full(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")
        with patch.object(metrics, "open", full, create=True):
            response, log = self.ask()
        self.assert_answered(response, log)
        self.assertIn("No space left on device", log)

    def test_an_upload_folder_that_cannot_be_made_still_returns_the_answer(self):
        path = self.unmakeable_upload_dir()
        response, log = self.ask()
        self.assert_answered(response, log)
        # The log names the file it couldn't write, not "None".
        self.assertIn(f"to {path};", log)


class TornLineTests(MetricsTestCase):
    """S2: a write that stopped part-way doesn't cost the next record."""

    def test_the_next_record_starts_its_own_line(self):
        self.path.write_text(json.dumps(entry(1)) + "\n" + json.dumps(entry(2)) + "\n"
                             + '{"ts": "2026-10-21T00:0')
        record_all([3])
        self.restart()
        metrics.load_from_disk()
        self.assertEqual(self.ring_numbers(), [1, 2, 3])


class BoundTests(MetricsTestCase):
    """S3: the file never holds more than 1,000 lines and keeps the newest records."""

    def test_the_file_stays_bounded_and_keeps_the_newest_records(self):
        sizes = []
        record_all(range(2500), after_each=lambda: sizes.append(len(self.lines())))
        self.assertLessEqual(max(sizes), LIMIT)
        self.assertGreaterEqual(min(sizes[RING:]), RING)
        numbers = self.file_numbers()
        self.assertEqual(numbers, list(range(2500 - len(numbers), 2500)))
        self.assertEqual(self.ring_numbers(), list(range(2000, 2500)))
        self.assertEqual(self.leftovers(), [])


class CompactionSafetyTests(MetricsTestCase):
    """S4: a failed compaction leaves the old file whole; concurrent records all land."""

    def fill(self, count):
        for n in range(count):
            metrics._append_sync(json.dumps(entry(n)))

    def check_failed_compaction(self, target):
        self.fill(LIMIT - 1)
        with patch.object(metrics.os, target, side_effect=OSError(errno.EIO, "SENTINEL")), \
                self.assertLogs("services.metrics", "ERROR") as logs:
            record_all([LIMIT - 1])
        self.assertIn("SENTINEL", logged(logs))
        # The old file, with the record just appended, whole; no temporary file.
        self.assertEqual(self.file_numbers(), list(range(LIMIT)))
        self.assertEqual(self.leftovers(), [])
        # Tried again on the next append, which works.
        record_all([LIMIT])
        self.assertEqual(self.file_numbers(), list(range(LIMIT + 1 - RING, LIMIT + 1)))
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_replace_keeps_the_old_file(self):
        self.check_failed_compaction("replace")

    def test_a_failed_temporary_write_keeps_the_old_file(self):
        self.check_failed_compaction("fsync")

    def test_concurrent_records_all_land(self):
        async def run():
            await asyncio.gather(*(record(n) for n in range(1100)))
        asyncio.run(run())
        lines = self.lines()
        # 1,000 lines compacted to 500, then 100 more.
        self.assertEqual(len(lines), 600)
        numbers = [json.loads(line)["retrieval_ms"] for line in lines]
        self.assertEqual(len(set(numbers)), 600)
        self.assertEqual(self.leftovers(), [])


class RestartTests(MetricsTestCase):
    """S5: startup restores the last 500 records and reads only what it keeps."""

    def test_the_last_records_reload_after_a_restart(self):
        record_all(range(700))
        self.restart()
        metrics.load_from_disk()
        self.assertEqual(self.ring_numbers(), list(range(200, 700)))
        # The file now holds only what was kept.
        self.assertEqual(self.file_numbers(), list(range(200, 700)))
        # And keeps growing from there, still bounded.
        record_all(range(700, 1300))
        self.assertLessEqual(len(self.lines()), LIMIT)
        self.restart()
        metrics.load_from_disk()
        self.assertEqual(self.ring_numbers(), list(range(800, 1300)))

    def test_a_large_legacy_file_is_read_in_part_and_compacted(self):
        line = json.dumps(entry(0)) + "\n"
        with open(self.path, "w") as f:
            for _ in range(40 * 1024 * 1024 // len(line)):
                f.write(line)
            f.write("not json\n")
            for n in range(1, 601):
                f.write(json.dumps(entry(n)) + "\n")
        tracemalloc.start()
        try:
            metrics.load_from_disk()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 8 * 1024 * 1024)
        self.assertEqual(self.ring_numbers(), list(range(101, 601)))
        self.assertEqual(self.file_numbers(), list(range(101, 601)))
        self.assertEqual(self.leftovers(), [])

    def test_lines_that_are_not_records_are_skipped_and_compacted_away(self):
        self.path.write_text(json.dumps(entry(1)) + "\nnot json\n\n" + json.dumps(entry(2)) + "\n")
        metrics.load_from_disk()
        self.assertEqual(self.ring_numbers(), [1, 2])
        self.assertEqual(self.file_numbers(), [1, 2])

    def test_leftovers_of_an_interrupted_compaction_are_removed(self):
        self.path.write_text(json.dumps(entry(1)) + "\n")
        (self.dir / ".metrics.jsonl.abc123.tmp").write_text("partial")
        (self.dir / ".other.json.abc123.tmp").write_text("not ours")
        metrics.load_from_disk()
        self.assertEqual(self.ring_numbers(), [1])
        self.assertEqual(self.leftovers(), [".other.json.abc123.tmp"])

    def test_a_whole_file_is_not_rewritten_at_startup(self):
        # A file within the bound is loaded as it is, not compacted.
        self.path.write_text("".join(json.dumps(entry(n)) + "\n" for n in range(300)))
        before = os.stat(self.path)
        metrics.load_from_disk()
        after = os.stat(self.path)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(self.ring_numbers(), list(range(300)))
        # The line count carries over: the 700th further append compacts the file.
        record_all(range(300, 999))
        self.assertEqual(len(self.lines()), 999)
        record_all([999])
        self.assertEqual(self.file_numbers(), list(range(500, 1000)))

    def test_a_tail_without_a_line_break_loads_nothing_and_startup_goes_on(self):
        # The last 1 MiB is one unfinished line; nothing in it is a record.
        self.path.write_text(json.dumps(entry(1)) + "\n" + "x" * (2 * 1024 * 1024))
        metrics.load_from_disk()
        self.assertEqual(len(metrics._ring), 0)
        self.assertEqual(self.path.read_bytes(), b"")
        self.assertEqual(self.leftovers(), [])
        record_all([2])
        self.assertEqual(self.file_numbers(), [2])

    def test_an_unreadable_file_is_logged_and_startup_goes_on(self):
        self.path.mkdir()
        with self.assertLogs("services.metrics", "ERROR") as logs:
            metrics.load_from_disk()
        self.assertIn("metrics.jsonl", logged(logs))
        self.assertEqual(len(metrics._ring), 0)

    def test_an_upload_folder_that_cannot_be_made_is_logged_and_startup_goes_on(self):
        path = self.unmakeable_upload_dir()
        with self.assertLogs("services.metrics", "ERROR") as logs:
            metrics.load_from_disk()
        # The log names the file it couldn't load, not "None".
        self.assertIn(f"Could not load the query metrics from {path}\n", logged(logs))
        self.assertEqual(len(metrics._ring), 0)

    def test_a_failed_startup_compaction_is_logged_and_startup_goes_on(self):
        self.path.write_text("".join(json.dumps(entry(n)) + "\n" for n in range(600)))
        with patch.object(metrics.os, "replace", side_effect=OSError(errno.ENOSPC, "SENTINEL")), \
                self.assertLogs("services.metrics", "ERROR") as logs:
            metrics.load_from_disk()
        self.assertIn("SENTINEL", logged(logs))
        self.assertEqual(self.ring_numbers(), list(range(100, 600)))
        self.assertEqual(self.file_numbers(), list(range(600)))
        self.assertEqual(self.leftovers(), [])


class ImplementationCopyTests(unittest.TestCase):
    def test_embedded_copy_matches(self):
        document = (ROOT / "IMPLEMENTATION.md").read_text()
        name = "api/services/metrics.py"
        header = "### " + name + "\n\n```"
        start = document.index("\n", document.index(header) + len(header)) + 1
        end = document.index("\n```\n", start)
        self.assertEqual(document[start:end], (ROOT / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
