from __future__ import annotations

import logging

from fastapi import APIRouter

from models.schemas import QueryRequest, QueryResponse
from services import metrics
from services import rag_pipeline
from services import weaviate_client as wc
from utils import api_error, collection_check_unavailable

router = APIRouter(prefix="/query")
_log = logging.getLogger(__name__)


def _unavailable(collection: str, exc: rag_pipeline.ServiceUnavailable):
    """The 503 for a dependency failure, naming the embedder too when there is one (#411)."""
    service, embedder = exc.service, exc.embedder
    if embedder is None:
        _log.exception("Query on %r failed: %s did not answer", collection, service)
        message = (f"The question was not answered: {service} could not be reached, "
                   f"timed out or returned an error. Try again when {service} is available.")
    else:
        _log.exception("Query on %r failed: %s (or %s, embedding the question for it) "
                       "did not answer", collection, service, embedder)
        message = (f"The question was not answered: {service} could not be reached, "
                   f"timed out or returned an error, or {embedder} could not embed the "
                   f"question for it. Try again when {service} and {embedder} are available.")
    return api_error(503, "SERVICE_UNAVAILABLE", message, detail={"collection": collection})


@router.post("", response_model=QueryResponse)
async def run_query(body: QueryRequest):
    try:
        exists = await wc.collection_exists(body.collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists for a query", body.collection)
        return collection_check_unavailable(body.collection, "No query was run")
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{body.collection}' not found.")

    try:
        result = await rag_pipeline.run_query(
            question=body.question,
            collection=body.collection,
            retrieval_mode=body.retrieval_mode,
            top_k=body.top_k,
            alpha=body.alpha,
            include_citations=body.include_citations,
            response_format=body.response_format,
        )
    except rag_pipeline.ServiceUnavailable as exc:
        # The cause is logged, not sent (#373).
        return _unavailable(body.collection, exc)

    await metrics.record(
        collection=body.collection,
        retrieval_mode=body.retrieval_mode,
        retrieval_ms=result["retrieval_latency_ms"],
        llm_ms=result["llm_latency_ms"],
        chunks_retrieved=result["chunks_retrieved"],
    )

    return QueryResponse(**result)
