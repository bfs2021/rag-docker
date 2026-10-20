"""Re-chunking, re-embedding and re-indexing a collection in place.

Spec §7. Every operation here rebuilds the collection, because chunk identity
or vector width changes and Weaviate cannot alter either in place.

**Safety.** Each rebuild is staged: the new chunks are built into a temporary
collection first, and the live one is replaced only once that succeeds. Weaviate
has no rename (see `importer.py`), so the final step copies vectors out of the
staging collection rather than re-embedding — one embedding pass, not two. A
failure before replacement leaves the original untouched. After replacement
starts, a verified recovery copy and its sidecars survive failure and restart.
Identity-changing operations flag retained evaluations before replacement;
failed reindex cutovers flag them when exact identity cannot be verified.

**Gold standard.** Anything that changes chunk identity marks every session for
the collection `stale`, with a reason and a timestamp. Sessions are never
deleted and never remapped (spec §7.3).
"""
from __future__ import annotations
from services import telemetry

import asyncio
import copy
import logging
import math
import mimetypes
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from services import collection_writes, jobstore
from config import settings
from services import goldstandard
from services import sources
from services import weaviate_client as wc
from services import batch_write, collection_recovery
from services.chunker import chunk as do_chunk
from services.ingest_pipeline import _parse_file, source_type_of
from services.packager import PackageError

_log = logging.getLogger(__name__)

# Job records survive an API restart (#23); see jobstore.py.
_jobs = jobstore.JobStore("tune")
_active: set[str] = set()
_lock = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


# ── Reading the collection's own chunks ───────────────────────────────────────

def _existing_chunks(collection: str) -> list[dict]:
    """Stored properties, without vectors. Used when re-embedding chunk text."""
    col = wc.get_client().collections.get(collection)
    return [dict(o.properties or {}) for o in telemetry.iterate(col.iterator)]


def _existing_records(collection: str) -> list[dict]:
    """Read the supported single-vector corpus without regenerating identity."""
    return list(_iter_existing_records(collection))


def _iter_existing_records(collection: str):
    seen = set()
    col = wc.get_client().collections.get(collection)
    for obj in telemetry.iterate(col.iterator, include_vector=True):
        identity = str(obj.uuid)
        vector = obj.vector
        if isinstance(vector, dict):
            if set(vector) != {"default"}:
                raise RuntimeError("Reindex requires the collection's single default vector")
            vector = vector["default"]
        if (not isinstance(vector, list) or not vector
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in vector)):
            raise RuntimeError(f"Reindex cannot preserve the stored vector for {identity}")
        if identity in seen:
            raise RuntimeError(f"Reindex received duplicate stored UUID {identity}")
        seen.add(identity)
        yield {"id": identity, "vector": copy.deepcopy(vector),
               "properties": copy.deepcopy(dict(obj.properties or {}))}


def _write_records(collection: str, records: list[dict]) -> None:
    """Supply exact records, drain the batch, then compare backend readback."""
    col = wc.get_client().collections.get(collection)
    with telemetry.span("weaviate.batch"):
        with col.batch.dynamic() as batch:
            for record in records:
                batch.add_object(properties=copy.deepcopy(record["properties"]),
                                 uuid=record["id"], vector=copy.deepcopy(record["vector"]))
        if batch.number_errors:
            raise RuntimeError(f"{batch.number_errors} error(s) copying reindex records")
    _verify_records(collection, records)


def _verify_records(collection: str, records: list[dict]) -> None:
    expected = {record["id"]: record for record in records}
    seen = set()
    for record in _iter_existing_records(collection):
        if record != expected.get(record["id"]):
            raise RuntimeError("Reindex backend readback changed UUIDs, properties or vectors")
        seen.add(record["id"])
    if seen != expected.keys():
        raise RuntimeError("Reindex backend readback changed UUIDs, properties or vectors")


_MISSING_CHUNK_INDEX = object()


