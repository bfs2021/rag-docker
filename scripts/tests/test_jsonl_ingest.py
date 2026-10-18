"""JSON Lines (.jsonl, .jsonl.gz) is ingested one record per line (issue #291).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_jsonl_ingest.py

Weaviate is mocked; nothing reaches a backend.
"""
import gzip
import json
import logging
import os
import sys
import tempfile
import time
import tracemalloc
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import ingest_pipeline, sources, tuning
from services import weaviate_client as wc

LINE_LIMIT = ("is longer than 10,000,000 bytes, the most one line can hold; "
              "split the record across lines or files")


def too_large(line):
    return (f"converts to more than 10,000,000 characters of text by line {line}, "
            f"the per-file limit for every chunking strategy; split the file")


class JsonlTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def write(self, content, name="doc.jsonl"):
        path = self.dir / name
        data = content.encode("utf-8") if isinstance(content, str) else content
        if ingest_pipeline._is_jsonl_gz(name):
            data = gzip.compress(data, mtime=0)
        path.write_bytes(data)
        return path

    def parse(self, content, name="doc.jsonl"):
        return ingest_pipeline._parse_file(self.write(content, name))

    def records(self, content, name="doc.jsonl"):
        return [str(e) for e in self.parse(content, name)[1]]

    def assert_fails(self, content, message, name="doc.jsonl"):
        with self.assertRaises(ValueError) as caught:
            self.parse(content, name)
        self.assertEqual(str(caught.exception), message)


class ConversionTests(JsonlTestCase):
    # ── S3.1–S3.3 ────────────────────────────────────────────────────────────
    def test_each_line_is_one_record_joined_by_a_blank_line(self):
        text, elements = self.parse('{"title": "A", "meta": {"author": "Zoë"}}\n'
                                    '{"title": "B", "tags": ["x", "y"]}\n')
        self.assertEqual(text, "title: A\nmeta.author: Zoë\n\ntitle: B\ntags: x, y")
        self.assertEqual([type(e).__name__ for e in elements], ["Text", "Text"])
        self.assertEqual([str(e) for e in elements],
                         ["title: A\nmeta.author: Zoë", "title: B\ntags: x, y"])

    def test_key_order_numbers_and_literals_as_written(self):
        self.assertEqual(self.records('{"z": 1.50, "a": 1e3, "t": true, "n": null}'),
                         ["z: 1.50\na: 1e3\nt: true\nn: null"])

    def test_an_array_line_is_one_record_with_indexed_paths(self):
        self.assertEqual(self.records('[{"a": 1}, "loose", ["p", "q"]]\n["x", 2, false]\n'),
                         ["[0].a: 1\n[1]: loose\n[2]: p, q", "x, 2, false"])

    def test_a_scalar_line_is_its_text(self):
        self.assertEqual(self.records('"just text"\n42\nnull\n'), ["just text", "42", "null"])

    def test_lines_with_no_text_add_nothing(self):
        text, elements = self.parse('{}\n{"a": 1}\n[]\n""\n{"b": {}}\n{"c": 2}\n')
        self.assertEqual(text, "a: 1\n\nc: 2")
        self.assertEqual(len(elements), 2)

    def test_element_shaped_lines_are_not_rehydrated(self):
        line = json.dumps([{"type": "Title", "text": "Heading", "element_id": "1", "metadata": {}}])
        with patch("unstructured.partition.json.partition_json",
                   side_effect=AssertionError("partition_json called")):
            self.assertEqual(self.records(line + "\n"),
                             ["[0].type: Title\n[0].text: Heading\n[0].element_id: 1"])

    # ── S2.1, S2.3, S2.4, S3.5 ───────────────────────────────────────────────
    def test_crlf_bom_and_no_final_newline(self):
        self.assertEqual(self.records('﻿{"a": 1}\r\n{"b": 2}\r\n{"c": 3}'),
                         ["a: 1", "b: 2", "c: 3"])

    def test_trailing_blank_lines_are_accepted(self):
        self.assertEqual(self.records('{"a": 1}\n\n  \n\t\r\n'), ["a: 1"])

    def test_empty_and_blank_only_files_give_nothing(self):
        for content in ("", "\n\n", " \t\r\n", "﻿", "{}\n[]\n"):
            with self.subTest(content=content):
                self.assertEqual(self.parse(content), ("", []))

    def test_u2028_inside_a_string_stays_in_its_line(self):
        self.assertEqual(self.records('{"a": "one two"}\n{"b": 2}\n'),
                         ["a: one two", "b: 2"])

    def test_source_type(self):
        for name, wanted in (("x.jsonl", "jsonl"), ("X.JSONL", "jsonl"), ("x.jsonl.gz", "jsonl"),
                             ("X.JSONL.GZ", "jsonl"), ("x.json.gz", "json"), ("x.json", "json")):
            with self.subTest(name=name):
                self.assertEqual(ingest_pipeline.source_type_of(Path(name)), wanted)


