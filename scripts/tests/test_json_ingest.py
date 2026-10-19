"""Ordinary JSON is ingested as readable `key: value` text (issue #295), with
its input, records and text bounded (#300).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_json_ingest.py

Weaviate and source retention are mocked; nothing reaches a backend.
"""
import gzip
import json
import logging
import os
import re
import sys
import tempfile
import time
import tracemalloc
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from services import ingest_pipeline
from services import weaviate_client as wc
from services.chunker import chunk

TOO_LARGE = ("converts to more than 10,000,000 characters of text, "
             "the per-file limit for every chunking strategy; split the file")
TOO_MANY = "has more than 1,000,000 records, the most one file can hold; split the file"
TOO_BIG = ("is larger than 30,000,000 bytes, the most one JSON file can hold; "
           "split the file or convert it to JSON Lines")
GZ_TOO_BIG = ("decompresses to more than 30,000,000 bytes, the most one JSON file can hold; "
              "split the file or convert it to JSON Lines")
LIMIT_BYTES = 30_000_000


def padded(doc, size, prefix=b""):
    """doc followed by spaces (still valid JSON), `size` bytes with prefix."""
    body = prefix + doc.encode("utf-8")
    return body + b" " * (size - len(body))


def traced_peak(func, *args):
    """func(*args)'s traced peak in bytes, and the exception it raised (or None)."""
    tracemalloc.start()
    try:
        try:
            func(*args)
            raised = None
        except Exception as exc:  # noqa: BLE001 - returned for the caller to check
            raised = exc
        return tracemalloc.get_traced_memory()[1], raised
    finally:
        tracemalloc.stop()


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

    def test_element_schema_with_byte_order_mark_is_rehydrated(self):
        doc = "\ufeff" + json.dumps([{"type": "Title", "text": "Hello"}])
        text, elements = self.parse(doc.encode("utf-8"))
        self.assertEqual(([type(e).__name__ for e in elements], text), (["Title"], "Hello"))

    def test_element_shape_check_agrees_with_partition_json(self):
        # A number is not element text for partition_json, so this is ordinary JSON.
        text, elements = self.parse('[{"type": "Title", "text": 5}]')
        self.assertEqual(text, "type: Title\ntext: 5")
        self.assertEqual(type(elements[0]).__name__, "Text")

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

    def test_utf8_byte_order_mark_is_accepted(self):
        self.assertEqual(self.text_of('\ufeff{"name": "Zoë"}'.encode("utf-8")), "name: Zoë")

    def test_invalid_utf8(self):
        self.assert_invalid(b'{"a": "\xff"}', r"^is not valid JSON: it is not UTF-8 text \(.+ at byte 7\)$")

    def test_too_deeply_nested(self):
        # Refused by the parser...
        self.assert_invalid("[" * 100000 + "]" * 100000, r"^is not valid JSON: it is nested too deeply to read$")
        # ...or, if the parser accepts a depth the converter can't walk, by the converter.
        with patch.object(ingest_pipeline, "_json_lines", side_effect=RecursionError):
            self.assert_invalid('{"a": 1}', r"^is not valid JSON: it is nested too deeply to read$")

    # ── S3.5: converted text is bounded (PR #299 review) ─────────────────────
    def test_long_key_over_many_values_fails_fast_with_bounded_memory(self):
        # The review's shape: ~512 KB of JSON whose lines would total ~17 GB.
        doc = '{"' + "a" * 262144 + '": [' + ",".join(["[0]"] * 65536) + "]}"
        path = self.write(doc)
        tracemalloc.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ValueError) as caught:
                ingest_pipeline._parse_file(path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(str(caught.exception), TOO_LARGE)
        self.assertLess(time.monotonic() - started, 10)
        self.assertLess(peak, 100 * 1024 * 1024, f"peak {peak} bytes")

    def test_deep_long_keys_with_empty_values_build_no_long_paths(self):
        # Many empty values under a long key write nothing and stay cheap.
        doc = '{"' + "a" * 262144 + '": {' + ",".join(f'"k{i}": {{}}' for i in range(65536)) + "}}"
        started = time.monotonic()
        self.assertEqual(self.parse(doc), ("", []))
        self.assertLess(time.monotonic() - started, 10)
        # Long keys nested 400 deep over an empty value (#300): a path built at every
        # level, as cccb20e did, holds about 160 MB of path text at the deepest one.
        key = "a" * 2000
        deep = "".join(f'{{"{key}": ' for _ in range(400)) + "{}" + "}" * 400
        path = self.write(deep)
        peak, raised = traced_peak(lambda: self.assertEqual(ingest_pipeline._parse_file(path), ("", [])))
        self.assertIsNone(raised)
        self.assertLess(peak, 40 * 1024 * 1024, f"peak {peak} bytes")

    def test_scalar_array_over_the_bound_fails_before_joining(self):
        limit = ingest_pipeline.MAX_JSON_TEXT_CHARACTERS
        self.assert_invalid(json.dumps({"k": ["x" * 1000] * (limit // 1000 + 1)}), "^" + re.escape(TOO_LARGE) + "$")
        # Fifty references to one 1 MB string: joined, they would be a 50 MB line (#300).
        peak, raised = traced_peak(ingest_pipeline._json_records, {"k": ["x" * 1_000_000] * 50})
        self.assertIsInstance(raised, ingest_pipeline._JsonTooLarge)
        self.assertLess(peak, 10 * 1024 * 1024, f"peak {peak} bytes")

    def test_text_just_under_the_bound_converts_and_just_over_fails(self):
        limit = ingest_pipeline.MAX_JSON_TEXT_CHARACTERS
        # Lines "k00000: <991 x>" are 999 characters, 1,000 with their newline.
        count = limit // 1000
        doc = {f"k{i:05d}": "x" * 991 for i in range(count)}
        text, elements = self.parse(json.dumps(doc))
        self.assertEqual(sum(len(str(e)) for e in elements), limit - 1)
        # The chunker accepts this much text, so the bound refused nothing it could store.
        self.assertTrue(chunk(text=text, strategy="context_aware", chunk_size=6000, chunk_overlap_size=0,
                              min_chunk_size=0, elements=elements))
        doc[f"k{count:05d}"] = ""
        self.assert_invalid(json.dumps(doc), "^" + re.escape(TOO_LARGE) + "$")

    def test_bound_is_shared_by_the_records_of_a_top_level_array(self):
        # Reviewer test (PR #299), adjusted for #300's separator charge: one budget for all
        # records and the blank lines between them, exactly at the limit, and a dropped
        # (whitespace-only) record gives its characters back.
        limit = ingest_pipeline.MAX_JSON_TEXT_CHARACTERS
        count = (limit + 2) // 996  # each record is "k: <991 x>", 994 characters, 996 with its blank line
        rest = limit - (count * 996 - 2) - 2  # the last record's characters, after its blank line
        records = [" "] + [{"k": "x" * 991}] * count + [{"k": "x" * (rest - 3)}]
        text, elements = self.parse(json.dumps(records))
        self.assertEqual(len(elements), count + 1)
        self.assertEqual(len(text), limit)
        self.assertEqual(sum(len(str(e)) for e in elements) + 2 * count, limit)
        records.append({"k": ""})
        self.assert_invalid(json.dumps(records), "^" + re.escape(TOO_LARGE) + "$")

    # ── #300: records, input size, element-schema parse ──────────────────────
    def test_record_cap_counts_only_records_with_text(self):
        with patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 3):
            text, elements = self.parse('[{"a": 1}, {}, ["b"], [], " ", {"c": 3}, {}]')
            self.assertEqual([str(e) for e in elements], ["a: 1", "b", "c: 3"])
            self.assertEqual(text, "a: 1\n\nb\n\nc: 3")
            self.assert_invalid('[{"a": 1}, {}, ["b"], {"c": 3}, {"d": 4}]',
                                r"^has more than 3 records, the most one file can hold; split the file$")

    def test_many_tiny_records_fail_fast_with_bounded_memory(self):
        self.assertEqual(ingest_pipeline.MAX_JSON_RECORDS, 1_000_000)
        path = self.write("[" + ",".join(["[1]"] * 1_000_001) + "]")
        from unstructured.documents import elements as unstructured_elements
        built = []
        real_text = unstructured_elements.Text
        started = time.monotonic()
        with patch.object(unstructured_elements, "Text",
                          side_effect=lambda **kw: built.append(1) or real_text(**kw)):
            peak, raised = traced_peak(ingest_pipeline._parse_file, path)
        self.assertIsInstance(raised, ValueError)
        self.assertEqual(str(raised), TOO_MANY)
        self.assertEqual(built, [])
        self.assertLess(time.monotonic() - started, 30)
        self.assertLess(peak, 300 * 1024 * 1024, f"peak {peak} bytes")

    def test_record_cap_json_gz_member_reason(self):
        with patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 2), \
                self.assertRaises(ValueError) as caught:
            self.parse(gzip.compress(b'[{"a": 1}, {"a": 2}, {"a": 3}]'), "doc.json.gz")
        self.assertEqual(str(caught.exception), "was decompressed, but it has more than 2 records, "
                                                "the most one file can hold; split the file")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["doc.json.gz"])

    def test_element_schema_record_cap_at_and_past_the_edge(self):
        # #321: an element file's items count, before partition_json builds any.
        from unstructured.partition import json as partition_json_module
        items = [{"type": "NarrativeText", "text": f"t{i}"} for i in range(4)]
        with patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 3), \
                patch.object(partition_json_module, "partition_json",
                             wraps=partition_json_module.partition_json) as partition:
            text, elements = self.parse(json.dumps(items[:3]))
            self.assertEqual((len(elements), text), (3, "t0\nt1\nt2"))
            self.assertEqual(partition.call_count, 1)
            with self.assertRaises(ValueError) as caught:
                self.parse(json.dumps(items))
            self.assertEqual(str(caught.exception),
                             "has more than 3 records, the most one file can hold; split the file")
            for path in self.dir.iterdir():
                path.unlink()
            with self.assertRaises(ValueError) as caught:
                self.parse(gzip.compress(json.dumps(items).encode()), "doc.json.gz")
            self.assertEqual(str(caught.exception), "was decompressed, but it has more than 3 records, "
                                                    "the most one file can hold; split the file")
            self.assertEqual(partition.call_count, 1)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["doc.json.gz"])

    def test_element_schema_items_with_no_text_count_toward_the_cap(self):
        # Reviewer test (#321): every element item counts, empty text included,
        # unlike ordinary records with no text.
        items = [{"type": "NarrativeText", "text": ""} for _ in range(3)]
        with patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 2):
            with self.assertRaises(ValueError) as caught:
                self.parse(json.dumps(items))
        self.assertEqual(str(caught.exception),
                         "has more than 2 records, the most one file can hold; split the file")

    def test_input_size_limit(self):
        self.assertEqual(ingest_pipeline.MAX_JSON_INPUT_BYTES, LIMIT_BYTES)
        self.assertEqual(self.parse(padded('{"k": "v"}', LIMIT_BYTES))[0], "k: v")
        cases = {
            "ordinary": padded('{"k": "v"}', LIMIT_BYTES + 1),
            "element schema": padded('[{"type": "Title", "text": "Hello"}]', LIMIT_BYTES + 1),
            "byte-order mark": padded('{"k": "v"}', LIMIT_BYTES + 1, prefix=b"\xef\xbb\xbf"),
            "not even JSON": b"\xff" * (LIMIT_BYTES + 1),
        }
        for label, content in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError) as caught:
                    self.parse(content)
                self.assertEqual(str(caught.exception), TOO_BIG)

    def test_json_gz_member_stops_decompressing_at_the_input_limit(self):
        at_limit = gzip.compress(padded('{"k": "v"}', LIMIT_BYTES))
        self.assertEqual(self.parse(at_limit, "doc.json.gz")[0], "k: v")
        for size in (LIMIT_BYTES + 1, 200_000_000):
            with self.subTest(size=size):
                read = []
                real_open = gzip.open

                def counting_open(*args, **kwargs):
                    stream = real_open(*args, **kwargs)
                    real_read = stream.read
                    stream.read = lambda n=-1: (lambda b: read.append(len(b)) or b)(real_read(n))
                    return stream

                content = gzip.compress(padded('{"k": "v"}', size))
                with patch.object(ingest_pipeline.gzip, "open", counting_open), \
                        self.assertRaises(ValueError) as caught:
                    self.parse(content, "doc.json.gz")
                self.assertEqual(str(caught.exception), GZ_TOO_BIG)
                self.assertLessEqual(sum(read), LIMIT_BYTES + 1024 * 1024)
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["doc.json.gz"])

    def test_element_schema_first_parse_is_freed_before_partition_json(self):
        from unstructured.partition import json as partition_json_module
        raw = json.dumps([{"type": "Title", "text": f"t{i}"} for i in range(50_000)])
        path = self.write(raw)
        held = []

        def fake_partition_json(**kwargs):
            held.append(tracemalloc.get_traced_memory()[0])
            return []

        with patch.object(partition_json_module, "partition_json", fake_partition_json):
            _, raised = traced_peak(ingest_pipeline._parse_file, path)
        self.assertIsNone(raised)
        # The text alone, not the text and its parsed value (several times its size).
        self.assertEqual(len(held), 1)
        self.assertLess(held[0], 2 * len(raw), f"{held[0]} bytes held for {len(raw)} of text")

    def test_bound_matches_the_chunker_limit(self):
        from services import chunker
        self.assertEqual(ingest_pipeline.MAX_JSON_TEXT_CHARACTERS, chunker.MAX_TEXT_CHARACTERS)

    def test_over_the_bound_json_gz_member_reason(self):
        doc = ('{"' + "a" * 262144 + '": [' + ",".join(["[0]"] * 65536) + "]}").encode()
        with self.assertRaises(ValueError) as caught:
            self.parse(gzip.compress(doc), "doc.json.gz")
        self.assertEqual(str(caught.exception), "was decompressed, but it " + TOO_LARGE)

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
                patch.object(wc, "_source_has_chunks_sync", return_value=False), \
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

    def test_over_limit_files_fail_only_themselves(self):
        with patch.object(ingest_pipeline, "MAX_JSON_INPUT_BYTES", 100), \
                patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 2):
            job, stored = self.run_job([("big.json", padded('{"a": 1}', 101)),
                                        ("many.json", b'[{"a": 1}, {"a": 2}, {"a": 3}]'),
                                        ("good.json", b'{"a": 1}')], "fixed")
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["errors"], [
            "big.json: is larger than 100 bytes, the most one JSON file can hold; "
            "split the file or convert it to JSON Lines",
            "many.json: has more than 2 records, the most one file can hold; split the file"])
        self.assertEqual([c["content"] for c in stored], ["a: 1"])

    def test_over_cap_element_schema_file_stores_nothing(self):
        items = [{"type": "NarrativeText", "text": t} for t in ("x", "y", "z")]
        with patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 2):
            job, stored = self.run_job([("elements.json", json.dumps(items).encode()),
                                        ("good.json", b'{"a": 1}')], "fixed")
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["errors"], [
            "elements.json: has more than 2 records, the most one file can hold; split the file"])
        self.assertEqual([c["content"] for c in stored], ["a: 1"])

    def test_invalid_json_fails_only_that_file(self):
        job, stored = self.run_job([("bad.json", b'{"a": }'), ("good.json", b'{"a": 1}')], "fixed")
        self.assertEqual(job["status"], "partial")
        self.assertEqual(job["errors"], ["bad.json: is not valid JSON: Expecting value at line 1, column 7"])
        self.assertEqual([c["content"] for c in stored], ["a: 1"])


