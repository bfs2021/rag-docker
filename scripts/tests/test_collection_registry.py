"""The collection registry is written atomically and kept when it can't be read (issue #388).

Runs with the API's dependencies, in the built API image with no network
(`01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_collection_registry.py

Each test points UPLOAD_DIR, and the registry with it, at a fresh temporary folder.
Requests go through `main.app` with FastAPI's TestClient, without the lifespan, with
Weaviate mocked; nothing reaches a backend.
"""
import asyncio
import errno
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from models.schemas import CreateCollectionRequest
from routers import collections as route
from services import weaviate_client as wc

ROOT = Path(__file__).resolve().parents[2]
NAME = "collection_registry.json"
OLD = {"Kept": "2026-01-01T00:00:00+00:00", "Other": "2026-02-02T00:00:00+00:00"}

# What a registry that can't be read looks like on disk. Each writes it at `path`.
UNREADABLE = {
    "not JSON": lambda path: path.write_bytes(b'{"Kept": "2026-01-01T00:0'),
    "a JSON array": lambda path: path.write_bytes(b'["Kept"]'),
    "not UTF-8": lambda path: path.write_bytes(b'{"Kept": "\xff\xfe"}'),
    "not a file": lambda path: (path.mkdir(), (path / "inside").write_text("x")),
}


def logged(logs):
    """What the captured records put in the log, tracebacks and their causes included."""
    return "\n".join(logging.Formatter().format(record) for record in logs.records)


def described(c):
    return {"name": c, "object_count": 0, "index_type": "hnsw", "distance_metric": "cosine",
            "hnsw_config": None}


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.path = self.dir / NAME
        for patcher in (patch.object(settings, "upload_dir", temp.name),
                        patch.object(route, "_REGISTRY_FILE", self.path),
                        patch.object(route, "_registry_lock", asyncio.Lock())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def write(self, registry):
        self.path.write_text(json.dumps(registry, indent=2))

    def read(self):
        return json.loads(self.path.read_text())

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name != NAME)

    def snapshot(self):
        """The registry path's bytes, or a directory's listing and contents."""
        if self.path.is_dir():
            return {p.name: p.read_bytes() for p in self.path.iterdir()}
        return self.path.read_bytes()

    def create(self, name, exists=False):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=exists)), \
                patch.object(wc, "create_collection", AsyncMock()):
            return self.client.post("/collections", json={"name": name})

    def delete(self, name):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(wc, "delete_collection_when_idle", AsyncMock(return_value=3)):
            return self.client.delete(f"/collections/{name}?confirm=true")

    def listed(self, names):
        with patch.object(wc, "get_collections", AsyncMock(return_value=[described(n) for n in names])):
            return self.client.get("/collections")


