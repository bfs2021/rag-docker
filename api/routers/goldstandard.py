from __future__ import annotations
import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import FileResponse

from models.schemas import (
    GenerateRequest,
    GenerateResponse,
    GoldPair,
    PatchPairRequest,
    RegenerateRequest,
    SaveRequest,
    SaveResponse,
    SessionResponse,
    SessionValidity,
)
from services import goldstandard as gs
from services import weaviate_client as wc
from utils import api_error, collection_check_unavailable

router = APIRouter(prefix="/goldstandard")
_log = logging.getLogger(__name__)


@router.post("/generate", response_model=GenerateResponse, status_code=202)
async def generate(body: GenerateRequest):
    try:
        exists = await wc.collection_exists(body.collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists for a gold-standard generation",
                       body.collection)
        return collection_check_unavailable(body.collection, "No generation was started")
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{body.collection}' not found.")

    try:
        result = await gs.start_generation(
            collection=body.collection,
            sample_size=body.sample_size,
            seed=body.seed,
        )
    except gs.GoldStandardError as exc:
        return api_error(exc.status, exc.code, exc.message, detail=exc.detail)
    return GenerateResponse(**result)


@router.get("/session/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str):
    session = gs.get_session(session_id)
    if session is None:
        return api_error(404, "SESSION_NOT_FOUND", f"Session '{session_id}' not found.")
    pairs = []
    for p in session.get("pairs", []):
        try:
            pairs.append(GoldPair(**p))
        except Exception:
            pass
    return SessionResponse(
        session_id=session["session_id"],
        status=session["status"],
        pairs_total=session["pairs_total"],
        pairs_attempted=session.get("pairs_attempted", session["pairs_completed"]),
        pairs_completed=session["pairs_completed"],
        pairs_failed=session.get("pairs_failed", 0),
        pairs=pairs,
        collection=session.get("collection", ""),
        errors=session.get("errors", []),
        imported_from=session.get("imported_from"),
        **SessionValidity.model_validate(session).model_dump(),
    )


@router.patch("/session/{session_id}/pair/{pair_id}", response_model=GoldPair)
async def patch_pair(session_id: str, pair_id: str, body: PatchPairRequest):
    updates = body.model_dump(exclude_none=True)
    try:
        pair = await gs.update_pair(session_id, pair_id, updates)
    except gs.GoldStandardError as exc:
        return api_error(exc.status, exc.code, exc.message)
    if pair is None:
        return api_error(404, "PAIR_NOT_FOUND", f"Pair '{pair_id}' not found in session '{session_id}'.")
    return GoldPair(**pair)


@router.post("/regenerate", response_model=GoldPair)
async def regenerate(body: RegenerateRequest):
    try:
        pair = await gs.regenerate_pair(body.session_id, body.pair_id)
    except gs.GoldStandardError as exc:
        return api_error(exc.status, exc.code, exc.message)
    if pair is None:
        return api_error(404, "PAIR_NOT_FOUND", f"Pair '{body.pair_id}' not found in session '{body.session_id}'.")
    return GoldPair(**pair)


@router.post("/save", response_model=SaveResponse)
async def save(body: SaveRequest):
    try:
        result = await gs.save_session(body.session_id, body.filename, body.allow_historical)
    except gs.GoldStandardError as exc:
        return api_error(exc.status, exc.code, exc.message)
    if result is None:
        return api_error(404, "SESSION_NOT_FOUND", f"Session '{body.session_id}' not found.")
    return SaveResponse(**result)


@router.get("/download/{filename}")
async def download(filename: str):
    # Only files in the exports folder are served, never other upload state (#370).
    try:
        target = gs.export_path(filename)
        gs.export_folder_exists(target.parent)
    except gs.GoldStandardError as exc:
        return api_error(exc.status, exc.code, exc.message)
    if gs.export_entry_is_other(target):
        return api_error(400, "INVALID_PATH", f"'{filename}' is not an export file.")
    if not target.is_file():
        return api_error(404, "FILE_NOT_FOUND", f"File '{filename}' not found.")
    return FileResponse(
        path=str(target),
        media_type="application/json",
        filename=filename,
    )


@router.get("/diagnostics")
async def diagnostics():
    return {"issues": await asyncio.to_thread(gs.session_diagnostics)}
