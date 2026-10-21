"""In-app help, rendered from the same templates as a package's own README.

Spec §10 requires one shared source for both, so the page and the packages it
describes cannot drift apart.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from models.schemas import HelpResponse
from services import ollama_client, packager
from utils import api_error

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/help")


async def _embedding_dimensions() -> int | str:
    """The real width, from an actual embedding call.

    Falls back to a word rather than a number: quoting a made-up dimension count
    in a page about why dimensions must match would be its own small lie.
    """
    try:
        return len(await ollama_client.embed("dimension probe"))
    except Exception as exc:                          # noqa: BLE001
        _log.warning("Could not probe embedding dimensions for help page: %s", exc)
        return "its own number of"


@router.get("/transfer", response_model=HelpResponse)
async def transfer_help():
    dimensions = await _embedding_dimensions()
    try:
        markdown = packager.render_help(dimensions)
    except RuntimeError as exc:
        return api_error(500, "HELP_RENDER_FAILED", str(exc))
    except OSError:
        # A template file missing or unreadable. Its path is logged, not sent (#373).
        _log.exception("Could not read a help template")
        return api_error(500, "HELP_RENDER_FAILED",
                         "The help page could not be rendered: a help template file "
                         "could not be read.")
    return HelpResponse(topic="transfer", markdown=markdown)
