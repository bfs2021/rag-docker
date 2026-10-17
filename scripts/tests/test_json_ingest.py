"""Ordinary JSON is ingested as readable `key: value` text (issue #295).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_json_ingest.py

Weaviate and source retention are mocked; nothing reaches a backend.
"""
import gzip
import json
import logging
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from services import ingest_pipeline
from services import weaviate_client as wc
from services.chunker import chunk


class JsonFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def write(self, content, name="doc.json"):
        path = self.dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def parse(self, content, name="doc.json"):
        return ingest_pipeline._parse_file(self.write(content, name))

    def text_of(self, content):
        return self.parse(content)[0]

    def assert_invalid(self, content, pattern):
        with self.assertRaisesRegex(ValueError, pattern):
            self.parse(content)

    # ── S2.1, S2.2, S2.8: lines, paths, scalars ───────────────────────────────
    def test_issue_sample(self):
        text, elements = self.parse('{"title": "Test", "body": "Some text", "meta": {"author": "A"}}')
        self.assertEqual(text, "title: Test\nbody: Some text\nmeta.author: A")
        self.assertEqual(len(elements), 1)
        self.assertEqual(type(elements[0]).__name__, "Text")
        self.assertEqual(str(elements[0]), text)

    def test_keys_keep_file_order(self):
        self.assertEqual(self.text_of('{"zeta": 1, "alpha": 2, "mid": {"b": 3, "a": 4}}'),
                         "zeta: 1\nalpha: 2\nmid.b: 3\nmid.a: 4")

    def test_non_ascii_is_kept(self):
        text = self.text_of(json.dumps({"name": "Zoë", "city": "Zürich", "note": "日本"}))
        self.assertEqual(text, "name: Zoë\ncity: Zürich\nnote: 日本")
        self.assertNotIn("\\u", text)

    def test_strings_are_written_as_is(self):
        self.assertEqual(self.text_of(json.dumps({"q": 'say "hi"', "multi": "one\ntwo", "empty": ""})),
                         'q: say "hi"\nmulti: one\ntwo\nempty: ')

    def test_numbers_as_written_and_literals(self):
        self.assertEqual(
            self.text_of('{"a": 1.50, "b": 1e3, "c": -0, "d": 123456789012345678901234567890,'
                         ' "t": true, "f": false, "n": null}'),
            "a: 1.50\nb: 1e3\nc: -0\nd: 123456789012345678901234567890\nt: true\nf: false\nn: null")

    # ── S2.3, S2.4: arrays and empty values inside a value ────────────────────
    def test_array_of_scalars_is_one_line(self):
        self.assertEqual(self.text_of('{"tags": ["a", "b", "c"], "mix": [1, true, null, "x"]}'),
                         "tags: a, b, c\nmix: 1, true, null, x")

    def test_array_of_objects_uses_indexed_paths(self):
        self.assertEqual(self.text_of('{"items": [{"name": "x", "sub": {"k": 1}}, {"name": "y"}]}'),
                         "items[0].name: x\nitems[0].sub.k: 1\nitems[1].name: y")

    def test_mixed_array_items(self):
        self.assertEqual(self.text_of('{"items": [{"name": "x"}, "y", ["p", "q"], [{"k": "v"}]]}'),
                         "items[0].name: x\nitems[1]: y\nitems[2]: p, q\nitems[3][0].k: v")

    def test_nested_empty_values_write_nothing(self):
        self.assertEqual(self.text_of('{"a": {}, "b": [], "c": 1, "d": {"e": {}}, "f": [{}]}'), "c: 1")

    # ── S2.5, S2.6, S2.7, S3.1: top level ─────────────────────────────────────
    def test_top_level_array_of_objects_is_one_element_per_object(self):
        text, elements = self.parse('[{"policy": "p0", "detail": "Remote."}, {"policy": "p1", "detail": "Expenses."}]')
        self.assertEqual([str(e) for e in elements],
                         ["policy: p0\ndetail: Remote.", "policy: p1\ndetail: Expenses."])
        self.assertEqual(text, "policy: p0\ndetail: Remote.\n\npolicy: p1\ndetail: Expenses.")

    def test_top_level_array_of_scalars_is_one_line(self):
        text, elements = self.parse('["a", 2, false]')
        self.assertEqual((text, len(elements)), ("a, 2, false", 1))

    def test_top_level_mixed_array_is_one_record_per_item(self):
        text, elements = self.parse('[{"a": 1}, "loose", ["p", "q"], [{"a": 1}, "z"], {}]')
        self.assertEqual([str(e) for e in elements],
                         ["a: 1", "loose", "p, q", "[0].a: 1\n[1]: z"])
        self.assertEqual(text, "a: 1\n\nloose\n\np, q\n\n[0].a: 1\n[1]: z")

    def test_top_level_scalar(self):
        self.assertEqual(self.parse('"just a string"')[0], "just a string")
        self.assertEqual(self.parse("42")[0], "42")
        self.assertEqual(self.parse("null")[0], "null")

    def test_empty_values_and_files_give_nothing(self):
        for content in ("{}", "[]", "[{}, {}]", '{"a": {}, "b": []}', "", "  \n\t ", '""'):
            with self.subTest(content=content):
                self.assertEqual(self.parse(content), ("", []))

    # ── S1.2, S3.4: element schema ────────────────────────────────────────────
    def test_element_schema_is_rehydrated(self):
        text, elements = self.parse(json.dumps([{"type": "Title", "text": "Hello"},
                                                {"type": "NarrativeText", "text": "World body."}]))
        self.assertEqual([type(e).__name__ for e in elements], ["Title", "NarrativeText"])
        self.assertEqual(text, "Hello\nWorld body.")

    def test_partly_element_shaped_array_is_ordinary_json(self):
        text = self.text_of(json.dumps([{"type": "Title", "text": "Hello"}, {"name": "x"}]))
        self.assertEqual(text, "type: Title\ntext: Hello\n\nname: x")

    def test_corrupt_element_schema_keeps_partition_json_error(self):
        self.assert_invalid(json.dumps([{"type": "Title", "text": "x", "metadata": {"coordinates": "bad"}}]),
                            "could not be reconstructed")

    # ── S3.2: invalid JSON ────────────────────────────────────────────────────
    def test_syntax_error_names_the_position(self):
        self.assert_invalid('{"a": 1,\n "b" 2}', r"^is not valid JSON: Expecting ':' delimiter at line 2, column 6$")

    def test_non_standard_constants_are_rejected(self):
        for content in ("NaN", '{"a": Infinity}', "[-Infinity]"):
            with self.subTest(content=content):
                self.assert_invalid(content, r"^is not valid JSON: -?(NaN|Infinity) is not a JSON value$")

    def test_invalid_utf8(self):
        self.assert_invalid(b'{"a": "\xff"}', r"^is not valid JSON: it is not UTF-8 text \(.+ at byte 7\)$")

    def test_too_deeply_nested(self):
        # Refused by the parser...
        self.assert_invalid("[" * 100000 + "]" * 100000, r"^is not valid JSON: it is nested too deeply to read$")
        # ...or, if the parser accepts a depth the converter can't walk, by the converter.
        with patch.object(ingest_pipeline, "_json_lines", side_effect=RecursionError):
            self.assert_invalid('{"a": 1}', r"^is not valid JSON: it is nested too deeply to read$")

    # ── S1.1, S3.3: .json.gz members ──────────────────────────────────────────
    def test_json_gz_member_is_converted_the_same_way(self):
        doc = '{"title": "Test", "meta": {"author": "Zoë"}}'.encode()
        text, elements = self.parse(gzip.compress(doc), "doc.json.gz")
        self.assertEqual(text, "title: Test\nmeta.author: Zoë")
        self.assertEqual(len(elements), 1)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["doc.json.gz"])

    def test_invalid_json_gz_member_reason(self):
        with self.assertRaises(ValueError) as caught:
            self.parse(gzip.compress(b"{not json"), "doc.json.gz")
        self.assertEqual(str(caught.exception),
                         "was decompressed, but its JSON could not be parsed: "
                         "Expecting property name enclosed in double quotes at line 1, column 2")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["doc.json.gz"])

    def test_empty_json_gz_member_gives_nothing(self):
        self.assertEqual(self.parse(gzip.compress(b"[]"), "doc.json.gz"), ("", []))

    # ── S2.7: chunkers see the records ────────────────────────────────────────
    def test_language_and_context_aware_split_between_records(self):
        records = [{"id": f"r{i}", "detail": "word " * 12} for i in range(2)]
        text, elements = self.parse(json.dumps(records))
        first, second = (str(e) for e in elements)
        size = len(first) + 10
        for strategy in ("language", "context_aware"):
            with self.subTest(strategy=strategy):
                chunks = chunk(text=text, strategy=strategy, chunk_size=size, chunk_overlap_size=0,
                               min_chunk_size=0, elements=elements if strategy == "context_aware" else None)
                self.assertEqual([c.strip() for c in chunks], [first.strip(), second.strip()])