class LineErrorTests(JsonlTestCase):
    # ── S2.2–S2.6 ────────────────────────────────────────────────────────────
    def test_a_blank_line_between_records_names_the_first_blank(self):
        self.assert_fails('{"a": 1}\n\n \n{"b": 2}\n', "line 2 is blank")

    def test_a_leading_blank_line(self):
        self.assert_fails('\n{"a": 1}\n', "line 1 is blank")

    def test_invalid_json_names_line_and_column(self):
        self.assert_fails('{"a": 1}\n{"b": }\n', "line 2 is not valid JSON: Expecting value at column 7")

    def test_bom_on_a_later_line_is_invalid_json(self):
        with self.assertRaisesRegex(ValueError, r"^line 2 is not valid JSON: "):
            self.parse('{"a": 1}\n﻿{"b": 2}\n')

    def test_nan_is_refused(self):
        self.assert_fails('{"a": NaN}\n', "line 1 is not valid JSON: NaN is not a JSON value")

    def test_deep_nesting(self):
        self.assert_fails("[" * 100000 + "]" * 100000 + "\n",
                          "line 1 is not valid JSON: it is nested too deeply to read")

    def test_non_utf8_names_the_line_and_byte(self):
        self.assert_fails(b'{"a": 1}\n{"b": "\xff"}\n',
                          "line 2 is not UTF-8 text (invalid start byte at byte 7)")

    def test_non_utf8_byte_offset_counts_the_bom_on_line_1(self):
        self.assert_fails(b'\xef\xbb\xbf{"b": "\xff"}\n',
                          "line 1 is not UTF-8 text (invalid start byte at byte 10)")

    def test_only_the_first_bad_line_is_reported(self):
        self.assert_fails('{"a": 1}\n{bad\n{"b": }\n\n{"c": 3}\n',
                          "line 2 is not valid JSON: Expecting property name enclosed in "
                          "double quotes at column 2")

    def test_nothing_after_a_bad_line_is_returned(self):
        with self.assertRaises(ValueError):
            self.parse('{"a": 1}\n{bad\n')

    # ── S2.2 ─────────────────────────────────────────────────────────────────
    def test_a_line_of_exactly_the_limit_parses(self):
        line = '"' + "a" * (ingest_pipeline.MAX_JSONL_LINE_BYTES - 2) + '"'
        self.assertEqual(len(line.encode()), 10_000_000)
        text, elements = self.parse(line + "\n\n")
        self.assertEqual((len(text), len(elements)), (10_000_000 - 2, 1))

    def test_a_line_one_byte_over_the_limit_fails(self):
        line = '"' + "a" * (ingest_pipeline.MAX_JSONL_LINE_BYTES - 1) + '"'
        self.assert_fails('{"a": 1}\n' + line + "\n", f"line 2 {LINE_LIMIT}")

    def test_a_last_line_over_the_limit_without_newline_fails(self):
        self.assert_fails(" " * 10_000_001, f"line 1 {LINE_LIMIT}")

    def test_a_blank_line_is_reported_before_a_later_long_line(self):
        self.assert_fails('{"a": 1}\n\n' + "x" * 10_000_001 + "\n", "line 2 is blank")


