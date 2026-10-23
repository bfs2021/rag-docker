from __future__ import annotations
from services import telemetry
import asyncio
import json
import logging
import copy
import os
import stat
import tempfile
import threading

import httpx
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from models.schemas import SessionResponse, SessionValidity
from services import ollama_client as ollama
from services import weaviate_client as wc
from services.collection_writes import aliases, end_job, register_job

GS_SYSTEM = (
    "You are creating evaluation data for a RAG system. Given a text chunk, generate one question "
    "that can be answered from this chunk, the correct answer based only on this chunk, and a ground "
    "truth answer (same as the answer). Return a JSON object with keys: question, answer, ground_truth. "
    "Do not include any text outside the JSON object."
)
GS_RETRY_SUFFIX = (
    " Your previous response was not valid JSON. Return ONLY the JSON object with keys: "
    "question, answer, ground_truth. No markdown, no explanation."
)

log = logging.getLogger(__name__)


class GoldStandardError(Exception):
    """Carries an API error code so the router does not have to guess."""

    def __init__(self, code: str, message: str, status: int, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail

_sessions: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()
_state_lock = threading.RLock()
_diagnostics: dict[str, dict] = {}
_scan_lock = threading.Lock()
# Parse/validation results by path, keyed on (inode, mtime_ns, size); scans only.
_scan_cache: dict[str, tuple[tuple[int, int, int], dict | None]] = {}
_store_revision = 0
_SESSION_ID = re.compile(r"gs_[0-9a-f]{8}")
_PENDING_DIR = "pending_markers"
_MARKER_FLAGS = ("stale", "orphaned")
_PERSISTENCE_CODES = ("SESSION_WRITE_FAILED", "SESSION_DURABILITY_UNCERTAIN")
_MARKER_MESSAGES = {
    True: "History marker could not be saved in the session file. A pending marker keeps the session historical after restart, and current export is refused. Check local storage; the next successful write of this session saves the marker.",
    False: "History marker could not be saved. The session is treated as historical and current export is refused only until restart. Check local storage, then edit this session to save the marker.",
}


def validate_session_id(session_id: str) -> None:
    """Imported identities use the same grammar as locally generated ones."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid evaluation session ID.")


def validate_session(session: dict) -> None:
    """Validate without normalising or dropping historical validity metadata."""
    SessionResponse.model_validate(session, strict=True)
    validate_session_id(session["session_id"])


def _session_storage_root() -> Path:
    upload = Path(settings.upload_dir).resolve()
    p = upload / "goldstandard_sessions"
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        raise ValueError("Evaluation session storage is not a regular directory.")
    root = p.resolve()
    if root.parent != upload:
        raise ValueError("Evaluation session storage is outside the upload directory.")
    return root


def _sessions_dir() -> Path:
    p = _session_storage_root()
    created = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if created:
        _sync_directory(p.parent)
    return p


def _session_path(session_id: str) -> Path:
    validate_session_id(session_id)
    # Preflight must be read-only; the writer creates the directory only after
    # every imported session has been checked.
    root = _session_storage_root()
    candidate = root / f"{session_id}.json"
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
        raise ValueError("Evaluation session destination is not a regular file.")
    target = candidate.resolve()
    # Grammar prevents metadata-derived paths; containment also refuses an
    # existing file symlink which would redirect a valid identity's write.
    if target.parent != root:
        raise ValueError("Evaluation session destination is outside session storage.")
    return target


def _session_key(session_id: str) -> Path:
    # The scan's diagnostic key form, without the storage checks that may be
    # what failed; one key per file keeps Health's entries distinct.
    return Path(settings.upload_dir).resolve() / "goldstandard_sessions" / f"{session_id}.json"


def _pending_root() -> Path:
    p = _session_storage_root() / _PENDING_DIR
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        raise ValueError("Pending marker storage is not a regular directory.")
    return p


def _pending_path(session_id: str) -> Path:
    validate_session_id(session_id)
    root = _pending_root()
    candidate = root / f"{session_id}.json"
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
        raise ValueError("Pending marker destination is not a regular file.")
    if candidate.resolve().parent != root.resolve():
        raise ValueError("Pending marker destination is outside marker storage.")
    return candidate


def _load_pending(path: Path) -> dict:
    """A pending marker holds only history flags that a session file could not take."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Pending marker must be a regular file.")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("session_id") != path.stem:
        raise ValueError("Pending marker does not match its session.")
    validate_session_id(data["session_id"])
    allowed = {"session_id"} | {f"{flag}{suffix}" for flag in _MARKER_FLAGS for suffix in ("", "_reason", "_at")}
    if set(data) - allowed or not any(flag in data for flag in _MARKER_FLAGS):
        raise ValueError("Pending marker has unexpected fields.")
    for flag in _MARKER_FLAGS:
        if flag in data and data[flag] is not True:
            raise ValueError("Pending marker flag must be true.")
        for suffix in ("_reason", "_at"):
            if not isinstance(data.get(flag + suffix), (str, type(None))):
                raise ValueError("Pending marker metadata must be text.")
    return data


