"""Completed-batch checks with bounded, disk-backed record expectations."""
from __future__ import annotations
from services import telemetry

import hashlib
import json
import logging
import math
import sqlite3
import struct
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from weaviate.classes.query import Filter
from config import settings

log = logging.getLogger(__name__)
LOOKUP_SIZE = 100


class BatchVerificationError(RuntimeError):
    """A completed read proved that stored records differ from expectations."""


class BatchCleanupError(RuntimeError):
    def __init__(self, original: Exception, cleanup: Exception):
        self.original = original
        self.cleanup = cleanup
        super().__init__(f"{type(original).__name__}: {original}; ingestion cleanup "
                         f"could not be confirmed ({type(cleanup).__name__}: {cleanup}). "
                         "Accepted chunks may remain; resolve cleanup before retrying.")


def _properties(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _properties(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_properties(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _record_properties(value: dict) -> dict:
    value = dict(value)
    if isinstance(value.get("created_at"), str):
        value["created_at"] = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    return _properties(value)


def _valid_vector(vector) -> bool:
    return isinstance(vector, list) and bool(vector) and all(
        isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
        for n in vector)


def _property_digest(properties: dict) -> bytes:
    return hashlib.sha256(json.dumps(_record_properties(properties), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).digest()


def _vector_digest(vector) -> bytes:
    if not _valid_vector(vector):
        raise ValueError("Missing or invalid vector")
    digest = hashlib.sha256()
    for number in vector:
        try:
            encoded = struct.pack("<f", number)
        except (OverflowError, struct.error) as exc:
            raise ValueError("Vector exceeds finite float32 storage") from exc
        if not math.isfinite(struct.unpack("<f", encoded)[0]):
            raise ValueError("Vector exceeds finite float32 storage")
        digest.update(encoded)
    return digest.digest()


def _factory(records):
    if callable(records):
        return records
    if iter(records) is records:
        raise ValueError("A one-shot iterator needs a reusable record factory")
    return lambda: iter(records)


class ExpectedRecords:
    """UUIDs and SHA-256 fingerprints on disk; no corpus vectors held in RAM.

    The SQLite cache is limited to 1 MiB. Capture/preflight and writing each
    stream records separately; transient verification state never changes a
    durable expectation snapshot used by restart cleanup.
    """
    def __init__(self):
        Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="batch-verify-", dir=settings.upload_dir)
        self.path = Path(self.temp.name) / "expected.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA cache_size=-1024")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA user_version=1")
        self.db.execute("CREATE TABLE expected (position INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, "
                        "properties BLOB NOT NULL, vector BLOB, seen INTEGER NOT NULL DEFAULT 0)")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        # Temporary index cleanup cannot turn a confirmed write into a failed
        # ingest after its rollback boundary has already passed.
        try:
            self.db.close()
            self.temp.cleanup()
        except (OSError, sqlite3.Error):
            log.exception("Could not remove temporary batch verification index %s", self.path)

    @property
    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM expected").fetchone()[0]

    def capture(self, records, expected_count=None, *, generated_only=False):
        if expected_count is not None and (type(expected_count) is not int or expected_count < 0):
            raise ValueError("Declared object count must be a nonnegative integer")
        for position, record in enumerate(_factory(records)()):
            if generated_only and "id" in record:
                raise ValueError("Owned ingestion cleanup requires newly generated UUIDs")
            key = str(uuid.UUID(str(record["id"]))) if "id" in record else str(uuid.uuid4())
            vector = _vector_digest(record["vector"]) if "vector" in record else None
            try:
                self.db.execute("INSERT INTO expected(position,id,properties,vector) VALUES (?,?,?,?)",
                                (position, key, _property_digest(record["properties"]), vector))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Duplicate chunk UUID {key}") from exc
        if expected_count is not None and self.count != expected_count:
            raise ValueError(f"Package contains {self.count} objects but declares {expected_count}")
        self.db.commit()

    def snapshot(self, destination: Path):
        with closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)

    def load_snapshot(self, source: Path, expected_count: int):
        with closing(sqlite3.connect("file:" + quote(str(source)) + "?mode=ro", uri=True)) as original:
            if original.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported expected-record snapshot")
            original.backup(self.db)
        self.db.execute("PRAGMA cache_size=-1024")
        if self.count != expected_count:
            raise ValueError("Expected-record snapshot count mismatch")
        for position, row in enumerate(self.db.execute(
                "SELECT position,id,properties,vector,seen FROM expected ORDER BY position")):
            index, key, properties, vector, seen = row
            if (index != position or str(uuid.UUID(key)) != key
                    or not isinstance(properties, bytes) or len(properties) != 32
                    or (vector is not None and (not isinstance(vector, bytes) or len(vector) != 32))
                    or seen != 0):
                raise ValueError("Invalid expected-record snapshot entry")

    def prepare(self, position, record):
        expected = self.db.execute("SELECT id,properties,vector FROM expected WHERE position=?",
                                   (position,)).fetchone()
        if expected is None:
            raise ValueError("Record stream changed after preflight")
        key, properties, vector = expected
        actual_id = str(uuid.UUID(str(record["id"]))) if "id" in record else key
        actual_vector = _vector_digest(record["vector"]) if "vector" in record else None
        if (actual_id != key or _property_digest(record["properties"]) != properties or actual_vector != vector):
            raise ValueError("Record stream changed after preflight")
        return {**record, "id": key}

    def id_batches(self):
        cursor = self.db.execute("SELECT id FROM expected ORDER BY position")
        while rows := cursor.fetchmany(LOOKUP_SIZE):
            yield [row[0] for row in rows]

    def verify(self, collection, *, exact: bool) -> int:
        self.db.execute("UPDATE expected SET seen=0")
        vectorless_allowed = None

        def stored_objects():
            if exact:
                yield from telemetry.iterate(collection.iterator, include_vector=True)
            else:
                for ids in self.id_batches():
                    yield from telemetry.call("weaviate.query", collection.query.fetch_objects,
                        filters=Filter.by_id().contains_any(ids), limit=len(ids), include_vector=True).objects

        for obj in stored_objects():
            key = str(obj.uuid)
            expected = self.db.execute("SELECT properties,vector,seen FROM expected WHERE id=?", (key,)).fetchone()
            if expected is None:
                if exact:
                    raise BatchVerificationError(f"Unexpected stored object {key}")
                continue
            properties, vector, seen = expected
            if seen:
                raise BatchVerificationError(f"Duplicate stored object {key}")
            if _property_digest(obj.properties or {}) != properties:
                raise BatchVerificationError(f"Stored properties differ for {key}")
            stored_vector = (obj.vector or {}).get("default")
            if stored_vector is None and vector is None:
                if vectorless_allowed is None:
                    config = getattr(collection, "config", None)
                    vectorizer = telemetry.call("weaviate.config", config.get).vectorizer if config is not None else None
                    vectorless_allowed = getattr(vectorizer, "value", None) == "none"
                if not vectorless_allowed:
                    raise BatchVerificationError(f"Stored vector is missing or invalid for {key}")
                actual_vector = None
            else:
                try:
                    actual_vector = _vector_digest(stored_vector)
                except ValueError as exc:
                    raise BatchVerificationError(f"Stored vector is missing or invalid for {key}") from exc
            if vector is not None and vector != actual_vector:
                raise BatchVerificationError(f"Stored float32 vector differs for {key}")
            self.db.execute("UPDATE expected SET seen=1 WHERE id=?", (key,))
        confirmed = self.db.execute("SELECT COUNT(*) FROM expected WHERE seen=1").fetchone()[0]
        if confirmed != self.count:
            raise BatchVerificationError(f"Confirmed {confirmed} of {self.count} expected objects")
        return confirmed

    def rollback(self, collection):
        for ids in self.id_batches():
            result = telemetry.call("weaviate.query", collection.data.delete_many, where=Filter.by_id().contains_any(ids))
            if result.failed:
                raise RuntimeError(f"Could not remove {result.failed} owned ingestion object(s)")
            remaining = telemetry.call("weaviate.query", collection.query.fetch_objects, filters=Filter.by_id().contains_any(ids), limit=len(ids))
            if remaining.objects:
                raise RuntimeError("Owned ingestion objects remain after cleanup")


def verify(collection, records, *, exact: bool) -> int:
    with ExpectedRecords() as expected:
        expected.capture(records)
        return expected.verify(collection, exact=exact)


def _record_counts(exc: Exception, submitted: int, accepted: int) -> None:
    """Attach what a failed insert sent and had acknowledged, for ingest's job
    counts (#302): `chunks_stored` only counts checked chunks."""
    try:
        exc.write_counts = (submitted, accepted)
    except Exception:                     # an exception type that refuses attributes
        pass


def write_counts(exc: BaseException | None) -> tuple[int, int] | None:
    """(submitted, accepted) recorded on a failed insert's exception or its cause."""
    for error in (exc, getattr(exc, "__cause__", None)):
        counts = getattr(error, "write_counts", None)
        if counts is not None:
            return counts
    return None


def insert(collection, records, *, exact: bool = True, expected_count: int | None = None,
           cleanup_owned: bool = False) -> int:
    """Preflight a reusable stream, flush, then compare persisted fingerprints.

    Only ingestion-generated UUIDs may be rolled back individually. Import and
    tuning own new collections and retain/remove them at their operation boundary.
    A failure carries `write_counts` = (records queued, records the flushed batch
    acknowledged without a per-object error); 0 acknowledged if it never flushed.
    """
    factory = _factory(records)
    with ExpectedRecords() as expected:
        expected.capture(factory, expected_count, generated_only=cleanup_owned)
        queued = accepted = 0
        try:
            with telemetry.span("weaviate.batch"):
                with collection.batch.dynamic() as batch:
                    for position, record in enumerate(factory()):
                        record = expected.prepare(position, record)
                        batch.add_object(properties=record["properties"], uuid=record["id"], vector=record.get("vector"))
                        queued += 1
                    if queued != expected.count:
                        raise ValueError("Record stream changed after preflight")
                failed = collection.batch.failed_objects
                accepted = queued - len(failed or ())
                if failed:
                    detail = getattr(failed[0], "message", None)
                    raise RuntimeError(
                        f"Weaviate rejected {len(failed)} batch object(s)"
                        + (f": {detail}" if detail else ""))
            return expected.verify(collection, exact=exact)
        except Exception as original:
            _record_counts(original, queued, accepted)
            if cleanup_owned:
                try:
                    expected.rollback(collection)
                except Exception as cleanup:
                    error = BatchCleanupError(original, cleanup)
                    _record_counts(error, queued, accepted)
                    raise error from original
            raise
