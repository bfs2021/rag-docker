from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import (
    CollectionInfo,
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


def _load_registry() -> dict:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_registry(reg: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2))


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
    registry = await asyncio.to_thread(_load_registry)
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
        registry = await asyncio.to_thread(_load_registry)
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
async def delete_collection(name: str, confirm: str | None = None):
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
        registry = await asyncio.to_thread(_load_registry)
        for spelling in aliases(name):
            registry.pop(spelling, None)
        await asyncio.to_thread(_save_registry, registry)

    return {"name": name, "objects_deleted": count}
