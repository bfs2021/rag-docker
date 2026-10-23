"""Import refuses unreadable, unsafe and malformed packages (issue #387).

Checks 1 and 2 of RAG_EXPORT_SPECIFICATIONS.md section 6.2 on `packager.open_package`, and the
collection-name and `chunks.jsonl` record checks through `importer._run`, each with a package
that is digest-valid, so only the case under test decides.

Runs with the API's dependencies, inside the verify project's API container
(`15_transfer_paths.sh`):

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_package_refusals.py

Weaviate, Ollama and the model store are mocked; nothing reaches a backend.
"""
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import importer, packager
from services import weaviate_client as wc

WIDTH = 4


def records(count=3):
    return [{"id": str(uuid.UUID(int=i + 1)), "vector": [0.1 * (i + 1)] * WIDTH,
             "properties": {"content": f"chunk {i}", "source_file": "doc.txt",
                            "chunk_index": i},
             "source_sha256": None} for i in range(count)]


def lines(items):
    return "".join(json.dumps(item) + "\n" for item in items)


def add_bytes(tar, name, data, **fields):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for key, value in fields.items():
        setattr(info, key, value)
    tar.addfile(info, io.BytesIO(data))


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.exports = self.root / "exports"
        self.uploads = self.root / "uploads"
        self.exports.mkdir()
        self.uploads.mkdir()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(settings, "exports_dir", str(self.exports)))
        stack.enter_context(patch.object(settings, "upload_dir", str(self.uploads)))
        stack.enter_context(patch.object(settings, "sources_dir", str(self.root / "sources")))
        stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack = stack

    def package(self, name="ragpkg-corpus.tar.gz", chunks=None, manifest=None,
                collection="Corpus", count=None):
        """A digest-valid package: `chunks.jsonl` is signed in the manifest."""
        stage = self.root / ("stage-" + uuid.uuid4().hex[:8]) / "ragpkg-corpus"
        stage.mkdir(parents=True)
        items = records() if chunks is None else chunks
        text = items if isinstance(items, str) else lines(items)
        (stage / "chunks.jsonl").write_text(text)
        data = {"package_format": 1, "created_at": "2026-10-21T00:00:00Z",
                "collection": {"name": collection,
                               "chunk_count": len(records()) if count is None else count,
                               "source_document_count": 0},
                "embedding": {"model": settings.embed_model, "dimensions": WIDTH},
                "fidelity": "chunks-only", "models_bundled": False, "bundled_models": [],
                "files": {"chunks.jsonl": "sha256:" + packager.sha256_file(stage / "chunks.jsonl")},
                "warnings": []}
        data.update(manifest or {})
        (stage / "manifest.json").write_text(json.dumps(data))
        archive = self.exports / name
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname=stage.name)
        return archive


