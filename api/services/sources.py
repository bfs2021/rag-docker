"""Retention of original uploaded documents.

Ingest previously parsed uploads and deleted them, so only chunked text survived.
Export, re-chunking and re-embedding all need the originals, so accepted files
are copied here instead.

Files are content-addressed: the SHA-256 of the raw bytes is the storage key, so
re-ingesting the same document stores one copy and records the extra logical
name. Storage is proportional to the corpus rather than to upload count.

A logical name belongs to one digest at a time (#22): ingest stores with
replace=True, so a name ingested with new content leaves the digest it had, and a
digest left with no names is removed with its blob.
"""
from __future__ import annotations
from services import telemetry

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"
INDEX_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def validate_index(index: dict) -> dict:
    """Refuse source identities that could address anything but a stored blob."""
    if not isinstance(index, dict) or not isinstance(index.get("documents"), dict):
        raise ValueError("Invalid retained source index")
    for digest, entry in index["documents"].items():
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid retained source digest")
        if not isinstance(entry, dict):
            raise ValueError("Invalid retained source entry")
        names = entry.get("filenames")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Invalid retained source filenames")
    return index


def blob_path(collection: str, digest: str) -> Path:
    """Return a retained blob path only when it cannot escape its collection."""
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid retained source digest")
    directory = collection_dir(collection)
    if directory.is_symlink():
        raise ValueError("Retained source directory is a link")
    blob = directory / digest
    if blob.is_symlink():
        raise ValueError("Retained source blob is a link")
    return blob


def _root() -> Path:
    return Path(settings.sources_dir)


def collection_dir(collection: str) -> Path:
    return _root() / collection


def _index_path(collection: str) -> Path:
    return collection_dir(collection) / INDEX_NAME


def load_index(collection: str) -> dict:
    p = _index_path(collection)
    if p.is_symlink():
        raise ValueError("Retained source index is a link")
    if not p.exists():
        return {"version": INDEX_VERSION, "documents": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("Unreadable source index for %r; treating as empty", collection)
        return {"version": INDEX_VERSION, "documents": {}}
    if not isinstance(data, dict):
        raise ValueError("Invalid retained source index")
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("documents", {})
    return validate_index(data)


def _save_index(collection: str, index: dict) -> None:
    p = _index_path(collection)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file in the same directory so a crash cannot truncate an
    # existing index.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True))
    tmp.replace(p)


def restore_package(package: Path, collection: str) -> None:
    """Copy only preflighted indexed originals, including into recovery copies.

    Import preflight has checked the private extraction's index and blob bytes.
    Unindexed files must never become retained originals under a future digest.
    """
    src = package / "sources"
    if not (src / INDEX_NAME).is_file():
        return
    index = validate_index(json.loads((src / INDEX_NAME).read_text()))
    dest = collection_dir(collection)
    dest.mkdir(parents=True, exist_ok=True)
    for digest in index["documents"]:
        shutil.copyfile(src / digest, dest / digest)
    shutil.copyfile(src / INDEX_NAME, dest / INDEX_NAME)


def _drop_blobs(collection: str, digests: list[str]) -> None:
    """Delete blobs the index no longer lists. Called after the index is saved,
    so it never names a missing blob; a blob left behind is only unused space."""
    for digest in digests:
        try:
            blob_path(collection, digest).unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            log.error("Could not delete unreferenced source %s of %r: %s",
                      digest[:12], collection, exc)


@telemetry.traced("rag.retain")
def store(collection: str, filename: str, data: bytes, media_type: str | None = None,
          *, replace: bool = False) -> str:
    """Retain one accepted upload. Returns its sha256.

    With replace=True (ingest, #22) the filename is first released from every
    other digest, and digests left with no names are dropped. That happens even
    when writing the new blob fails, so the name never keeps claiming the
    version it replaced, which a rechunk would otherwise rebuild.
    """
    digest = hashlib.sha256(data).hexdigest()
    target = collection_dir(collection) / digest
    target.parent.mkdir(parents=True, exist_ok=True)

    index = load_index(collection)
    orphans: list[str] = []
    if replace:
        for other, entry in list(index["documents"].items()):
            if other != digest and filename in entry["filenames"]:
                entry["filenames"] = [n for n in entry["filenames"] if n != filename]
                if not entry["filenames"]:
                    del index["documents"][other]
                    orphans.append(other)

    try:
        if not target.exists():
            tmp = target.with_name(digest + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
    except BaseException:
        if replace:
            _save_index(collection, index)
            _drop_blobs(collection, orphans)
        raise

    now = datetime.now(timezone.utc).isoformat()
    entry = index["documents"].get(digest)
    if entry is None:
        index["documents"][digest] = {
            "filenames": [filename],
            "size": len(data),
            "media_type": media_type,
            "first_seen": now,
            "last_seen": now,
        }
    else:
        if filename not in entry["filenames"]:
            entry["filenames"].append(filename)
        entry["last_seen"] = now
    _save_index(collection, index)
    _drop_blobs(collection, orphans)
    return digest


def digests_for_filename(collection: str, filename: str) -> list[str]:
    """All digests ever stored under this logical filename.

    Returns more than one when the same name was ingested with different
    content. Callers must decide what that means rather than assume one.
    """
    index = load_index(collection)
    return [d for d, e in index["documents"].items() if filename in e.get("filenames", [])]


def has_sources(collection: str) -> bool:
    """True when the collection has retained originals (export fidelity)."""
    return bool(load_index(collection)["documents"])


def stats(collection: str) -> dict:
    docs = load_index(collection)["documents"]
    return {
        "document_count": len(docs),
        "total_bytes": sum(e.get("size", 0) for e in docs.values()),
    }


def delete(collection: str) -> None:
    """Remove every retained source for a collection.

    Called when the collection is deleted. Without this the volume leaks
    silently: it is surfaced nowhere in the UI.
    """
    shutil.rmtree(collection_dir(collection), ignore_errors=True)