def _apply_pending(session: dict, pending: dict) -> bool:
    changed = False
    for flag in _MARKER_FLAGS:
        if pending.get(flag) is True and session.get(flag) is not True:
            session[flag] = True
            session[f"{flag}_reason"] = pending.get(f"{flag}_reason")
            session[f"{flag}_at"] = pending.get(f"{flag}_at")
            changed = True
    return changed


def _record_issue(path: Path, code: str, message: str | None = None) -> None:
    changed = _diagnostics.get(str(path), {}).get("code") != code
    messages = {
        "SESSION_STORAGE_UNAVAILABLE": "Session storage could not be inspected. Existing files are preserved; inspect local storage and restart after recovery.",
        "SESSION_READ_FAILED": "Session file could not be loaded. Original bytes are preserved; restore a valid copy and restart the API.",
        "SESSION_INTERRUPTED_WRITE": "An interrupted write left an unpublished temporary snapshot. The final JSON file remains authoritative; inspect the temporary file before removing it.",
        "SESSION_WRITE_FAILED": "Update failed before replacement; the previous snapshot remains authoritative. Check local storage before retrying.",
        "SESSION_DURABILITY_UNCERTAIN": "Replacement occurred but directory durability could not be confirmed. Refresh the session and inspect local storage before retrying.",
    }
    filename = f"{_PENDING_DIR}/{path.name}" if path.parent.name == _PENDING_DIR else path.name
    _diagnostics[str(path)] = {"filename": filename, "code": code, "message": message or messages[code]}
    if changed:
        log.error("Session persistence issue %s for %s", code, path.name)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_durably(path: Path, payload: str) -> None:
    """Write a unique fsynced temporary file and replace `path` with it.

    Returning means the replacement happened; raising means it did not. The
    caller syncs the directory and decides what a failure means.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix="." + path.stem + "-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                log.exception("Could not remove owned temporary file %s", temporary.name)


def _save_session_sync(session: dict) -> None:
    """Publish one immutable snapshot under the single-process writer lock.

    Before replace, errors leave both the prior disk snapshot and cache intact.
    After replace, cache reflects disk even if directory fsync reports an
    uncertain durability outcome. Neither failure is acknowledged as success.
    """
    global _store_revision
    with _state_lock:
        snapshot = copy.deepcopy(session)
        try:
            path = _session_path(snapshot["session_id"])
            _sessions_dir()
        except (OSError, ValueError) as exc:
            _record_issue(Path(settings.upload_dir) / "goldstandard_sessions" / (str(snapshot["session_id"]) + ".json"), "SESSION_WRITE_FAILED")
            raise GoldStandardError("SESSION_WRITE_FAILED", "Session directory could not be prepared. The previous snapshot is unchanged.", 503) from exc
        replaced = False
        try:
            payload = json.dumps(snapshot, indent=2, allow_nan=False)
            _replace_durably(path, payload)
            replaced = True
            _sessions[snapshot["session_id"]] = snapshot
            _store_revision += 1
            _sync_directory(path.parent)
            # Only generation's own codes are evidence; an imported or edited
            # value is not, and must not turn a completed write into an error.
            error = snapshot.get("persistence_error")
            if isinstance(error, dict) and error.get("code") in _PERSISTENCE_CODES:
                _record_issue(path, error["code"])
            else:
                _diagnostics.pop(str(path), None)
            try:
                _discard_pending_marker(snapshot)
            except Exception:                         # noqa: BLE001
                log.exception("Could not remove pending history marker for %s", snapshot["session_id"])
        except (OSError, ValueError, TypeError) as exc:
            code = "SESSION_DURABILITY_UNCERTAIN" if replaced else "SESSION_WRITE_FAILED"
            _record_issue(path, code)
            message = ("Session replacement occurred but durability could not be confirmed. Refresh the session and inspect diagnostics before retrying."
                       if replaced else "Session update could not be persisted. The previous snapshot is unchanged.")
            raise GoldStandardError(code, message, 503) from exc


def _write_pending_marker(session_id: str, fields: dict) -> bool:
    """Keep a history flag across restart when its session file cannot take it."""
    with _state_lock:
        try:
            path = _pending_path(session_id)
            merged = {"session_id": session_id}
            if path.exists():
                try:
                    merged.update(_load_pending(path))
                except (OSError, ValueError):
                    # Nothing readable to keep; the new marker replaces it.
                    pass
            merged.update(fields)
            if not path.parent.exists():
                path.parent.mkdir()
                _sync_directory(path.parent.parent)
            _replace_durably(path, json.dumps(merged, indent=2))
            _sync_directory(path.parent)
            return True
        except (OSError, ValueError):
            log.exception("Could not save pending history marker for %s", session_id)
            return False


def _discard_pending_marker(snapshot: dict) -> None:
    """A successful write that carries every pending flag makes the marker obsolete."""
    path = _pending_path(snapshot["session_id"])
    if not path.exists():
        return
    try:
        pending = _load_pending(path)
    except (OSError, ValueError):
        # An unreadable marker is preserved and the scan reports it.
        return
    if not _apply_pending(copy.deepcopy(snapshot), pending):
        path.unlink()
        _sync_directory(path.parent)


def _scan_sessions() -> None:
    """Inspect disk without blocking writers, then publish only a current scan."""
    global _store_revision
    with _scan_lock:
        with _state_lock:
            revision = _store_revision
        storage_label = Path(settings.upload_dir) / "goldstandard_sessions"
        loaded = {}
        issues = {}
        try:
            root = _session_storage_root()
            with os.scandir(root) as entries:
                paths = [Path(entry.path) for entry in entries]
        except FileNotFoundError:
            paths = []
        except (OSError, ValueError, RuntimeError):
            with _state_lock:
                if revision == _store_revision:
                    _record_issue(storage_label, "SESSION_STORAGE_UNAVAILABLE")
            return
        for path in paths:
            if re.fullmatch(r"\.gs_[0-9a-f]{8}-.+\.tmp", path.name):
                issues[path] = ("SESSION_INTERRUPTED_WRITE", None)
            elif path.name.endswith(".json"):
                try:
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Evaluation session must be a regular file.")
                    stat = path.stat()
                    signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                    cached = _scan_cache.get(str(path))
                    if cached is not None and cached[0] == signature:
                        if cached[1] is None:
                            raise ValueError("Evaluation session is unchanged and still invalid.")
                        data = copy.deepcopy(cached[1])
                    else:
                        # Only parse and validation outcomes are remembered; a
                        # failed read is retried on the next scan.
                        _scan_cache.pop(str(path), None)
                        text = path.read_text()
                        _scan_cache[str(path)] = (signature, None)
                        data = json.loads(text)
                        validate_session(data)
                        _scan_cache[str(path)] = (signature, copy.deepcopy(data))
                    if path != _session_path(data["session_id"]):
                        raise ValueError("Evaluation session filename does not match its identity.")
                    loaded[data["session_id"]] = data
                    retained_error = data.get("persistence_error")
                    if isinstance(retained_error, dict) and retained_error.get("code") in _PERSISTENCE_CODES:
                        issues[path] = (retained_error["code"], None)
                except (OSError, ValueError, TypeError):
                    issues[path] = ("SESSION_READ_FAILED", None)
        listed = {str(path) for path in paths}
        for key in list(_scan_cache):
            if key not in listed:
                _scan_cache.pop(key, None)
        pending_root = root / _PENDING_DIR
        pending_paths = []
        try:
            if pending_root.is_symlink() or (pending_root.exists() and not pending_root.is_dir()):
                raise ValueError("Pending marker storage is not a regular directory.")
            with os.scandir(pending_root) as entries:
                pending_paths = [Path(entry.path) for entry in entries]
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            issues[pending_root] = ("SESSION_READ_FAILED", None)
        for path in pending_paths:
            if not re.fullmatch(r"gs_[0-9a-f]{8}\.json", path.name):
                continue
            try:
                pending = _load_pending(path)
            except (OSError, ValueError):
                issues[path] = ("SESSION_READ_FAILED", None)
                continue
            data = loaded.get(pending["session_id"])
            # A marker whose session file is missing has nothing to apply to.
            if data is not None and _apply_pending(data, pending):
                issues[root / path.name] = ("SESSION_WRITE_FAILED", _MARKER_MESSAGES[True])
        with _state_lock:
            # A concurrent durable commit wins over an older inspection, including
            # its cache and write-failure diagnostics. The next refresh rescans.
            if revision != _store_revision:
                return
            _diagnostics.pop(str(storage_label), None)
            present = {str(path) for path in paths + pending_paths} | {str(pending_root)}
            for key, issue in list(_diagnostics.items()):
                if issue["code"] in ("SESSION_READ_FAILED", "SESSION_INTERRUPTED_WRITE") and Path(key).parent in (root, pending_root) and (key not in present or Path(key) not in issues):
                    _diagnostics.pop(key, None)
            for sid, data in loaded.items():
                if sid not in _sessions:
                    _sessions[sid] = data
                    _store_revision += 1
            for path, (code, message) in issues.items():
                # Preserve the independent failed-write policy at the same path.
                if _diagnostics.get(str(path), {}).get("code") not in ("SESSION_WRITE_FAILED", "SESSION_DURABILITY_UNCERTAIN"):
                    _record_issue(path, code, message)


def load_sessions_from_disk() -> None:
    _scan_sessions()


def reconcile_interrupted_generations() -> None:
    """Startup only: no generation worker survives a process restart."""
    if _tasks:
        raise RuntimeError("Generation recovery must run before workers start")
    interruption = RuntimeError("Generation interrupted by API restart. Retained pairs are available for review; start a new generation for missing pairs.")
    with _state_lock:
        interrupted = [sid for sid, value in _sessions.items() if value.get("status") == "generating"]
    for session_id in interrupted:
        def settle(current):
            if current.get("status") == "generating":
                _failed_generation(current, interruption)
        try:
            _update_session_sync(session_id, settle)
        except Exception as write_error:
            # Keep the stored snapshot for a later retry, but never advertise a
            # live worker that does not exist. Existing diagnostics expose the
            # persistence failure and a subsequent restart retries settlement.
            log.exception("Could not persist interrupted generation %s", session_id)
            _publish_generation_failure(session_id, interruption, write_error)


def _sessions_on_disk() -> list[dict]:
    """Return detached, validated files for export without changing the cache."""
    try:
        paths = sorted(_session_storage_root().glob("*.json"))
    except (OSError, ValueError, RuntimeError):
        log.exception("Cannot read evaluation session storage; existing files are unchanged")
        return []
    sessions = []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Evaluation session must be a regular file.")
            data = json.loads(path.read_text())
            validate_session(data)
            if path != _session_path(data["session_id"]):
                raise ValueError("Evaluation session filename does not match its identity.")
        except (OSError, ValueError, RuntimeError) as exc:
            # ValidationError text can include document-derived field values.
            log.warning("Skipping invalid evaluation session %s; file is unchanged (%s)",
                        path.name, type(exc).__name__)
            continue
        # A package must not carry a session as current when its history
        # marker is only pending. An unreadable marker is reported by the scan.
        try:
            pending = _pending_path(data["session_id"])
            if pending.exists():
                _apply_pending(data, _load_pending(pending))
        except (OSError, ValueError):
            pass
        sessions.append(data)
    return sessions


def session_diagnostics() -> list[dict]:
    _scan_sessions()
    with _state_lock:
        return copy.deepcopy([_diagnostics[key] for key in sorted(_diagnostics)])


def sessions_for(collection: str) -> list[dict]:
    _scan_sessions()
    with _state_lock:
        found = []
        for sid, session in _sessions.items():
            if session.get("collection") != collection:
                continue
            try:
                validate_session(session)
                if sid != session.get("session_id"):
                    raise ValueError("Cached session key does not match identity")
                _session_path(sid)
            except (OSError, ValueError, RuntimeError) as exc:
                log.warning("Skipping invalid cached evaluation session %s (%s)",
                            sid, type(exc).__name__)
                _record_issue(_session_key(str(sid)), "SESSION_READ_FAILED")
                continue
            found.append(session)
        return copy.deepcopy(found)


def store_session(session: dict) -> None:
    """Write through the durable store so the cache cannot undo an import.

    Anything outside this module that writes a session file directly will be
    silently undone: the cache still holds the previous version, and the next
    cache-based write puts that back over the file. Import learned this the
    hard way — a restored session reverted to its pre-import orphaned state.
    """
    validate_session(session)
    _save_session_sync(session)


def _identity_available(session_id: str) -> bool:
    """Treat every existing directory entry as occupied, even unreadable bytes."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        # Source identities are provenance, never local filesystem addresses.
        return False
    if session_id in _sessions:
        return False
    path = _session_storage_root() / f"{session_id}.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _allocate_identity(preferred: str | None = None, tried: list[str] | None = None) -> str:
    # Callers hold _state_lock across allocation and durable publication.
    if preferred is not None and _identity_available(preferred):
        return preferred
    for _ in range(128):
        candidate = f"gs_{uuid.uuid4().hex[:8]}"
        if tried is not None:
            tried.append(candidate)
        if _identity_available(candidate):
            return candidate
    raise RuntimeError("Could not allocate an unoccupied session identity")


