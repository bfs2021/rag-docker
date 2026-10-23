"""The collection registry is written atomically and kept when it can't be read (issue #388);
its folder is fsynced after the replace and startup removes the temporary files of
interrupted saves (issue #445).

Runs with the API's dependencies, in the built API image with no network
(`01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_collection_registry.py

Each test points UPLOAD_DIR, and the registry with it, at a fresh temporary folder.
Requests go through `main.app` with FastAPI's TestClient, without the lifespan, with
Weaviate mocked; nothing reaches a backend. The lifespan case runs in a child process,
with the backend sweeps mocked, so this process's module state stays untouched.
"""
import asyncio
import errno
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

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


def same_file(fd, path):
    """Whether descriptor `fd` is open on `path`."""
    opened, named = os.fstat(fd), os.stat(path)
    return (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)


class FolderSyncTests(RegistryTestCase):
    """The folder is fsynced after the replace, so the rename survives a power loss (#445)."""

    def record(self, events, fail_folder_sync=False):
        """Patches recording replace, open, fsync and close of the router's os module.
        With fail_folder_sync, an fsync of a folder fails as on a broken volume."""
        real = {name: getattr(route.os, name) for name in ("replace", "open", "fsync", "close")}

        def replace(source, target):
            events.append(("replace", Path(target)))
            return real["replace"](source, target)

        def open_(path, flags, *args):
            fd = real["open"](path, flags, *args)
            events.append(("open", fd))
            return fd

        def fsync(fd):
            folder = stat.S_ISDIR(os.fstat(fd).st_mode)
            events.append(("fsync folder" if folder else "fsync file", fd))
            if folder and fail_folder_sync:
                raise OSError(errno.EIO, "I/O error")
            return real["fsync"](fd)

        def close(fd):
            events.append(("close", fd))
            return real["close"](fd)

        return [patch.object(route.os, "replace", side_effect=replace),
                patch.object(route.os, "open", side_effect=open_),
                patch.object(route.os, "fsync", side_effect=fsync),
                patch.object(route.os, "close", side_effect=close)]

    def run_save(self, events, fail_folder_sync=False):
        patches = self.record(events, fail_folder_sync)
        for patcher in patches:
            patcher.start()
        try:
            route._save_registry({"New": "2026-03-03T00:00:00+00:00"})
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def test_the_folder_is_fsynced_after_the_replace(self):
        self.write(OLD)
        events = []
        self.run_save(events)
        names = [name for name, _ in events]
        self.assertIn("replace", names, events)
        after = events[names.index("replace") + 1:]
        self.assertEqual([name for name, _ in after], ["open", "fsync folder", "close"], events)
        fd = after[0][1]
        self.assertEqual(after[1][1], fd)
        self.assertEqual(after[2][1], fd)
        self.assertEqual(events[names.index("replace")][1], self.path)
        self.assertEqual(self.read(), {"New": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(self.leftovers(), [])

    def test_the_synced_folder_is_the_registry_folder(self):
        seen = []
        real = route.os.fsync

        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                seen.append(same_file(fd, self.dir))
            return real(fd)

        with patch.object(route.os, "fsync", side_effect=fsync):
            route._save_registry({"New": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(seen, [True])

    def test_a_failed_folder_sync_raises_with_the_new_file_in_place(self):
        self.write(OLD)
        events = []
        with self.assertRaises(OSError):
            self.run_save(events, fail_folder_sync=True)
        self.assertEqual(self.read(), {"New": "2026-03-03T00:00:00+00:00"})
        self.assertEqual(self.leftovers(), [])
        names = [name for name, _ in events]
        after = events[names.index("replace") + 1:]
        self.assertEqual([name for name, _ in after], ["open", "fsync folder", "close"], events)
        self.assertEqual(after[2][1], after[0][1], "the folder's descriptor was left open")


class SweepTests(RegistryTestCase):
    """Startup removes the temporary files of saves a crash interrupted (#445)."""

    def test_leftovers_are_removed_and_everything_else_is_kept(self):
        self.write(OLD)
        before = self.path.read_bytes()
        (self.dir / f".{NAME}.abc123.tmp").write_bytes(b"")
        (self.dir / f".{NAME}.def456.tmp").write_bytes(b'{"Kept": "2026-01-01T0')
        kept = [".metrics.jsonl.x1.tmp", f"{NAME}.tmp", f".{NAME}.x2", f".{NAME}.tmp.x3",
                "notes.txt"]
        for name in kept:
            (self.dir / name).write_text("keep")
        (self.dir / "jobs").mkdir()
        (self.dir / "jobs" / f".{NAME}.x4.tmp").write_text("keep")
        with self.assertNoLogs("routers.collections", "WARNING"):
            route.remove_interrupted_saves()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), sorted([NAME, "jobs"] + kept))
        self.assertEqual((self.dir / "jobs" / f".{NAME}.x4.tmp").read_text(), "keep")
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_link_is_removed_without_following_it(self):
        target = self.dir / "target"
        target.write_text("keep")
        (self.dir / f".{NAME}.abc.tmp").symlink_to(target)
        route.remove_interrupted_saves()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["target"])
        self.assertEqual(target.read_text(), "keep")

    def test_a_leftover_that_cannot_be_removed_is_logged_and_the_rest_removed(self):
        folder = self.dir / f".{NAME}.a.tmp"
        folder.mkdir()
        (folder / "inside").write_text("keep")
        (self.dir / f".{NAME}.b.tmp").write_bytes(b"")
        with self.assertLogs("routers.collections", "ERROR") as logs:
            route.remove_interrupted_saves()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [folder.name])
        self.assertEqual((folder / "inside").read_text(), "keep")
        self.assertIn(str(folder), logged(logs))

    def test_a_missing_folder_sweeps_nothing_and_is_not_created(self):
        missing = self.dir / "uploads" / NAME
        with patch.object(route, "_REGISTRY_FILE", missing):
            route.remove_interrupted_saves()
        self.assertFalse(missing.parent.exists())


class LifespanTests(RegistryTestCase):
    def test_startup_removes_leftovers_and_leaves_the_registry_alone(self):
        self.write(OLD)
        before = self.path.read_bytes()
        leftover = self.dir / f".{NAME}.k1x9.tmp"
        leftover.write_bytes(b'{"Kept": "2026-01-01T0')
        script = """
import asyncio, sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[1])
from main import app, lifespan
from services import importer, packager, weaviate_client as wc
async def run():
    with patch.object(wc, 'sweep_staging', new=AsyncMock(return_value=[])), \\
         patch.object(importer, 'sweep_interrupted_imports', return_value=[]), \\
         patch.object(importer, 'sweep_stale_workdirs', return_value=[]), \\
         patch.object(packager, 'sweep_build_leftovers', return_value=[]), \\
         patch.object(wc, 'close_client'):
        async with lifespan(app):
            pass
asyncio.run(run())
"""
        exports = self.dir / "exports"
        env = {**os.environ, "UPLOAD_DIR": str(self.dir), "EXPORTS_DIR": str(exports),
               "SOURCES_DIR": str(self.dir / "sources")}
        result = subprocess.run([sys.executable, "-c", script, API_DIR], capture_output=True,
                                text=True, timeout=120, cwd=API_DIR, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(leftover.exists(), "startup left the interrupted registry write")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.dir.glob(f".{NAME}*")], [])


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
