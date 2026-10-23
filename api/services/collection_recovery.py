"""Durable ownership of scratch collections and retained recovery copies.

Names are never cleanup authority. Only a valid record created by this service
allows startup to remove scratch; recovery records survive until explicit
collection deletion or successful completion of their owning operation.
"""
from __future__ import annotations
from services import telemetry

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from config import settings
from services import sources

log = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Z][A-Za-z0-9_]*")


def _root() -> Path:
    root = Path(settings.upload_dir) / "collection_operations"
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    if created:
        _sync_dir(root.parent)
    return root


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as output:
        json.dump(record, output, indent=2, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    tmp.replace(path)
    _sync_dir(path.parent)


def _write(record: dict) -> None:
    atomic_json(_root() / f"{record['operation_id']}.json", record)


def begin(target: str, operation: str, client) -> dict:
    if not _NAME.fullmatch(target) or operation not in ("import", "tune"):
        raise ValueError("Invalid collection operation")
    token = uuid.uuid4().hex
    marker = "__importing_" if operation == "import" else "__tuning_"
    staging = f"{target}{marker}{token}"
    if telemetry.call("weaviate.exists", client.collections.exists, staging):
        raise RuntimeError(f"Recovery name '{staging}' is already in use")
    record = dict(version=1, operation_id=token, operation=operation,
                  target=target, staging=staging, state="scratch")
    _write(record)  # Ownership is persisted before collection creation.
    return record


def _copy(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def retain(record: dict, *, package: Path | None = None, source_collection: str | None = None) -> None:
    """Snapshot sidecars and mark recovery durably BEFORE deleting the target."""
    metadata = _root() / record["operation_id"]
    metadata.mkdir()
    target, staging = source_collection or record["target"], record["staging"]
    upload = Path(settings.upload_dir)
    if package is not None:
        sources.restore_package(package, staging)
    else:
        _copy(sources.collection_dir(target), sources.collection_dir(staging))
    for kind in ("ingest", "retrieval"):
        origin = package / f"{kind}_config.json" if package else upload / f"{kind}_configs" / f"{target}.json"
        _copy(origin, metadata / f"{kind}_config.json")
        if origin.is_file():
            config = json.loads(origin.read_text())
            config["collection"] = staging
            out = upload / f"{kind}_configs" / f"{staging}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(config, indent=2, sort_keys=True))
    if package:
        _copy(package / "goldstandard", metadata / "goldstandard")
        _copy(package / "collection.json", metadata / "collection.json")
        _copy(package / "manifest.json", metadata / "manifest.json")
    else:
        sessions = upload / "goldstandard_sessions"
        for path in sessions.glob("*.json"):
            # Preserve unreadable state rather than guessing that it is unrelated.
            try:
                belongs = json.loads(path.read_text()).get("collection") == target
            except (ValueError, OSError, AttributeError):
                belongs = True
            if belongs:
                _copy(path, metadata / "goldstandard" / path.name)
    # Sources live on a separate named volume. Flush both snapshots before the
    # state transition; a crash before the transition leaves the original safe.
    paths = [metadata, sources.collection_dir(staging)]
    paths += [upload / f"{kind}_configs" / f"{staging}.json" for kind in ("ingest", "retrieval")]
    for path in paths:
        files = list(path.rglob("*")) if path.is_dir() else [path]
        for item in files:
            if item.is_file():
                with item.open("rb") as data:
                    os.fsync(data.fileno())
        if path.is_dir():
            for directory in sorted((p for p in path.rglob("*") if p.is_dir()), reverse=True):
                _sync_dir(directory)
            _sync_dir(path)
        if path.exists():
            _sync_dir(path.parent)
    _sync_dir(upload)
    updated = {**record, "state": "recovery"}
    _write(updated)
    record.update(updated)


def cutover_description(record: dict) -> str:
    return "rag-tune:" + record["operation_id"]


def begin_cutover(record: dict) -> None:
    """Persist final-write intent before deleting a tuning target."""
    updated = {**record, "cutover_pending": True}
    _write(updated)
    record.update(updated)


def _finish_cutover_check(record: dict, outcome: str) -> None:
    """Retire only the check; recovery data remains available for inspection."""
    updated = {**record, "cutover_pending": False, "cutover_checked": outcome}
    _write(updated)
    record.update(updated)


def _check_tuning_cutover(record: dict, client) -> None:
    """Check the owned target without deleting data based on mutable recovery."""
    from services import batch_write, goldstandard
    target, staging = record["target"], record["staging"]
    if not telemetry.call("weaviate.exists", client.collections.exists, staging):
        log.warning("Tuning recovery %r is unavailable; target and journal preserved", staging)
        return
    if telemetry.call("weaviate.exists", client.collections.exists, target):
        collection = client.collections.get(target)
        if telemetry.call("weaviate.config", collection.config.get).description != cutover_description(record):
            log.warning("Tuning target %r has another instance; preserved", target)
            _finish_cutover_check(record, "other-instance")
            return
        def expected():
            for obj in telemetry.iterate(client.collections.get(staging).iterator, include_vector=True):
                vector = obj.vector
                if isinstance(vector, dict):
                    if set(vector) != {"default"}:
                        raise ValueError("Unsupported recovery vectors")
                    vector = vector["default"]
                yield {"id": str(obj.uuid), "properties": dict(obj.properties or {}), "vector": vector}
        try:
            batch_write.verify(collection, expected, exact=True)
            _finish_cutover_check(record, "complete")
            return  # Fully written target: no historical flag or deletion.
        except batch_write.BatchVerificationError:
            goldstandard.mark_stale(target, "interrupted tuning final write; verified recovery retained")
            log.warning("Incomplete owned tuning target %r; target and recovery %r preserved for inspection", target, staging)
    else:
        goldstandard.mark_stale(target, "interrupted tuning cutover; verified recovery retained")
    _finish_cutover_check(record, "stale")


def sidecar_reference(record: dict) -> str:
    """The snapshot directory relative to UPLOAD_DIR, for job error details.

    Job results are served without authentication, so the absolute path stays
    in the server log.
    """
    metadata = _root() / record["operation_id"]
    log.warning("Recovery collection %r keeps sidecar snapshots in %s", record["staging"], metadata)
    # Built, not derived with relative_to: the root needn't resolve under
    # UPLOAD_DIR (a symlinked mount, or the reindex verifier's own root).
    return "collection_operations/" + record["operation_id"]


def discard(record: dict, client) -> None:
    """Delete an owned copy after success, or scratch while the target is safe."""
    # Persist intent before the first deletion. Startup can finish this exact
    # authorized cleanup even if backend or filesystem cleanup is interrupted.
    if record["state"] != "cleanup":
        updated = {**record, "state": "cleanup"}
        _write(updated)
        record.update(updated)
    name = record["staging"]
    if telemetry.call("weaviate.exists", client.collections.exists, name):
        telemetry.call("weaviate.delete", client.collections.delete, name)
    sources.delete(name)
    if sources.collection_dir(name).exists():
        raise OSError(f"Could not remove recovery sources for {name}")
    for kind in ("ingest", "retrieval"):
        (Path(settings.upload_dir) / f"{kind}_configs" / f"{name}.json").unlink(missing_ok=True)
    metadata = _root() / record["operation_id"]
    if metadata.exists():
        shutil.rmtree(metadata)
    (_root() / f"{record['operation_id']}.json").unlink(missing_ok=True)
    _sync_dir(_root())



def _read_owned_record(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Ownership must be a regular metadata file of at most 4096 bytes")
    record = json.loads(path.read_text())
    token = record["operation_id"]
    operation = record["operation"]
    marker = "__importing_" if operation == "import" else "__tuning_"
    if (type(record.get("version")) is not int or record["version"] != 1 or operation not in ("import", "tune")
            or not re.fullmatch(r"[0-9a-f]{32}", token)
            or path.name != f"{token}.json" or not _NAME.fullmatch(record["target"])
            or record["staging"] != f"{record['target']}{marker}{token}"
            or record["state"] not in ("scratch", "recovery", "cleanup")
            or ("cutover_pending" in record and (type(record["cutover_pending"]) is not bool
                                                  or operation != "tune"))):
        raise ValueError("Invalid collection ownership record")
    return record


def retire_deleted(name: str, client) -> None:
    """Retire exact recovery ownership only after explicit backend deletion.

    A missing backend copy at startup does not itself authorize losing retained
    snapshots. Invalid or unrelated journals never grant cleanup authority.
    """
    for path in sorted(_root().glob("*.json")):
        try:
            record = _read_owned_record(path)
        except Exception:
            log.exception("Unreadable collection ownership %s; preserved", path)
            continue
        if record["staging"] == name and record["state"] in ("recovery", "cleanup"):
            discard(record, client)


def sweep(client) -> list[str]:
    removed = []
    for path in sorted(_root().glob("*.json")):
        try:
            record = _read_owned_record(path)
            if record["state"] == "recovery":
                if record.get("cutover_pending"):
                    _check_tuning_cutover(record, client)
                log.warning("Retained recovery collection %r; sidecar snapshots: %s",
                            record["staging"], _root() / record["operation_id"])
                continue
            discard(record, client)
            removed.append(record["staging"])
        except Exception:  # Unreadable ownership never grants deletion authority.
            log.exception("Could not resolve collection ownership %s; preserved", path)
    return removed