def _store_generated_session(session: dict) -> dict:
    snapshot = copy.deepcopy(session)
    with _state_lock:
        tried: list[str] = []
        try:
            snapshot["session_id"] = _allocate_identity(tried=tried)
        except (OSError, ValueError, RuntimeError) as exc:
            # Nothing was written. Report it like the writer's own prepare
            # failure, under the candidate being checked or the last one tried.
            _record_issue(Path(settings.upload_dir) / "goldstandard_sessions" / (tried[-1] + ".json"), "SESSION_WRITE_FAILED")
            raise GoldStandardError("SESSION_WRITE_FAILED", "Session storage could not be inspected to allocate an identity. No session was written.", 503) from exc
        store_session(snapshot)
    return copy.deepcopy(snapshot)


def store_imported_session(session: dict, source_collection: str) -> dict:
    """Serialize source-ID collision handling with generation and other imports."""
    snapshot = copy.deepcopy(session)
    # Import preflight (check 4a) has already refused noncanonical source IDs.
    # Allocation keeps the source ID only when it is free; it stays provenance.
    SessionResponse.model_validate(snapshot, strict=True)
    source_id = snapshot["session_id"]
    with _state_lock:
        snapshot["session_id"] = _allocate_identity(source_id)
        snapshot["imported_from"] = {
            "session_id": source_id,
            "collection": source_collection,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        }
        store_session(snapshot)
    return copy.deepcopy(snapshot)


