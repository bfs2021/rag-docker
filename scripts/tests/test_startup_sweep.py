"""Startup sweeps of a killed job's leftovers (issue #377).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_startup_sweep.py

An ingest killed mid-job left its upload staging (default `tmp` prefix) in
UPLOAD_DIR, and a killed export its `.build-*` staging directory and
`.<stem>.tar.gz.partial` archive in EXPORTS_DIR, for good. Disposable
directories only; nothing reaches a backend. The lifespan case runs in a child
process, so this process's module state stays untouched.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from config import settings
from services import importer, ingest_pipeline, packager

MB = 1024 * 1024


def write(path, size=0, text=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text)
    else:
        with open(path, "wb") as fh:
            fh.truncate(size)
    return path


class SweepTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.uploads = self.base / "uploads"
        self.exports = self.base / "exports"
        # Outside both swept folders: what a link points at must survive.
        self.outside = self.base / "outside"
        write(self.outside / "dir" / "keep.txt", text="keep")
        write(self.outside / "file.bin", size=3 * MB)
        for key, value in (("upload_dir", self.uploads), ("exports_dir", self.exports)):
            change = patch.object(settings, key, str(value))
            change.start()
            self.addCleanup(change.stop)

    def assert_outside_intact(self):
        self.assertEqual((self.outside / "dir" / "keep.txt").read_text(), "keep")
        self.assertEqual((self.outside / "file.bin").stat().st_size, 3 * MB)


class WorkdirSweepTests(SweepTestCase):
    def test_ingest_staging_and_other_work_directories_are_removed(self):
        write(self.uploads / "ingest-abc123" / "report.pdf", size=2 * MB)
        write(self.uploads / "ingest-abc123" / "archive.zip_members" / "a" / "b.txt", size=MB)
        write(self.uploads / "import-x1" / "pkg" / "manifest.json", text="{}")
        write(self.uploads / "rechunk-x2" / "doc.txt", text="text")
        write(self.uploads / "batch-verify-x3" / "rows.sqlite3", text="")
        removed = importer.sweep_stale_workdirs()
        self.assertEqual(removed, ["batch-verify-x3 (0 MB)", "import-x1 (0 MB)",
                                   "ingest-abc123 (3 MB)", "rechunk-x2 (0 MB)"])
        self.assertEqual(sorted(p.name for p in self.uploads.iterdir()), [])

    def test_only_matching_real_directories_are_removed(self):
        keep = [write(self.uploads / "ingest_configs" / "Docs.json", text="{}"),
                write(self.uploads / "jobs" / "ingest" / "abcd1234.json", text="{}"),
                write(self.uploads / "metrics.jsonl", text=""),
                write(self.uploads / "xingest-a" / "f.txt", text="x"),
                write(self.uploads / "staged-ingest-" / "f.txt", text="x"),
                write(self.uploads / "tmpk2j3h4" / "f.txt", text="x"),
                write(self.uploads / "ingest-file", text="a file, not a directory")]
        (self.uploads / "ingest-link").symlink_to(self.outside / "dir")
        (self.uploads / "import-link").symlink_to(self.outside / "dir")
        (self.uploads / "ingest-file-link").symlink_to(self.outside / "file.bin")
        self.assertEqual(importer.sweep_stale_workdirs(), [])
        for path in keep:
            self.assertTrue(path.exists(), path)
        for name in ("ingest-link", "import-link", "ingest-file-link"):
            self.assertTrue((self.uploads / name).is_symlink(), name)
        self.assert_outside_intact()

    def test_links_inside_a_removed_directory_are_not_followed(self):
        stage = self.uploads / "ingest-linked"
        write(stage / "upload.txt", size=2 * MB)
        (stage / "to-dir").symlink_to(self.outside / "dir")
        (stage / "to-file").symlink_to(self.outside / "file.bin")
        self.assertEqual(importer.sweep_stale_workdirs(), ["ingest-linked (2 MB)"])
        self.assertFalse(os.path.lexists(stage))
        self.assert_outside_intact()

    def test_a_failed_removal_is_logged_and_not_reported(self):
        write(self.uploads / "ingest-stuck" / "f.txt", text="x")
        with patch.object(packager.shutil, "rmtree", side_effect=PermissionError("denied")), \
                self.assertLogs(packager._log, "ERROR") as logs:
            self.assertEqual(importer.sweep_stale_workdirs(), [])
        self.assertTrue((self.uploads / "ingest-stuck" / "f.txt").exists())
        self.assertIn("Could not remove", logs.output[0])
        self.assertIn("ingest-stuck", logs.output[0])

    def test_a_missing_upload_dir_sweeps_nothing(self):
        self.assertEqual(importer.sweep_stale_workdirs(), [])
        self.assertFalse(self.uploads.exists())


class ExportSweepTests(SweepTestCase):
    def test_build_directories_and_partial_archives_are_removed(self):
        write(self.exports / ".build-k3j4h5" / "ragpkg-docs" / "chunks.jsonl", size=2 * MB)
        write(self.exports / ".build-k3j4h5" / "ragpkg-docs" / "models" / "blob", size=MB)
        write(self.exports / ".ragpkg-docs-20261021T120000Z-1a2b3c4d.tar.gz.partial", size=MB)
        removed = packager.sweep_build_leftovers()
        self.assertEqual(removed, [".build-k3j4h5 (3 MB)",
                                   ".ragpkg-docs-20261021T120000Z-1a2b3c4d.tar.gz.partial (1 MB)"])
        self.assertEqual(list(self.exports.iterdir()), [])

    def test_packages_and_everything_else_are_kept(self):
        package = write(self.exports / "ragpkg-docs-20261021T120000Z-1a2b3c4d.tar.gz", size=MB)
        keep = [package,
                write(self.exports / "notes.txt", text="mine"),
                write(self.exports / "build-x" / "f.txt", text="x"),
                write(self.exports / ".build" / "f.txt", text="x"),
                write(self.exports / "x.tar.gz.partial", text="not hidden"),
                write(self.exports / ".y.tar.gz.partial.bak", text="other suffix"),
                write(self.exports / ".ragpkg-docs.tar.gz", text="not partial"),
                write(self.exports / ".build-file", text="a file, not a directory"),
                write(self.exports / ".z.tar.gz.partial" / "f.txt", text="a directory, not a file"),
                write(self.exports / "sub" / ".build-q" / "f.txt", text="not at the top"),
                write(self.exports / "sub" / ".q.tar.gz.partial", text="not at the top")]
        (self.exports / ".build-link").symlink_to(self.outside / "dir")
        (self.exports / ".w.tar.gz.partial").symlink_to(self.outside / "file.bin")
        (self.exports / ".build-dangling").symlink_to(self.outside / "missing")
        self.assertEqual(packager.sweep_build_leftovers(), [])
        for path in keep:
            self.assertTrue(path.exists(), path)
        self.assertEqual(package.stat().st_size, MB)
        for name in (".build-link", ".w.tar.gz.partial", ".build-dangling"):
            self.assertTrue((self.exports / name).is_symlink(), name)
        self.assert_outside_intact()

    def test_links_inside_a_removed_build_are_not_followed(self):
        build = self.exports / ".build-linked"
        write(build / "ragpkg-docs" / "chunks.jsonl", size=MB)
        (build / "ragpkg-docs" / "to-dir").symlink_to(self.outside / "dir")
        (build / "to-file").symlink_to(self.outside / "file.bin")
        self.assertEqual(packager.sweep_build_leftovers(), [".build-linked (1 MB)"])
        self.assertFalse(os.path.lexists(build))
        self.assert_outside_intact()

    def test_a_failed_removal_is_logged_and_not_reported(self):
        partial = write(self.exports / ".ragpkg-docs.tar.gz.partial", text="x")
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == partial.name:
                raise PermissionError("denied")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink), self.assertLogs(packager._log, "ERROR") as logs:
            self.assertEqual(packager.sweep_build_leftovers(), [])
        self.assertTrue(partial.exists())
        self.assertIn("Could not remove", logs.output[0])

    def test_a_missing_exports_dir_is_not_created(self):
        self.assertEqual(packager.sweep_build_leftovers(), [])
        self.assertFalse(self.exports.exists())


class _Stop(Exception):
    pass


class IngestStagingTests(SweepTestCase):
    def test_ingest_staging_is_named_for_the_sweep(self):
        self.uploads.mkdir()
        made = []
        real = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            made.append(Path(real(*args, **kwargs)))
            raise _Stop()
        with patch.object(ingest_pipeline.tempfile, "mkdtemp", mkdtemp):
            with self.assertRaises(_Stop):
                asyncio.run(ingest_pipeline._admit_ingest_job(
                    "abcd1234", [], "Docs", "fixed", 300, 50, 0.85, 50))
        self.assertEqual(len(made), 1)
        stage = made[0]
        self.assertEqual(stage.parent, self.uploads)
        self.assertTrue(stage.name.startswith("ingest-"), stage.name)
        self.assertEqual(importer.sweep_stale_workdirs(), [f"{stage.name} (0 MB)"])
        self.assertFalse(stage.exists())


class LifespanTests(SweepTestCase):
    def test_lifespan_sweeps_both_folders_and_logs_what_it_removed(self):
        write(self.uploads / "ingest-k1" / "upload.txt", size=MB)
        write(self.exports / ".build-k2" / "f.txt", size=2 * MB)
        write(self.exports / ".ragpkg-docs-1.tar.gz.partial", size=MB)
        package = write(self.exports / "ragpkg-docs-1.tar.gz", size=MB)
        script = """
