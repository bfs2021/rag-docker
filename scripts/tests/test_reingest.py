"""Re-ingesting replaces a name's chunks; same-named inputs don't collide (issue #22).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_reingest.py

Weaviate and gold-standard sessions are mocked; nothing reaches a backend.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import uuid
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi import UploadFile

from config import settings
from models.schemas import JobStatusResponse
from routers import ingest
from services import goldstandard, ingest_pipeline, sources, tuning
from services import weaviate_client as wc


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, warnings.catch_warnings():
        # writestr warns on a repeated name; repeating is the point here.
        warnings.simplefilter("ignore", UserWarning)
        for name, data in members:
            z.writestr(zipfile.ZipInfo(name), data)
    return buf.getvalue()


def upload(name, data):
    return UploadFile(file=io.BytesIO(data if isinstance(data, bytes) else data.encode()),
                      filename=name)


class AdmissionTests(unittest.TestCase):
    """What an upload queues: one file per logical name, the last one winning."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for change in (patch.object(settings, "upload_dir", temp.name),
                       patch.object(wc, "collection_exists", AsyncMock(return_value=True))):
            change.start(); self.addCleanup(change.stop)

    def admit(self, *files):
        started = {}
        with patch.object(ingest_pipeline, "_process_job_sync",
                          lambda *args: started.setdefault("args", args)):
            response = asyncio.run(ingest.ingest_upload(
                collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
                similarity_threshold="0.85", min_chunk_size="0", files=list(files)))
        self.assertFalse(hasattr(response, "status_code"), getattr(response, "body", b""))
        job = ingest_pipeline.get_job(response.job_id)
        self.addCleanup(ingest_pipeline._jobs.pop, response.job_id, None)
        paths, labels, names = started["args"][1], started["args"][-2], started["args"][-1]
        queued = [(names[p], labels[p], p.read_text()) for p in paths]
        return job, queued, paths

    def test_same_name_twice_in_one_body_keeps_the_last_bytes(self):
        job, queued, _ = self.admit(upload("r.txt", "first"), upload("r.txt", "second"))
        self.assertEqual(queued, [("r.txt", "r.txt", "second")])
        self.assertEqual(job["files_total"], 1)
        self.assertEqual(job["chunks_replaced"], 0)
        self.assertEqual(job["skipped"],
                         ["r.txt (replaced by r.txt later in this upload, same name 'r.txt')"])

    def test_repeated_zip_entries_keep_the_last(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("a/r.txt", "one"), ("a/r.txt", "two")])))
        self.assertEqual(queued, [("a/r.txt", "u.zip:a/r.txt", "two")])
        self.assertEqual(job["skipped"], [
            "u.zip:a/r.txt (replaced by u.zip:a/r.txt later in this upload, same name 'a/r.txt')"])

    def test_same_path_in_two_zips_is_a_collision(self):
        job, queued, _ = self.admit(upload("one.zip", zip_bytes([("docs/r.txt", "one")])),
                                    upload("two.zip", zip_bytes([("docs/r.txt", "two")])))
        self.assertEqual(queued, [("docs/r.txt", "two.zip:docs/r.txt", "two")])
        self.assertEqual(len(job["skipped"]), 1)
        self.assertTrue(job["skipped"][0].startswith("one.zip:docs/r.txt (replaced by two.zip:docs/r.txt"))

    def test_direct_upload_and_top_level_member_share_a_name(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes([("r.txt", "zipped")])),
                                    upload("r.txt", "direct"))
        self.assertEqual(queued, [("r.txt", "r.txt", "direct")])
        self.assertEqual(job["skipped"],
                         ["u.zip:r.txt (replaced by r.txt later in this upload, same name 'r.txt')"])

    def test_folders_inside_a_zip_keep_documents_apart(self):
        job, queued, paths = self.admit(upload("u.zip", zip_bytes(
            [("a/report.txt", "alpha"), ("b/report.txt", "beta"), ("report.txt", "top")])))
        self.assertEqual(sorted(queued), [("a/report.txt", "u.zip:a/report.txt", "alpha"),
                                          ("b/report.txt", "u.zip:b/report.txt", "beta"),
                                          ("report.txt", "u.zip:report.txt", "top")])
        self.assertEqual(job["skipped"], [])
        # Each in a directory of its own, under its own basename for the parser.
        self.assertEqual({p.name for p in paths}, {"report.txt"})
        self.assertEqual(len({p.parent for p in paths}), 3)

    def test_internal_dot_dot_is_normalised_and_its_bytes_are_queued(self):
        # ZipFile.extract would write a/../b.txt to a/b.txt; the queue named b.txt,
        # a file that did not exist.
        job, queued, paths = self.admit(upload("u.zip", zip_bytes(
            [("a/../b.txt", "dotted"), ("./c.txt", "dot")])))
        self.assertEqual(sorted(queued), [("b.txt", "u.zip:a/../b.txt", "dotted"),
                                          ("c.txt", "u.zip:./c.txt", "dot")])
        self.assertTrue(all(p.is_file() for p in paths))

    def test_normalised_name_collides_with_its_plain_spelling(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("b.txt", "plain"), ("a/../b.txt", "dotted")])))
        self.assertEqual(queued, [("b.txt", "u.zip:a/../b.txt", "dotted")])
        self.assertEqual(job["skipped"], [
            "u.zip:b.txt (replaced by u.zip:a/../b.txt later in this upload, same name 'b.txt')"])

    def test_members_leaving_the_archive_are_still_dropped(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("../out.txt", "x"), ("/abs.txt", "y"), ("a/../../up.txt", "z"), ("in.txt", "ok")])))
        self.assertEqual(queued, [("in.txt", "u.zip:in.txt", "ok")])
        self.assertEqual(job["skipped"], [])

    def test_a_dropped_input_leaves_no_staged_file(self):
        _, _, paths = self.admit(upload("r.txt", "first"), upload("r.txt", "second"))
        staged = [p for p in Path(paths[0]).parents[1].rglob("*") if p.is_file()]
        self.assertEqual(staged, paths)