def _update_session_sync(session_id: str, change):
    with _state_lock:
        current = _sessions.get(session_id)
        if current is None:
            return None
        snapshot = copy.deepcopy(current)
        result = change(snapshot)
        if snapshot != current:
            _save_session_sync(snapshot)
        return copy.deepcopy(result)


def _flag_sessions(collection: str, flag: str, reason: str, *, require_durable: bool = False) -> int:
    """Mark every session for a collection, on disk and in memory.

    Sessions are never deleted and never remapped. A remap that guesses which
    new chunk replaces an old one corrupts an evaluation baseline silently,
    which is worse than an honest flag the user can act on.

    If a completed mutation cannot persist a marker to the session file, the
    historical guard is kept in memory and in a pending marker that survives
    restart.
    """
    global _store_revision
    sessions = sessions_for(collection)
    now = datetime.now(timezone.utc).isoformat()
    marked = 0
    for session in sessions:
        def change(current):
            current[flag] = True
            current[f"{flag}_reason"] = reason
            current[f"{flag}_at"] = now
        try:
            _update_session_sync(session["session_id"], change)
            marked += 1
        except (GoldStandardError, OSError, ValueError, RuntimeError):
            # The primary collection mutation already happened. Preserve the
            # failed-write diagnostic. Publish the marker in the process cache
            # even when disk still holds the prior snapshot, so a completed
            # delete/rebuild cannot export this session as a current baseline.
            # A pending marker carries it across restart; a later successful
            # session write persists it and removes the pending marker.
            with _state_lock:
                current = _sessions.get(session["session_id"])
                if current is not None:
                    snapshot = copy.deepcopy(current)
                    change(snapshot)
                    _sessions[session["session_id"]] = snapshot
                    _store_revision += 1
            log.exception("Could not durably mark session %s %s", session["session_id"], flag)
            saved = _write_pending_marker(session["session_id"],
                                          {flag: True, f"{flag}_reason": reason, f"{flag}_at": now})
            with _state_lock:
                key = _session_key(session["session_id"])
                # An uncertain-durability report keeps its own wording.
                if _diagnostics.get(str(key), {}).get("code", "SESSION_WRITE_FAILED") == "SESSION_WRITE_FAILED":
                    _record_issue(key, "SESSION_WRITE_FAILED", _MARKER_MESSAGES[saved])
            if require_durable and not saved:
                raise GoldStandardError("SESSION_WRITE_FAILED", "Cannot replace collection: a historical marker could not be persisted.", 503)
    return marked