def _source_coverage(collection: str, documents: dict) -> tuple[list[str], set[str]]:
    """The stored source files a rebuild from the retained originals would not
    reproduce, and every name that has stored chunks, from one walk of the
    collection. A rebuild emits only the stored names (#311)."""
    by_filename: dict[str, list[str]] = {}
    for digest, entry in documents.items():
        for filename in entry["filenames"]:
            by_filename.setdefault(filename, []).append(digest)
    seen: dict[str, set] = {}
    uncovered = set()
    stored = set()
    for obj in telemetry.iterate(wc.get_client().collections.get(collection).iterator):
        props = obj.properties or {}
        filename = props.get("source_file")
        if not isinstance(filename, str) or not filename:
            uncovered.add("<unknown>")
            continue
        stored.add(filename)
        # A rebuild emits one chunk set per (digest, name) (#22), so a name is
        # covered when exactly one digest lists it, whichever of its names it is.
        if len(by_filename.get(filename, [])) != 1:
            uncovered.add(filename)
            continue
        index = props.get("chunk_index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = _MISSING_CHUNK_INDEX
        indices = seen.setdefault(filename, set())
        if index in indices:
            uncovered.add(filename)
        indices.add(index)
    return sorted(uncovered), stored


def _uncovered_source_files(collection: str, documents: dict) -> list[str]:
    """Stored source files a rebuild from the retained originals would not reproduce."""
    return _source_coverage(collection, documents)[0]


def _listed_names(documents: dict) -> set[str]:
    return {filename for entry in documents.values() for filename in entry["filenames"]}


class ServiceUnavailableError(Exception):
    """Weaviate couldn't be read for the up-front re-chunk check (#340) or for
    the tuning options (#349)."""


# A collection whose retained originals all lack stored chunks (every file
# produced none at ingest) has nothing to rebuild, since a rebuild emits only
# stored names (#311). It isn't offered re-chunking, and a request is refused
# before any job runs (#328).
def _nothing_to_rebuild(collection: str) -> PackageError:
    return PackageError(
        "SOURCES_REQUIRED",
        f"Cannot re-chunk '{collection}': no retained original file has stored "
        "chunks, so there is nothing to rebuild.",
        {"collection": collection})


def _has_stored_listed_name(collection: str, documents: dict) -> bool:
    """Whether any name the index lists has stored chunks; stops at the first."""
    names = _listed_names(documents)
    for obj in wc.get_client().collections.get(collection).iterator():
        if (obj.properties or {}).get("source_file") in names:
            return True
    return False


def _refuse_nothing_to_rebuild(collection: str) -> None:
    """The one re-chunk refusal made before a job is created (#328).

    The other refusals, and an invalid index, are still reported by the job.
    A Weaviate failure during the walk raises ServiceUnavailableError, which
    the router answers with 503 rather than a bare 500 (#340).
    """
    try:
        documents = sources.load_index(collection)["documents"]
    except ValueError:
        return
    if not documents:
        return
    try:
        stored = _has_stored_listed_name(collection, documents)
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Could not read %r for the re-chunk check", collection)
        raise ServiceUnavailableError(collection) from exc
    if not stored:
        raise _nothing_to_rebuild(collection)


# Why the tuning options don't offer re-chunking; see rechunk_refusal.
RECHUNK_NOTHING_TO_REBUILD = "nothing-to-rebuild"
RECHUNK_NOT_COVERED = "not-covered"


def rechunk_refusal(collection: str) -> str | None:
    """Why re-chunking would fail the checks made before parsing, or None.

    Used by the tuning options, so they offer re-chunking only when the job
    would accept it. Nothing to rebuild is reported first, as the start refuses
    it before the others (#328). An invalid index or blob path raises ValueError.
    A Weaviate failure during the walk raises ServiceUnavailableError, which
    the router answers with 503 rather than a bare 500 (#349).
    """
    documents = sources.load_index(collection)["documents"]
    if not documents:
        return RECHUNK_NOT_COVERED
    try:
        uncovered, stored = _source_coverage(collection, documents)
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Could not read %r for the tuning options", collection)
        raise ServiceUnavailableError(collection) from exc
    if not stored & _listed_names(documents):
        return RECHUNK_NOTHING_TO_REBUILD
    for digest in documents:
        if not sources.blob_path(collection, digest).is_file():
            return RECHUNK_NOT_COVERED
    return RECHUNK_NOT_COVERED if uncovered else None


@telemetry.traced("rag.chunk")
def _chunks_from_sources(collection: str, strategy: str, chunk_size: int,
                         chunk_overlap: int, similarity_threshold: float,
                         min_chunk_size: int) -> list[dict]:
    """Re-parse and re-chunk every retained original.

    Everything is parsed before the collection is touched: parsing is the
    failure-prone step, and discovering a bad file after the rebuild has started
    would cost the collection.
    """
    index = sources.load_index(collection)
    documents = index.get("documents") or {}
    if not documents:
        raise PackageError(
            "SOURCES_REQUIRED",
            f"Collection '{collection}' has no retained source documents, so it "
            "cannot be re-chunked. Only collections ingested after source "
            "retention was added carry their originals; export shows this as "
            "fidelity 'chunks-only'.",
            {"collection": collection})

    # Re-chunking rebuilds one chunk set per (digest, name) pair, as ingest
    # stored it (#22), so a second name for the same content is rebuilt too. A
    # stored name is covered only when exactly one digest lists it and its
    # chunks form one set (no repeated chunk_index). A name with more than one
    # digest (re-uploaded before #22 made re-ingesting replace), duplicate chunk
    # sets and a name with no retained original are refused before staging:
    # replacing from the retained subset would lose chunks, and a successful
    # cutover discards their recovery copy. Only names with stored chunks are
    # rebuilt (#311): an index can list a name the collection never showed (a
    # file that produced no chunks, or an imported package listing one blob
    # under many names), and rebuilding it would cost a parse and an embedding
    # each time. With no listed name stored there is nothing to rebuild, which
    # is refused as such rather than as a chunking problem (#328).
    uncovered, stored = _source_coverage(collection, documents)
    if not stored & _listed_names(documents):
        raise _nothing_to_rebuild(collection)
    if uncovered:
        raise PackageError(
            "SOURCES_REQUIRED",
            "Cannot change chunk boundaries: some stored chunks have missing or "
            "ambiguous retained originals. Re-embed without chunking parameters "
            "or re-index to preserve the existing chunks.",
            {"collection": collection, "uncovered_source_files": uncovered})

    out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    work = Path(tempfile.mkdtemp(prefix="rechunk-", dir=settings.upload_dir))
    try:
        slots = 0
        for digest, entry in sorted(documents.items()):
            blob = sources.blob_path(collection, digest)
            if not blob.is_file():
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Retained source {digest[:12]} is missing from disk, so "
                    f"'{collection}' cannot be rebuilt from its originals.",
                    {"collection": collection, "digest": digest})
            for filename in entry["filenames"]:
                if filename not in stored:
                    continue
                # Parsers dispatch on the file extension, so the original name
                # has to be restored before parsing; each pair is staged in a
                # directory of its own, so a/report.txt and b/report.txt can't
                # collide.
                slots += 1
                slot = work / str(slots)
                slot.mkdir()
                staged = slot / PurePosixPath(filename).name
                shutil.copyfile(blob, staged)

                text, elements = _parse_file(staged)
                chunks = do_chunk(
                    text=text,
                    strategy=strategy,
                    chunk_size=chunk_size,
                    chunk_overlap_size=chunk_overlap,
                    similarity_threshold=similarity_threshold,
                    min_chunk_size=min_chunk_size,
                    elements=elements if strategy == "context_aware" else None,
                )
                source_type = source_type_of(staged)
                out.extend({
                    "content": c,
                    "source_file": filename,
                    "source_type": source_type,
                    "chunk_index": i,
                    "chunk_strategy": strategy,
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                    "created_at": now,
                } for i, c in enumerate(chunks))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not out:
        raise PackageError(
            "SOURCES_REQUIRED",
            f"Re-chunking '{collection}' produced no chunks; check the chunking "
            "parameters.", {"collection": collection})
    return out