class BudgetTests(JsonlTestCase):
    # ── S3.4, S2.7 ───────────────────────────────────────────────────────────
    def test_text_exactly_at_the_limit_with_separators_parses(self):
        # Two records of 4,999,999 characters and the blank line between them.
        half = 4_999_999
        content = json.dumps("a" * half) + "\n" + json.dumps("b" * half) + "\n"
        text, _ = self.parse(content)
        self.assertEqual(len(text), 10_000_000)

    def test_one_character_over_names_the_line(self):
        content = json.dumps("a" * 4_999_999) + "\n" + json.dumps("b" * 5_000_000) + "\n"
        self.assert_fails(content, too_large(2))

    def test_a_dropped_record_costs_nothing_at_the_edge(self):
        content = (json.dumps("a" * 4_999_999) + "\n{}\n[]\n" + json.dumps("b" * 4_999_999) + "\n")
        self.assertEqual(len(self.parse(content)[0]), 10_000_000)

    def test_reading_stops_once_the_limit_is_passed(self):
        record = json.dumps({"title": "Remote work policy", "body": "x" * 80}) + "\n"
        path = self.write(record * 200_000)  # about 21 MB
        positions = []
        real = ingest_pipeline._jsonl_records

        def watch(stream):
            try:
                return real(stream)
            finally:
                positions.append(stream.tell())

        tracemalloc.start()
        started = time.monotonic()
        try:
            with patch.object(ingest_pipeline, "MAX_JSON_TEXT_CHARACTERS", 100_000), \
                    patch.object(ingest_pipeline, "_jsonl_records", watch), \
                    self.assertRaises(ValueError) as caught:
                ingest_pipeline._parse_file(path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Each record's text, and the blank line before every one after the first.
        text = len("title: Remote work policy\nbody: ") + 80
        line = 100_000 // (text + 2) + 1
        self.assertEqual(str(caught.exception),
                         f"converts to more than 100,000 characters of text by line {line}, the "
                         f"per-file limit for every chunking strategy; split the file")
        # Read up to the line that passed the limit, not the rest of the file.
        self.assertLessEqual(positions[0], line * len(record))
        self.assertLess(time.monotonic() - started, 10)
        self.assertLess(peak, 5 * 1024 * 1024, f"peak {peak} bytes")


class GzipTests(JsonlTestCase):
    # ── S4 ───────────────────────────────────────────────────────────────────
    def assert_no_temp_left(self, keep):
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted(keep))

    def test_jsonl_gz_parses_like_jsonl(self):
        content = '{"a": 1}\n{"b": "Zoë"}\n'
        self.assertEqual(self.parse(content, "doc.jsonl.gz"), self.parse(content, "doc.jsonl"))
        self.assertEqual(self.records(content, "DOC.JSONL.GZ"), ["a: 1", "b: Zoë"])
        self.assert_no_temp_left(["DOC.JSONL.GZ", "doc.jsonl", "doc.jsonl.gz"])

    def test_line_errors_are_prefixed(self):
        self.assert_fails('{"a": 1}\n{"b": }\n',
                          "was decompressed, but line 2 is not valid JSON: Expecting value at column 7",
                          "doc.jsonl.gz")
        self.assert_fails('{"a": 1}\n\n{"b": 2}\n', "was decompressed, but line 2 is blank", "doc.jsonl.gz")

    def test_budget_error_is_prefixed(self):
        content = json.dumps("a" * 4_999_999) + "\n" + json.dumps("b" * 5_000_000) + "\n"
        self.assert_fails(content, "was decompressed, but it " + too_large(2), "doc.jsonl.gz")

    def test_corrupt_gzip(self):
        path = self.dir / "doc.jsonl.gz"
        path.write_bytes(b"this is not gzip data")
        with self.assertRaisesRegex(ValueError, "^is not a valid gzip file; it may be truncated or corrupt"):
            ingest_pipeline._parse_file(path)
        self.assert_no_temp_left(["doc.jsonl.gz"])

    def test_truncated_gzip(self):
        data = gzip.compress(('{"a": "' + "x" * 50 + '"}\n').encode() * 200)
        path = self.dir / "doc.jsonl.gz"
        path.write_bytes(data[:len(data) // 2])
        with self.assertRaisesRegex(ValueError, "^is not a valid gzip file; it may be truncated or corrupt"):
            ingest_pipeline._parse_file(path)

    def test_a_bad_line_before_a_truncated_end_is_reported_as_the_line(self):
        data = gzip.compress(b'{"a": 1}\n{bad\n' + b'{"a": 1}\n' * 2000)
        path = self.dir / "doc.jsonl.gz"
        path.write_bytes(data[:len(data) - 20])
        with self.assertRaisesRegex(ValueError, "^was decompressed, but line 2 is not valid JSON"):
            ingest_pipeline._parse_file(path)

    def test_decompressed_size_is_bounded(self):
        path = self.write('{"a": 1}\n' * 500, "doc.jsonl.gz")
        with patch.object(ingest_pipeline, "MAX_GZIP_DECOMPRESSED_BYTES", 1000), \
                self.assertRaises(ValueError) as caught:
            ingest_pipeline._parse_file(path)
        self.assertEqual(str(caught.exception),
                         "decompresses to more than 0 MB, the limit for one compressed file; "
                         "nothing from it was stored")
        self.assert_no_temp_left(["doc.jsonl.gz"])

    def test_the_gzip_limit_message_is_unchanged(self):
        self.assertEqual(str(ingest_pipeline._gzip_too_large()),
                         "decompresses to more than 512 MB, the limit for one compressed file; "
                         "nothing from it was stored")


class JobTests(JsonlTestCase):
    # ── S2.8, S5.1 ───────────────────────────────────────────────────────────
    def test_a_bad_file_stores_nothing_and_the_good_one_is_stored(self):
        tmp = Path(tempfile.mkdtemp())
        good = tmp / "good.jsonl.gz"; good.write_bytes(gzip.compress(b'{"a": 1}\n{"b": 2}\n'))
        bad = tmp / "bad.jsonl"; bad.write_bytes(b'{"a": 1}\n{"b": }\n')
        labels = {good: "u.zip:good.jsonl.gz", bad: "bad.jsonl"}
        job_id = "t" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": 2, "files_completed": 0,
            "files_failed": 0, "chunks_stored": 0, "errors": [], "skipped": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        stored, retained = [], []
        with patch.object(wc, "_insert_chunks_sync", lambda _c, chunks: stored.extend(chunks)), \
                patch.object(wc, "_source_has_chunks_sync", return_value=False), \
                patch.object(ingest_pipeline.sources, "store",
                             lambda _c, name, data, *a, **k: retained.append((name, data))), \
                self.assertLogs("services.ingest_pipeline", "WARNING"):
            ingest_pipeline._process_job_sync(job_id, [good, bad], tmp, "Coll",
                                              "fixed", 1000, 0, 0.85, 0, labels)
        job = ingest_pipeline._jobs[job_id]
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["errors"], ["bad.jsonl: line 2 is not valid JSON: Expecting value at column 7"])
        self.assertEqual([(c["source_file"], c["source_type"], c["content"]) for c in stored],
                         [("good.jsonl.gz", "jsonl", "a: 1\n\nb: 2")])
        self.assertEqual([name for name, _ in retained], ["good.jsonl.gz"])
        self.assertTrue(retained[0][1].startswith(b"\x1f\x8b"))  # the file as uploaded


class RechunkTests(unittest.TestCase):
    # ── S5.3 ─────────────────────────────────────────────────────────────────
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for change in (patch.object(settings, "sources_dir", str(Path(temp.name) / "s")),
                       patch.object(settings, "upload_dir", temp.name)):
            change.start(); self.addCleanup(change.stop)

    def test_a_retained_jsonl_gz_rebuilds_the_same_text(self):
        data = gzip.compress('{"title": "A", "meta": {"author": "Zoë"}}\n{"title": "B"}\n'.encode())
        sources.store("Coll", "corpus/bills.jsonl.gz", data, replace=True)
        sources.store("Coll", "plain.jsonl", b'{"k": "v"}\n', replace=True)
        objects = [SimpleNamespace(properties={"source_file": n, "chunk_index": 0})
                   for n in ("corpus/bills.jsonl.gz", "plain.jsonl")]
        coll = SimpleNamespace(iterator=lambda **_: iter(objects))
        with patch.object(wc, "get_client", return_value=SimpleNamespace(
                collections=SimpleNamespace(get=lambda name: coll))):
            chunks = tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual(sorted((c["source_file"], c["source_type"], c["content"]) for c in chunks), [
            ("corpus/bills.jsonl.gz", "jsonl", "title: A\nmeta.author: Zoë\n\ntitle: B"),
            ("plain.jsonl", "jsonl", "k: v")])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
