"""Atomic settings publication shared by ingest and retrieval saves."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile
import threading

log = logging.getLogger(__name__)

# Saves are short filesystem transactions dispatched to API worker threads.
# One lock also covers collection names that map to the same sanitized path.
_write_lock = threading.Lock()


def _sync_folder(path: Path) -> None:
    """fsync a folder, so a rename into it survives a power loss (#445)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path: Path, config: dict) -> None:
    """Publish this request's complete value, preserving the old file on failure.

    Serialize the whole transaction within this API process. Exclusive temporary
    names ensure even independent processes cannot rename each other's payloads.
    A later successful save may supersede this value after atomic replacement.
    The folder is fsynced after the replacement (#445); if that fails, the save
    raises with the new value already in place.
    """
    with _write_lock:
        payload = json.dumps(config, indent=2, sort_keys=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(payload)
            temporary.replace(path)
            temporary = None
            _sync_folder(path.parent)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove settings temporary file %s", temporary)