# ── Rebuilding ────────────────────────────────────────────────────────────────

@telemetry.traced("rag.rebuild")
@collection_writes.serialized("collection")
def _rebuild(collection: str, properties: list[dict], index_type: str | None,
             distance_metric: str | None, progress, *, records: list[dict] | None = None,
             source_collection: str | None = None, before_replace=None) -> int:
    """Stage and verify, then replace the live collection under its writer guard."""
    source_collection = source_collection or collection
    collection = collection_writes.canonical(collection)
    if records is not None:
        wc._validate_reindex_vectorizer_sync(collection)
    config = wc._collection_config_sync(collection)
    new_index = index_type or config["index_type"]
    new_distance = distance_metric or config["distance_metric"]
    hnsw = config.get("hnsw_config") or {}
    client = wc.get_client()
    ownership = collection_recovery.begin(collection, "tune", client)
    staging = ownership["staging"]
    cutover_started = False
    completed = False
    original_intact = False
    try:
        wc._create_collection_sync(staging, new_index, new_distance, hnsw, preserve_hnsw=True)
        if records is not None:
            _write_records(staging, records)
            # The guard covers application writers; independently connected
            # backend writers are detected by comparing the source again.
            _verify_records(collection, records)
        else:
            wc._insert_chunks_sync(staging, properties)
            staged_count = telemetry.call("weaviate.aggregate", client.collections.get(staging).aggregate.over_all, total_count=True).total_count
            if staged_count != len(properties):
                raise RuntimeError(f"staged {staged_count} chunks but expected {len(properties)}")
        if source_collection != collection:
            collection_recovery.retain(ownership, source_collection=source_collection)
        else:
            collection_recovery.retain(ownership)
        if before_replace:
            before_replace()
        collection_recovery.begin_cutover(ownership)
        cutover_started = True
        telemetry.call("weaviate.delete", client.collections.delete, collection)
        wc._create_collection_sync(collection, new_index, new_distance, hnsw, preserve_hnsw=True,
                                   description=collection_recovery.cutover_description(ownership))
        if records is not None:
            _write_records(collection, records)
            written = len(records)
        else:
            def staged():
                return (
                    {"id": str(obj.uuid), "vector": (obj.vector or {}).get("default"),
                     "properties": dict(obj.properties or {})}
                    for obj in telemetry.iterate(client.collections.get(staging).iterator, include_vector=True)
                )
            written = batch_write.insert(client.collections.get(collection), staged,
                                         expected_count=len(properties))
        if progress:
            progress(written)
        completed = True
        return written
    except Exception as exc:
        if records is not None and cutover_started:
            try:
                _verify_records(collection, records)
                original_intact = wc._collection_config_sync(collection) == config
            except Exception:
                original_intact = False
            if not original_intact:
                try:
                    goldstandard.mark_stale(source_collection, "reindex replacement failed after cutover began; exact record preservation was not verified")
                except Exception:
                    _log.exception("Could not mark evaluation historical after failed reindex")
        elif cutover_started:
            try:
                goldstandard.mark_stale(source_collection, "collection replacement failed after cutover began; retained pairs require historical review")
            except Exception:
                _log.exception("Could not mark evaluation historical after failed rebuild")
        if cutover_started and not original_intact and ownership["state"] == "recovery":
            raise PackageError(
                "TUNE_FAILED", f"{type(exc).__name__}: {exc}. Verified data is retained as '{staging}'.",
                {"recovered_as": staging,
                 "sidecar_snapshots": collection_recovery.sidecar_reference(ownership)}) from exc
        if not cutover_started and ownership["state"] == "recovery":
            # Only before_replace runs between retain and cutover. The original
            # was never deleted, so the copy is discarded below.
            raise PackageError(
                "TUNE_FAILED", f"{type(exc).__name__}: {exc}. Gold-standard sessions could not be "
                "marked stale before replacement, so the original collection is unchanged.") from exc
        raise
    finally:
        # Before cutover the original is intact, so a retained copy is not needed.
        if completed or original_intact or not cutover_started or ownership["state"] == "scratch":
            try:
                collection_recovery.discard(ownership, client)
            except Exception:
                _log.exception("Could not remove owned staging collection %r", staging)


