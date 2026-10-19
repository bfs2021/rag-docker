"""Ingest jobs count chunks submitted, accepted and stored separately (issue #302).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_write_counts.py

Weaviate is a small in-memory fake; nothing reaches a backend.
"""
import asyncio
import copy
import os
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx

from config import settings
from main import app
from services import batch_write, ingest_pipeline
from services import weaviate_client as wc


class Batch:
    """A batch that stores what it is given, unless told to fail a certain way."""

    def __init__(self, collection):
        self.collection = collection
        self.failed_objects = []

    def dynamic(self):
        self.failed_objects = []
        self.pending = []
        return self

    def __enter__(self):
        return self

    def add_object(self, properties, uuid, vector=None):
        if self.collection.fault == "send" and len(self.pending) == 1:
            raise ConnectionError("Owned send failure")
        self.pending.append({"id": str(uuid), "properties": copy.deepcopy(properties)})

    def __exit__(self, *args):
        if args[0] is not None:
            return False
        rejected = self.collection.rejected
        if rejected:
            self.failed_objects = [SimpleNamespace(message=self.collection.reject_message)] * rejected
        stored = self.pending[rejected:]
        for record in stored:
            if self.collection.fault == "properties":
                record["properties"] = {**record["properties"], "content": "wrong"}
            self.collection.rows[record["id"]] = record
        return False


class Collection:
    def __init__(self, fault=None, rejected=0, reject_message="Owned rejection"):
        self.rows = {}
        self.fault = fault
        self.rejected = rejected
        self.reject_message = reject_message
        self.batch = Batch(self)
        self.query = SimpleNamespace(fetch_objects=self.fetch_objects)
        self.data = SimpleNamespace(delete_many=self.delete_many)

    def fetch_objects(self, filters, **kwargs):
        return SimpleNamespace(objects=[
            SimpleNamespace(uuid=uuid.UUID(row["id"]), properties=copy.deepcopy(row["properties"]),
                            vector={"default": [0.25, 0.5]})
            for key, row in self.rows.items() if key in filters.value])

    def delete_many(self, where):
        if self.fault == "cleanup":
            return SimpleNamespace(failed=1)
        for key in where.value:
            self.rows.pop(key, None)
        return SimpleNamespace(failed=0)


def chunks(n):
    return [{"content": f"chunk {i}", "source_file": "a.txt", "chunk_index": i} for i in range(n)]


class TempUploadDir(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(settings, "upload_dir", str(self.root)))


class InsertCountTests(TempUploadDir):
    def insert(self, collection, n=4):
        records = [{"properties": c} for c in chunks(n)]
        return batch_write.insert(collection, records, exact=False, cleanup_owned=True)

    def counts_of(self, collection, n=4, error=Exception):
        with self.assertRaises(error) as raised:
            self.insert(collection, n)
        return batch_write.write_counts(raised.exception)

    def test_a_clean_insert_returns_its_checked_count(self):
        self.assertEqual(self.insert(Collection()), 4)

    def test_rejected_objects_are_submitted_but_not_accepted(self):
        self.assertEqual(self.counts_of(Collection(rejected=1), error=RuntimeError), (4, 3))

    def test_a_failed_check_counts_every_chunk_accepted(self):
        collection = Collection(fault="properties")
        self.assertEqual(self.counts_of(collection, error=batch_write.BatchVerificationError), (4, 4))
        self.assertEqual(collection.rows, {})          # rolled back: nothing stored

    def test_a_send_failure_has_nothing_accepted(self):
        self.assertEqual(self.counts_of(Collection(fault="send"), error=ConnectionError), (1, 0))

    def test_a_cleanup_failure_carries_the_counts(self):
        collection = Collection(fault="cleanup", rejected=2)
        self.assertEqual(self.counts_of(collection, error=batch_write.BatchCleanupError), (4, 2))

    def test_counts_are_found_on_the_cause(self):
        original = RuntimeError("inner"); original.write_counts = (3, 1)
        try:
            try:
                raise original
            except RuntimeError as exc:
                raise ValueError("outer") from exc
        except ValueError as outer:
            self.assertEqual(batch_write.write_counts(outer), (3, 1))
        self.assertIsNone(batch_write.write_counts(RuntimeError("none")))
        self.assertIsNone(batch_write.write_counts(None))


class RetryCountTests(TempUploadDir):
    def test_a_retried_file_is_counted_from_its_final_attempt(self):
        # The first attempt meets an index that isn't ready (retried); the second
        # fails its check. The counts are the second attempt's, not both summed.
        not_ready = "could not find index for class Owned. It might have been deleted"
        collection = Collection(rejected=3, reject_message=not_ready)
        attempts = []
        real_insert = batch_write.insert
        def insert(coll, records, **kwargs):
            attempts.append(1)
            if len(attempts) == 2:
                coll.rejected = 0; coll.fault = "properties"
            return real_insert(coll, records, **kwargs)
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: collection))
        with patch.object(wc, "get_client", return_value=client), \
             patch.object(wc, "_INSERT_RETRY_DELAY", 0), \
             patch.object(batch_write, "insert", side_effect=insert):
            with self.assertRaises(batch_write.BatchVerificationError) as raised:
                wc._insert_chunks_sync("Owned", chunks(3))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(batch_write.write_counts(raised.exception), (3, 3))