class JsonJobTests(unittest.TestCase):
    def run_job(self, files, strategy):
        tmp = Path(tempfile.mkdtemp())
        paths = []
        for name, content in files:
            path = tmp / name
            path.write_bytes(content)
            paths.append(path)
        job_id = "j" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": len(paths), "files_completed": 0,
            "files_failed": 0, "chunks_stored": 0, "errors": [], "skipped": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        stored = []
        with patch.object(wc, "_insert_chunks_sync", lambda _c, chunks: stored.extend(chunks)), \
                patch.object(ingest_pipeline.sources, "store"):
            ingest_pipeline._process_job_sync(job_id, paths, tmp, "Coll", strategy, 1000, 0, 0.85, 0)
        return ingest_pipeline._jobs[job_id], stored

    def test_job_stores_converted_text(self):
        for strategy in ("fixed", "context_aware"):
            with self.subTest(strategy=strategy):
                job, stored = self.run_job(
                    [("sample.json", '{"title": "Test", "body": "Some text", "meta": {"author": "Zoë"}}'.encode()),
                     ("empty.json", b"[]")], strategy)
                self.assertEqual((job["status"], job["files_completed"], job["errors"]), ("completed", 2, []))
                self.assertEqual([c["content"] for c in stored], ["title: Test\nbody: Some text\nmeta.author: Zoë"])
                self.assertEqual({c["source_type"] for c in stored}, {"json"})
                self.assertEqual(job["chunks_stored"], 1)

    def test_invalid_json_fails_only_that_file(self):
        job, stored = self.run_job([("bad.json", b'{"a": }'), ("good.json", b'{"a": 1}')], "fixed")
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["errors"], ["bad.json: is not valid JSON: Expecting value at line 1, column 7"])
        self.assertEqual([c["content"] for c in stored], ["a: 1"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
