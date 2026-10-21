from __future__ import annotations
from services import telemetry
import time

import httpx

from models.schemas import QueryRequest
from services import ollama_client as ollama
from services import weaviate_client as wc

REFORMULATE_SYSTEM = (
    "You are a search query optimizer. Given a user's question, rewrite it as a concise "
    "search query that maximizes retrieval of relevant text chunks from a vector database. "
    "Return only the rewritten query with no explanation."
)

SYNTHESIS_END_USER_SYSTEM = (
    "You are a helpful assistant. Answer the user's question using only the provided context. "
    "If the context does not contain enough information to answer the question, say so clearly. "
    "Do not use any knowledge outside the provided context. "
    "Write in plain, clear language for a non-technical reader. "
    "Keep the answer short: a few sentences, without technical detail."
)

SYNTHESIS_ENGINEER_SYSTEM = (
    "You are a precise technical assistant. Answer the user's question using only the provided context. "
    "Include relevant technical details. Indicate the confidence level of your answer (high/medium/low) "
    "based on how directly the context addresses the question. "
    "If the context is insufficient, state this explicitly."
)


class ServiceUnavailable(Exception):
    """Ollama or Weaviate failed during a query; `service` names which (#373)."""

    def __init__(self, service: str):
        super().__init__(f"{service} failed during the query")
        self.service = service


async def _from_ollama(call):
    """Await an Ollama call; a connect error, timeout or error status is ServiceUnavailable."""
    try:
        return await call
    except httpx.HTTPError as exc:
        raise ServiceUnavailable("Ollama") from exc


async def _from_weaviate(call):
    """Await a Weaviate query; any error from it is ServiceUnavailable."""
    try:
        return await call
    except Exception as exc:                          # noqa: BLE001
        raise ServiceUnavailable("Weaviate") from exc


def _build_context_end_user(chunks: list[dict]) -> str:
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] {c['content']}")
    return "\n\n".join(lines)


def _build_context_engineer(chunks: list[dict]) -> str:
    lines = []
    for i, c in enumerate(chunks, 1):
        lines.append(f"[{i}] (source: {c['source_file']}, chunk {c['chunk_index']}) {c['content']}")
    return "\n\n".join(lines)


@telemetry.traced("rag.query")
async def run_query(
    question: str,
    collection: str,
    retrieval_mode: str,
    top_k: int,
    alpha: float,
    include_citations: bool,
    response_format: str,
) -> dict:
    config = QueryRequest(question=question, collection=collection, retrieval_mode=retrieval_mode,
                          top_k=top_k, alpha=alpha, include_citations=include_citations,
                          response_format=response_format)
    retrieval_mode, top_k, alpha, response_format = (
        config.retrieval_mode, config.top_k, config.alpha, config.response_format)
    with telemetry.span("rag.reformulate"):
        reformulated = await _from_ollama(
            ollama.chat(REFORMULATE_SYSTEM, f"Original question: {question}"))
        reformulated = reformulated.strip()

    t0 = time.monotonic()
    with telemetry.span("rag.retrieval"):
        if retrieval_mode in ("hnsw", "flat"):
            vector = await _from_ollama(ollama.embed(reformulated))
            chunks = await _from_weaviate(wc.near_vector_query(collection, vector, top_k))
        elif retrieval_mode == "hybrid":
            chunks = await _from_weaviate(wc.hybrid_query(collection, reformulated, alpha, top_k))
        elif retrieval_mode == "semantic":
            chunks = await _from_weaviate(wc.near_text_query(collection, reformulated, top_k))
    retrieval_ms = int((time.monotonic() - t0) * 1000)

    t1 = time.monotonic()
    with telemetry.span("rag.synthesis"):
        if response_format == "engineer":
            context = _build_context_engineer(chunks)
            answer = await _from_ollama(ollama.chat(
                SYNTHESIS_ENGINEER_SYSTEM, f"Context:\n{context}\n\nQuestion: {question}"))
        else:
            context = _build_context_end_user(chunks)
            answer = await _from_ollama(ollama.chat(
                SYNTHESIS_END_USER_SYSTEM, f"Context:\n{context}\n\nQuestion: {question}"))
    llm_ms = int((time.monotonic() - t1) * 1000)

    citations = None
    if include_citations:
        citations = [
            {
                "source_file": c["source_file"],
                "chunk_index": c["chunk_index"],
                "score": round(c["score"], 4),
                "excerpt": c["content"][:200],
            }
            for c in chunks
        ]

    return {
        "answer": answer.strip(),
        "citations": citations,
        "retrieval_latency_ms": retrieval_ms,
        "llm_latency_ms": llm_ms,
        "chunks_retrieved": len(chunks),
    }