class AtomicSaveTests(RegistryTestCase):
    def test_a_save_replaces_the_file_and_never_rewrites_it_in_place(self):
        # A crash while rewriting in place leaves a torn file. Replacing it leaves
        # the old file's own bytes untouched, which a hard link to it still shows.
        self.write(OLD)
        before = self.path.read_bytes()
        link = self.dir / "link-to-old"
        os.link(self.path, link)
        route._save_registry({"New": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(link.read_bytes(), before, "the old registry was rewritten in place")
        link.unlink()
        self.assertEqual(self.read(), {"New": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(self.path.read_text(),
                         json.dumps({"New": "2026-03-03T00:00:00+00:00"}, indent=2))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.leftovers(), [])

    def test_a_first_save_creates_the_folder_and_the_file(self):
        nested = self.dir / "uploads" / NAME
        with patch.object(route, "_REGISTRY_FILE", nested):
            route._save_registry({"First": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(json.loads(nested.read_text()), {"First": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(sorted(p.name for p in nested.parent.iterdir()), [NAME])

    def test_a_failed_write_or_replace_keeps_the_old_file_and_no_temporary_file(self):
        failures = {
            "fsync (a full volume)": patch.object(route.os, "fsync",
                                                  side_effect=OSError(errno.ENOSPC, "No space left")),
            "replace": patch.object(route.os, "replace", side_effect=OSError(errno.EIO, "I/O error")),
        }
        for label, failure in failures.items():
            with self.subTest(label):
                self.write(OLD)
                before = self.path.read_bytes()
                with failure, self.assertRaises(OSError):
                    route._save_registry({"New": "2026-03-03T00:00:00+00:00"})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.leftovers(), [])

    def test_a_registry_that_cannot_be_serialized_leaves_the_file_alone(self):
        self.write(OLD)
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            route._save_registry({"New": object()})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])

    def test_two_saves_use_different_temporary_files(self):
        seen = []
        real = route.os.replace

        def recording(source, target):
            seen.append(Path(source))
            return real(source, target)

        with patch.object(route.os, "replace", side_effect=recording):
            route._save_registry({"A": "1"})
            route._save_registry({"B": "2"})
        self.assertEqual(len(set(seen)), 2, seen)
        for source in seen:
            self.assertEqual(source.parent, self.dir)
            self.assertTrue(source.name.startswith(f".{NAME}.") and source.name.endswith(".tmp"),
                            source.name)
        self.assertEqual(self.read(), {"B": "2"})


class UnreadableRegistryTests(RegistryTestCase):
    def test_create_keeps_an_unreadable_registry_and_logs_it(self):
        for label, make in UNREADABLE.items():
            with self.subTest(label):
                self.setUp()
                make(self.path)
                before = self.snapshot()
                with self.assertLogs("routers.collections", "ERROR") as logs:
                    response = self.create("Fresh")
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json(), {"name": "Fresh", "status": "created"})
                self.assertEqual(self.snapshot(), before, "the unreadable registry was overwritten")
                self.assertEqual(self.leftovers(), [])
                text = logged(logs)
                self.assertIn("'Fresh'", text)
                self.assertIn(str(self.path), text)
                self.assertIn("left unchanged", text)

    def test_delete_keeps_an_unreadable_registry_and_logs_it(self):
        for label, make in UNREADABLE.items():
            with self.subTest(label):
                self.setUp()
                make(self.path)
                before = self.snapshot()
                with self.assertLogs("routers.collections", "ERROR") as logs:
                    response = self.delete("Kept")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"name": "Kept", "objects_deleted": 3})
                self.assertEqual(self.snapshot(), before, "the unreadable registry was overwritten")
                self.assertEqual(self.leftovers(), [])
                text = logged(logs)
                self.assertIn("'Kept'", text)
                self.assertIn(str(self.path), text)

    def test_list_answers_without_created_at_and_logs_it(self):
        for label, make in UNREADABLE.items():
            with self.subTest(label):
                self.setUp()
                make(self.path)
                before = self.snapshot()
                with self.assertLogs("routers.collections", "WARNING") as logs:
                    response = self.listed(["Kept", "Other"])
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([(c["name"], c["created_at"]) for c in response.json()["collections"]],
                                 [("Kept", None), ("Other", None)])
                self.assertEqual(self.snapshot(), before)
                self.assertIn(str(self.path), logged(logs))

    def test_updates_resume_once_the_registry_is_readable_again(self):
        self.path.write_bytes(b"{torn")
        with self.assertLogs("routers.collections", "ERROR"):
            self.assertEqual(self.create("Lost").status_code, 201)
        self.assertEqual(self.path.read_bytes(), b"{torn")
        self.write(OLD)                     # someone fixed the file
        self.assertEqual(self.create("Fresh").status_code, 201)
        self.assertEqual(set(self.read()), {"Kept", "Other", "Fresh"})


class ReadableRegistryTests(RegistryTestCase):
    def test_create_on_a_missing_registry_records_created_at(self):
        response = self.create("Docs")
        self.assertEqual(response.status_code, 201, response.text)
        registry = self.read()
        self.assertEqual(list(registry), ["Docs"])
        self.assertIsNotNone(datetime.fromisoformat(registry["Docs"]).tzinfo)
        listed = self.listed(["Docs", "Unknown"]).json()["collections"]
        self.assertEqual([(c["name"], c["created_at"]) for c in listed],
                         [("Docs", registry["Docs"]), ("Unknown", None)])
        self.assertEqual(self.leftovers(), [])

    def test_create_keeps_the_other_entries(self):
        self.write(OLD)
        self.assertEqual(self.create("Docs").status_code, 201)
        registry = self.read()
        self.assertEqual({k: v for k, v in registry.items() if k != "Docs"}, OLD)
        self.assertIn("Docs", registry)

    def test_delete_removes_both_spellings_and_keeps_the_others(self):
        self.write({**OLD, "Docs": "a", "docs": "b"})
        self.assertEqual(self.delete("docs").status_code, 200)
        self.assertEqual(self.read(), OLD)
        self.assertEqual(self.leftovers(), [])

    def test_a_missing_registry_lists_without_created_at_and_logs_nothing(self):
        with self.assertNoLogs("routers.collections", "WARNING"):
            response = self.listed(["Docs"])
        self.assertEqual(response.json()["collections"][0]["created_at"], None)
        self.assertFalse(self.path.exists())

    def test_concurrent_creates_are_all_recorded(self):
        names = [f"Coll{n}" for n in range(20)]

        async def run():
            with patch.object(wc, "collection_exists", AsyncMock(return_value=False)), \
                    patch.object(wc, "create_collection", AsyncMock()):
                await asyncio.gather(*(route.create_collection(CreateCollectionRequest(name=n))
                                       for n in names))

        self.write(OLD)
        with patch.object(route, "_registry_lock", asyncio.Lock()):
            asyncio.run(run())
        self.assertEqual(set(self.read()), set(OLD) | set(names))
        self.assertEqual(self.leftovers(), [])


class ImplementationCopyTests(unittest.TestCase):
    def test_implementation_md_holds_the_current_router(self):
        document = (ROOT / "IMPLEMENTATION.md").read_text()
        name = "api/routers/collections.py"
        header = "### " + name + "\n\n```"
        start = document.index("\n", document.index(header) + len(header)) + 1
        end = document.index("\n```\n", start)
        self.assertEqual(document[start:end], (ROOT / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