class RechunkElementSchemaTests(unittest.TestCase):
    """Reviewer test (#321): rechunk parses an element file the same way, cap included."""

    def test_retained_element_file_at_and_past_the_cap(self):
        from config import settings
        from services import tuning
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        digest = "e" * 64
        blob = root / digest
        index = {"documents": {digest: {"filenames": ["elements.json"]}}}
        items = [{"type": "NarrativeText", "text": f"t{i}"} for i in range(4)]
        with patch.object(settings, "upload_dir", str(root)), \
                patch.object(ingest_pipeline, "MAX_JSON_RECORDS", 3), \
                patch.object(tuning.sources, "load_index", lambda _c: index), \
                patch.object(tuning.sources, "blob_path", lambda _c, _d: blob), \
                patch.object(tuning, "_source_coverage", lambda _c, _d: ([], {"elements.json"})):
            blob.write_text(json.dumps(items[:3]))
            out = tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
            self.assertEqual([c["content"] for c in out], ["t0\nt1\nt2"])
            blob.write_text(json.dumps(items))
            with self.assertRaises(ValueError) as caught:
                tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual(str(caught.exception),
                         "has more than 3 records, the most one file can hold; split the file")
        self.assertEqual(sorted(p.name for p in root.iterdir()), [digest])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
