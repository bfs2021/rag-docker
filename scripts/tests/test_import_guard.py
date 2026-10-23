"""Concurrent imports of one package file or into one target (issue #391).

Runs with the API's dependencies, on the host or inside the verify project's API
container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_import_guard.py

Weaviate is mocked; nothing reaches a backend. A second create of an existing
collection fails as Weaviate 1.39.6 answers it (422 "class name X already exists",
checked at runtime for #391). Orderings are arranged with events, not sleeps.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx

from config import settings
from main import app
from services import collection_writes as writes
from services import importer
from services import weaviate_client as wc


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


class ImportGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.backend = set()                 # canonical names that exist
        self.packages = {}                   # package file name -> collection name
        self.contents = {}                   # package file name -> package it holds
        self.arrived = []                    # targets whose build has started, in order
        self.release = threading.Event()     # lets every held build finish
        self.addCleanup(self.release.set)
        self.built = []
        self.create_first = False            # create the collection before holding, as Weaviate does
        self.fail_build = set()              # targets whose build raises
        stack = ExitStack()
        self.addCleanup(stack.close)
        for change in [
            patch.object(settings, "upload_dir", str(self.root)),
            patch.object(importer.packager, "exports_dir", return_value=self.root),
            patch.object(importer.packager, "open_package", side_effect=self.open_package),
            patch.object(importer.packager, "verify_digests"),
            patch.object(importer, "_check_embedding"),
            patch.object(importer, "_ensure_models", return_value=[]),
            patch.object(importer, "_restore_sidecars", return_value=[]),
            patch.object(importer, "_mark_started", side_effect=self.mark_started),
            patch.object(importer, "_mark_finished", side_effect=self.mark_finished),
            patch.object(importer, "_build", side_effect=self.build),
            # A replace over an existing collection is not exercised here; a
            # second replace must never get that far.
            patch.object(importer.collection_recovery, "begin",
                         side_effect=AssertionError("replace staging reached")),
            patch.object(wc, "_collection_exists_sync",
                         side_effect=lambda name: writes.canonical(name) in self.backend),
        ]:
            stack.enter_context(change)
        self.addCleanup(self.assert_released)

    # Mocked package, markers and backend

    def open_package(self, archive, work):
        collection = self.packages[Path(archive).name]
        held = self.contents.get(Path(archive).name, Path(archive).name)
        pkg = self.root / "packages" / Path(archive).name
        pkg.mkdir(parents=True, exist_ok=True)
        # The rename suffix is the manifest's digest: one package, one suffix.
        (pkg / "manifest.json").write_text(json.dumps({"package": held}))
        return pkg, {"collection": {"name": collection, "chunk_count": 1},
                     "fidelity": "chunks-only"}

    def mark_started(self, collection, expected, job_id, records):
        # Writes the real marker path, so an overwrite or a removal is visible.
        importer._marker_path(collection).write_text(json.dumps({"job_id": job_id}))
        return f"instance-{job_id}"

    def mark_finished(self, collection):
        importer._marker_path(collection).unlink(missing_ok=True)

    def marker_owner(self, collection):
        path = importer._marker_path(collection)
        return json.loads(path.read_text())["job_id"] if path.exists() else None

    def build(self, target, pkg, manifest, progress, instance=None):
        self.arrived.append(target)
        if self.create_first:
            # The real _build creates the collection, then inserts: the target
            # exists while the first import is still writing it.
            self.backend.add(target)
            assert self.release.wait(10)
            self.built.append(target)
            return 1
        assert self.release.wait(10)
        if target in self.fail_build:
            raise RuntimeError(f"insert into {target} failed")
        if target in self.backend:
            raise RuntimeError("Collection may not have been created properly.! Unexpected "
                               "status code: 422, with response body: {'error': [{'message': "
                               f"'class name {target} already exists'}}]}}.")
        self.backend.add(target)
        self.built.append(target)
        return 1

    def assert_released(self):
        self.release.set()
        # `_targets` is #391's; read so the file also runs against the code before it.
        targets = lambda: getattr(importer, "_targets", {})
        self.assertTrue(wait_until(lambda: not importer._active and not targets()),
                        (importer._active, targets()))
        self.assertEqual(writes._active_jobs, {})

    # Driving imports

    def start(self, loop, filename, on_conflict):
        return loop.run_until_complete(importer.start_import_job(filename, on_conflict))

    def job(self, job_id):
        return importer.get_job(job_id)

    def finished(self, job_id):
        return self.job(job_id)["status"] in ("completed", "failed")

    def wait_finished(self, loop, job_id, timeout=5.0):
        async def poll():
            deadline = time.monotonic() + timeout
            while not self.finished(job_id):
                if time.monotonic() > deadline:
                    return False
                await asyncio.sleep(0.01)
            return True
        return loop.run_until_complete(poll())

    def wait_arrived(self, loop, count, timeout=5.0):
        async def poll():
            deadline = time.monotonic() + timeout
            while len(self.arrived) < count:
                if time.monotonic() > deadline:
                    return False
                await asyncio.sleep(0.01)
            return True
        return loop.run_until_complete(poll())

    def new_loop(self):
        loop = asyncio.new_event_loop()

        def close():
            self.release.set()
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
        self.addCleanup(close)
        return loop

    def assert_refused(self, job, collection):
        self.assertEqual((job["status"], job["error_code"], job["error_detail"]),
                         ("failed", "IMPORT_IN_PROGRESS", {"collection": collection}), job)
        self.assertEqual(job["error"], f"Another import into '{collection}' is running. "
                                       "Start this import again once it finishes.")

    def held_then_second(self, first, second, collection="Shared", existing=()):
        """Start `first` and hold it in its build, then run `second` to its end.

        Each is (package file, collection, on_conflict). Returns both job ids and
        the marker owner seen once the second ended, while the first still builds.
        """
        self.backend.update(existing)
        loop = self.new_loop()
        self.packages[first[0]] = first[1]
        self.packages[second[0]] = second[1]
        first_id = self.start(loop, first[0], first[2])
        self.assertTrue(self.wait_arrived(loop, 1), self.job(first_id))
        second_id = self.start(loop, second[0], second[2])
        if not self.wait_finished(loop, second_id, timeout=2.0):
            # Without the guard the second waits or builds too; let the first
            # create its collection so the second meets Weaviate's error.
            self.release.set()
            self.assertTrue(self.wait_finished(loop, second_id), self.job(second_id))
        owner = self.marker_owner(collection)
        self.release.set()
        self.assertTrue(self.wait_finished(loop, first_id), self.job(first_id))
        return loop, first_id, second_id, owner

    # Tests

    def test_two_spellings_of_one_package_are_refused(self):
        loop = self.new_loop()
        self.packages["x.tar.gz"] = "Spelled"
        first_id = self.start(loop, "x.tar.gz", "abort")
        self.assertTrue(self.wait_arrived(loop, 1), self.job(first_id))

        async def post(filename):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://guard") as api:
                return await api.post("/import", json={"filename": filename, "on_conflict": "abort"})
        for spelling in ("./x.tar.gz", "a/../x.tar.gz", "x.tar.gz"):
            with self.subTest(spelling=spelling):
                response = loop.run_until_complete(post(spelling))
                self.assertEqual(response.status_code, 409, response.text)
                error = response.json()["error"]
                self.assertEqual(error["code"], "IMPORT_IN_PROGRESS")
                self.assertEqual(error["detail"], {"filename": spelling})
        self.release.set()
        self.assertTrue(self.wait_finished(loop, first_id))
        self.assertEqual(self.job(first_id)["status"], "completed", self.job(first_id))
        self.assertEqual(self.built, ["Spelled"])

    def test_a_second_import_into_the_same_new_target_is_refused(self):
        loop, first_id, second_id, owner = self.held_then_second(
            ("a.tar.gz", "Shared", "abort"), ("b.tar.gz", "Shared", "abort"))
        # The first import's marker survived the second, for hard-kill recovery.
        self.assertEqual(owner, first_id, self.job(second_id))
        self.assert_refused(self.job(second_id), "Shared")
        self.assertEqual(self.job(first_id)["status"], "completed", self.job(first_id))
        self.assertEqual(self.arrived, ["Shared"])
        self.assertIsNone(self.marker_owner("Shared"))

    def test_every_conflict_mode_pair_into_one_target_is_refused(self):
        pairs = [("rename", "rename"), ("replace", "replace"), ("abort", "replace"),
                 ("replace", "abort"), ("abort", "rename")]
        for first_mode, second_mode in pairs:
            with self.subTest(first=first_mode, second=second_mode):
                self.backend.clear(); self.arrived.clear(); self.built.clear()
                self.release.clear()
                name = f"Mode{first_mode.capitalize()}{second_mode.capitalize()}"
                loop, first_id, second_id, owner = self.held_then_second(
                    (f"{name}-a.tar.gz", name, first_mode), (f"{name}-b.tar.gz", name, second_mode),
                    collection=name)
                self.assert_refused(self.job(second_id), name)
                self.assertEqual(owner, first_id)
                self.assertEqual(self.job(first_id)["status"], "completed", self.job(first_id))
                self.assertEqual(self.built, [name])
                self.assertTrue(wait_until(lambda: not getattr(importer, "_targets", {})))

    def test_a_lowercase_spelling_of_the_target_is_the_same_target(self):
        loop, first_id, second_id, owner = self.held_then_second(
            ("a.tar.gz", "Shared", "abort"), ("b.tar.gz", "shared", "abort"))
        self.assert_refused(self.job(second_id), "Shared")
        self.assertEqual(owner, first_id)

    def test_imports_into_different_targets_both_complete(self):
        loop = self.new_loop()
        self.packages.update({"a.tar.gz": "First", "b.tar.gz": "Second"})
        first_id = self.start(loop, "a.tar.gz", "abort")
        second_id = self.start(loop, "b.tar.gz", "rename")
        # Both reach their build at once: neither waits for the other.
        self.assertTrue(self.wait_arrived(loop, 2), (self.job(first_id), self.job(second_id)))
        self.assertEqual(self.marker_owner("First"), first_id)
        self.assertEqual(self.marker_owner("Second"), second_id)
        self.release.set()
        for job_id, name in ((first_id, "First"), (second_id, "Second")):
            self.assertTrue(self.wait_finished(loop, job_id))
            job = self.job(job_id)
            self.assertEqual((job["status"], job["collection"]), ("completed", name), job)
        self.assertEqual(sorted(self.built), ["First", "Second"])

    def test_rename_imports_whose_new_names_coincide_are_refused(self):
        # The same package under two file names, over an existing collection:
        # both rename to one `_imported_<id8>` name.
        self.contents.update({"one.tar.gz": "same", "two.tar.gz": "same"})
        loop, first_id, second_id, owner = self.held_then_second(
            ("one.tar.gz", "Taken", "rename"), ("two.tar.gz", "Taken", "rename"),
            existing=("Taken",))
        first = self.job(first_id)
        self.assertEqual(first["status"], "completed", first)
        renamed = first["collection"]
        self.assertTrue(renamed.startswith("Taken_imported_"), first)
        self.assert_refused(self.job(second_id), renamed)
        self.assertEqual(self.built, [renamed])

    def test_a_renamed_import_does_not_hold_the_original_name(self):
        loop, first_id, second_id, owner = self.held_then_second(
            ("a.tar.gz", "Taken", "rename"), ("b.tar.gz", "Taken", "abort"),
            existing=("Taken",))
        second = self.job(second_id)
        # The original name is not this import's target: the abort meets the
        # existing collection, not the renaming import.
        self.assertEqual((second["status"], second["error_code"]), ("failed", "COLLECTION_EXISTS"), second)
        self.assertEqual(self.job(first_id)["status"], "completed", self.job(first_id))

    def test_a_target_is_free_again_once_its_import_ends(self):
        loop, first_id, second_id, owner = self.held_then_second(
            ("a.tar.gz", "Freed", "abort"), ("b.tar.gz", "Freed", "abort"))
        self.assert_refused(self.job(second_id), "Freed")
        self.assertTrue(wait_until(lambda: not getattr(importer, "_targets", {})))
        # A later import of the same target is judged on the collection, not refused.
        third_id = self.start(loop, "b.tar.gz", "abort")
        self.assertTrue(self.wait_finished(loop, third_id))
        third = self.job(third_id)
        self.assertEqual((third["status"], third["error_code"]), ("failed", "COLLECTION_EXISTS"), third)

    def test_the_claim_comes_before_the_existence_check(self):
        # The first import's collection exists while it builds, as with Weaviate.
        # A second import of that target is refused for the import in progress,
        # not judged on the half-built collection: no COLLECTION_EXISTS, and no
        # rename of a collection that is still being written.
        self.create_first = True
        for mode in ("abort", "rename", "replace"):
            with self.subTest(second=mode):
                self.backend.clear(); self.arrived.clear(); self.built.clear()
                self.release.clear()
                name = f"Early{mode.capitalize()}"
                loop, first_id, second_id, owner = self.held_then_second(
                    (f"{name}-a.tar.gz", name, "abort"), (f"{name}-b.tar.gz", name, mode),
                    collection=name)
                self.assert_refused(self.job(second_id), name)
                self.assertEqual(owner, first_id)
                self.assertEqual(self.built, [name])
                self.assertTrue(wait_until(lambda: not getattr(importer, "_targets", {})))

    def test_a_refused_import_registers_nothing(self):
        loop = self.new_loop()
        self.packages.update({"a.tar.gz": "Registered", "b.tar.gz": "Registered"})
        first_id = self.start(loop, "a.tar.gz", "abort")
        self.assertTrue(self.wait_arrived(loop, 1), self.job(first_id))

        async def post():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://guard") as api:
                return await api.post("/import", json={"filename": "b.tar.gz", "on_conflict": "abort"})
        # The target refusal is the job's, not the request's: POST still answers 202.
        response = loop.run_until_complete(post())
        self.assertEqual(response.status_code, 202, response.text)
        second_id = response.json()["job_id"]
        self.assertTrue(self.wait_finished(loop, second_id, timeout=2.0), self.job(second_id))
        self.assert_refused(self.job(second_id), "Registered")
        # Only the first import is registered against the collection (#40).
        self.assertEqual(writes.active_jobs("Registered"), [("import", first_id)])
        self.release.set()
        self.assertTrue(self.wait_finished(loop, first_id))

    def test_a_target_is_free_again_after_its_import_fails(self):
        loop = self.new_loop()
        self.fail_build.add("Broken")
        self.packages.update({"a.tar.gz": "Broken", "b.tar.gz": "Broken"})
        first_id = self.start(loop, "a.tar.gz", "abort")
        self.release.set()
        self.assertTrue(self.wait_finished(loop, first_id))
        first = self.job(first_id)
        self.assertEqual((first["status"], first["error_code"]), ("failed", "IMPORT_FAILED"), first)
        self.assertTrue(wait_until(lambda: not getattr(importer, "_targets", {})))
        self.fail_build.clear()
        second_id = self.start(loop, "b.tar.gz", "abort")
        self.assertTrue(self.wait_finished(loop, second_id))
        self.assertEqual(self.job(second_id)["status"], "completed", self.job(second_id))
        self.assertEqual(self.built, ["Broken"])

    def test_a_failed_start_releases_every_spelling(self):
        # A start whose first record can't be stored releases the package name,
        # not the spelling it was sent with.
        loop = self.new_loop()
        self.packages["x.tar.gz"] = "Retried"
        with patch.object(importer._jobs, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.start(loop, "./x.tar.gz", "abort")
        self.assertEqual(importer._active, set())
        job_id = self.start(loop, "x.tar.gz", "abort")
        self.release.set()
        self.assertTrue(self.wait_finished(loop, job_id))
        self.assertEqual(self.job(job_id)["status"], "completed", self.job(job_id))


if __name__ == "__main__":
    unittest.main()
