from __future__ import annotations
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from utils import api_error
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from services.telemetry import bootstrap
    runtime = bootstrap()
    app.state.telemetry = runtime
    try:
        import logging
        from services import goldstandard, metrics
        from services import weaviate_client as wc
        goldstandard.load_sessions_from_disk()
        goldstandard.reconcile_interrupted_generations()
        metrics.load_from_disk()
        # Sweep only durably owned scratch. Verified recovery collections and
        # unowned marker-like names must survive startup.
        log = logging.getLogger(__name__)
        try:
            abandoned = await wc.sweep_staging()
            if abandoned:
                log.warning("Removed %d staging collection(s) left by a previous run: %s. "
                            "Re-import the package to try again; it is still in ./exports.",
                            len(abandoned), ", ".join(abandoned))
        except Exception:                                 # noqa: BLE001
            log.exception("Startup sweep of staging collections failed")
        try:
            from services import importer
            partial = await asyncio.to_thread(importer.sweep_interrupted_imports)
            if partial:
                log.warning("Removed %d collection(s) left half-built by an interrupted "
                            "import: %s. Re-import the package to try again.",
                            len(partial), ", ".join(partial))
            stale_dirs = await asyncio.to_thread(importer.sweep_stale_workdirs)
            if stale_dirs:
                log.warning("Removed %d abandoned extraction directory(ies): %s",
                            len(stale_dirs), ", ".join(stale_dirs))
        except Exception:                                 # noqa: BLE001
            log.exception("Startup sweep of interrupted imports failed")
        # Job records survive a restart (#23). After the sweeps, so a job interrupted
        # mid-import is reported failed and its half-built collection already swept.
        # No worker from the previous process is alive, so any job still queued or
        # running is marked interrupted before the first request is served.
        from services import exporter, importer, ingest_pipeline, tuning
        interrupted = []
        for kind, module in (("ingest", ingest_pipeline), ("export", exporter),
                             ("import", importer), ("tune", tuning)):
            try:
                marked = await asyncio.to_thread(module._jobs.load)
                interrupted += [f"{kind} {job_id}" for job_id in marked]
            except Exception:                             # noqa: BLE001
                log.exception("Could not load the %s job records", kind)
        if interrupted:
            log.warning("Marked %d job(s) interrupted by the restart: %s. Their status now "
                        "says so; start them again.", len(interrupted), ", ".join(interrupted))
        yield
    finally:
        try:
            from services import weaviate_client as wc
            wc.close_client()
        finally:
            await asyncio.to_thread(runtime.shutdown)


app = FastAPI(title="RAG API", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, exc):
    # Invalid settings can include non-finite JSON numbers. Echoing their raw
    # values in a JSONResponse would raise a serialization error instead of 422.
    errors = [{key: value for key, value in error.items() if key not in ("input", "ctx")}
              for error in exc.errors()]
    return api_error(422, "INVALID_PARAMETER", "Request parameters are invalid.", detail=errors)

from services.telemetry import RequestTracing

_log = logging.getLogger(__name__)

# Routing errors answer in the envelope like every other error (#373). Other
# statuses (FastAPI's 400 for an unparsable body) keep FastAPI's own answer.
_ROUTING_ERRORS = {
    404: ("NOT_FOUND", "No API route matches this path."),
    405: ("METHOD_NOT_ALLOWED", "This API route does not accept this method."),
}


@app.exception_handler(StarletteHTTPException)
async def routing_error(request, exc):
    if exc.status_code not in _ROUTING_ERRORS:
        return await http_exception_handler(request, exc)
    code, message = _ROUTING_ERRORS[exc.status_code]
    response = api_error(exc.status_code, code, message)
    response.headers.update(exc.headers or {})  # a 405 keeps its Allow header
    return response


@app.exception_handler(Exception)
async def unhandled_error(request, exc):
    # The cause is logged, not sent (#373). Starlette re-raises the exception
    # after this response is sent, so the server logs its traceback as well.
    _log.error("Unhandled error on %s %s: %s: %s", request.method, request.url.path,
               type(exc).__name__, exc)
    return api_error(500, "INTERNAL_ERROR",
                     "The API hit an unexpected error. The cause is in the API log.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestTracing)

from routers import help as help_router, health, collections, ingest, query, goldstandard, metrics, retrieval_config, transfer, tuning

app.include_router(health.router)
app.include_router(help_router.router)
app.include_router(collections.router)
app.include_router(ingest.router)
app.include_router(query.router)
app.include_router(goldstandard.router)
app.include_router(metrics.router)
app.include_router(retrieval_config.router)
app.include_router(transfer.router)
app.include_router(tuning.router)
