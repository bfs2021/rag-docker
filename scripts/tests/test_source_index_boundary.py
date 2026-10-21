"""Untrusted retained-source identities never select filesystem paths."""
import asyncio
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import importer, ingest_pipeline, packager, sources
from services.packager import PackageError


class SourceIndexBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_patch = patch.object(settings, "sources_dir", str(self.root / "retained"))
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.package = self.root / "package"
        (self.package / "sources").mkdir(parents=True)
        self.outside = self.root / "outside.txt"
        self.outside.write_text("controlled outside sentinel")

    def index(self, digest):
        return {"version": 1, "documents": {
            digest: {"filenames": ["document.txt"], "size": 10}}}

    def test_valid_content_addressed_source_round_trips(self):
        content = b"retained source"
        digest = hashlib.sha256(content).hexdigest()
        (self.package / "sources" / digest).write_bytes(content)
        (self.package / "sources" / "index.json").write_text(json.dumps(self.index(digest)))
        importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
        retained = sources.collection_dir("Valid")
        retained.mkdir(parents=True)
        (retained / digest).write_bytes(content)
        (retained / "index.json").write_text(json.dumps(self.index(digest)))
        self.assertEqual(sources.load_index("Valid")["documents"].keys(), {digest})
        self.assertEqual(sources.blob_path("Valid", digest).read_bytes(), content)

    def test_manifest_verified_blob_is_hashed_only_once(self):
        content = b"retained source"
        digest = hashlib.sha256(content).hexdigest()
        blob = self.package / "sources" / digest
        blob.write_bytes(content)
        (blob.parent / "index.json").write_text(json.dumps(self.index(digest)))
        manifest = {"fidelity": "with-sources", "files": {
            f"sources/{digest}": f"sha256:{digest}"}}
        with patch.object(packager, "sha256_file", wraps=packager.sha256_file) as hashed:
            packager.verify_digests(self.package, manifest)
            importer._validate_package_sources(self.package, manifest)
        hashed.assert_called_once_with(blob)

    def test_verified_manifest_cannot_bless_a_wrong_source_identity(self):
        content = b"other bytes authenticated by the manifest"
        digest = hashlib.sha256(b"claimed source").hexdigest()
        blob = self.package / "sources" / digest
        blob.write_bytes(content)
        (blob.parent / "index.json").write_text(json.dumps(self.index(digest)))
        manifest = {"fidelity": "with-sources", "files": {
            f"sources/{digest}": "sha256:" + hashlib.sha256(content).hexdigest()}}
        packager.verify_digests(self.package, manifest)
        with patch.object(packager, "sha256_file", side_effect=AssertionError("second hash")):
            with self.assertRaises(PackageError) as raised:
                importer._validate_package_sources(self.package, manifest)
        self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")

    def test_tampered_listed_blob_fails_before_source_validation_or_live_work(self):
        digest = hashlib.sha256(b"claimed source").hexdigest()
        (self.package / "sources" / digest).write_bytes(b"tampered")
        (self.package / "sources" / "index.json").write_text(json.dumps(self.index(digest)))
        manifest = {"fidelity": "with-sources", "files": {
            f"sources/{digest}": f"sha256:{digest}"}}
        job = {"status": "queued"}
        with patch.object(settings, "upload_dir", str(self.root)), \
             patch.object(importer, "_jobs", {"owned": job}), \
             patch.object(importer, "_active", {"owned.tar.gz"}), \
             patch.object(packager, "exports_dir", return_value=self.root), \
             patch.object(packager, "open_package", return_value=(self.package, manifest)), \
             patch.object(importer, "_validate_package_sources") as validate, \
             patch.object(importer, "_ensure_models") as models, \
             patch.object(importer.wc, "get_client") as backend:
            importer._run("owned", "owned.tar.gz", "replace")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "PACKAGE_CORRUPT")
        validate.assert_not_called()
        models.assert_not_called()
        backend.assert_not_called()

    def test_restore_ignores_unindexed_blobs_and_indexless_source_folders(self):
        real = b"retained source"
        digest = hashlib.sha256(real).hexdigest()
        future = b"future legitimate source"
        future_digest = hashlib.sha256(future).hexdigest()
        src = self.package / "sources"
        (src / digest).write_bytes(real)
        (src / future_digest).write_bytes(b"unindexed unrelated bytes")
        (src / "index.json").write_text(json.dumps(self.index(digest)))
        importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
        importer._restore_sidecars("Imported", self.package, "Original", [])
        dest = sources.collection_dir("Imported")
        self.assertEqual({p.name for p in dest.iterdir()}, {digest, "index.json"})
        self.assertEqual((dest / digest).read_bytes(), real)
        (src / "index.json").unlink()
        importer._validate_package_sources(self.package, {"fidelity": "chunks-only"})
        importer._restore_sidecars("Indexless", self.package, "Original", [])
        self.assertFalse(sources.collection_dir("Indexless").exists())

    def test_record_only_tuning_does_not_read_invalid_sources(self):
        from services import tuning
        for operation in ("reindex", "reembed"):
            with self.subTest(operation=operation), \
                 patch.object(tuning, "_jobs", {"owned": {}}), \
                 patch.object(tuning.sources, "has_sources", side_effect=AssertionError("sources read")), \
                 patch.object(tuning, "_existing_chunks", return_value=[]), \
                 patch.object(tuning, "_existing_records", return_value=[]), \
                 patch.object(tuning, "_rebuild", return_value=0), \
                 patch.object(tuning.goldstandard, "mark_stale", return_value=0):
                tuning._run("owned", "Invalid", operation, {})
                self.assertEqual(tuning._jobs["owned"]["status"], "completed")

    def test_import_rejects_paths_before_restoring_sources(self):
        for key in (str(self.outside), "../outside.txt", "a/b", "index.json"):
            with self.subTest(key=key):
                (self.package / "sources" / "index.json").write_text(json.dumps(self.index(key)))
                with self.assertRaises(PackageError) as raised:
                    importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
                self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")
                self.assertFalse(sources.collection_dir("Imported").exists())

    def test_import_job_rejects_index_before_backend_or_model_work(self):
        (self.package / "sources" / "index.json").write_text(
            json.dumps(self.index(str(self.outside))))
        check_embedding = Mock()
        ensure_models = Mock()
        backend = Mock()
        job = {"status": "queued"}
        with patch.object(settings, "upload_dir", str(self.root)), \
             patch.object(importer, "_jobs", {"owned": job}), \
             patch.object(importer, "_active", {"owned.tar.gz"}), \
             patch.object(importer.packager, "exports_dir", return_value=self.root), \
             patch.object(importer.packager, "open_package", return_value=(
                 self.package, {"fidelity": "with-sources"})), \
             patch.object(importer.packager, "verify_digests"), \
             patch.object(importer, "_check_embedding", check_embedding), \
             patch.object(importer, "_ensure_models", ensure_models), \
             patch.object(importer.wc, "get_client", backend):
            importer._run("owned", "owned.tar.gz", "replace")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "PACKAGE_CORRUPT")
        check_embedding.assert_not_called()
        ensure_models.assert_not_called()
        backend.assert_not_called()

    def test_export_rejects_outside_identity_and_link(self):
        retained = sources.collection_dir("Imported")
        retained.mkdir(parents=True)
        (retained / "index.json").write_text(json.dumps(self.index(str(self.outside))))
        with self.assertRaises(ValueError):
            sources.load_index("Imported")
        digest = hashlib.sha256(self.outside.read_bytes()).hexdigest()
        (retained / digest).symlink_to(self.outside)
        (retained / "index.json").write_text(json.dumps(self.index(digest)))
        with self.assertRaises(ValueError):
            sources.blob_path("Imported", digest)
        (retained / "index.json").unlink()
        (retained / "index.json").symlink_to(self.outside)
        with self.assertRaises(ValueError):
            sources.load_index("Imported")
        (retained / "index.json").unlink()
        (retained / "index.json").write_text(json.dumps(self.index(digest)))
        with patch.object(packager, "exports_dir", return_value=self.root), \
             patch.object(packager, "read_chunks", return_value=iter(())), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager.retrieval_config, "resolve", return_value=({}, True)), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]):
            with self.assertRaises(ValueError):
                packager.build("Imported")
        self.assertFalse(list(self.root.glob("*.tar.gz")))


    def test_validate_index_rejects_malformed_shapes(self):
        digest = "a" * 64
        good = {"filenames": ["document.txt"]}
        for bad in ([], {"documents": []}, {"documents": {digest: "entry"}},
                    {"documents": {digest: {"filenames": "document.txt"}}},
                    {"documents": {digest: {"filenames": [1]}}},
                    {"documents": {digest.upper(): good}},
                    {"documents": {digest + "\n": good}}):
            with self.subTest(index=bad):
                with self.assertRaises(ValueError):
                    sources.validate_index(bad)
        self.assertEqual(sources.validate_index({"documents": {digest: good}})["documents"].keys(),
                         {digest})

    def test_import_rejects_missing_or_mismatched_sources(self):
        bare = self.root / "bare"
        bare.mkdir()
        importer._validate_package_sources(bare, {"fidelity": "chunks-only"})
        with self.assertRaises(PackageError) as raised:
            importer._validate_package_sources(bare, {"fidelity": "with-sources"})
        self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")
        digest = hashlib.sha256(b"retained source").hexdigest()
        (self.package / "sources" / "index.json").write_text(json.dumps(self.index(digest)))
        for blob in (None, b"different bytes"):
            with self.subTest(blob=blob):
                path = self.package / "sources" / digest
                if blob is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(blob)
                with self.assertRaises(PackageError) as raised:
                    importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
                self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")

    def test_blob_path_refuses_linked_collection_directory(self):
        content = self.outside.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / digest).write_bytes(content)
        Path(settings.sources_dir).mkdir(parents=True)
        sources.collection_dir("Linked").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            sources.blob_path("Linked", digest)

    def test_export_omits_missing_blob_from_shipped_index(self):
        content = b"available source"
        available = hashlib.sha256(content).hexdigest()
        missing = hashlib.sha256(b"missing source").hexdigest()
        retained = sources.collection_dir("Exported")
        retained.mkdir(parents=True)
        (retained / available).write_bytes(content)
        (retained / "index.json").write_text(json.dumps({
            "version": 1, "documents": {
                available: {"filenames": ["available.txt"]},
                missing: {"filenames": ["missing.txt"]},
            }}))
        with patch.object(packager, "exports_dir", return_value=self.root), \
             patch.object(packager, "read_chunks", return_value=iter(())), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager.wc, "_meta_sync", return_value={}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager.retrieval_config, "resolve", return_value=({}, True)), \
             patch.object(packager.retrieval_config, "validate", return_value={}), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]):
            result = packager.build("Exported")
        self.assertEqual(result["source_document_count"], 1)
        self.assertTrue(any("missing on disk" in warning for warning in result["warnings"]))
        with tarfile.open(self.root / result["filename"]) as archive:
            names = archive.getnames()
            index_name = next(name for name in names if name.endswith("/sources/index.json"))
            index = json.load(archive.extractfile(index_name))
            self.assertEqual(set(index["documents"]), {available})
            self.assertTrue(any(name.endswith(f"/sources/{available}") for name in names))
            self.assertFalse(any(name.endswith(f"/sources/{missing}") for name in names))
        # The package that omitted the missing blob passes the import's own
        # checks 1-3 and the retained-source check (the PR's "remains importable").
        opened = self.root / "reopened"
        opened.mkdir()
        pkg, manifest = packager.open_package(self.root / result["filename"], opened)
        packager.verify_digests(pkg, manifest)
        self.assertEqual(manifest["fidelity"], "with-sources")
        importer._validate_package_sources(pkg, manifest)

    def test_tune_options_reports_invalid_index_as_typed_error(self):
        from routers import tuning as tuning_router
        retained = sources.collection_dir("Invalid")
        retained.mkdir(parents=True)
        (retained / "index.json").write_text(json.dumps(self.index("../outside.txt")))
        with patch.object(tuning_router.wc, "collection_exists", new_callable=AsyncMock,
                          return_value=True):
            response = asyncio.run(tuning_router.tune_options("Invalid"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["error"]["code"], "SOURCE_INDEX_INVALID")

    def test_ingest_keeps_stored_chunks_when_source_index_is_invalid(self):
        upload = self.root / "accepted.txt"
        upload.write_text("accepted text")
        job_id = "source-index-retention-error"
        job = {"status": "queued", "files_total": 1, "files_completed": 0,
               "files_failed": 0, "chunks_stored": 0, "errors": []}
        with patch.dict(ingest_pipeline._jobs, {job_id: job}, clear=True), \
             patch.object(ingest_pipeline, "_parse_file", return_value=("accepted text", [])), \
             patch.object(ingest_pipeline.wc, "_insert_chunks_sync") as insert, \
             patch.object(ingest_pipeline.wc, "_source_has_chunks_sync", return_value=False), \
             patch.object(ingest_pipeline.sources, "store", side_effect=ValueError("Invalid retained source index")):
            ingest_pipeline._process_job_sync(job_id, [upload], self.root,
                                              "Invalid", "fixed", 150, 0, 0.85, 0)
        insert.assert_called_once()
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"],
                          job["chunks_stored"]), ("completed", 1, 0, 1))