class JobCountTests(TempUploadDir):
    def run_job(self, outcomes, seen=None):
        """outcomes: per file, None (stored), an exception to raise, or "parse".
        seen, when given, gets the job's counts as each file's source is retained."""
        paths = []
        for i in range(len(outcomes)):
            path = self.root / f"f{i}.txt"; path.write_text(f"file {i}"); paths.append(path)
        job_id = "counts" + uuid.uuid4().hex[:6]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": len(paths), "files_completed": 0,
            "files_failed": 0, "chunks_stored": 0, "chunks_submitted": 0, "chunks_accepted": 0,
            "chunks_replaced": 0, "errors": [], "skipped": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        by_name = {p.name: o for p, o in zip(paths, outcomes)}
        def parse(path):
            if by_name[path.name] == "parse":
                raise ValueError("Owned parse failure")
            return path.read_text(), []
        def insert(collection, stored):
            outcome = by_name[stored[0]["source_file"]]
            if isinstance(outcome, BaseException):
                raise outcome
        def retain(*args, **kwargs):
            if seen is not None:
                seen.append(self.counts(ingest_pipeline._jobs[job_id]))
        with patch.object(ingest_pipeline, "_parse_file", side_effect=parse), \
             patch.object(ingest_pipeline, "do_chunk", side_effect=lambda text, **_: [text, text + "!"]), \
             patch.object(wc, "_insert_chunks_sync", side_effect=insert), \
             patch.object(wc, "_source_has_chunks_sync", return_value=False), \
             patch.object(wc, "_collection_exists_sync", return_value=True), \
             patch.object(ingest_pipeline.sources, "store", side_effect=retain):
            ingest_pipeline._process_job_sync(job_id, paths, self.root, "Owned",
                                              "fixed", 300, 0, 0.85, 0)
        return ingest_pipeline._jobs[job_id]

    def counts(self, job):
        return job["chunks_submitted"], job["chunks_accepted"], job["chunks_stored"]

    def test_a_clean_job_has_three_equal_counts(self):
        self.assertEqual(self.counts(self.run_job([None, None])), (4, 4, 4))

    def test_a_clean_job_never_shows_its_counts_apart(self):
        # Observed between each file's insert and the end of its processing,
        # where a poll could see stored lag the other two (#324).
        seen = []
        self.run_job([None, None], seen)
        self.assertEqual(seen, [(2, 2, 2), (4, 4, 4)])

    def test_failed_and_unparsed_files_count_what_was_sent(self):
        rejected = RuntimeError("Weaviate rejected 1 batch object(s)"); rejected.write_counts = (2, 1)
        unchecked = batch_write.BatchVerificationError("Confirmed 0 of 2 expected objects")
        unchecked.write_counts = (2, 2)
        job = self.run_job([None, rejected, "parse", unchecked])
        self.assertEqual(self.counts(job), (6, 5, 2))
        self.assertEqual(job["status"], "partial")

    def test_an_error_without_counts_adds_nothing_sent(self):
        self.assertEqual(self.counts(self.run_job([OSError("no client")])), (0, 0, 0))

    def test_a_deleted_collection_counts_through_the_cause(self):
        rejected = RuntimeError("class not present in schema"); rejected.write_counts = (2, 0)
        with patch.object(ingest_pipeline, "_collection_gone", return_value=True):
            paths_job = self.run_job([None, rejected, None])
        self.assertEqual(self.counts(paths_job), (4, 2, 2))
        self.assertEqual(paths_job["status"], "failed")

    def test_a_record_without_the_counters_still_counts(self):
        job = {"chunks_stored": 0}
        ingest_pipeline._count_writes(job, 3, 2)
        self.assertEqual((job["chunks_submitted"], job["chunks_accepted"]), (3, 2))


class StatusRouteTests(TempUploadDir):
    def get(self, job_id):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://owned") as api:
                return await api.get(f"/ingest/job/{job_id}")
        return asyncio.run(run())

    def test_a_new_job_reports_both_counts(self):
        job_id = "fresh" + uuid.uuid4().hex[:6]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "completed", "files_total": 1, "files_completed": 1,
            "files_failed": 0, "chunks_stored": 2, "chunks_submitted": 2, "chunks_accepted": 2,
            "errors": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        body = self.get(job_id).json()
        self.assertEqual((body["chunks_submitted"], body["chunks_accepted"], body["chunks_stored"]),
                         (2, 2, 2))

    def test_a_record_written_before_the_counts_reads_back_null(self):
        job_id = "older" + uuid.uuid4().hex[:6]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "completed", "files_total": 1, "files_completed": 1,
            "files_failed": 0, "chunks_stored": 5, "errors": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        response = self.get(job_id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsNone(body["chunks_submitted"])
        self.assertIsNone(body["chunks_accepted"])
        self.assertEqual(body["chunks_stored"], 5)


if __name__ == "__main__":
    import logging
    if not os.environ.get("RAG_TEST_LOG_LEVEL"):
        logging.disable(logging.CRITICAL)
    unittest.main()