def mark_stale(collection: str, reason: str, *, require_durable: bool = False) -> int:
    """Chunk identity changed, so the pairs no longer describe what is stored."""
    # Backend aliases address the same corpus, but retained provenance keeps
    # the spelling supplied when a session was created or imported.
    return sum(_flag_sessions(alias, "stale", reason, require_durable=require_durable) for alias in aliases(collection))


def mark_orphaned(collection: str, reason: str) -> int:
    """The collection is gone. Retained rather than deleted — see RAG_EXPORT_SPECIFICATIONS.md §8 rule 4."""
    return _flag_sessions(collection, "orphaned", reason)


# Published cache snapshots are immutable (see save_session), so readers take a
# copy without waiting for the writer lock, which is held through fsync.
def get_session(session_id: str) -> dict | None:
    return copy.deepcopy(_sessions.get(session_id))


def _parse_gs_json(text: str) -> dict:
    """Pull the JSON object out of a model reply.

    The model reliably returns a correct object and then keeps talking --
    "Extra data: line 6 column 1" was the single most common generation
    failure, costing pairs on nearly every session. `raw_decode` reads the
    leading value and ignores whatever follows, so trailing commentary is no
    longer fatal. A reply that opens with prose is still handled, by starting
    at the first brace.
    """
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    decoder = json.JSONDecoder()
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    if not positions:
        raise ValueError("model reply contained no JSON object")
    # Anchoring on the first brace is not enough: a reply that explains itself
    # first ("return an object like { this }") puts a brace before the real
    # payload. Try each candidate and keep the first that decodes.
    last_error: Exception | None = None
    for start in positions:
        try:
            result, _ = decoder.raw_decode(text[start:])
        except ValueError as exc:
            last_error = exc
            continue
        if isinstance(result, dict):
            return result
        last_error = ValueError(f"Expected JSON object, got {type(result).__name__}")
    raise last_error or ValueError("model reply contained no usable JSON object")


async def _chat_once(system: str, user: str) -> str:
    """One chat call, retried once if the transport fails.

    Ollama serialises requests per model, so generating while someone is
    querying can push a call past the client timeout. `httpx.ReadTimeout`
    carries an empty message, which is why these used to be recorded as an
    empty string. A transient timeout should cost a retry, not a pair.
    """
    try:
        return await ollama.chat(system, user)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("Ollama call failed (%s); retrying once", type(exc).__name__)
        return await ollama.chat(system, user)


# One initial attempt plus two reprompts. The model's failure mode is malformed
# JSON (a missing comma, an unterminated string), which is independent between
# attempts, so a second reprompt converts most remaining failures into pairs.
# Each attempt costs an LLM call, so the budget is small and fixed.
_GENERATION_ATTEMPTS = 3

# Three validation attempts, each allowing two nominal 300s transport calls.
# Bound the whole model operation: per-read transport timeouts are not a total
# wall-clock deadline. nginx and verification clients allow completion headroom.
_REGENERATION_TIMEOUT_SECONDS = 1800