class ManifestFilesBoundaryTests(unittest.TestCase):
    """Check 3 never hashes a file outside the package (#371)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        chunks = b'{"id": "chunk"}\n'
        (self.package / "chunks.jsonl").write_bytes(chunks)
        self.chunks_digest = "sha256:" + hashlib.sha256(chunks).hexdigest()
        self.outside = self.root / "outside.txt"
        self.outside.write_bytes(b"controlled outside sentinel")
        self.outside_hex = hashlib.sha256(self.outside.read_bytes()).hexdigest()
        (self.package / "link").symlink_to(self.root, target_is_directory=True)

    def manifest(self, key, digest):
        return {"files": {"chunks.jsonl": self.chunks_digest, key: digest}}

    def assert_refused(self, manifest):
        with patch.object(packager, "sha256_file", wraps=packager.sha256_file) as hashed:
            with self.assertRaises(PackageError) as raised:
                packager.verify_digests(self.package, manifest)
        error = raised.exception
        self.assertEqual((error.code, error.detail), ("PACKAGE_CORRUPT", {"file": "manifest.json"}))
        self.assertNotIn(self.outside_hex, error.message + json.dumps(error.detail))
        self.assertNotIn("outside.txt", error.message + json.dumps(error.detail))
        hashed.assert_not_called()

    def test_valid_contained_keys_pass_and_mismatch_is_unchanged(self):
        (self.package / "sources").mkdir()
        (self.package / "sources" / "blob").write_bytes(b"blob")
        manifest = {"files": {"chunks.jsonl": self.chunks_digest,
                              "sources/blob": "sha256:" + hashlib.sha256(b"blob").hexdigest()}}
        with patch.object(packager, "sha256_file", wraps=packager.sha256_file) as hashed:
            packager.verify_digests(self.package, manifest)
        self.assertEqual(sorted(call.args[0] for call in hashed.call_args_list),
                         [self.package / "chunks.jsonl", self.package / "sources" / "blob"])
        wrong = "sha256:" + "0" * 64
        with self.assertRaises(PackageError) as raised:
            packager.verify_digests(self.package, {"files": {"chunks.jsonl": wrong}})
        self.assertEqual((raised.exception.code, raised.exception.detail), (
            "PACKAGE_CORRUPT",
            {"file": "chunks.jsonl", "expected": wrong, "actual": self.chunks_digest}))

    def test_outside_keys_are_refused_before_hashing(self):
        keys = (str(self.outside), "../outside.txt", "a/../../outside.txt", "link/outside.txt",
                "link")
        for key in keys:
            for digest in ("sha256:" + self.outside_hex, "sha256:" + "0" * 64):
                with self.subTest(key=key, digest=digest):
                    self.assert_refused(self.manifest(key, digest))

    def test_non_normalised_keys_are_refused(self):
        for key in ("", ".", "./chunks.jsonl", "a//b", "chunks.jsonl/", "a/./b",
                    "chunks.jsonl\x00"):
            with self.subTest(key=key):
                self.assert_refused(self.manifest(key, self.chunks_digest))

    def assert_missing(self, key):
        with patch.object(packager, "sha256_file", wraps=packager.sha256_file) as hashed:
            with self.assertRaises(PackageError) as raised:
                packager.verify_digests(self.package, self.manifest(key, "sha256:" + "0" * 64))
        error = raised.exception
        self.assertEqual((error.code, error.detail), ("PACKAGE_CORRUPT", {"file": key}))
        self.assertIn("listed in the manifest but missing", error.message)
        self.assertNotIn(self.package / key, [call.args[0] for call in hashed.call_args_list])

    def test_listed_missing_and_directory_keys_are_refused_as_missing(self):
        # Both keys pass the containment check, so they are refused naming the key
        # itself, not manifest.json, and never hashed.
        (self.package / "sources").mkdir()
        for key in ("sources/missing", "sources"):
            with self.subTest(key=key):
                self.assert_missing(key)

    def test_import_job_reports_no_outside_digest_and_touches_nothing(self):
        for key in (str(self.outside), "../outside.txt"):
            with self.subTest(key=key):
                job = {"status": "queued"}
                validate, check_embedding, ensure_models, backend = Mock(), Mock(), Mock(), Mock()
                with patch.object(settings, "upload_dir", str(self.root)), \
                     patch.object(importer, "_jobs", {"owned": job}), \
                     patch.object(importer, "_active", {"owned.tar.gz"}), \
                     patch.object(importer.packager, "exports_dir", return_value=self.root), \
                     patch.object(importer.packager, "open_package", return_value=(
                         self.package, self.manifest(key, "sha256:" + "0" * 64))), \
                     patch.object(importer, "_validate_package_sources", validate), \
                     patch.object(importer, "_check_embedding", check_embedding), \
                     patch.object(importer, "_ensure_models", ensure_models), \
                     patch.object(importer.wc, "get_client", backend):
                    importer._run("owned", "owned.tar.gz", "replace")
                self.assertEqual((job["status"], job["error_code"], job["error_detail"]),
                                 ("failed", "PACKAGE_CORRUPT", {"file": "manifest.json"}))
                self.assertNotIn(self.outside_hex, json.dumps(job))
                for seam in (validate, check_embedding, ensure_models, backend):
                    seam.assert_not_called()


if __name__ == "__main__":
    unittest.main()