class OpenPackageTests(Fixture):
    """Checks 1 and 2: nothing is extracted from a package that fails them."""

    def refused(self, archive, code):
        dest = self.root / "work" / "dest"
        dest.mkdir(parents=True)
        with self.assertRaises(packager.PackageError) as caught:
            packager.open_package(archive, dest)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception, dest

    def assert_nothing_extracted(self, dest):
        self.assertEqual(sorted(p.name for p in dest.iterdir()), [])
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["dest"])

    def test_a_missing_file_is_unreadable(self):
        error, dest = self.refused(self.exports / "ragpkg-absent.tar.gz", "PACKAGE_UNREADABLE")
        self.assertEqual(error.message,
                         "No package named 'ragpkg-absent.tar.gz' in the exports directory.")
        self.assert_nothing_extracted(dest)

    def test_a_file_that_is_not_gzip_is_unreadable(self):
        archive = self.exports / "ragpkg-text.tar.gz"
        archive.write_text("this is not a gzip archive\n")
        error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message, "'ragpkg-text.tar.gz' is not a readable .tar.gz archive.")
        self.assertIn("reason", error.detail)
        self.assert_nothing_extracted(dest)

    def test_a_truncated_archive_is_unreadable_and_extracts_nothing(self):
        # Large enough that the cut falls inside the compressed stream, not after it.
        whole = self.package(chunks=records(400), count=400)
        data = whole.read_bytes()
        archive = self.exports / "ragpkg-cut.tar.gz"
        archive.write_bytes(data[: len(data) // 2])
        error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message, "'ragpkg-cut.tar.gz' is not a readable .tar.gz archive.")
        self.assert_nothing_extracted(dest)

    def with_member(self, name, **fields):
        """A valid package with one extra member appended."""
        archive = self.package()
        out = self.exports / ("ragpkg-" + name.replace("/", "_").replace(".", "_") + ".tar.gz")
        with tarfile.open(archive, "r:gz") as src, tarfile.open(out, "w:gz") as tar:
            for member in src.getmembers():
                tar.addfile(member, src.extractfile(member) if member.isfile() else None)
            add_bytes(tar, name, b"" if fields else b"outside", **fields)
        return out

    def test_a_symlink_member_is_refused_naming_it(self):
        target = self.root / "outside.txt"
        archive = self.with_member("ragpkg-corpus/link", type=tarfile.SYMTYPE,
                                   linkname=str(target))
        error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message,
                         "Package contains a link ('ragpkg-corpus/link'), which is not allowed.")
        self.assertEqual(error.detail, {"member": "ragpkg-corpus/link"})
        self.assert_nothing_extracted(dest)
        self.assertFalse(target.exists())

    def test_a_hardlink_member_is_refused_naming_it(self):
        archive = self.with_member("ragpkg-corpus/hard", type=tarfile.LNKTYPE,
                                   linkname="ragpkg-corpus/manifest.json")
        error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message,
                         "Package contains a link ('ragpkg-corpus/hard'), which is not allowed.")
        self.assertEqual(error.detail, {"member": "ragpkg-corpus/hard"})
        self.assert_nothing_extracted(dest)

    def test_a_member_outside_the_archive_is_refused_and_not_written(self):
        archive = self.with_member("../escape-387.txt")
        error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message,
                         "Package contains a path outside the archive ('../escape-387.txt').")
        self.assertEqual(error.detail, {"member": "../escape-387.txt"})
        self.assert_nothing_extracted(dest)
        self.assertFalse((dest.parent / "escape-387.txt").exists())

    def test_two_top_level_directories_are_refused(self):
        archive = self.with_member("second/extra.txt")
        error, _dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message, f"'{archive.name}' does not expand to a single directory.")
        self.assertEqual(error.detail, {"found": ["ragpkg-corpus", "second"]})

    def manifest_case(self, text):
        stage = self.root / "manifest-case" / "ragpkg-corpus"
        stage.mkdir(parents=True)
        (stage / "chunks.jsonl").write_text(lines(records()))
        if text is not None:
            (stage / "manifest.json").write_text(text)
        archive = self.exports / "ragpkg-manifest.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname=stage.name)
        return self.refused(archive, "PACKAGE_FORMAT_UNSUPPORTED")[0]

    def test_a_package_without_a_manifest_is_unsupported(self):
        error = self.manifest_case(None)
        self.assertEqual(error.message,
                         "'ragpkg-manifest.tar.gz' has no manifest.json, so it is not a RAG package.")

    def test_a_manifest_that_is_not_json_is_unsupported(self):
        error = self.manifest_case("{not json")
        self.assertEqual(error.message, "The manifest in 'ragpkg-manifest.tar.gz' is not valid JSON.")
        self.assertIn("reason", error.detail)

    def test_a_newer_package_format_is_unsupported(self):
        error = self.manifest_case(json.dumps({"package_format": packager.PACKAGE_FORMAT + 1}))
        self.assertEqual(error.detail, {"package_format": packager.PACKAGE_FORMAT + 1,
                                        "supported": packager.PACKAGE_FORMAT})
        self.assertIn("newer than this instance understands", error.message)

    def test_the_current_format_is_accepted(self):
        dest = self.root / "ok"
        dest.mkdir()
        pkg, manifest = packager.open_package(self.package(), dest)
        self.assertEqual((pkg.name, manifest["package_format"]), ("ragpkg-corpus", 1))