@telemetry.traced("rag.pair")
async def _generate_pair(chunk: dict) -> dict:
    user_msg = f"Chunk:\n{chunk['content']}"
    data = None
    last_error: Exception | None = None
    for attempt in range(_GENERATION_ATTEMPTS):
        system = GS_SYSTEM if attempt == 0 else GS_SYSTEM + GS_RETRY_SUFFIX
        raw = await _chat_once(system, user_msg)
        try:
            candidate = _parse_gs_json(raw)
            for field in ("question", "answer", "ground_truth"):
                value = candidate.get(field, candidate.get("answer") if field == "ground_truth" else None)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Model {field} must be a nonempty string")
                candidate[field] = value
            data = candidate
            break
        except Exception as exc:                      # noqa: BLE001
            last_error = exc
            log.info("Pair generation attempt %d/%d did not yield valid JSON: %s",
                     attempt + 1, _GENERATION_ATTEMPTS, exc)
    if data is None:
        raise last_error or ValueError("no usable reply from the model")

    return {
        "pair_id": f"p_{uuid.uuid4().hex[:8]}",
        "question": data["question"],
        "answer": data["answer"],
        "contexts": [chunk["content"]],
        "ground_truth": data["ground_truth"],
        "source_file": str(chunk.get("source_file") or ""),
        "chunk_index": int(chunk.get("chunk_index", 0)),
        "status": "pending",
    }


def _failed_generation(current: dict, exc: Exception) -> None:
    current["status"] = "failed"
    reason = (f"{exc.code}: {exc.message}" if isinstance(exc, GoldStandardError)
              else str(exc) or type(exc).__name__)
    errors = current.setdefault("errors", [])
    if reason not in errors:
        errors.append(reason)
    if isinstance(exc, GoldStandardError):
        current["persistence_error"] = {"code": exc.code, "message": exc.message}


def _publish_generation_failure(session_id: str, exc: Exception, write_error: Exception) -> None:
    """Publish a failed status the store could not save, as marker failures do."""
    global _store_revision
    with _state_lock:
        current = _sessions.get(session_id)
        if current is None:
            return
        snapshot = copy.deepcopy(current)
        _failed_generation(snapshot, exc)
        _failed_generation(snapshot, write_error)
        _sessions[session_id] = snapshot
        _store_revision += 1


@telemetry.traced("rag.failure_report")
async def _record_generation_failure(session_id: str, exc: Exception) -> None:
    try:
        await asyncio.to_thread(_update_session_sync, session_id,
                                lambda current: _failed_generation(current, exc))
    except Exception as write_error:
        telemetry.outcome("error", write_error)
        log.exception("Could not durably report failed generation for %s", session_id)
        # Otherwise `generating` stays in the cache for as long as the fault
        # lasts, and the UI polls it forever. The cache runs ahead of disk;
        # the next successful write of this session saves the failed status.
        # The lock can be held through fsync, so this stays off the event loop.
        try:
            await asyncio.to_thread(_publish_generation_failure, session_id, exc, write_error)
        except Exception:
            log.exception("Could not publish failed generation for %s", session_id)


@telemetry.traced("rag.evaluation")
async def _run_generation(session_id: str, chunks: list[dict]) -> None:
    cancelled = False
    persistence_failure = None
    try:
        for chunk in chunks:
            try:
                pair = await _generate_pair(chunk)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                telemetry.outcome("partial", exc)
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Gold-standard pair generation failed: %s", reason, exc_info=True)
                def failed(current):
                    current["pairs_failed"] = current.get("pairs_failed", 0) + 1
                    current.setdefault("errors", []).append(reason)
                    current["pairs_attempted"] = current.get("pairs_attempted", 0) + 1
                await asyncio.to_thread(_update_session_sync, session_id, failed)
            else:
                def completed(current):
                    current["pairs"].append(pair)
                    current["pairs_completed"] += 1
                    current["pairs_attempted"] = current.get("pairs_attempted", 0) + 1
                await asyncio.to_thread(_update_session_sync, session_id, completed)
    except GoldStandardError as exc:
        persistence_failure = exc
        raise
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        await _settle_generation(session_id, cancelled, persistence_failure)


async def _settle_generation(session_id: str, cancelled: bool,
                             persistence_failure: Exception | None = None) -> None:
    # Before the status turns terminal: a client that sees generation end
    # can delete the collection at once (#40).
    end_job("goldstandard", session_id)
    def finish(current):
        if persistence_failure is not None:
            _failed_generation(current, persistence_failure)
            return
        if current.get("status") == "generating":
            current["status"] = ("cancelled" if cancelled else
                "failed" if not current["pairs"] and current.get("errors") else "completed")
        if current.get("status") == "failed":
            telemetry.outcome("error")
    await asyncio.to_thread(_update_session_sync, session_id, finish)


def _track_settling(session_id: str, task: asyncio.Task) -> None:
    """Keep a settling task alive, and report it if it raises, as _on_task_done does."""
    _tasks.add(task)
    def done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            reporter = asyncio.create_task(_record_generation_failure(session_id, exc))
            _tasks.add(reporter)
            reporter.add_done_callback(_tasks.discard)
    task.add_done_callback(done)


def _settle_abandoned_start(prepare: asyncio.Future) -> None:
    """The request that started a generation was cancelled while it was being
    prepared (#302). The preparation still ran to the end in its thread; if it
    published and registered a session, nothing will generate for it, so it is
    settled the way a cancelled generation is: registration ended, `cancelled`."""
    if prepare.cancelled() or prepare.exception() is not None:
        return                        # nothing was registered
    session, _ = prepare.result()
    session_id = session["session_id"]
    _track_settling(session_id, asyncio.create_task(_settle_generation(session_id, cancelled=True)))


