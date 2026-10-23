"""The package listing reads each manifest without decompressing the package (issue #383),
and lists a package whose manifest has wrongly typed fields as unreadable (issue #451).

Runs with the API's dependencies, in the built API image with no network
(`05_transfer.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_package_listing.py

Each test points the exports folder at a fresh temporary folder and writes its
packages there. `GET /packages` goes through `main.app` with FastAPI's TestClient,
without the lifespan; nothing reaches a backend.
"""
import contextlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from services import packager

ROOT = Path(__file__).resolve().parents[2]
STEM = "ragpkg-policies-20260918T153127Z-c5c39e72"
MANIFEST = {
    "package_format": 1,
    "created_at": "2026-09-18T15:31:27Z",
    "collection": {"name": "Policies", "chunk_count": 1842, "source_document_count": 0},
    "fidelity": "chunks-only",
    "files": {},
}
# What reading only the manifest may cost, in bytes read from the file. The
# packages below carry several MiB of incompressible data after it.
READ_BOUND = 1024 * 1024
# The manifest size bound, SPECIFICATIONS.md `GET /packages`.
MANIFEST_LIMIT = 16 * 1024 * 1024
NULLS = {"collection": None, "chunk_count": None, "fidelity": None, "created_at": None}


def write_archive(path, members):
    """Write a .tar.gz of (name, bytes) pairs in the given order; bytes None is a directory."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    return path


def manifest_first(path, after=b"", manifest=None):
    body = json.dumps(MANIFEST if manifest is None else manifest).encode()
    return write_archive(path, [(STEM, None), (f"{STEM}/manifest.json", body),
                                (f"{STEM}/chunks.jsonl", after)])


class CountingFile(io.FileIO):
    """A file that counts the bytes read from it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bytes_read = 0

    def read(self, size=-1):
        data = super().read(size)
        self.bytes_read += len(data or b"")
        return data

    def readall(self):
        data = super().readall()
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer):
        count = super().readinto(buffer)
        self.bytes_read += count or 0
        return count


@contextlib.contextmanager
def counting_reads():
    """Route every archive opened by path through a CountingFile; yields the files."""
    real_open = tarfile.open
    opened = []

    def counted(name=None, mode="r", fileobj=None, **kwargs):
        if name is not None and fileobj is None and mode.startswith("r"):
            fileobj = CountingFile(name, "rb")
            opened.append(fileobj)
        return real_open(name, mode, fileobj, **kwargs)

    with patch.object(tarfile, "open", counted):
        try:
            yield opened
        finally:
            for fh in opened:
                fh.close()


class ListingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exports = Path(self.tmp.name)
        patcher = patch.object(settings, "exports_dir", str(self.exports))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def listing(self):
        response = TestClient(app, raise_server_exceptions=False).get("/packages")
        self.assertEqual(response.status_code, 200, response.text)
        return {p["filename"]: p for p in response.json()["packages"]}

    def assert_readable(self, entry):
        self.assertEqual(entry["collection"], "Policies")
        self.assertEqual(entry["chunk_count"], 1842)
        self.assertEqual(entry["fidelity"], "chunks-only")
        self.assertEqual(entry["created_at"], "2026-09-18T15:31:27Z")
        self.assertTrue(entry["readable"])

    def test_package_cut_off_after_its_manifest_still_lists(self):
        path = manifest_first(self.exports / f"{STEM}.tar.gz", after=os.urandom(1024 * 1024))
        with path.open("r+b") as fh:
            fh.truncate(path.stat().st_size // 2)
        self.assertEqual(packager.read_manifest(path), MANIFEST)
        self.assert_readable(self.listing()[path.name])

    def test_listing_reads_only_up_to_the_manifest(self):
        path = manifest_first(self.exports / f"{STEM}.tar.gz", after=os.urandom(8 * 1024 * 1024))
        self.assertGreater(path.stat().st_size, 8 * 1024 * 1024)
        with counting_reads() as opened:
            self.assertEqual(packager.read_manifest(path), MANIFEST)
        self.assertEqual(len(opened), 1)
        self.assertLess(opened[0].bytes_read, READ_BOUND)

    def test_oversized_manifest_is_unreadable_and_not_read(self):
        # Valid JSON, but over the bound; random hex keeps the file several MiB.
        big = dict(MANIFEST, padding=os.urandom(MANIFEST_LIMIT // 2).hex())
        path = manifest_first(self.exports / f"{STEM}.tar.gz", manifest=big)
        with counting_reads() as opened:
            with self.assertRaises(ValueError):
                packager.read_manifest(path)
        self.assertLess(opened[0].bytes_read, READ_BOUND)
        self.assertEqual(self.listing()[path.name], dict(
            filename=path.name, size_bytes=path.stat().st_size, readable=False, **NULLS))

    def test_manifest_size_bound_is_inclusive(self):
        # Boundary of the bound: exactly MANIFEST_LIMIT bytes lists, one byte more does not.
        base = len(json.dumps(dict(MANIFEST, padding="")).encode())
        at_limit = dict(MANIFEST, padding="a" * (MANIFEST_LIMIT - base))
        self.assertEqual(len(json.dumps(at_limit).encode()), MANIFEST_LIMIT)
        path = manifest_first(self.exports / f"{STEM}.tar.gz", manifest=at_limit)
        self.assertEqual(packager.read_manifest(path), at_limit)
        self.assert_readable(self.listing()[path.name])
        over = dict(MANIFEST, padding="a" * (MANIFEST_LIMIT - base + 1))
        path = manifest_first(self.exports / f"{STEM}.tar.gz", manifest=over)
        with self.assertRaises(ValueError):
            packager.read_manifest(path)
        self.assertFalse(self.listing()[path.name]["readable"])

    def test_unreadable_files_are_listed_readable_false(self):
        good = manifest_first(self.exports / f"{STEM}.tar.gz")
        bad = {
            "not-gzip": lambda p: p.write_bytes(b"this is not a package"),
            "empty": lambda p: p.write_bytes(b""),
            "no-manifest": lambda p: write_archive(p, [("x", None), ("x/chunks.jsonl", b"{}\n")]),
            "nested-manifest": lambda p: write_archive(
                p, [("x", None), ("x/sub", None), ("x/sub/manifest.json", json.dumps(MANIFEST).encode())]),
            "invalid-json": lambda p: write_archive(p, [("x", None), ("x/manifest.json", b'{"package_format": 1,')]),
            "not-utf8": lambda p: write_archive(p, [("x", None), ("x/manifest.json", b'{"a": "\xff\xfe"}')]),
            "json-array": lambda p: write_archive(p, [("x", None), ("x/manifest.json", b'["Policies"]')]),
        }
        for name, make in bad.items():
            make(self.exports / f"ragpkg-{name}.tar.gz")
        listed = self.listing()
        self.assertEqual(set(listed), {good.name} | {f"ragpkg-{n}.tar.gz" for n in bad})
        self.assert_readable(listed[good.name])
        for name in bad:
            path = self.exports / f"ragpkg-{name}.tar.gz"
            with self.subTest(file=name):
                self.assertEqual(listed[path.name], dict(
                    filename=path.name, size_bytes=path.stat().st_size, readable=False, **NULLS))

    def test_wrongly_typed_manifest_fields_are_listed_unreadable(self):
        # Each case replaces one listed value with a wrong JSON type (#451).
        good = manifest_first(self.exports / f"{STEM}.tar.gz")
        collection = MANIFEST["collection"]
        bad = {
            "collection-string": dict(MANIFEST, collection="Policies"),
            "name-number": dict(MANIFEST, collection=dict(collection, name=5)),
            "count-string": dict(MANIFEST, collection=dict(collection, chunk_count="12")),
            "count-fraction": dict(MANIFEST, collection=dict(collection, chunk_count=1.5)),
            "count-float": dict(MANIFEST, collection=dict(collection, chunk_count=12.0)),
            "count-boolean": dict(MANIFEST, collection=dict(collection, chunk_count=True)),
            "fidelity-number": dict(MANIFEST, fidelity=1),
            "created-at-number": dict(MANIFEST, created_at=20260918),
        }
        for name, manifest in bad.items():
            manifest_first(self.exports / f"ragpkg-{name}.tar.gz", manifest=manifest)
        listed = self.listing()
        self.assertEqual(set(listed), {good.name} | {f"ragpkg-{n}.tar.gz" for n in bad})
        self.assert_readable(listed[good.name])
        for name in bad:
            path = self.exports / f"ragpkg-{name}.tar.gz"
            with self.subTest(file=name):
                self.assertEqual(listed[path.name], dict(
                    filename=path.name, size_bytes=path.stat().st_size, readable=False, **NULLS))

    def test_null_or_absent_fields_list_as_null(self):
        cases = {
            "collection-null": dict(MANIFEST, collection=None, fidelity=None, created_at=None),
            "fields-absent": {"package_format": 1, "files": {}},
        }
        for name, manifest in cases.items():
            manifest_first(self.exports / f"ragpkg-{name}.tar.gz", manifest=manifest)
        listed = self.listing()
        for name in cases:
            path = self.exports / f"ragpkg-{name}.tar.gz"
            with self.subTest(file=name):
                self.assertEqual(listed[path.name], dict(
                    filename=path.name, size_bytes=path.stat().st_size, readable=True, **NULLS))

    def test_new_archives_put_the_manifest_first(self):
        stage = Path(self.tmp.name) / "stage"
        (stage / "goldstandard").mkdir(parents=True)
        (stage / "sources").mkdir()
        files = {
            "README.md": b"readme", "chunks.jsonl": b"{}\n", "collection.json": b"{}",
            "manifest.json": json.dumps(MANIFEST).encode(), "retrieval_config.json": b"{}",
            "goldstandard/s1.json": b"{}", "sources/index.json": b"{}", "sources/abc": b"raw",
        }
        for rel, data in files.items():
            (stage / rel).write_bytes(data)
        new = Path(self.tmp.name) / "new.tar.gz"
        packager._write_archive(stage, STEM, new)
        old = Path(self.tmp.name) / "old.tar.gz"
        with tarfile.open(old, "w:gz") as tar:
            tar.add(stage, arcname=STEM)
        with tarfile.open(new, "r:gz") as tar:
            names = tar.getnames()
            contents = {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
        with tarfile.open(old, "r:gz") as tar:
            old_names = tar.getnames()
        manifest = f"{STEM}/manifest.json"
        self.assertEqual(names[:2], [STEM, manifest])
        self.assertEqual(names[2:], [n for n in old_names if n not in (STEM, manifest)])
        self.assertEqual(contents, {f"{STEM}/{rel}": data for rel, data in files.items()})

    def test_older_member_order_still_lists(self):
        # Before #383, chunks.jsonl (and README.md, collection.json) came first.
        path = write_archive(self.exports / f"{STEM}.tar.gz", [
            (STEM, None), (f"{STEM}/README.md", b"readme"),
            (f"{STEM}/chunks.jsonl", os.urandom(256 * 1024)),
            (f"{STEM}/collection.json", b"{}"),
            (f"{STEM}/manifest.json", json.dumps(MANIFEST).encode()),
            (f"{STEM}/retrieval_config.json", b"{}")])
        self.assertEqual(packager.read_manifest(path), MANIFEST)
        self.assert_readable(self.listing()[path.name])

    def test_embedded_copy_matches(self):
        document = (ROOT / "IMPLEMENTATION.md").read_text()
        for name in ("api/services/packager.py", "api/routers/transfer.py"):
            with self.subTest(file=name):
                header = "### " + name + "\n\n```"
                start = document.index("\n", document.index(header) + len(header)) + 1
                end = document.index("\n```\n", start)
                self.assertEqual(document[start:end], (ROOT / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
