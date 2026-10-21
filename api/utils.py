from __future__ import annotations
from fastapi.responses import JSONResponse


def api_error(status_code: int, code: str, message: str, detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


def collection_deleting_error(name: str) -> JSONResponse:
    """A new job refused because a delete of its collection is in progress (#302)."""
    from services.collection_writes import deleting_message
    return api_error(409, "COLLECTION_DELETING", deleting_message(name),
                     detail={"collection": name})


def collection_check_unavailable(name: str, outcome: str) -> JSONResponse:
    """A request refused because Weaviate couldn't say whether its collection exists (#366)."""
    return api_error(503, "SERVICE_UNAVAILABLE",
                     f"Cannot check whether '{name}' exists: Weaviate could not be read. "
                     f"{outcome}; try again when Weaviate is available.",
                     detail={"collection": name})
