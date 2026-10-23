"""Tuning a collection after import: re-chunk, re-embed, re-index.

Spec §7. Each operation rebuilds the collection and is long-running, so all
three follow the job-and-poll pattern.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from models.schemas import (
    CollectionName,
    ReembedRequest,
    ReindexRequest,
    RechunkRequest,
    TuneJobStatusResponse,
    TuneOptionsResponse,
    TuneStartResponse,
)
from services import sources, tuning
from services import weaviate_client as wc
from services.collection_writes import CollectionDeletingError
from utils import api_error, collection_deleting_error

router = APIRouter(prefix="/tune")
_log = logging.getLogger(__name__)


async def _start(collection: str, operation: str, params: dict):
    try:
        exists = await wc.collection_exists(collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#349).
        _log.exception("Could not check whether %r exists for a tune start", collection)
        return api_error(503, "SERVICE_UNAVAILABLE",
                         f"Cannot check whether '{collection}' exists: Weaviate could not "
                         "be read. No job was started; try again when Weaviate is "
                         "available.",
                         detail={"collection": collection})
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")
    try:
        job_id = await tuning.start_tune_job(collection, operation, params)
    except CollectionDeletingError:
        return collection_deleting_error(collection)
    except tuning.PackageError as exc:
        # Nothing to rebuild: refused before a job is created (#328).
        return api_error(409, exc.code, exc.message, detail=exc.detail)
    except tuning.ServiceUnavailableError:
        # Weaviate couldn't be read for that check; the cause is logged (#340).
        return api_error(503, "SERVICE_UNAVAILABLE",
                         f"Cannot check whether '{collection}' can be re-chunked: Weaviate "
                         "could not be read. No job was started; try again when Weaviate "
                         "is available.",
                         detail={"collection": collection})
    except RuntimeError as exc:
        return api_error(409, "TUNE_IN_PROGRESS",
                         f"'{exc}' is already being tuned.",
                         detail={"collection": str(exc)})
    return TuneStartResponse(job_id=job_id, status="queued",
                             collection=collection, operation=operation)


@router.post("/rechunk", response_model=TuneStartResponse, status_code=202)
async def rechunk(body: RechunkRequest):
    """Re-split the retained originals and rebuild. Needs `with-sources`."""
    return await _start(body.collection, "rechunk", {"chunking": body.chunking()})


@router.post("/reembed", response_model=TuneStartResponse, status_code=202)
async def reembed(body: ReembedRequest):
    """Regenerate vectors.

    With no chunking parameters this re-embeds what is stored, leaving chunk
    boundaries alone. With them, it is a re-chunk as well, which a chunks-only
    collection refuses rather than half-honouring (spec §7.2).
    """
    return await _start(body.collection, "reembed",
                        {"chunking": body.chunking() if body.has_chunking() else None})


@router.post("/reindex", response_model=TuneStartResponse, status_code=202)
async def reindex(body: ReindexRequest):
    """Change index type or distance metric, reusing the existing vectors."""
    return await _start(body.collection, "reindex",
                        {"index_type": body.index_type,
                         "distance_metric": body.distance_metric})


@router.get("/job/{job_id}", response_model=TuneJobStatusResponse)
async def tune_job_status(job_id: str):
    job = tuning.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return TuneJobStatusResponse(**job)


def _options_unavailable(collection: str):
    return api_error(503, "SERVICE_UNAVAILABLE",
                     f"Cannot read the tuning options for '{collection}': Weaviate could "
                     "not be read. Try again when Weaviate is available.",
                     detail={"collection": collection})


@router.get("/{collection}", response_model=TuneOptionsResponse)
async def tune_options(collection: CollectionName):
    """What this collection can be tuned with, given its fidelity."""
    try:
        exists = await wc.collection_exists(collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#349).
        _log.exception("Could not check whether %r exists for the tuning options", collection)
        return _options_unavailable(collection)
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")
    try:
        has_sources = await asyncio.to_thread(sources.has_sources, collection)
        stats = await asyncio.to_thread(sources.stats, collection)
        refusal = (await asyncio.to_thread(tuning.rechunk_refusal, collection)
                   if has_sources else None)
    except ValueError:
        return api_error(409, "SOURCE_INDEX_INVALID", "Retained source index is invalid.")
    except tuning.ServiceUnavailableError:
        # Weaviate couldn't be read for the re-chunk check; the cause is logged (#349).
        return _options_unavailable(collection)
    can_rechunk = has_sources and refusal is None
    if not has_sources:
        note = ("No original documents were retained, so this collection cannot be "
                "re-chunked. Re-embedding works from the stored chunk text, which "
                "leaves chunk boundaries unchanged.")
    elif refusal == tuning.RECHUNK_NOTHING_TO_REBUILD:
        note = ("No retained original file has stored chunks, so this collection "
                "cannot be re-chunked: there is nothing to rebuild. Re-embedding works "
                "from the stored chunk text, which leaves chunk boundaries unchanged, "
                "and re-indexing is available.")
    elif can_rechunk:
        note = "Every tuning operation is available."
    else:
        note = ("Some stored source files have no single retained original, or a "
                "retained original is missing, so this collection cannot be "
                "re-chunked. Re-embedding works from the stored chunk text, which "
                "leaves chunk boundaries unchanged, and re-indexing is available.")
    return TuneOptionsResponse(
        collection=collection,
        fidelity="with-sources" if has_sources else "chunks-only",
        source_document_count=stats["document_count"],
        can_rechunk=can_rechunk,
        can_reembed=True,
        can_reindex=True,
        note=note,
    )
