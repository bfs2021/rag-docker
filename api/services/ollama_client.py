from __future__ import annotations
from services import telemetry
import time

import httpx

from config import settings

_BASE = f"http://{settings.ollama_host}:{settings.ollama_port}"


@telemetry.traced("ollama.embed")
async def embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_BASE}/api/embeddings",
            json={"model": settings.embed_model, "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


@telemetry.traced("ollama.chat")
async def chat(system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{_BASE}/api/chat",
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


@telemetry.traced("ollama.models")
async def list_models() -> set[str]:
    """Full names (`name:tag`) of the models Ollama reports."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_BASE}/api/tags")
        resp.raise_for_status()
        return {m["name"] for m in resp.json().get("models", [])}


@telemetry.traced("ollama.health")
async def check_health() -> dict:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_BASE}/api/tags")
            resp.raise_for_status()
            latency_ms = int((time.monotonic() - start) * 1000)
            models = {m["name"].split(":")[0] for m in resp.json().get("models", [])}
            llm_ok = settings.llm_model.split(":")[0] in models
            embed_ok = settings.embed_model.split(":")[0] in models
            if not (llm_ok and embed_ok):
                telemetry.outcome("error")
            return {
                "llm": {"status": "ok" if llm_ok else "error", "latency_ms": latency_ms, "model": settings.llm_model},
                "embed": {"status": "ok" if embed_ok else "error", "latency_ms": latency_ms, "model": settings.embed_model},
            }
    except Exception as exc:
        telemetry.outcome("error", exc)
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "llm": {"status": "error", "latency_ms": latency_ms, "model": settings.llm_model},
            "embed": {"status": "error", "latency_ms": latency_ms, "model": settings.embed_model},
        }