@telemetry.traced("rag.tuning")
@collection_writes.serialized("collection")
def _run(job_id: str, collection: str, operation: str, params: dict, *, source_collection: str | None = None) -> None:
    telemetry.attribute("rag.tuning_operation", operation)
    source_collection = source_collection or collection
    collection = collection_writes.canonical(collection)
    job = _jobs[job_id]
    job["collection"] = collection
    job["status"] = "running"

    def progress(n: int) -> None:
        job["chunks_written"] = n

    def finish(**fields) -> None:
        # The registration ends before the status turns terminal, so a client
        # that sees the job finish can delete the collection at once (#40).
        collection_writes.end_job("tune", job_id)
        job.update(fields)

    try:
        needs_sources = operation == "rechunk" or (
            operation == "reembed" and params.get("chunking") is not None)
        has_sources = sources.has_sources(source_collection) if needs_sources else False
        records = None

        if operation == "rechunk":
            if not has_sources:
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Collection '{collection}' is chunks-only: its original "
                    "documents were not retained, so it cannot be re-chunked. "
                    "Re-embedding from the stored chunk text is available, but "
                    "chunk boundaries cannot change.",
                    {"collection": collection})
            properties = _chunks_from_sources(source_collection, **params["chunking"])
            reason = "the collection was re-chunked, so its chunks no longer match these pairs"

        elif operation == "reembed":
            if params.get("chunking") is not None:
                # Spec §7.2: refuse the combination rather than silently
                # dropping one half of what was asked for.
                if not has_sources:
                    raise PackageError(
                        "SOURCES_REQUIRED",
                        f"Collection '{collection}' is chunks-only. Re-embedding "
                        "regenerates vectors from the stored chunk text, so chunk "
                        "boundaries cannot change; this request also asked for new "
                        "chunking parameters. Send one or the other.",
                        {"collection": collection})
                properties = _chunks_from_sources(source_collection, **params["chunking"])
                reason = "the collection was re-chunked and re-embedded"
            else:
                properties = _existing_chunks(collection)
                reason = ("the collection was re-embedded from stored chunk text, "
                          "so its vectors changed")

        elif operation == "reindex":
            records = _existing_records(collection)
            properties = [record["properties"] for record in records]
            reason = None            # exact UUID/vector/property copy is verified
        else:
            raise PackageError("TUNE_UNSUPPORTED", f"Unknown operation '{operation}'.")

        job["chunks_total"] = len(properties)
        stale_count = 0

        def mark_before_replace() -> None:
            nonlocal stale_count
            stale_count = goldstandard.mark_stale(source_collection, reason, require_durable=True)

        written = _rebuild(
            collection, properties, params.get("index_type"), params.get("distance_metric"),
            progress, records=records, source_collection=source_collection,
            before_replace=mark_before_replace if reason else None)

        notes = []
        if reason:
            if stale_count:
                notes.append(f"{stale_count} gold-standard session(s) marked stale")
        else:
            notes.append("UUIDs, properties and vectors verified unchanged after reindex; "
                         "gold-standard sessions were left alone")

        finish(status="completed", chunks_written=written, notes=notes)

    except PackageError as exc:
        telemetry.outcome("error", exc)
        finish(status="failed", error_code=exc.code, error=exc.message,
               error_detail=exc.detail)
    except Exception as exc:                          # noqa: BLE001
        telemetry.outcome("error", exc)
        _log.exception("Tuning %r on %r failed", operation, collection)
        finish(status="failed", error_code="TUNE_FAILED",
               error=f"{type(exc).__name__}: {exc}")
    finally:
        collection_writes.end_job("tune", job_id)
        with _lock:
            _active.discard(collection)