class FakeCollection:
    """Chunks by name, with the replacement helpers' contract."""

    def __init__(self, **existing):
        self.chunks = {name: [{"content": c, "created_at": "old"} for c in contents]
                       for name, contents in existing.items()}
        self.calls = []

    def has(self, collection, name):
        self.calls.append(("has", name))
        return bool(self.chunks.get(name))

    def insert(self, collection, chunks):
        self.calls.append(("insert", chunks[0]["source_file"] if chunks else None))
        for chunk in chunks:
            self.chunks.setdefault(chunk["source_file"], []).append(chunk)

    def delete(self, collection, name, created_at, expected):
        self.calls.append(("delete", name))
        stamp = created_at.isoformat()
        mine = [c for c in self.chunks[name] if c["created_at"] == stamp]
        if len(mine) != expected:
            raise wc.ReplaceError("mismatch", 0)
        removed = len(self.chunks[name]) - len(mine)
        self.chunks[name] = mine
        return removed


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stored = []
        self.marks = []

    def run_job(self, backend, files, *, mark=None, delete=None, store=None):
        paths, names, labels = [], {}, {}
        for i, (name, text) in enumerate(files):
            path = self.root / str(i) / Path(name).name
            path.parent.mkdir(); path.write_text(text)
            paths.append(path); names[path] = name; labels[path] = f"up.zip:{name}"
        job_id = "r" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": len(paths),
            "files_completed": 0, "files_failed": 0, "chunks_stored": 0,
            "chunks_replaced": 0, "errors": [], "skipped": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)

        def default_mark(collection, reason, require_durable=False):
            self.marks.append((collection, reason, require_durable))
            backend.calls.append(("mark", reason))
            return 1

        def default_store(collection, name, data, media_type=None, *, replace=False):
            self.stored.append((name, data, replace))

        with patch.object(wc, "_source_has_chunks_sync", side_effect=backend.has), \
                patch.object(wc, "_insert_chunks_sync", side_effect=backend.insert), \
                patch.object(wc, "_delete_earlier_chunks_sync", side_effect=delete or backend.delete), \
                patch.object(goldstandard, "mark_stale", side_effect=mark or default_mark), \
                patch.object(ingest_pipeline.sources, "store", side_effect=store or default_store), \
                patch.object(ingest_pipeline, "do_chunk", side_effect=lambda text, **_: text.split()):
            ingest_pipeline._process_job_sync(job_id, paths, self.root, "Coll", "fixed", 200, 0,
                                              0.85, 0, labels, names)
        return ingest_pipeline._jobs[job_id]

    def test_new_documents_mark_nothing_and_replace_nothing(self):
        backend = FakeCollection()
        job = self.run_job(backend, [("a/r.txt", "one two"), ("b/r.txt", "three")])
        self.assertEqual((job["status"], job["files_completed"], job["chunks_stored"],
                          job["chunks_replaced"]), ("completed", 2, 3, 0))
        self.assertEqual(self.marks, [])
        self.assertNotIn("delete", [c[0] for c in backend.calls])
        self.assertEqual(sorted(backend.chunks), ["a/r.txt", "b/r.txt"])
        self.assertEqual([(n, r) for n, _, r in self.stored], [("a/r.txt", True), ("b/r.txt", True)])

    def test_a_replacement_marks_once_then_inserts_before_deleting(self):
        backend = FakeCollection(**{"p.txt": ["v1a", "v1b", "v1c"], "q.txt": ["old"]})
        job = self.run_job(backend, [("p.txt", "v2a v2b"), ("new.txt", "n"), ("q.txt", "q2")])
        self.assertEqual((job["status"], job["files_completed"], job["chunks_replaced"]),
                         ("completed", 3, 4))
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v2a", "v2b"])
        self.assertEqual(len(self.marks), 1)
        collection, reason, durable = self.marks[0]
        self.assertEqual((collection, durable), ("Coll", True))
        self.assertEqual(reason, "documents were re-ingested, replacing their earlier chunks: p.txt, q.txt")
        steps = [c[0] for c in backend.calls if c[0] != "has"]
        self.assertEqual(steps, ["mark", "insert", "delete", "insert", "insert", "delete"])

    def test_unchanged_content_is_still_replaced(self):
        backend = FakeCollection(**{"p.txt": ["same", "words"]})
        job = self.run_job(backend, [("p.txt", "same words")])
        self.assertEqual((job["status"], job["chunks_stored"], job["chunks_replaced"]),
                         ("completed", 2, 2))
        self.assertEqual(len(backend.chunks["p.txt"]), 2)

    def test_a_marking_failure_fails_only_that_file_and_changes_nothing(self):
        backend = FakeCollection(**{"p.txt": ["v1"], "q.txt": ["old"]})
        attempts = []

        def mark(collection, reason, require_durable=False):
            attempts.append(reason)
            if len(attempts) == 1:
                raise goldstandard.GoldStandardError("SESSION_WRITE_FAILED", "disk full", 503)
            return 0

        with self.assertLogs("services.ingest_pipeline", "WARNING"):
            job = self.run_job(backend, [("p.txt", "v2"), ("q.txt", "q2")], mark=mark)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("partial", 1, 1))
        self.assertEqual(job["errors"], [
            "up.zip:p.txt: could not mark gold-standard sessions stale before replacing its "
            "chunks, so nothing was changed: disk full"])
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v1"])
        # The next replacing file tries the marking again.
        self.assertEqual(len(attempts), 2)
        self.assertEqual([c["content"] for c in backend.chunks["q.txt"]], ["q2"])
        self.assertEqual([n for n, _, _ in self.stored], ["q.txt"])

    def test_a_failed_insert_keeps_the_earlier_version(self):
        backend = FakeCollection(**{"p.txt": ["v1"]})

        def insert(collection, chunks):
            raise RuntimeError("Weaviate rejected 1 batch object(s)")

        with patch.object(wc, "_collection_exists_sync", return_value=True), \
                self.assertLogs("services.ingest_pipeline", "WARNING"):
            backend.insert = insert
            job = self.run_job(backend, [("p.txt", "v2")])
        self.assertEqual(job["status"], "failed")
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v1"])
        self.assertNotIn("delete", [c[0] for c in backend.calls])
        self.assertEqual(self.stored, [])

    def test_a_removal_failure_keeps_new_chunks_and_reports_partial(self):
        backend = FakeCollection(**{"p.txt": ["v1", "v1b"]})

        def delete(collection, name, created_at, expected):
            raise wc.ReplaceError("1 earlier chunk(s) remain", 1)

        with self.assertLogs("services.ingest_pipeline", "ERROR"):
            job = self.run_job(backend, [("p.txt", "v2"), ("n.txt", "n")], delete=delete)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"],
                          job["chunks_stored"], job["chunks_replaced"]), ("partial", 1, 1, 2, 1))
        self.assertEqual(job["errors"], [
            "up.zip:p.txt: its new chunks were stored, but earlier chunks could not all be "
            "removed (1 earlier chunk(s) remain); upload it again to retry"])
        # Retention still follows the new version.
        self.assertEqual([(n, r) for n, _, r in self.stored], [("p.txt", True), ("n.txt", True)])

    def test_a_deleted_collection_is_reported_at_the_lookup(self):
        backend = FakeCollection()

        def has(collection, name):
            raise RuntimeError("could not find class Coll in schema")

        backend.has = has
        with patch.object(wc, "_collection_exists_sync", return_value=False), \
                self.assertLogs("services.ingest_pipeline", "WARNING"):
            job = self.run_job(backend, [("p.txt", "v2"), ("q.txt", "q")])
        self.assertEqual((job["status"], job["files_failed"]), ("failed", 2))
        self.assertEqual(job["errors"], ["collection 'Coll' was deleted while this job was running"])

    def test_reason_lists_ten_names_then_a_count(self):
        names = {f"f{i:02}.txt": ["old"] for i in range(12)}
        backend = FakeCollection(**names)
        self.run_job(backend, [(n, "new") for n in sorted(names)])
        self.assertEqual(len(self.marks), 1)
        self.assertTrue(self.marks[0][1].endswith("f08.txt, f09.txt and 2 more"), self.marks[0][1])


