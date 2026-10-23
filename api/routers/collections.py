from __future__ import annotations
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import (
    CollectionInfo,
    CollectionName,
    CollectionsResponse,
    CreateCollectionRequest,
)
from services import weaviate_client as wc
from services.collection_writes import CollectionInUseError, aliases
from utils import api_error, collection_check_unavailable

router = APIRouter(prefix="/collections")
_log = logging.getLogger(__name__)

_REGISTRY_FILE: Path | None = None
_registry_lock = asyncio.Lock()


def _registry_path() -> Path:
    global _REGISTRY_FILE
    if _REGISTRY_FILE is None:
        _REGISTRY_FILE = Path(settings.upload_dir) / "collection_registry.json"
    return _REGISTRY_FILE


class _RegistryUnreadable(Exception):
    """The registry file exists but can't be read as a JSON object (#388)."""


def _load_registry() -> dict:
    """The registry, or {} when the file doesn't exist yet. A file that exists but
    can't be read, isn't JSON or isn't an object raises _RegistryUnreadable, so it
    is never taken for an empty registry and overwritten by the next save (#388)."""
    p = _registry_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise _RegistryUnreadable(f"{p} can't be read: {exc}") from exc
    try:
        reg = json.loads(text)
    except ValueError as exc:
        raise _RegistryUnreadable(f"{p} isn't valid JSON: {exc}") from exc
    if not isinstance(reg, dict):
        raise _RegistryUnreadable(f"{p} isn't a JSON object")
    return reg


def _sync_folder(path: Path) -> None:
    """fsync a folder, so a rename into it survives a power loss (#445)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _save_registry(reg: dict) -> None:
    """Publish the whole registry atomically (#388): a uniquely named temporary file
    in the same folder, fsynced, then os.replace, then the folder fsynced (#445). A
    failure before the replace leaves the old file whole and no temporary file, and
    raises; a failed folder fsync raises with the new file in place. Callers hold
    _registry_lock."""
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(reg, indent=2)
    # mkstemp creates the file 0600, like the job and session files.
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _sync_folder(p.parent)


def remove_interrupted_saves() -> None:
    """Remove the temporary files of registry saves a crash interrupted (#445).
    The registry itself is whole: a save replaces it only once its temporary file
    is complete. Called once at startup, before any request, so no save is in
    progress. A leftover that can't be removed is logged and kept."""
    p = _registry_path()
    for leftover in p.parent.glob(f".{p.name}.*.tmp"):
        try:
            leftover.unlink()
        except OSError:
            _log.exception("Could not remove interrupted registry write %s", leftover)


@router.get("", response_model=CollectionsResponse)
async def list_collections():
    try:
        raw = await wc.get_collections()
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be read; the cause is logged, not sent (#373).
        _log.exception("Could not list the collections")
        return api_error(503, "SERVICE_UNAVAILABLE",
                         "Cannot list the collections: Weaviate could not be read. "
                         "Try again when Weaviate is available.")
    try:
        registry = await asyncio.to_thread(_load_registry)
    except _RegistryUnreadable as exc:
        # Listed without created_at; the file is left for someone to fix (#388).
        _log.warning("The collection registry can't be read, so created_at is null "
                     "for every collection: %s", exc)
        registry = {}
    items = [
        CollectionInfo(
            name=c["name"],
            object_count=c["object_count"],
            index_type=c["index_type"],
            distance_metric=c["distance_metric"],
            created_at=registry.get(c["name"]),
            hnsw_config=c.get("hnsw_config"),
        )
        for c in raw
    ]
    return CollectionsResponse(collections=items)


@router.post("", status_code=201)
async def create_collection(body: CreateCollectionRequest):
    try:
        exists = await wc.collection_exists(body.name)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists before creating it", body.name)
        return collection_check_unavailable(body.name, "No collection was created")
    if exists:
        return api_error(409, "COLLECTION_EXISTS", f"Collection '{body.name}' already exists.")

    try:
        await wc.create_collection(
            name=body.name,
            index_type=body.index_type,
            distance_metric=body.distance_metric,
            hnsw_config=body.hnsw_config.model_dump(),
        )
    except Exception:                                 # noqa: BLE001
        # The cause is logged, not sent (#373).
        _log.exception("Could not create collection %r", body.name)
        return api_error(500, "CREATE_FAILED", "Failed to create collection.")

    async with _registry_lock:
        try:
            registry = await asyncio.to_thread(_load_registry)
        except _RegistryUnreadable:
            # Kept as it is, never overwritten with this one entry (#388).
            _log.exception("Collection %r was created, but its created_at was not recorded: "
                           "the collection registry can't be read and is left unchanged",
                           body.name)
        else:
            registry[body.name] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(_save_registry, registry)

    return JSONResponse(status_code=201, content={"name": body.name, "status": "created"})


# How a refused delete names the job in the way (#40). The UI shows the message
# as it is, so each one reads as a sentence on its own.
_BUSY_PHRASES = {
    "ingest": "An ingest job is running for this collection",
    "export": "An export job is running for this collection",
    "import": "An import job is running for this collection",
    "tune": "A tuning job is running for this collection",
    "goldstandard": "A gold-standard generation job is running for this collection",
}


def _busy_error(name: str, jobs: list[tuple[str, str]]):
    kind, job_id = jobs[0]
    phrase = _BUSY_PHRASES.get(kind, f"A {kind} job is running for this collection")
    return api_error(
        409, "COLLECTION_BUSY",
        f"{phrase} ('{name}', job {job_id}). Delete the collection after the job finishes.",
        detail={"collection": name, "job_kind": kind, "job_id": job_id,
                "jobs": [{"job_kind": k, "job_id": j} for k, j in jobs]})


@router.delete("/{name}", status_code=200)
async def delete_collection(name: CollectionName, confirm: str | None = None):
    # Checked first, before the collection is even looked up, and as text, so a
    # value other than `true` is a 400 like a missing one, never a 422 (#302).
    if (confirm or "").lower() != "true":
        return api_error(400, "CONFIRMATION_REQUIRED", "Add ?confirm=true to confirm deletion.")
    try:
        exists = await wc.collection_exists(name)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists before deleting it", name)
        return collection_check_unavailable(name, "Nothing was deleted")
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{name}' not found.")

    try:
        # Refused, not queued, while a job is active: waiting deleted the job's
        # data after it finished, and a delete that ran first let a queued
        # ingest recreate the collection (#40).
        count = await wc.delete_collection_when_idle(name)
    except CollectionInUseError as exc:
        return _busy_error(name, exc.jobs)
    except Exception:                                 # noqa: BLE001
        # The cause is logged, not sent (#373).
        _log.exception("Could not delete collection %r", name)
        return api_error(500, "DELETE_FAILED", "Failed to delete collection.")

    async with _registry_lock:
        try:
            registry = await asyncio.to_thread(_load_registry)
        except _RegistryUnreadable:
            # Kept as it is, never overwritten without its other entries (#388).
            _log.exception("Collection %r was deleted, but its created_at was not removed: "
                           "the collection registry can't be read and is left unchanged",
                           name)
        else:
            for spelling in aliases(name):
                registry.pop(spelling, None)
            await asyncio.to_thread(_save_registry, registry)

    return {"name": name, "objects_deleted": count}