async def start_tune_job(collection: str, operation: str, params: dict) -> str:
    source_collection = collection
    collection = collection_writes.canonical(collection)
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        if collection in _active:
            raise RuntimeError(collection)
        _active.add(collection)
    # Lets DELETE /collections refuse while this job rebuilds the collection
    # (#40). Before the job record, so a refusal during a delete leaves none (#302).
    try:
        collection_writes.register_job(collection, "tune", job_id)
    except collection_writes.CollectionDeletingError:
        with _lock:
            _active.discard(collection)
        raise

    # The first write is durable, so it runs off the event loop (#309). A start
    # cancelled meanwhile releases its guards; the store removes the record.
    try:
        # A re-chunk with nothing to rebuild is refused here, before any job
        # exists (#328); the walk runs off the event loop too.
        if operation == "rechunk" or (operation == "reembed" and params.get("chunking") is not None):
            await asyncio.to_thread(_refuse_nothing_to_rebuild, source_collection)
        await _jobs.create(job_id, {
            "job_id": job_id,
            "status": "queued",
            "collection": collection,
            "operation": operation,
            "chunks_total": 0,
            "chunks_written": 0,
            "notes": [],
            "error": None,
            "error_code": None,
            "error_detail": None,
        })
    except BaseException:
        collection_writes.end_job("tune", job_id)
        with _lock:
            _active.discard(collection)
        raise
    task = asyncio.create_task(asyncio.to_thread(telemetry.admitted(_run), job_id, collection, operation, params, source_collection=source_collection))
    # _run ends it first; this also covers a task that never ran its worker.
    task.add_done_callback(lambda _task: collection_writes.end_job("tune", job_id))
    return job_id
