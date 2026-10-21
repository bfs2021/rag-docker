"""Export and import endpoints.

Import arrives in Phase 5; this module carries the export half and the shared
job-polling shape so both live behind one prefix-free router.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter

from models.schemas import (
    ExportJobStatusResponse,
    ExportRequest,
    ExportStartResponse,
    ImportJobStatusResponse,
    ImportRequest,
    ImportStartResponse,
    PackageListResponse,
    PackageSummary,
)
from services import exporter, importer, packager
from services import weaviate_client as wc
from services.collection_writes import CollectionDeletingError
from utils import api_error, collection_check_unavailable, collection_deleting_error

router = APIRouter()
_log = logging.getLogger(__name__)


@router.post("/export", response_model=ExportStartResponse, status_code=202)
async def start_export(body: ExportRequest):
    try:
        exists = await wc.collection_exists(body.collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists for an export", body.collection)
        return collection_check_unavailable(body.collection, "No job was started")
    if not exists:
        # Checked before a job exists, so a typo does not leave a failed job
        # lying around for the user to interpret.
        return api_error(404, "COLLECTION_NOT_FOUND",
                         f"Collection '{body.collection}' not found.")
    try:
        job_id = await exporter.start_export_job(body.collection, body.include_models)
    except CollectionDeletingError:
        return collection_deleting_error(body.collection)
    except exporter.ExportStartingError:
        # The running start hasn't stored its record yet, so no job_id to name (#338).
        return api_error(409, "EXPORT_STARTING",
                         f"An export of '{body.collection}' is starting; retry shortly.")
    except RuntimeError as exc:
        # start_export_job puts the already-running job id in the exception.
        return api_error(
            409, "EXPORT_IN_PROGRESS",
            f"An export of '{body.collection}' is already running.",
            detail={"job_id": str(exc)},
        )
    return ExportStartResponse(job_id=job_id, status="queued", collection=body.collection)


@router.get("/export/job/{job_id}", response_model=ExportJobStatusResponse)
async def export_job_status(job_id: str):
    job = exporter.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return ExportJobStatusResponse(**job)


@router.post("/import", response_model=ImportStartResponse, status_code=202)
async def start_import(body: ImportRequest):
    # Everything else — unreadable archive, bad digest, embedding mismatch,
    # name collision — is reported through the job, because it is only knowable
    # after reading the package, which takes long enough to need a job.
    try:
        job_id = await importer.start_import_job(body.filename, body.on_conflict)
    except RuntimeError as exc:
        return api_error(409, "IMPORT_IN_PROGRESS",
                         f"An import of '{exc}' is already running.",
                         detail={"filename": str(exc)})
    return ImportStartResponse(job_id=job_id, status="queued", filename=body.filename)


@router.get("/import/job/{job_id}", response_model=ImportJobStatusResponse)
async def import_job_status(job_id: str):
    job = importer.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return ImportJobStatusResponse(**job)


def _list_packages_sync() -> list[dict]:
    out = []
    for p in sorted(packager.exports_dir().glob("ragpkg-*.tar.gz")):
        try:
            manifest = packager.read_manifest(p)
        except Exception:
            # A file that is not a readable package still belongs in the listing;
            # import will report precisely why it cannot be used.
            out.append({"filename": p.name, "size_bytes": p.stat().st_size,
                        "collection": None, "chunk_count": None,
                        "fidelity": None, "created_at": None, "readable": False})
            continue
        out.append({
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "collection": manifest.get("collection", {}).get("name"),
            "chunk_count": manifest.get("collection", {}).get("chunk_count"),
            "fidelity": manifest.get("fidelity"),
            "created_at": manifest.get("created_at"),
            "readable": True,
        })
    return out


@router.get("/packages", response_model=PackageListResponse)
async def list_packages():
    """Packages sitting in ./exports — both exported here and dropped in to import."""
    packages = await asyncio.to_thread(_list_packages_sync)
    return PackageListResponse(packages=[PackageSummary(**p) for p in packages])
