"""Collection management tools."""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import ragclient
from mcpapp import server

_INDEX_TYPES = ("hnsw", "flat")
_DISTANCES = ("cosine", "dot", "l2-squared")

_CASE_NOTE = (
    "Weaviate capitalises the first letter of a collection name; the rest is "
    "case-sensitive. 'myDocs' is stored as 'MyDocs', and both forms work, but "
    "'mydocs' will not be found."
)


@server.tool(
    name="rag_list_collections",
    description=(
        "List all collections with their document counts and index "
        "configuration. " + _CASE_NOTE
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True),
)
async def rag_list_collections() -> dict:
    return await ragclient.get("/collections")


@server.tool(
    name="rag_create_collection",
    description=(
        "Create a new collection to hold documents. " + _CASE_NOTE + " The "
        "stored name is returned, which may differ from the one submitted."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False),
)
async def rag_create_collection(
    name: str,
    index_type: str = "hnsw",
    distance_metric: str = "cosine",
    ef_construction: int = 128,
    max_connections: int = 64,
    ef: int = 64,
) -> dict:
    # Closed enums on purpose. The API silently substitutes rather than
    # rejecting -- an unknown metric becomes cosine, any index_type but "flat"
    # becomes hnsw -- so a typo would otherwise build a working collection with
    # the wrong index and no warning.
    if index_type not in _INDEX_TYPES:
        raise ToolError(f"index_type must be one of {_INDEX_TYPES}, got {index_type!r}")
    if distance_metric not in _DISTANCES:
        raise ToolError(f"distance_metric must be one of {_DISTANCES}, got {distance_metric!r}")

    await ragclient.post(
        "/collections",
        json={
            "name": name,
            "index_type": index_type,
            "distance_metric": distance_metric,
            # The API expects these nested under hnsw_config, in camelCase.
            "hnsw_config": {
                "efConstruction": ef_construction,
                "maxConnections": max_connections,
                "ef": ef,
            },
        },
    )

    # Read the stored name back rather than echoing the input: Weaviate
    # normalises the first letter, and a model that trusts its own input will
    # later fail to match what listing returns.
    listing = await ragclient.get("/collections")
    stored = next(
        (c["name"] for c in listing.get("collections", []) if c["name"].lower() == name.lower()),
        name,
    )
    return {"name": stored, "requested_name": name, "status": "created"}


@server.tool(
    name="rag_delete_collection",
    description=(
        "Permanently delete a collection and every document chunk in it. "
        "This cannot be undone. " + _CASE_NOTE
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
)
async def rag_delete_collection(name: str) -> dict:
    # The API refuses a delete without confirm=true (#302). The client's own
    # confirmation comes from destructive_hint, so the tool always sends it.
    result = await ragclient.delete(f"/collections/{name}", params={"confirm": "true"})
    return result if isinstance(result, dict) else {"status": "deleted", "name": name}