import asyncio, sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[1])
from main import app, lifespan
from services import importer, weaviate_client as wc
async def run():
    with patch.object(wc, 'sweep_staging', new=AsyncMock(return_value=[])), \\
         patch.object(importer, 'sweep_interrupted_imports', return_value=[]), \\
         patch.object(wc, 'close_client'):
        async with lifespan(app):
            pass
asyncio.run(run())
"""
        env = {**os.environ, "UPLOAD_DIR": str(self.uploads), "EXPORTS_DIR": str(self.exports),
               "SOURCES_DIR": str(self.base / "sources")}
        result = subprocess.run([sys.executable, "-c", script, API_DIR], capture_output=True,
                                text=True, timeout=120, cwd=API_DIR, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.uploads / "ingest-k1").exists())
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()), [package.name])
        self.assertIn("Removed 2 leftover(s) of an interrupted export from the exports folder: "
                      ".build-k2 (2 MB), .ragpkg-docs-1.tar.gz.partial (1 MB)", result.stderr)
        self.assertIn("ingest-k1 (1 MB)", result.stderr)

    def test_a_failing_export_sweep_is_logged_and_startup_carries_on(self):
        write(self.uploads / "ingest-k3" / "upload.txt", size=MB)
        build = write(self.exports / ".build-k4" / "f.txt", size=MB)
        script = """
import asyncio, sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[1])
from main import app, lifespan
from services import importer, packager, weaviate_client as wc
async def run():
    with patch.object(wc, 'sweep_staging', new=AsyncMock(return_value=[])), \\
         patch.object(importer, 'sweep_interrupted_imports', return_value=[]), \\
         patch.object(packager, 'sweep_build_leftovers', side_effect=OSError('exports unreadable')), \\
         patch.object(wc, 'close_client'):
        async with lifespan(app):
            print('SERVING')
asyncio.run(run())
"""
        env = {**os.environ, "UPLOAD_DIR": str(self.uploads), "EXPORTS_DIR": str(self.exports),
               "SOURCES_DIR": str(self.base / "sources")}
        result = subprocess.run([sys.executable, "-c", script, API_DIR], capture_output=True,
                                text=True, timeout=120, cwd=API_DIR, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SERVING", result.stdout)
        self.assertIn("Startup sweep of interrupted exports failed", result.stderr)
        self.assertIn("exports unreadable", result.stderr)
        # The work-directory sweep is independent of the export sweep.
        self.assertFalse((self.uploads / "ingest-k3").exists())
        self.assertTrue(build.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
