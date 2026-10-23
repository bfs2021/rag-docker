from __future__ import annotations

from fastapi import APIRouter, Query

from models.schemas import CollectionName, MetricsResponse
from services import metrics

router = APIRouter(prefix="/metrics")


@router.get("/latency", response_model=MetricsResponse)
async def latency_summary(
    collection: CollectionName | None = Query(default=None),
    retrieval_mode: str | None = Query(default=None),
    history_limit: int = Query(default=100, ge=0, le=500),
):
    return metrics.get_summary(
        collection=collection,
        retrieval_mode=retrieval_mode,
        history_limit=history_limit,
    )