_GENERATION_GUARD_TIMEOUT_SECONDS = 1.0


def _prepare_generation_sync(collection: str, sample_size: int, seed: int | None):
    """Publish the sampled corpus identity before a writer can invalidate it."""
    from services.collection_writes import (CollectionBusyError, delete_in_progress,
                                            deleting_message, guard)
    # Before the guard's one-second wait, so a delete in progress is reported as
    # one, like the other job kinds report it (#302).
    if delete_in_progress(collection):
        raise GoldStandardError("COLLECTION_DELETING", deleting_message(collection), 409,
                                detail={"collection": collection})
    try:
        with guard(collection, timeout=_GENERATION_GUARD_TIMEOUT_SECONDS):
            all_chunks = wc._sample_chunks_sync(collection, limit=sample_size, seed=seed)
            actual_size = len(all_chunks)

            session = {
                "session_id": "",
                "collection": collection,
                "status": "generating",
                "pairs_total": actual_size,
                # `attempted` drives progress and always reaches `total`; `completed`
                # counts pairs that actually exist. Reporting one number for both made
                # a session with a failed pair read "3/3" while holding 2.
                "pairs_attempted": 0,
                "pairs_completed": 0,
                "pairs_failed": 0,
                "pairs": [],
            }
            session = _store_generated_session(session)
            # Still inside the guard, so no delete can pass between publication
            # and registration; DELETE /collections refuses from here (#40).
            register_job(collection, "goldstandard", session["session_id"])
            return session, all_chunks
    except CollectionBusyError as exc:
        raise GoldStandardError("COLLECTION_BUSY",
            f"Collection '{collection}' is busy. Retry after its current operation finishes.", 409) from exc
    except wc.CollectionNotFoundError as exc:
        raise GoldStandardError("COLLECTION_NOT_FOUND",
            f"Collection '{collection}' not found.", 404) from exc


async def start_generation(
    collection: str,
    sample_size: int,
    seed: int | None,
) -> dict:
    from models.schemas import GenerateRequest
    request = GenerateRequest(collection=collection, sample_size=sample_size, seed=seed)
    # Shielded: cancelling a plain to_thread await loses the thread's result, and
    # with it the registration it made, which then refused DELETE until a
    # restart (#302). A cancelled caller hands the result to a settle step.
    prepare = asyncio.ensure_future(asyncio.to_thread(
        _prepare_generation_sync, collection, request.sample_size, request.seed))
    try:
        session, all_chunks = await asyncio.shield(prepare)
    except asyncio.CancelledError:
        prepare.add_done_callback(_settle_abandoned_start)
        raise
    session_id = session["session_id"]

    try:
        task = asyncio.create_task(telemetry.admitted(_run_generation)(session_id, all_chunks))
    except BaseException:
        end_job("goldstandard", session_id)
        raise
    _tasks.add(task)

    report_failure = telemetry.admitted(_record_generation_failure)

    def _on_task_done(t: asyncio.Task) -> None:
        # _run_generation ends it first; this covers a task that never ran it.
        end_job("goldstandard", session_id)
        _tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            reporter = asyncio.create_task(report_failure(session_id, exc))
            _tasks.add(reporter)
            reporter.add_done_callback(_tasks.discard)

    task.add_done_callback(_on_task_done)

    return {
        "session_id": session_id,
        "status": "generating",
        "pairs_total": session["pairs_total"],
        "pairs_completed": 0,
    }


def _session_not_found(session_id: str) -> GoldStandardError:
    return GoldStandardError("SESSION_NOT_FOUND", f"Session '{session_id}' not found.", 404)


async def update_pair(session_id: str, pair_id: str, updates: dict) -> dict | None:
    """The updated pair, or None for an unknown pair. An unknown session raises
    SESSION_NOT_FOUND, so the two 404s stay apart (#381)."""
    def change(current):
        for pair in current["pairs"]:
            if pair["pair_id"] == pair_id:
                pair.update({key: value for key, value in updates.items() if value is not None})
                return pair
        return None

    def apply():
        with _state_lock:
            if session_id not in _sessions:
                raise _session_not_found(session_id)
            return _update_session_sync(session_id, change)
    return await asyncio.to_thread(apply)


