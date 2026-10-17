"""Upload unpacking errors and gzipped JSON members (issue #290).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_ingest_errors.py

Weaviate and source retention are mocked; nothing reaches a backend.
"""
import asyncio
import gzip
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi import UploadFile

from config import settings
from routers import ingest
from services import ingest_pipeline
from services import weaviate_client as wc

# The verify fixture's JSON shape, which the JSON parser accepts.
JSON_DOC = json.dumps([{"policy": "p0", "detail": "Remote work needs manager approval."},
                       {"policy": "p1", "detail": "Expenses are filed within thirty days."}])


def zip_bytes(members, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def truncated_zip():
    """Like the maintainer's file: a valid local header, no central directory."""
    data = zip_bytes([("policies.txt", "Some text. " * 50)], zipfile.ZIP_STORED)
    return data[:data.index(b"PK\x01\x02")]


def patch_header(data, local_offset, central_offset, value, set_bits=False):
    """Change a 2-byte field of the first member in both of its headers."""
    out = bytearray(data)
    central = data.index(b"PK\x01\x02")
    for pos in (local_offset, central + central_offset):
        current = int.from_bytes(out[pos:pos + 2], "little")
        out[pos:pos + 2] = ((current | value) if set_bits else value).to_bytes(2, "little")
    return bytes(out)


def shifted_offsets(data, shift):
    """Like the maintainer's full file (D7): the end record says the central
    directory starts `shift` bytes later than it does, so every member's
    offset is read `shift` bytes too early: negative seeks and bad headers."""
    out = bytearray(data)
    end = data.rindex(b"PK\x05\x06")
    offset = int.from_bytes(out[end + 16:end + 20], "little")
    out[end + 16:end + 20] = (offset + shift).to_bytes(4, "little")
    return bytes(out)


def bad_crc(data, needle):
    out = bytearray(data)
    out[out.index(needle)] ^= 0x01
    return bytes(out)


def upload(name, data):
    return UploadFile(file=io.BytesIO(data), filename=name)


class UploadErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.uploads = Path(self.temp.name)
        for target, value in ((settings, "upload_dir"),):
            p = patch.object(target, value, str(self.uploads))
            p.start(); self.addCleanup(p.stop)
        p = patch.object(wc, "collection_exists", AsyncMock(return_value=True))
        p.start(); self.addCleanup(p.stop)
        self.jobs_before = set(ingest_pipeline._jobs)

    def post(self, *files):
        response = asyncio.run(ingest.ingest_upload(
            collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
            similarity_threshold="0.85", min_chunk_size="0", files=list(files)))
        if hasattr(response, "status_code"):
            return response.status_code, json.loads(response.body)
        return 202, response.model_dump()      # the route's declared success status

    def assert_refused(self, status, body, code, *in_message):
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], code, body)
        for text in in_message:
            self.assertIn(text, body["error"]["message"])
        # Nothing queued and nothing left behind.
        self.assertEqual(set(ingest_pipeline._jobs), self.jobs_before)
        self.assertEqual(list(self.uploads.iterdir()), [])

    def test_truncated_zip_is_a_readable_400(self):
        with self.assertLogs("services.ingest_pipeline", "WARNING"):
            status, body = self.post(upload("corpus.zip", truncated_zip()))
        self.assert_refused(status, body, "ARCHIVE_UNREADABLE",
                            "'corpus.zip' is damaged and can't be read, so nothing from it was imported",
                            "truncated", "Re-create the zip")
        self.assertEqual(body["error"]["detail"], {"file": "corpus.zip", "members": []})

    def test_wrong_member_offsets_name_every_unreadable_member(self):
        data = zip_bytes([("a.json", JSON_DOC), ("b.txt", "text " * 40),
                          ("c.jsonl.gz", gzip.compress(b"{}"))], zipfile.ZIP_STORED)
        status, body = self.post(upload("corpus.zip", shifted_offsets(data, 60)))
        self.assert_refused(status, body, "ARCHIVE_UNREADABLE",
                            "'corpus.zip' is damaged and can't be read", "Unreadable members: a.json (damaged:",
                            "Re-create the zip")
        members = body["error"]["detail"]["members"]
        self.assertEqual([m["member"] for m in members], ["a.json", "b.txt", "c.jsonl.gz"])
        self.assertTrue(all(m["reason"].startswith("damaged: ") for m in members), members)
        # The first member's offset is now before the file starts (EINVAL).
        self.assertEqual(members[0]["reason"], "damaged: its recorded position in the zip is invalid")

    def test_message_names_ten_members_and_counts_the_rest(self):
        data = zip_bytes([(f"m{i:02}.txt", "t") for i in range(12)], zipfile.ZIP_STORED)
        status, body = self.post(upload("many.zip", shifted_offsets(data, len(data))))
        self.assert_refused(status, body, "ARCHIVE_UNREADABLE", "m09.txt (damaged", "; and 2 more.")
        self.assertNotIn("m10.txt", body["error"]["message"])
        self.assertEqual(len(body["error"]["detail"]["members"]), 12)

    def test_bad_crc_is_a_readable_400(self):
        data = bad_crc(zip_bytes([("a.txt", "x" * 100)], zipfile.ZIP_STORED), b"x" * 100)
        status, body = self.post(upload("bad.zip", data))
        self.assert_refused(status, body, "ARCHIVE_UNREADABLE", "a.txt (damaged: Bad CRC-32")

    def test_damage_in_a_skipped_member_still_refuses_the_zip(self):
        payload = b"y" * 100
        data = zip_bytes([("good.txt", "Readable text."), ("records.jsonl.gz", payload)],
                         zipfile.ZIP_STORED)
        status, body = self.post(upload("mixed.zip", bad_crc(data, payload)),
                                 upload("other.txt", b"A separate, valid upload."))
        self.assert_refused(status, body, "ARCHIVE_UNREADABLE", "records.jsonl.gz (damaged")
        self.assertEqual([m["member"] for m in body["error"]["detail"]["members"]],
                         ["records.jsonl.gz"])

    def test_unsupported_compression_is_a_readable_400(self):
        data = patch_header(zip_bytes([("a.txt", "text")], zipfile.ZIP_STORED), 8, 10, 9)
        status, body = self.post(upload("d64.zip", data))
        self.assert_refused(status, body, "ARCHIVE_UNSUPPORTED", "'d64.zip' can't be read",
                            "a.txt (unsupported compression method 9)", "standard (Deflate)")
        self.assertEqual(body["error"]["detail"], {"file": "d64.zip", "members": [
            {"member": "a.txt", "reason": "unsupported compression method 9"}]})

    def test_encrypted_member_is_a_readable_400(self):
        data = patch_header(zip_bytes([("a.txt", "text")]), 6, 8, 0x1, set_bits=True)
        status, body = self.post(upload("locked.zip", data))
        self.assert_refused(status, body, "ARCHIVE_UNSUPPORTED", "a.txt (password-protected)",
                            "without a password")

    def test_filesystem_error_is_a_readable_400(self):
        name = "n" * 300 + ".txt"
        status, body = self.post(upload("long.zip", zip_bytes([(name, "text")])))
        self.assert_refused(status, body, "UPLOAD_WRITE_FAILED", "Could not extract",
                            "File name too long")
        self.assertEqual(body["error"]["detail"], {"file": "long.zip", "members": [
            {"member": name, "reason": "File name too long"}]})

    def test_unexpected_error_is_a_json_500_with_logged_traceback(self):
        boom = RuntimeError("boom at /internal/path")
        with patch.object(ingest_pipeline, "start_ingest_job", AsyncMock(side_effect=boom)), \
                self.assertLogs("routers.ingest", "ERROR") as logs:
            status, body = self.post(upload("a.txt", b"text"))
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "INGEST_FAILED")
        self.assertIn("unexpected server error", body["error"]["message"])
        self.assertNotIn("boom", body["error"]["message"])
        record = logs.records[0]
        self.assertIsNotNone(record.exc_info)
        self.assertIn("'Coll'", record.getMessage())
        self.assertIn("a.txt", record.getMessage())

    def test_json_gz_member_is_queued_and_other_gz_skipped(self):
        data = zip_bytes([("docs/a.json", JSON_DOC),
                          ("docs/b.json.gz", gzip.compress(JSON_DOC.encode())),
                          ("docs/c.txt.gz", gzip.compress(b"text")),
                          ("docs/d.jsonl", "{}\n"),
                          ("docs/e.JSONL.gz", gzip.compress(b"{}\n"))])
        started = {}

        def record(*args):
            started["args"] = args

        with patch.object(ingest_pipeline, "_process_job_sync", record):
            status, body = self.post(upload("batch.zip", data),
                                     upload("direct.json.gz", gzip.compress(b"{}")),
                                     upload("direct.jsonl", b"{}\n"))
        self.assertEqual(status, 202, body)
        self.addCleanup(ingest_pipeline._jobs.pop, body["job_id"], None)
        job = ingest_pipeline.get_job(body["job_id"])
        self.assertEqual(job["files_total"], 2)
        jsonl = "(JSON Lines (.jsonl) isn't supported yet, see #291)"
        self.assertEqual(sorted(job["skipped"]), sorted([
            "batch.zip:docs/c.txt.gz (unsupported type '.gz')",
            f"batch.zip:docs/d.jsonl {jsonl}",
            f"batch.zip:docs/e.JSONL.gz {jsonl}",
            "direct.json.gz (unsupported type '.gz')",
            f"direct.jsonl {jsonl}"]))
        paths, labels = started["args"][1], started["args"][-1]
        self.assertTrue(all(p.is_file() for p in paths))
        self.assertEqual(sorted(labels[p] for p in paths),
                         ["batch.zip:docs/a.json", "batch.zip:docs/b.json.gz"])


class JsonGzParseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def gz(self, data, name="doc.json.gz"):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def assert_no_temp_left(self, keep):
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted(keep))

    def test_valid_json_gz_parses_like_json(self):
        path = self.gz(gzip.compress(JSON_DOC.encode()))
        text, elements = ingest_pipeline._parse_file(path)
        self.assertIn("Remote work needs manager approval.", text)
        self.assertTrue(elements)
        self.assertEqual(ingest_pipeline.source_type_of(path), "json")
        self.assert_no_temp_left(["doc.json.gz"])

    def test_upper_case_suffix_is_recognised(self):
        path = self.gz(gzip.compress(JSON_DOC.encode()), "DOC.JSON.GZ")
        self.assertIn("Remote work", ingest_pipeline._parse_file(path)[0])

    def test_corrupt_gzip(self):
        path = self.gz(b"this is not gzip data")
        with self.assertRaisesRegex(ValueError, "is not a valid gzip file; it may be truncated or corrupt"):
            ingest_pipeline._parse_file(path)
        self.assert_no_temp_left(["doc.json.gz"])

    def test_truncated_gzip(self):
        data = gzip.compress(JSON_DOC.encode() * 20)
        path = self.gz(data[:len(data) // 2])
        with self.assertRaisesRegex(ValueError, "is not a valid gzip file; it may be truncated or corrupt"):
            ingest_pipeline._parse_file(path)
        self.assert_no_temp_left(["doc.json.gz"])

    def test_decompressed_size_is_bounded(self):
        path = self.gz(gzip.compress(b" " * 5000))
        with patch.object(ingest_pipeline, "MAX_GZIP_DECOMPRESSED_BYTES", 1000), \
                self.assertRaisesRegex(ValueError, "decompresses to more than .* MB, the limit"):
            ingest_pipeline._parse_file(path)
        self.assert_no_temp_left(["doc.json.gz"])

    def test_unparseable_json_after_decompression(self):
        path = self.gz(gzip.compress(b"{not json"))
        with self.assertRaisesRegex(ValueError, "was decompressed, but its JSON could not be parsed: .+"):
            ingest_pipeline._parse_file(path)
        self.assert_no_temp_left(["doc.json.gz"])


class JobErrorTests(unittest.TestCase):
    def test_per_file_errors_name_the_member_and_never_blank(self):
        tmp = Path(tempfile.mkdtemp())
        good = tmp / "good.json.gz"; good.write_bytes(gzip.compress(JSON_DOC.encode()))
        bad = tmp / "docs" / "bad.json.gz"; bad.parent.mkdir(); bad.write_bytes(b"junk")
        blank = tmp / "blank.txt"; blank.write_text("text")
        labels = {good: "batch.zip:good.json.gz", bad: "batch.zip:docs/bad.json.gz"}
        job_id = "t" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": 3, "files_completed": 0,
            "files_failed": 0, "chunks_stored": 0, "errors": [], "skipped": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        stored = []
        real_parse = ingest_pipeline._parse_file

        def parse(path):
            if path == blank:
                raise RuntimeError()
            return real_parse(path)

        with patch.object(wc, "_insert_chunks_sync", lambda _c, chunks: stored.extend(chunks)), \
                patch.object(ingest_pipeline.sources, "store"), \
                patch.object(ingest_pipeline, "_parse_file", parse), \
                self.assertLogs("services.ingest_pipeline", "WARNING") as logs:
            ingest_pipeline._process_job_sync(job_id, [good, bad, blank], tmp, "Coll",
                                              "fixed", 200, 0, 0.85, 0, labels)
        job = ingest_pipeline._jobs[job_id]
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["files_completed"], 1)
        self.assertEqual(len(job["errors"]), 2, job["errors"])
        self.assertTrue(job["errors"][0].startswith(
            "batch.zip:docs/bad.json.gz: is not a valid gzip file; it may be truncated or corrupt"),
            job["errors"])
        self.assertEqual(job["errors"][1], "blank.txt: RuntimeError")
        self.assertTrue(stored)
        self.assertEqual({c["source_type"] for c in stored}, {"json"})
        self.assertEqual({c["source_file"] for c in stored}, {"good.json.gz"})
        self.assertEqual(len(logs.records), 2)
        self.assertTrue(all(r.exc_info for r in logs.records))
        self.assertFalse(tmp.exists())


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
