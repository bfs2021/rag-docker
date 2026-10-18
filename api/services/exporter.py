"""The export job: lifecycle, progress and the one-at-a-time guard.

Format lives in `packager.py`. This module only decides when a build runs, what
its status looks like while it does, and that two builds of the same collection
never overlap — they would race on the staging directory and the temporary
archive.
"""
from __future__ import annotations
from services import telemetry

import asyncio
import logging
import threading
import uuid

from services import collection_writes, jobstore, packager

_log = logging.getLogger(__name__)

# Job records survive an API restart (#23); see jobstore.py.
_jobs = jobstore.JobStore("export")
# collection name -> job_id of the export currently running for it
_active: dict[str, str] = {}
_lock = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def active_job_for(collection: str) -> str | None:
    with _lock:
        return _active.get(collection)


@telemetry.traced("rag.export")
def _run(job_id: str, collection: str, include_models: bool) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    def progress(written: int) -> None:
        job["chunks_written"] = written

    def finish(**fields) -> None:
        # The registration ends before the status turns terminal, so a client
        # that sees the export finish can delete the collection at once (#40).
        collection_writes.end_job("export", job_id)
        job.update(fields)

    try:
        result = packager.build(collection, include_models=include_models, progress=progress)
    except packager.PackageError as exc:
        telemetry.outcome("error", exc)
        _log.warning("Export of %r refused (%s): %s", collection, exc.code, exc.message)
        finish(status="failed", error=f"PackageError: {exc.message}",
               error_code=exc.code, error_detail=exc.detail)
    except Exception as exc:                       # noqa: BLE001 - reported to the caller
        telemetry.outcome("error", exc)
        _log.exception("Export of %r failed", collection)
        finish(status="failed", error=f"{type(exc).__name__}: {exc}")
    else:
        finish(
            status="completed",
            filename=result["filename"],
            size_bytes=result["size_bytes"],
            chunks_written=result["chunk_count"],
            source_document_count=result["source_document_count"],
            fidelity=result["fidelity"],
            models_bundled=result["models_bundled"],
            retrieve_script=result["retrieve_script"],
            warnings=result["warnings"],
        )
    finally:
        collection_writes.end_job("export", job_id)
        with _lock:
            # Only clear the slot if it is still ours.
            if _active.get(collection) == job_id:
                del _active[collection]


async def start_export_job(collection: str, include_models: bool = False) -> str:
    """Register the job and run the build off the event loop.

    Raises RuntimeError carrying the running job id if one is already in flight
    for this collection.
    """
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        running = _active.get(collection)
        if running is not None:
            raise RuntimeError(running)
        _active[collection] = job_id

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "collection": collection,
        "chunks_written": 0,
        "filename": None,
        "size_bytes": None,
        "source_document_count": None,
        "fidelity": None,
        "models_bundled": None,
        "retrieve_script": None,
        "warnings": [],
        "error": None,
        "error_code": None,
        "error_detail": None,
    }
    # Lets DELETE /collections refuse while this export reads the collection (#40).
    collection_writes.register_job(collection, "export", job_id)

    # to_thread keeps the blocking Weaviate iteration off the event loop, so an
    # export does not stall ingest or query (spec §6.1).
    task = asyncio.create_task(asyncio.to_thread(telemetry.admitted(_run), job_id, collection, include_models))
    # _run ends it first; this also covers a task that never ran its worker.
    task.add_done_callback(lambda _task: collection_writes.end_job("export", job_id))
    return job_id
