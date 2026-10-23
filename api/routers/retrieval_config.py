"""Per-collection retrieval settings.

Mirrors the ingest-config endpoints, including the asymmetry: GET takes the
collection in the path, POST takes it in the body.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from models.schemas import CollectionName, RetrievalConfigResponse, SaveRetrievalConfigBody
from services import retrieval_config

router = APIRouter(prefix="/retrieval")


@router.get("/config/{collection}", response_model=RetrievalConfigResponse)
async def get_retrieval_config(collection: CollectionName):
    cfg, is_default = await asyncio.to_thread(retrieval_config.resolve, collection)
    return RetrievalConfigResponse(is_default=is_default, **cfg)


@router.post("/config", response_model=RetrievalConfigResponse, status_code=201)
async def save_retrieval_config(body: SaveRetrievalConfigBody):
    cfg = body.model_dump()
    await asyncio.to_thread(retrieval_config.save, cfg)
    return RetrievalConfigResponse(is_default=False, **cfg)