class RunTests(Fixture):
    """The name and record checks, through the import job itself."""

    def setUp(self):
        super().setUp()
        self.create = MagicMock(side_effect=RuntimeError("reached collection creation"))
        self.exists = MagicMock(return_value=False)
        self.client = MagicMock(side_effect=AssertionError("no Weaviate client may be opened"))
        for target, value in ((importer, "_check_embedding"), (importer, "_ensure_models")):
            self.stack.enter_context(patch.object(target, value, return_value=[]))
        self.stack.enter_context(patch.object(wc, "_create_collection_sync", self.create))
        self.stack.enter_context(patch.object(wc, "_collection_exists_sync", self.exists))
        self.stack.enter_context(patch.object(wc, "get_client", self.client))
        self.stack.enter_context(patch.object(importer, "_log"))

    def run_import(self, archive, on_conflict="rename"):
        job_id = "job" + uuid.uuid4().hex[:5]
        importer._jobs[job_id] = {"job_id": job_id, "status": "queued", "filename": archive.name,
                                  "on_conflict": on_conflict, "collection": None,
                                  "original_collection": None, "chunks_written": 0,
                                  "fidelity": None, "renamed": False, "notes": [],
                                  "restored_sessions": [], "error": None, "error_code": None,
                                  "error_detail": None}
        with importer._lock:
            importer._active.add(archive.name)
        importer._run(job_id, archive.name, on_conflict)
        return importer._jobs[job_id]

    def assert_corrupt(self, archive, fragment, detail=None):
        for conflict in ("abort", "rename"):
            with self.subTest(on_conflict=conflict):
                self.create.reset_mock()
                job = self.run_import(archive, conflict)
                self.assertEqual((job["status"], job["error_code"]), ("failed", "PACKAGE_CORRUPT"),
                                 job["error"])
                self.assertIn(fragment, job["error"])
                if detail is not None:
                    self.assertEqual(job["error_detail"], detail)
                self.create.assert_not_called()
                markers = self.uploads / "imports_in_progress"
                self.assertEqual(sorted(p.name for p in markers.iterdir()) if markers.exists() else [], [])
                self.assertEqual(sorted(p.name for p in self.uploads.glob("import-*")), [])

    def test_the_valid_package_reaches_collection_creation(self):
        job = self.run_import(self.package())
        self.assertEqual((job["status"], job["error_code"]), ("failed", "IMPORT_FAILED"))
        self.assertIn("reached collection creation", job["error"])
        self.create.assert_called_once()

    def test_a_line_that_is_not_json_is_corrupt_naming_the_line(self):
        text = lines(records()).splitlines()
        text[1] = "{not json"
        self.assert_corrupt(self.package(chunks="\n".join(text) + "\n"),
                            "chunks.jsonl line 2 is not valid JSON.",
                            {"file": "chunks.jsonl", "line": 2})

    def test_a_bad_uuid_is_corrupt(self):
        items = records()
        items[0]["id"] = "not-a-uuid"
        self.assert_corrupt(self.package(chunks=items), "Invalid chunk identity or properties",
                            {"file": "chunks.jsonl"})

    def test_properties_that_are_not_an_object_are_corrupt(self):
        items = records()
        items[2]["properties"] = ["content", "chunk 2"]
        self.assert_corrupt(self.package(chunks=items), "Chunk properties must be an object",
                            {"file": "chunks.jsonl"})

    def test_a_missing_vector_is_corrupt(self):
        items = records()
        del items[1]["vector"]
        self.assert_corrupt(self.package(chunks=items), f"Chunk {items[1]['id']} has no valid vector.")

    def test_a_vector_of_the_wrong_width_is_corrupt(self):
        items = records()
        items[1]["vector"] = items[1]["vector"][:-1]
        self.assert_corrupt(self.package(chunks=items),
                            f"Chunk {items[1]['id']} has the wrong vector width.")

    def test_a_count_that_does_not_match_the_manifest_is_corrupt(self):
        for declared in (len(records()) + 1, len(records()) - 1):
            with self.subTest(declared=declared):
                self.assert_corrupt(self.package(count=declared),
                                    f"Package contains {len(records())} objects but declares {declared}",
                                    {"file": "chunks.jsonl"})

    def test_a_collection_name_weaviate_cannot_use_is_unsupported(self):
        for name in ("1bad", "bad-name"):
            with self.subTest(name=name):
                self.exists.reset_mock()
                job = self.run_import(self.package(collection=name))
                self.assertEqual((job["status"], job["error_code"]),
                                 ("failed", "PACKAGE_FORMAT_UNSUPPORTED"), job["error"])
                self.assertEqual(job["error"], f"'{name}' is not a usable collection name.")
                self.assertEqual(job["error_detail"], {"name": name})
                self.exists.assert_not_called()
                self.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