@telemetry.traced("rag.regenerate")
async def regenerate_pair(session_id: str, pair_id: str) -> dict | None:
    session = get_session(session_id)
    if session is None:
        raise _session_not_found(session_id)
    if session.get("status") == "generating":
        # The generation loop is appending to session["pairs"] and saving it;
        # regenerating underneath that races with it and can lose a pair.
        raise GoldStandardError(
            "GENERATION_IN_PROGRESS",
            f"Session '{session_id}' is still generating. Wait for it to finish "
            "before regenerating a pair.", 409)
    for i, pair in enumerate(session["pairs"]):
        if pair["pair_id"] == pair_id:
            chunk = {
                "content": pair["contexts"][0],
                "source_file": pair["source_file"],
                "chunk_index": pair["chunk_index"],
            }
            try:
                new_pair = await asyncio.wait_for(
                    _generate_pair(chunk), timeout=_REGENERATION_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise GoldStandardError(
                    "PAIR_GENERATION_TIMEOUT",
                    "The model exceeded the regeneration time limit. "
                    "The existing pair is unchanged; try again.", 504) from exc
            except Exception as exc:                  # noqa: BLE001
                # The model regularly returns unparseable JSON. Generation
                # records that and moves on; regeneration used to let it escape
                # as a bare 500. Some of these carry an empty str(), so the
                # type name is always included or the message says nothing.
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Regeneration of pair %s failed: %s", pair_id, reason, exc_info=True)
                raise GoldStandardError(
                    "PAIR_GENERATION_FAILED",
                    f"The model did not return a usable question/answer pair "
                    f"({reason}). The existing pair is unchanged; try again.", 502) from exc
            new_pair["pair_id"] = pair_id
            def replace(current):
                for position, existing in enumerate(current["pairs"]):
                    if existing["pair_id"] == pair_id:
                        if existing != pair or current.get("status") == "generating":
                            raise GoldStandardError("PAIR_CHANGED_DURING_REGENERATION",
                                "The pair changed while regeneration was running. Its acknowledged edits are preserved; refresh before retrying.", 409)
                        current["pairs"][position] = new_pair
                        return new_pair
                return None
            return await asyncio.to_thread(_update_session_sync, session_id, replace)
    return None


# Exports have their own folder so that a caller-chosen name can never reach the
# registry, the metrics file or the session store beside them (#370).
EXPORT_DIRNAME = "goldstandard_exports"
_EXPORT_NAME_MAX = 128
_EXPORT_NAME_RE = re.compile(r"[A-Za-z0-9_. -]{1,%d}" % _EXPORT_NAME_MAX)


def export_path(filename) -> Path:
    """The path of export `filename`, or INVALID_PATH when the name breaks the rule."""
    if (not isinstance(filename, str) or not _EXPORT_NAME_RE.fullmatch(filename)
            or not filename.lower().endswith(".json")):
        raise GoldStandardError(
            "INVALID_PATH",
            "Invalid filename. Use 1-128 ASCII letters, digits, '_', '-', '.' or spaces, "
            "ending in .json, with no path.", 400)
    return Path(settings.upload_dir).resolve() / EXPORT_DIRNAME / filename


def export_entry_is_other(path: Path) -> bool:
    """True when something other than a regular file (a directory, a link) has the name."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    return not stat.S_ISREG(mode)


def export_folder_exists(folder: Path) -> bool:
    """False when the exports folder is missing; True when it is a directory.

    Anything else with its name (a file saved as `goldstandard_exports` before #370,
    a symbolic link) is refused rather than followed or failed on (#402).
    """
    try:
        mode = os.lstat(folder).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise GoldStandardError(
            "EXPORTS_FOLDER_INVALID",
            f"'{EXPORT_DIRNAME}' in the upload folder is not a directory (a file or a "
            "link has its name). Move or rename it, then retry.", 409)
    return True


# Published cache snapshots are immutable. Export captures one stable reference
# and never mutates or exposes it; later commits publish a different object.
# The export goes to a new temporary file that replaces the name, so a link at
# the name is replaced rather than followed, and no reader sees half a file (#402).
def _save_export_sync(out_path: Path, ragas: list[dict]) -> None:
    _replace_durably(out_path, json.dumps(ragas, indent=2))


async def save_session(session_id: str, filename: str | None, allow_historical: bool = False) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None

    # Keep direct Python callers as strict as the HTTP request model.
    if not isinstance(allow_historical, bool):
        raise ValueError("allow_historical must be a boolean")
    validity = SessionValidity.model_validate(session).model_dump()
    historical = validity["stale"] or validity["orphaned"]
    if historical and not allow_historical:
        raise GoldStandardError(
            "HISTORICAL_SESSION",
            "This retained session is stale or orphaned and is not a current "
            "collection baseline. Inspect its validity metadata and explicitly "
            "set allow_historical=true to export historical pairs.", 409)

    approved = [p for p in session["pairs"] if p["status"] in ("approved", "edited")]
    excluded = len(session["pairs"]) - len(approved)

    collection = session.get("collection", "export")
    if not filename:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_]", "", collection.replace(" ", "_"))
        suffix = f"_{ts}.json"
        filename = safe[:_EXPORT_NAME_MAX - len(suffix)] + suffix

    # The name is checked as given; a path is refused, not stripped (#370).
    out_path = export_path(filename)
    if not export_folder_exists(out_path.parent):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        export_folder_exists(out_path.parent)
    if export_entry_is_other(out_path):
        raise GoldStandardError(
            "INVALID_PATH", f"'{filename}' is not an export file.", 400)

    ragas = [
        {
            "question": p["question"],
            "answer": p["answer"],
            "contexts": p["contexts"],
            "ground_truth": p["ground_truth"],
        }
        for p in approved
    ]
    await asyncio.to_thread(_save_export_sync, out_path, ragas)

    return {
        "filename": filename,
        "pairs_saved": len(approved),
        "pairs_excluded": excluded,
        "download_url": f"/api/goldstandard/download/{filename}",
        "historical": historical,
        "session_validity": validity,
    }