class Result(SimpleNamespace):
    pass


class DeleteEarlierTests(unittest.TestCase):
    """The backend half: precondition, delete rounds, and the final count."""

    def collection(self, counts, deletes):
        counts, deletes = list(counts), list(deletes)
        calls = []

        def over_all(filters=None, total_count=False):
            calls.append(("count", filters))
            return Result(total_count=counts.pop(0))

        def delete_many(where=None):
            calls.append(("delete", where))
            return deletes.pop(0)

        coll = SimpleNamespace(aggregate=SimpleNamespace(over_all=over_all),
                               data=SimpleNamespace(delete_many=delete_many))
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        return client, calls

    def run_delete(self, counts, deletes, expected=3):
        client, calls = self.collection(counts, deletes)
        with patch.object(wc, "get_client", return_value=client):
            try:
                return wc._delete_earlier_chunks_sync(
                    "Coll", "p.txt", datetime(2026, 10, 18, tzinfo=timezone.utc), expected), calls
            except wc.ReplaceError as exc:
                return exc, calls

    def test_deletes_in_rounds_until_nothing_matches(self):
        deleted, calls = self.run_delete(
            [3, 3], [Result(successful=10000, matches=10000, failed=0),
                     Result(successful=5, matches=5, failed=0),
                     Result(successful=0, matches=0, failed=0)])
        self.assertEqual(deleted, 10005)
        self.assertEqual([c[0] for c in calls], ["count", "delete", "delete", "delete", "count"])

    def test_precondition_mismatch_deletes_nothing(self):
        exc, calls = self.run_delete([2], [])
        self.assertIsInstance(exc, wc.ReplaceError)
        self.assertEqual(exc.deleted, 0)
        self.assertIn("found 2 of its 3 new chunks", str(exc))
        self.assertEqual([c[0] for c in calls], ["count"])

    def test_failed_objects_stop_with_the_count_so_far(self):
        exc, _ = self.run_delete([3], [Result(successful=4, matches=6, failed=2)])
        self.assertEqual((exc.deleted, str(exc)), (4, "Weaviate failed to delete 2 earlier chunk(s)"))

    def test_leftover_chunks_are_reported(self):
        exc, _ = self.run_delete([3, 4], [Result(successful=1, matches=1, failed=0),
                                          Result(successful=0, matches=0, failed=0)])
        self.assertEqual((exc.deleted, str(exc)), (1, "1 earlier chunk(s) remain"))

    def test_rounds_are_bounded(self):
        rounds = [Result(successful=1, matches=1, failed=0)] * wc._REPLACE_DELETE_ROUNDS
        exc, calls = self.run_delete([3], rounds)
        self.assertIsInstance(exc, wc.ReplaceError)
        self.assertEqual(exc.deleted, wc._REPLACE_DELETE_ROUNDS)


class SourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        change = patch.object(settings, "sources_dir", temp.name)
        change.start(); self.addCleanup(change.stop)

    def docs(self):
        return sources.load_index("Coll")["documents"]

    def test_replace_releases_the_name_and_drops_the_orphan(self):
        v1 = sources.store("Coll", "p.txt", b"v1", replace=True)
        v2 = sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertEqual({d: e["filenames"] for d, e in self.docs().items()}, {v2: ["p.txt"]})
        self.assertFalse(sources.blob_path("Coll", v1).exists())
        self.assertTrue(sources.blob_path("Coll", v2).is_file())

    def test_shared_content_keeps_both_names_e2(self):
        d = sources.store("Coll", "a.txt", b"same", replace=True)
        sources.store("Coll", "b.txt", b"same", replace=True)
        self.assertEqual(self.docs()[d]["filenames"], ["a.txt", "b.txt"])
        # Replacing one name keeps the digest for the other.
        sources.store("Coll", "a.txt", b"changed", replace=True)
        self.assertEqual(self.docs()[d]["filenames"], ["b.txt"])
        self.assertTrue(sources.blob_path("Coll", d).is_file())

    def test_without_replace_behaves_as_before(self):
        v1 = sources.store("Coll", "p.txt", b"v1")
        v2 = sources.store("Coll", "p.txt", b"v2")
        self.assertEqual(sorted(sources.digests_for_filename("Coll", "p.txt")), sorted([v1, v2]))

    def test_name_is_released_even_when_the_blob_write_fails(self):
        v1 = sources.store("Coll", "p.txt", b"v1", replace=True)
        with patch.object(Path, "write_bytes", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertEqual(self.docs(), {})
        self.assertFalse(sources.blob_path("Coll", v1).exists())


class RechunkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for change in (patch.object(settings, "sources_dir", str(Path(temp.name) / "s")),
                       patch.object(settings, "upload_dir", temp.name)):
            change.start(); self.addCleanup(change.stop)

    def stored(self, *chunks):
        objects = [SimpleNamespace(properties={"source_file": n, "chunk_index": i}) for n, i in chunks]
        coll = SimpleNamespace(iterator=lambda **_: iter(objects))
        return patch.object(wc, "get_client", return_value=SimpleNamespace(
            collections=SimpleNamespace(get=lambda name: coll)))

    def test_same_basename_in_two_folders_rebuilds_both_under_full_names(self):
        sources.store("Coll", "a/report.txt", b"alpha text", replace=True)
        sources.store("Coll", "b/report.txt", b"beta text", replace=True)
        sources.store("Coll", "copy.txt", b"alpha text", replace=True)
        with self.stored(("a/report.txt", 0), ("b/report.txt", 0), ("copy.txt", 0)):
            chunks = tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        got = sorted((c["source_file"], c["content"], c["source_type"]) for c in chunks)
        self.assertEqual(got, [("a/report.txt", "alpha text", "txt"),
                               ("b/report.txt", "beta text", "txt"),
                               ("copy.txt", "alpha text", "txt")])

    def test_a_name_with_two_digests_is_still_refused(self):
        sources.store("Coll", "p.txt", b"v1")
        sources.store("Coll", "p.txt", b"v2")
        with self.stored(("p.txt", 0)):
            self.assertEqual(tuning._uncovered_source_files("Coll", sources.load_index("Coll")["documents"]),
                             ["p.txt"])

    def test_a_second_name_is_covered(self):
        sources.store("Coll", "a.txt", b"same", replace=True)
        sources.store("Coll", "b.txt", b"same", replace=True)
        with self.stored(("a.txt", 0), ("b.txt", 0)):
            self.assertEqual(tuning._uncovered_source_files("Coll", sources.load_index("Coll")["documents"]), [])


class SchemaTests(unittest.TestCase):
    def test_a_record_written_before_the_field_reads_as_zero(self):
        old = {"job_id": "j", "status": "completed", "files_total": 1, "files_completed": 1,
               "files_failed": 0, "chunks_stored": 2, "errors": [], "skipped": []}
        self.assertEqual(JobStatusResponse(**old).chunks_replaced, 0)
        self.assertEqual(JobStatusResponse(**old, chunks_replaced=5).chunks_replaced, 5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
