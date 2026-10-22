"""Gold-standard save and download touch only export files (#370, #402).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_goldstandard_exports.py

Each test uses its own temporary UPLOAD_DIR, a synthetic session and in-process
HTTP without startup sweeps. Nothing reaches Weaviate or Ollama.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from routers import goldstandard as route
from services import goldstandard as gs

SID = "gs_370abcde"
REGISTRY = b'{"Owned": {"created_at": "2026-10-21T00:00:00+00:00"}}'
METRICS = b'{"latency_ms": 1}\n'
UI_DEFAULT = "Documents_20261020123456..json"
REFUSED = [
    "metrics.jsonl", ".", "..", "goldstandard_sessions", "report.txt", "name",
    "../x.json", "a/b.json", "a\\b.json", "/etc/x.json", "x" * 124 + ".json",
    "caf\u00e9.json", "a\x00.json", "a\n.json", "name.json ",
]


def session(collection="Owned"):
    pair = {"pair_id": "p0", "question": "Inert question", "answer": "Inert answer",
            "contexts": ["Inert context"], "ground_truth": "Inert truth",
            "source_file": "inert.txt", "chunk_index": 0, "status": "approved"}
    return {"session_id": SID, "collection": collection, "status": "completed",
            "pairs_total": 1, "pairs_completed": 1, "pairs": [pair]}


class ExportFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        upload = patch.object(settings, "upload_dir", self.temp.name)
        upload.start()
        self.addCleanup(upload.stop)
        original = gs._sessions
        gs._sessions = {}
        self.addCleanup(setattr, gs, "_sessions", original)
        (self.root / "collection_registry.json").write_bytes(REGISTRY)
        (self.root / "metrics.jsonl").write_bytes(METRICS)
        gs.store_session(session())          # also creates goldstandard_sessions/
        self.exports = self.root / gs.EXPORT_DIRNAME
        self.client = TestClient(app)        # no startup sweeps

    def snapshot(self):
        """Every entry under UPLOAD_DIR: file bytes, link targets, directories."""
        out = {}
        for path in sorted(self.root.rglob("*")):
            key = str(path.relative_to(self.root))
            if path.is_symlink():
                out[key] = ("link", os.readlink(path))
            elif path.is_dir():
                out[key] = ("dir",)
            else:
                out[key] = ("file", path.read_bytes())
        return out

    def save(self, filename):
        return self.client.post("/goldstandard/save", json={"session_id": SID, "filename": filename})

    def assert_invalid(self, response):
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "INVALID_PATH")

    def assert_folder_invalid(self, response):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "EXPORTS_FOLDER_INVALID")

    # ---- save -------------------------------------------------------------

    def test_refused_names_get_400_and_touch_nothing(self):
        for name in REFUSED:
            with self.subTest(name=name):
                before = self.snapshot()
                self.assert_invalid(self.save(name))
                self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.exports.exists())
        self.assertEqual((self.root / "metrics.jsonl").read_bytes(), METRICS)

    def test_directory_and_link_in_exports_folder_are_refused(self):
        self.exports.mkdir()
        (self.exports / "dir.json").mkdir()
        (self.exports / "link.json").symlink_to(self.root / "collection_registry.json")
        for name in ("dir.json", "link.json"):
            with self.subTest(name=name):
                before = self.snapshot()
                self.assert_invalid(self.save(name))
                self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.exports / "dir.json").is_dir())
        self.assertEqual((self.root / "collection_registry.json").read_bytes(), REGISTRY)

    def test_registry_name_is_saved_in_the_exports_folder_only(self):
        response = self.save("collection_registry.json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.root / "collection_registry.json").read_bytes(), REGISTRY)
        rows = json.loads((self.exports / "collection_registry.json").read_text())
        self.assertEqual(rows[0]["question"], "Inert question")

    def test_valid_names_are_saved_in_the_exports_folder(self):
        for name in ("current.json", "UPPER.JSON", "my export-1.json", UI_DEFAULT, "x" * 123 + ".json"):
            with self.subTest(name=name):
                response = self.save(name)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["filename"], name)
                self.assertEqual(body["download_url"], "/api/goldstandard/download/" + name)
                self.assertTrue((self.exports / name).is_file())
                self.assertFalse((self.root / name).exists())
        self.assertEqual((self.root / "collection_registry.json").read_bytes(), REGISTRY)
        self.assertEqual((self.root / "metrics.jsonl").read_bytes(), METRICS)

    def test_default_name_for_null_and_empty_filename(self):
        for filename in (None, ""):
            with self.subTest(filename=filename):
                response = self.client.post("/goldstandard/save", json={"session_id": SID, "filename": filename})
                self.assertEqual(response.status_code, 200, response.text)
                name = response.json()["filename"]
                self.assertRegex(name, r"^Owned_\d{8}_\d{6}\.json$")
                self.assertTrue((self.exports / name).is_file())

    def test_long_collection_default_name_is_shortened_to_128(self):
        gs._sessions = {}
        gs.store_session(session("L" + "o" * 200))
        result = asyncio.run(gs.save_session(SID, None))
        self.assertEqual(len(result["filename"]), 128)
        self.assertRegex(result["filename"], r"^Lo+_\d{8}_\d{6}\.json$")
        self.assertTrue((self.exports / result["filename"]).is_file())

    def test_saving_twice_overwrites_the_export(self):
        self.assertEqual(self.save("same.json").status_code, 200)
        changed = session()
        changed["pairs"][0]["question"] = "Changed question"
        gs.store_session(changed)
        self.assertEqual(self.save("same.json").status_code, 200)
        rows = json.loads((self.exports / "same.json").read_text())
        self.assertEqual(rows[0]["question"], "Changed question")

    def test_name_rule_message_says_ascii_letters(self):
        response = self.save("caf\u00e9.json")
        self.assert_invalid(response)
        self.assertIn("ASCII letters", response.json()["error"]["message"])

    def test_link_appearing_at_the_name_is_replaced_not_followed(self):
        # The link turns up after the entry check (#402): the write replaces it.
        self.exports.mkdir()
        (self.exports / "race.json").symlink_to(self.root / "collection_registry.json")
        with patch.object(gs, "export_entry_is_other", return_value=False):
            response = self.save("race.json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.root / "collection_registry.json").read_bytes(), REGISTRY)
        self.assertFalse((self.exports / "race.json").is_symlink())
        rows = json.loads((self.exports / "race.json").read_text())
        self.assertEqual(rows[0]["question"], "Inert question")

    def test_failed_replace_keeps_the_previous_export(self):
        self.assertEqual(self.save("same.json").status_code, 200)
        self.assertEqual(sorted(os.listdir(self.exports)), ["same.json"])
        first = (self.exports / "same.json").read_bytes()
        changed = session()
        changed["pairs"][0]["question"] = "Changed question"
        gs.store_session(changed)
        with patch.object(os, "replace", side_effect=OSError("replace refused")):
            with self.assertRaises(OSError):
                self.save("same.json")
        self.assertEqual((self.exports / "same.json").read_bytes(), first)
        self.assertEqual(sorted(os.listdir(self.exports)), ["same.json"])

    # ---- exports folder (#402) --------------------------------------------

    def test_stray_file_named_like_the_exports_folder_is_refused(self):
        # A pre-#370 save could create a file with the folder's name.
        self.exports.write_text("[]")
        before = self.snapshot()
        self.assert_folder_invalid(self.save("a.json"))
        self.assert_folder_invalid(self.client.get("/goldstandard/download/a.json"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.exports.read_text(), "[]")

    def test_linked_exports_folder_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "kept.json").write_text("[]")
        self.exports.symlink_to(elsewhere, target_is_directory=True)
        before = self.snapshot()
        self.assert_folder_invalid(self.save("a.json"))
        self.assert_folder_invalid(self.client.get("/goldstandard/download/kept.json"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(os.listdir(elsewhere)), ["kept.json"])
        self.exports.unlink()
        self.exports.symlink_to(self.root / "missing")
        before = self.snapshot()
        self.assert_folder_invalid(self.save("a.json"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "missing").exists())

    def test_download_refuses_a_dangling_exports_link_and_temporary_names(self):
        # Reviewer addition: download meets the same folder check as save for a
        # dangling link, and a save's temporary file name is never served (#402).
        self.exports.symlink_to(self.root / "missing")
        self.assert_folder_invalid(self.client.get("/goldstandard/download/a.json"))
        self.assertFalse((self.root / "missing").exists())
        self.exports.unlink()
        self.assertEqual(self.save("same.json").status_code, 200)
        self.assertEqual(sorted(os.listdir(self.exports)), ["same.json"])
        (self.exports / ".same-abcd1234.tmp").write_text("[]")
        self.assert_invalid(self.client.get("/goldstandard/download/.same-abcd1234.tmp"))

    def test_link_appearing_at_the_exports_folder_during_mkdir_is_refused(self):
        # The link turns up between the first folder check and mkdir (#406):
        # mkdir succeeds through it, and the re-check refuses it.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        real_mkdir = Path.mkdir
        planted = []

        def planting_mkdir(path, *args, **kwargs):
            if path.resolve() == self.exports.resolve():
                planted.append(path.name)
                self.exports.symlink_to(elsewhere, target_is_directory=True)
            return real_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", planting_mkdir):
            self.assert_folder_invalid(self.save("a.json"))
        self.assertEqual(planted, [gs.EXPORT_DIRNAME])
        self.assertTrue(self.exports.is_symlink())
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertEqual((self.root / "collection_registry.json").read_bytes(), REGISTRY)
        self.assertEqual((self.root / "metrics.jsonl").read_bytes(), METRICS)

    def test_invalid_name_is_refused_before_the_folder_check(self):
        self.exports.write_text("[]")
        self.assert_invalid(self.save("report.txt"))
        self.assert_invalid(self.client.get("/goldstandard/download/report.txt"))

    def test_unknown_session_still_404_before_the_name_check(self):
        response = self.client.post("/goldstandard/save", json={"session_id": "gs_00000000", "filename": ".."})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "SESSION_NOT_FOUND")

    # ---- download ---------------------------------------------------------

    def test_download_serves_a_saved_export(self):
        name = self.save(UI_DEFAULT).json()["filename"]
        response = self.client.get("/goldstandard/download/" + name)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.json()[0]["answer"], "Inert answer")

    def test_download_refuses_names_and_non_files(self):
        self.exports.mkdir()
        (self.exports / "dir.json").mkdir()
        (self.exports / "link.json").symlink_to(self.root / "collection_registry.json")
        for name in ("metrics.jsonl", "goldstandard_sessions", "report.txt", "name", "dir.json", "link.json"):
            with self.subTest(name=name):
                self.assert_invalid(self.client.get("/goldstandard/download/" + name))

    def test_download_applies_the_name_rule_at_its_boundaries(self):
        # Reviewer addition: the download route's own 128/129 and letter-case edges.
        from urllib.parse import quote
        for name in ("UPPER.JSON", "my export-1.json", "x" * 123 + ".json"):
            with self.subTest(name=name):
                self.assertEqual(self.save(name).status_code, 200)
                response = self.client.get("/goldstandard/download/" + quote(name))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()[0]["question"], "Inert question")
        for name in ("x" * 124 + ".json", "caf\u00e9.json", "name.json "):
            with self.subTest(name=name):
                self.assert_invalid(self.client.get("/goldstandard/download/" + quote(name)))

    def test_download_does_not_serve_top_level_files(self):
        (self.root / "old_export.json").write_text("[]")
        for name in ("collection_registry.json", "old_export.json", "definitely_not_here.json"):
            with self.subTest(name=name):
                response = self.client.get("/goldstandard/download/" + name)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], "FILE_NOT_FOUND")

    def test_download_dot_names_through_the_route(self):
        # An HTTP client normalises "." and ".." out of the path, so call the route.
        for name in (".", ".."):
            with self.subTest(name=name):
                response = asyncio.run(route.download(name))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.body)["error"]["code"], "INVALID_PATH")


if __name__ == "__main__":
    unittest.main(argv=["test_goldstandard_exports"], verbosity=2)
