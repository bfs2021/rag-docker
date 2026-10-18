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


class CapTests(unittest.TestCase):
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
