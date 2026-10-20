"""The total expanded-size cap on an upload's ZIPs, and ZIP work off the event
loop (issue #298).

Runs with the API's dependencies, inside the verify project's API container,
with a small cap set through the environment:

    docker compose exec -T -e RAG_TEST_API_DIR=/app -e MAX_ZIP_EXPANDED_BYTES=1048576 \
        api python - < scripts/tests/test_zip_expansion.py

Without MAX_ZIP_EXPANDED_BYTES the test sets the same 1 MiB cap itself. Weaviate
is mocked and the ingest worker never runs; nothing reaches a backend.
"""
import asyncio
import gzip
import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))
# Before config is imported: the settings read the environment once.
os.environ.setdefault("MAX_ZIP_EXPANDED_BYTES", str(1024 * 1024))

from fastapi import UploadFile

import config
from config import settings
from routers import ingest
from services import ingest_pipeline
from services import weaviate_client as wc

CAP = settings.max_zip_expanded_bytes


def zip_bytes(members, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def upload(name, data):
    return UploadFile(file=io.BytesIO(data), filename=name)


def with_header_crc(data):
    """FHCRC set with a bogus header CRC: zlib's gzip mode refuses it, the
    job's gzip reader skips the field and decompresses everything."""
    return data[:3] + bytes([data[3] | 0x02]) + data[4:10] + b"\xde\xad" + data[10:]


def with_reserved_flag(data):
    """A reserved FLG bit set: also refused by zlib, read by the gzip module."""
    return data[:3] + bytes([data[3] | 0x20]) + data[4:]


def with_long_name(data, size):
    """FNAME set, with a name of size bytes: the gzip reader reads it one byte
    per call."""
    return data[:3] + bytes([data[3] | 0x08]) + data[4:10] + b"n" * size + b"\0" + data[10:]


def job_output(data):
    """How many bytes the job's reader (the gzip module) gets from data before
    it ends or refuses it."""
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
            while True:
                block = gz.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
    except (OSError, EOFError, zlib.error):
        pass
    return total


def message(name, limit="1 MB"):
    return (f"'{name}' expands to more than {limit} when unpacked, the most one upload's ZIPs "
            f"can expand to, so nothing from it was imported. Split it into smaller zips and "
            f"upload them in separate imports.")


class SettingTests(unittest.TestCase):
    def test_the_cap_comes_from_the_environment(self):
        self.assertEqual(CAP, int(os.environ["MAX_ZIP_EXPANDED_BYTES"]))

    def test_the_default_is_two_gib(self):
        with patch.dict(os.environ):
            os.environ.pop("MAX_ZIP_EXPANDED_BYTES", None)
            self.assertEqual(config.Settings(_env_file=None).max_zip_expanded_bytes, 2147483648)

    def test_limit_text(self):
        self.assertEqual(ingest_pipeline._limit_text(2 * 1024 ** 3), "2,048 MB")
        self.assertEqual(ingest_pipeline._limit_text(1024 * 1024), "1 MB")
        self.assertEqual(ingest_pipeline._limit_text(1000), "1,000 bytes")


class CapHarness(unittest.TestCase):
    """Posts uploads through the router with a 1 MiB cap; no worker runs."""

    def setUp(self):
        self.assertEqual(CAP, 1024 * 1024, "these cases are sized for a 1 MiB cap")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.uploads = Path(self.temp.name)
        p = patch.object(settings, "upload_dir", str(self.uploads))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(wc, "collection_exists", AsyncMock(return_value=True))
        p.start(); self.addCleanup(p.stop)
        # An accepted upload queues a job; its worker never runs here.
        self.started = []
        p = patch.object(ingest_pipeline, "_process_job_sync",
                         lambda *args: self.started.append(args))
        p.start(); self.addCleanup(p.stop)

    def post(self, *files):
        self.jobs_before = set(ingest_pipeline._jobs)
        self.uploads_before = set(self.uploads.iterdir())
        self.started_before = len(self.started)
        response = asyncio.run(ingest.ingest_upload(
            collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
            similarity_threshold="0.85", min_chunk_size="0", files=list(files)))
        if hasattr(response, "status_code"):
            return response.status_code, json.loads(response.body)
        body = response.model_dump()
        self.addCleanup(ingest_pipeline._jobs.pop, body["job_id"], None)
        return 202, body

    def assert_too_large(self, status, body, name):
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], {"code": "ARCHIVE_TOO_LARGE", "message": message(name),
                                         "detail": {"file": name, "members": []}})
        # Nothing queued and nothing left behind.
        self.assertEqual(set(ingest_pipeline._jobs), self.jobs_before)
        self.assertEqual(set(self.uploads.iterdir()), self.uploads_before)
        self.assertEqual(len(self.started), self.started_before)

    def queued_names(self):
        self.assertEqual(len(self.started), 1)
        return sorted(self.started[0][-1].values())



