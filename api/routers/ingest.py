from __future__ import annotations
import asyncio
import json
import logging
import re
from pathlib import Path
from fastapi import APIRouter, Form, UploadFile, File
from pydantic import ValidationError

from config import settings
from models.schemas import IngestConfig, IngestConfigResponse, IngestUploadResponse, JobStatusResponse
from services import ingest_config
from services import ingest_pipeline
from services import weaviate_client as wc
from services.collection_writes import CollectionDeletingError
from utils import api_error, collection_check_unavailable, collection_deleting_error

router = APIRouter(prefix="/ingest")
_log = logging.getLogger(__name__)

class SaveIngestConfigBody(IngestConfig):
    collection: str


@router.post("/upload", response_model=IngestUploadResponse, status_code=202)
async def ingest_upload(
    collection: str = Form(...),
    strategy: str = Form("overlap"),
    # Strings, so IngestConfig parses them and a non-numeric value is
    # INVALID_SETTINGS like any other invalid setting, not a form error.
    chunk_size: str = Form("1000"),
    chunk_overlap: str = Form("200"),
    similarity_threshold: str = Form("0.85"),
    min_chunk_size: str = Form("100"),
    files: list[UploadFile] = File(...),
):
    try:
        return await _ingest_upload(collection, strategy, chunk_size, chunk_overlap,
                                    similarity_threshold, min_chunk_size, files)
    except Exception:
        # Anything not handled below used to reach the client as a plain-text
        # 500, which the Import page could only show as "HTTP 500" (#290).
        # %r: a filename can hold a newline that would forge a log line (#298).
        _log.exception("Upload to collection %r failed unexpectedly (files: %r)",
                       collection, [f.filename for f in files])
        return api_error(500, "INGEST_FAILED",
                         "The upload failed because of an unexpected server error. "
                         "The API log has the details.")


async def _ingest_upload(collection, strategy, chunk_size, chunk_overlap,
                         similarity_threshold, min_chunk_size, files):
    try:
        config = IngestConfig(chunking_strategy=strategy, chunk_size=chunk_size,
                              chunk_overlap=chunk_overlap, similarity_threshold=similarity_threshold,
                              min_chunk_size=min_chunk_size)
    except ValidationError as exc:
        return api_error(422, "INVALID_SETTINGS", "Invalid chunking settings.",
                         detail={"errors": exc.errors(include_context=False, include_input=False)})
    try:
        exists = await wc.collection_exists(collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists for an ingest upload", collection)
        return collection_check_unavailable(collection, "No job was started")
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")

    try:
        job_id = await ingest_pipeline.start_ingest_job(
            files=files,
            collection=collection,
            strategy=config.chunking_strategy,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            similarity_threshold=config.similarity_threshold,
            min_chunk_size=config.min_chunk_size,
        )
    except CollectionDeletingError:
        return collection_deleting_error(collection)
    except ingest_pipeline.UploadRejected as exc:
        return api_error(exc.status, exc.code, exc.message, detail=exc.detail)
    except ValueError as exc:
        return api_error(400, "NO_SUPPORTED_FILES", str(exc))

    return IngestUploadResponse(
        job_id=job_id,
        status="queued",
        files_queued=len(files),
        collection=collection,
    )


@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str):
    job = ingest_pipeline.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return JobStatusResponse(**job)


@router.get("/config/{collection}", response_model=IngestConfigResponse)
async def get_ingest_config(collection: str):
    cfg, is_default = await asyncio.to_thread(ingest_config.resolve, collection)
    return IngestConfigResponse(is_default=is_default, **cfg)


@router.post("/config", response_model=IngestConfigResponse, status_code=201)
async def save_ingest_config(body: SaveIngestConfigBody):
    cfg = body.model_dump()
    await asyncio.to_thread(ingest_config.save, cfg)
    return IngestConfigResponse(is_default=False, **cfg)