class CapTests(CapHarness):
    # ── declared sizes ────────────────────────────────────────────────────────
    def test_declared_sizes_over_the_cap_refuse_before_reading(self):
        read = []
        real = ingest_pipeline._member_problem
        with patch.object(ingest_pipeline, "_member_problem",
                          lambda *a, **k: read.append(a[1].filename) or real(*a, **k)), \
                self.assertLogs("services.ingest_pipeline", "WARNING") as logs:
            status, body = self.post(upload("big.zip", zip_bytes([("a.txt", "a" * (CAP + 1))])),
                                     upload("other.txt", b"A separate, valid upload."))
        self.assert_too_large(status, body, "big.zip")
        self.assertEqual(read, [])
        self.assertIn("ARCHIVE_TOO_LARGE", logs.output[0])

    def test_a_total_at_the_cap_is_accepted(self):
        half = CAP // 2
        status, body = self.post(upload("full.zip", zip_bytes([("a.txt", "a" * half),
                                                               ("b.bin", b"b" * (CAP - half))])))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.queued_names(), ["a.txt"])

    def test_one_byte_over_the_cap_is_refused(self):
        half = CAP // 2
        status, body = self.post(upload("over.zip", zip_bytes([("a.txt", "a" * half),
                                                               ("b.bin", b"b" * (CAP - half + 1))])))
        self.assert_too_large(status, body, "over.zip")

    def test_the_cap_is_for_all_the_zips_of_one_upload(self):
        first = zip_bytes([("a.txt", "a" * (CAP // 2))])
        second = zip_bytes([("b.txt", "b" * (CAP // 2 + 1))])
        status, body = self.post(upload("first.zip", first), upload("second.zip", second))
        self.assert_too_large(status, body, "second.zip")
        # Each alone is within the cap, in a request of its own.
        self.assertEqual(self.post(upload("second.zip", second))[0], 202)

    # ── actual bytes ──────────────────────────────────────────────────────────
    def test_counting_refuses_even_when_declared_sizes_understate(self):
        with patch.object(ingest_pipeline, "_declared_size", lambda _members: 0):
            status, body = self.post(upload("lies.zip", zip_bytes([("a.txt", "a" * (CAP + 1))])))
        self.assert_too_large(status, body, "lies.zip")

    def test_gzipped_members_count_their_output(self):
        for name in ("docs/big.json.gz", "docs/big.JSONL.gz"):
            with self.subTest(name):
                data = zip_bytes([(name, gzip.compress(b" " * (CAP + 1)))])
                self.assertLess(len(data), CAP // 100)   # the declared size is far under
                status, body = self.post(upload("gz.zip", data))
                self.assert_too_large(status, body, "gz.zip")

    def test_other_gzip_members_are_not_decompressed(self):
        data = zip_bytes([("notes.txt.gz", gzip.compress(b" " * (CAP + 1))), ("a.txt", "text")])
        status, body = self.post(upload("other.zip", data))
        self.assertEqual(status, 202, body)

    def test_concatenated_streams_and_zero_padding_are_counted(self):
        one = gzip.compress(b" " * (CAP // 2 + 1))
        status, body = self.post(upload("one.zip", zip_bytes([("a.json.gz", one)])))
        self.assertEqual(status, 202, body)
        status, body = self.post(upload("two.zip", zip_bytes([("a.json.gz", one + b"\0" * 16 + one)])))
        self.assert_too_large(status, body, "two.zip")

    def test_a_corrupt_gzip_stops_its_count_and_stays_a_job_error(self):
        data = zip_bytes([("bad.json.gz", b"this is not gzip" * 10), ("a.txt", "text")])
        status, body = self.post(upload("bad.zip", data))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.queued_names(), ["a.txt", "bad.json.gz"])

    def test_a_truncated_gzip_counts_what_it_produced(self):
        data = gzip.compress(os.urandom(64) * (4 * CAP // 64))
        status, body = self.post(upload("cut.zip", zip_bytes([("a.jsonl.gz", data[:len(data) // 2])])))
        self.assert_too_large(status, body, "cut.zip")

    def test_a_direct_jsonl_gz_does_not_count(self):
        status, body = self.post(upload("direct.jsonl.gz", gzip.compress(b"{}\n" * CAP)))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.queued_names(), ["direct.jsonl.gz"])

    def test_passing_the_cap_wins_over_an_unreadable_member(self):
        payload = b"y" * 100
        data = bytearray(zip_bytes([("bad.txt", payload),
                                    ("big.json.gz", gzip.compress(b" " * (CAP + 1)))],
                                   zipfile.ZIP_STORED))
        data[data.index(payload)] ^= 0x01   # a bad CRC in the first member
        status, body = self.post(upload("mixed.zip", bytes(data)))
        self.assert_too_large(status, body, "mixed.zip")

    def test_an_unreadable_zip_within_the_cap_is_refused_as_before(self):
        payload = b"y" * 100
        data = bytearray(zip_bytes([("bad.txt", payload)], zipfile.ZIP_STORED))
        data[data.index(payload)] ^= 0x01
        status, body = self.post(upload("bad.zip", bytes(data)))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_UNREADABLE"))

    # ── checking stops as soon as the total passes the cap (Decision 2) ─────────
    def test_no_member_is_read_after_the_cap_is_passed(self):
        read = []
        real = ingest_pipeline._member_problem
        data = zip_bytes([("big.txt", "a" * (CAP + 1)), ("later.txt", "text"),
                          ("last.json.gz", gzip.compress(b"{}"))])
        with patch.object(ingest_pipeline, "_declared_size", lambda _members: 0), \
                patch.object(ingest_pipeline, "_member_problem",
                             lambda *a, **k: read.append(a[1].filename) or real(*a, **k)):
            status, body = self.post(upload("stop.zip", data))
        self.assert_too_large(status, body, "stop.zip")
        self.assertEqual(read, ["big.txt"])

    def test_counting_a_gzip_member_stops_soon_after_the_cap(self):
        # 64 MiB of output from about 64 KiB of gzip: counting it all would
        # spend 64 times the cap; it stops within one block of passing it.
        most = []
        real = ingest_pipeline._ExpansionBudget.spend

        def spend(budget, size):
            try:
                real(budget, size)
            finally:
                most.append(budget.used)

        data = zip_bytes([("huge.json.gz", gzip.compress(bytes(64 * CAP)))], zipfile.ZIP_STORED)
        with patch.object(ingest_pipeline._ExpansionBudget, "spend", spend):
            status, body = self.post(upload("huge.zip", data))
        self.assert_too_large(status, body, "huge.zip")
        self.assertLessEqual(max(most), CAP + ingest_pipeline._BLOCK, max(most))


class GzipHeaderTests(CapHarness):
    """The count reads gzip members as the job does, so a header the job
    accepts can't make a member count nothing (PR #318 security review)."""

    def test_headers_zlib_refuses_still_count(self):
        for label, variant in (("FHCRC", with_header_crc), ("reserved flag", with_reserved_flag)):
            with self.subTest(label):
                data = variant(gzip.compress(b" " * (CAP + 1)))
                # The job's reader gets it all; zlib's gzip mode refuses it.
                self.assertEqual(job_output(data), CAP + 1)
                with self.assertRaises(zlib.error):
                    zlib.decompressobj(31).decompress(data)
                status, body = self.post(upload("hdr.zip", zip_bytes([("a.json.gz", data)])))
                self.assert_too_large(status, body, "hdr.zip")

    def test_several_such_members_cannot_add_up_unseen(self):
        part = with_header_crc(gzip.compress(b" " * (CAP // 4 + 1)))
        data = zip_bytes([(f"m{i}.jsonl.gz", part) for i in range(4)])
        status, body = self.post(upload("many.zip", data))
        self.assert_too_large(status, body, "many.zip")

    def test_the_count_is_what_the_job_reader_gets(self):
        plain = gzip.compress(b"x" * 1000)
        cases = {
            "plain": plain,
            "concatenated with padding": plain + b"\0" * 7 + gzip.compress(b"y" * 300),
            "header CRC": with_header_crc(plain),
            "reserved flag": with_reserved_flag(plain),
            "truncated": gzip.compress(os.urandom(5000))[:3000],
            "trailing garbage": plain + b"junk",
            "not gzip": b"this is not gzip" * 10,
        }
        for label, data in cases.items():
            with self.subTest(label):
                buf = io.BytesIO(zip_bytes([("a.json.gz", data)], zipfile.ZIP_STORED))
                with zipfile.ZipFile(buf) as z:
                    budget = ingest_pipeline._ExpansionBudget(10 * CAP)
                    problem = ingest_pipeline._member_problem(z, z.infolist()[0], budget, True)
                self.assertIsNone(problem)
                # The member's own bytes, once, plus what the job would decompress.
                self.assertEqual(budget.used, len(data) + job_output(data))

    def test_damage_found_while_counting_a_gzip_member_is_damage(self):
        # The ZIP layer fails inside the gzip count (an invalid deflate block
        # in a .json.gz member): the zip is unreadable, not a gzip job error.
        data = bytearray(zip_bytes([("a.json.gz", gzip.compress(b'{"a": 1}' * 1000, mtime=0))]))
        data[30 + len("a.json.gz")] = 0x07   # BTYPE 11: an invalid deflate block
        status, body = self.post(upload("dmg.zip", bytes(data)))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_UNREADABLE"), body)
        self.assertEqual([m["member"] for m in body["error"]["detail"]["members"]], ["a.json.gz"])
        self.assertTrue(body["error"]["detail"]["members"][0]["reason"].startswith("damaged: Error -3"),
                        body["error"]["detail"]["members"])
        self.assertEqual(set(ingest_pipeline._jobs), self.jobs_before)
        self.assertEqual(set(self.uploads.iterdir()), self.uploads_before)


class GzipBufferTests(unittest.TestCase):
    """The count reads a gzipped member a block at a time, though the gzip
    reader reads header fields and padding one byte per call (#319)."""

    def count_reads(self, data):
        """How many reads of the ZIP member validating it takes; the count
        itself must stay the member's bytes plus what the job decompresses."""
        reads = []
        real = ingest_pipeline._CountedMember.read

        def read(member, size=-1):
            reads.append(size)
            return real(member, size)

        buf = io.BytesIO(zip_bytes([("a.json.gz", data)]))
        with zipfile.ZipFile(buf) as z, \
                patch.object(ingest_pipeline._CountedMember, "read", read):
            budget = ingest_pipeline._ExpansionBudget(10 * CAP)
            problem = ingest_pipeline._member_problem(z, z.infolist()[0], budget, True)
        self.assertIsNone(problem)
        self.assertEqual(budget.used, len(data) + job_output(data))
        return len(reads)

    def test_zero_padding_is_read_in_blocks(self):
        data = gzip.compress(b"x" * 1000) + b"\0" * (4 * 1024 * 1024)
        self.assertLess(self.count_reads(data), 100)

    def test_a_long_file_name_is_read_in_blocks(self):
        data = with_long_name(gzip.compress(b"x" * 1000), 2 * 1024 * 1024)
        self.assertEqual(job_output(data), 1000)
        self.assertLess(self.count_reads(data), 100)

    def test_data_read_ahead_past_the_stream_counts_once(self):
        # The stream ends early and more than a block follows: the buffer
        # reads part of it ahead, the caller reads the rest; each byte counts once.
        # (The gzip reader refuses the junk after the stream, so the job gets
        # nothing from it; the count matches that.)
        data = gzip.compress(b"x" * 1000) + b"junk" + os.urandom(3 * 1024 * 1024)
        self.assertLess(self.count_reads(data), 100)

    def test_the_cap_still_stops_a_buffered_read(self):
        # The member's own bytes pass the cap while the buffer fills for the
        # gzip reader: _OverCap comes through the buffer and the gzip reader,
        # not swallowed as invalid gzip data.
        data = with_long_name(gzip.compress(b"x" * 1000), 2 * 1024 * 1024)
        buf = io.BytesIO(zip_bytes([("a.json.gz", data)]))
        with zipfile.ZipFile(buf) as z:
            budget = ingest_pipeline._ExpansionBudget(CAP)
            with self.assertRaises(ingest_pipeline._OverCap):
                ingest_pipeline._member_problem(z, z.infolist()[0], budget, True)
        self.assertLessEqual(budget.used, CAP + ingest_pipeline._BLOCK, budget.used)


class EventLoopTests(unittest.TestCase):
    def test_zip_work_runs_off_the_event_loop(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        released = threading.Event()
        seen = {}

        def extract(dest, safe_name, queue, budget):
            seen["thread"] = threading.get_ident()
            # Only another coroutine on the loop can set this: if extraction
            # ran on the loop, it would wait out the timeout.
            seen["released"] = released.wait(5)

        async def other_request():
            await asyncio.sleep(0.05)
            released.set()

        async def main():
            seen["loop"] = threading.get_ident()
            admit = ingest_pipeline.start_ingest_job(
                [upload("a.zip", zip_bytes([("a.txt", "text")]))], "Coll", "fixed",
                200, 0, 0.85, 0)
            return await asyncio.gather(admit, other_request(), return_exceptions=True)

        with patch.object(settings, "upload_dir", temp.name), \
                patch.object(ingest_pipeline, "_extract_zip", extract):
            results = asyncio.run(main())
        # Nothing was queued by the stand-in, so admission ends with no files.
        self.assertIsInstance(results[0], ValueError)
        self.assertTrue(seen["released"], "the event loop was blocked during ZIP work")
        self.assertNotEqual(seen["thread"], seen["loop"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
