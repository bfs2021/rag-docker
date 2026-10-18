"""Job records that survive an API restart (#23).

Ingest, export, import and tune jobs used to keep their status in a plain dict,
so any restart (a crash, an OOM kill, `docker compose restart`) turned every job
the UI was polling into `404 JOB_NOT_FOUND`. A `JobStore` is used exactly like
the dict it replaces (`store[job_id] = {...}`, `job = store[job_id]`,
`job["status"] = "running"`, `job.update(...)`, `store.get(job_id)`), and once
the API's startup has called `load()`, it also keeps one JSON file per job under
`<UPLOAD_DIR>/jobs/<kind>/<job_id>.json`.

**Unbound until loaded.** Before `load()` a store is only an in-memory dict that
keeps the caller's own dict object. Verify helpers run inside the API container,
on the same upload volume, and store jobs directly (some then assert on the dict
they stored); without this rule they would leave job files that the next restart
loads. Only a process that runs the API lifespan binds its stores.

**When a change reaches the disk.** A record changed through its own methods is
written at once when its `status` changes, and at once whenever its status is
already terminal (import names its recovery copy after `finish`). Other changes
(progress counters, which export and import update every few hundred chunks) are
written at most once a second per job, and never later than about a second
after they were made. Changes made in place to a nested list or dict
(`job["errors"].append(...)`) are not seen by themselves; the next write
carries them. Every job path ends with a status change, so the final record on
disk is complete.

**At startup** `load()` marks every record still `queued` or `running` as
interrupted: its worker thread died with the old process. Unreadable files are
left as they are and logged. The newest `RETENTION` files per kind are kept.

A job record is never recovery authority: retained import and tuning data is
owned by `collection_recovery` and the import markers (#43, #44), never by
anything in `jobs/`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import MutableMapping
from pathlib import Path

from config import settings

_log = logging.getLogger(__name__)

KINDS = ("ingest", "export", "import", "tune")
# The statuses a job never leaves; the same set scripts/verify/lib.sh waits for.
TERMINAL = frozenset({"completed", "failed", "partial", "cancelled"})
# Progress-only changes are written at most this often per job. Export and import
# report progress every few hundred chunks; writing each one would be a disk-I/O
# regression on a large export.
THROTTLE_S = 1.0
# Records kept per kind, newest first by modification time. About 1 KB each, so
# the volume can't grow without bound. Nothing in jobs/ is ever recovery data.
RETENTION = 200
# Generated ids are 8 hex characters; anything else could name another path.
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

INTERRUPTED_MESSAGE = "Interrupted: the API restarted while this job was running."
_REMEDY = {
    "ingest": "Re-upload the files to try again.",
    "export": "Start the export again.",
    "import": "Start the import again.",
    "tune": "Start the operation again.",
}


class _Job(dict):
    """A record bound to its store: changes made through its methods are saved."""

    __slots__ = ("_store", "_id", "_last", "_timer")

    def __setitem__(self, key, value):
        status_changed = key == "status" and self.get("status") != value
        dict.__setitem__(self, key, value)
        self._store._changed(self, status_changed)

    def update(self, *args, **kwargs):
        before = self.get("status")
        dict.update(self, *args, **kwargs)
        self._store._changed(self, self.get("status") != before)

    def setdefault(self, key, default=None):
        if key in self:
            return dict.__getitem__(self, key)
        dict.__setitem__(self, key, default)
        self._store._changed(self, key == "status")
        return default

    def pop(self, key, *default):
        status_changed = key == "status" and key in self
        value = dict.pop(self, key, *default)
        self._store._changed(self, status_changed)
        return value

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._store._changed(self, key == "status")


class JobStore(MutableMapping):
    """The job records of one kind; see the module docstring."""

    def __init__(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"Unknown job kind {kind!r}")
        self.kind = kind
        self._records: dict = {}
        self._dir: Path | None = None          # None: unbound, memory only
        # One lock per store. Re-entrant because load() and _changed() write
        # while holding it.
        self._lock = threading.RLock()

    # ── mapping ─────────────────────────────────────────────────────────────
    # Lookups never touch the disk: after load() memory holds every kept record,
    # and a job id from a URL is never turned into a path.

    def __getitem__(self, job_id):
        return self._records[job_id]

    def __iter__(self):
        return iter(list(self._records))

    def __len__(self):
        return len(self._records)

    def __setitem__(self, job_id, doc):
        if self._dir is None:
            self._records[job_id] = doc        # the caller's own object
            return
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ValueError(f"Invalid job id {job_id!r}")
        record = self._bind(job_id, doc)
        with self._lock:
            old = self._records.get(job_id)
            if isinstance(old, _Job):
                self._cancel(old)
            self._records[job_id] = record
            self._write(record, durable=True)

    def __delitem__(self, job_id):
        with self._lock:
            record = self._records.pop(job_id)
            if isinstance(record, _Job):
                self._cancel(record)
            if self._dir is not None and isinstance(job_id, str) and _ID.fullmatch(job_id):
                try:
                    (self._dir / f"{job_id}.json").unlink(missing_ok=True)
                except OSError:
                    _log.exception("Could not remove %s job record %s", self.kind, job_id)

    # ── persistence ─────────────────────────────────────────────────────────

    def _bind(self, job_id: str, doc) -> _Job:
        record = _Job(doc)
        record._store = self
        record._id = job_id
        record._last = 0.0
        record._timer = None
        return record

    @staticmethod
    def _cancel(record: _Job) -> None:
        if record._timer is not None:
            record._timer.cancel()
            record._timer = None

    def _changed(self, record: _Job, status_changed: bool) -> None:
        with self._lock:
            if status_changed or record.get("status") in TERMINAL:
                self._cancel(record)
                self._write(record, durable=True)
                return
            now = time.monotonic()
            if now - record._last >= THROTTLE_S:
                self._cancel(record)
                self._write(record, durable=False)
            elif record._timer is None:
                # Carry this change to disk even if the job changes no further
                # (an ingest file can embed for tens of seconds after it).
                timer = threading.Timer(record._last + THROTTLE_S - now, self._flush, (record,))
                timer.daemon = True
                record._timer = timer
                timer.start()

    def _flush(self, record: _Job) -> None:
        with self._lock:
            # A cancelled or superseded timer does nothing, so a late timer
            # never adds a second write to one window.
            if record._timer is not threading.current_thread():
                return
            record._timer = None
            self._write(record, durable=False)

    def _write(self, record: _Job, durable: bool) -> None:
        """Atomically replace the record's file. Never raises: a job's outcome
        must not depend on whether its status could be saved."""
        with self._lock:
            if self._dir is None or self._records.get(record._id) is not record:
                return                          # deleted or replaced meanwhile
            tmp = None
            try:
                payload = json.dumps(dict(record), default=str)
                # mkstemp creates the file 0600, like the session files.
                fd, tmp = tempfile.mkstemp(prefix=f".{record._id}.json.", suffix=".tmp",
                                           dir=self._dir)
                with os.fdopen(fd, "w") as output:
                    output.write(payload)
                    if durable:
                        output.flush()
                        os.fsync(output.fileno())
                os.replace(tmp, self._dir / f"{record._id}.json")
                tmp = None
                if durable:
                    _sync_dir(self._dir)
            except Exception:                   # noqa: BLE001 - logged, never raised
                _log.exception("Could not save %s job %s", self.kind, record._id)
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            finally:
                # Also after a failure, so a lasting fault is retried at the
                # throttle's pace rather than on every change.
                record._last = time.monotonic()

    # ── startup ─────────────────────────────────────────────────────────────

    def load(self) -> list[str]:
        """Bind to the disk, apply retention and mark interrupted jobs.

        Called once, by the API lifespan, before any job can start. Returns the
        ids of the jobs it marked interrupted.
        """
        with self._lock:
            directory = Path(settings.upload_dir) / "jobs" / self.kind
            directory.mkdir(parents=True, exist_ok=True)
            self._dir = directory

            for leftover in directory.glob(".*.json.*.tmp"):
                try:
                    leftover.unlink()
                except OSError:
                    _log.exception("Could not remove interrupted write %s", leftover)

            files = [p for p in directory.glob("*.json") if not p.name.startswith(".")]
            readable = {}
            for path in files:
                try:
                    readable[path] = _read_record(path)
                except (OSError, ValueError) as exc:
                    # Kept as it is: never rewritten or reset (only retention,
                    # below, removes it once it is old).
                    _log.warning("Unreadable %s job record %s kept as it is: %s",
                                 self.kind, path, exc)

            files.sort(key=_mtime, reverse=True)
            for path in files[RETENTION:]:
                try:
                    path.unlink()
                    _log.info("Removed old %s job record %s (keeping the newest %d)",
                              self.kind, path.name, RETENTION)
                except OSError:
                    _log.exception("Could not remove old job record %s", path)

            marked = []
            for path in files[:RETENTION]:
                if path not in readable:
                    continue
                record = self._bind(path.stem, readable[path])
                self._records[path.stem] = record
                if record.get("status") not in TERMINAL:
                    self._mark_interrupted(record)
                    self._write(record, durable=True)
                    marked.append(path.stem)
            return marked

    def _mark_interrupted(self, record: _Job) -> None:
        # Plain dict methods only: one durable write afterwards, so no record
        # reaches the disk terminal but without its message.
        if self.kind == "ingest":
            completed = dict.get(record, "files_completed")
            dict.__setitem__(record, "status", "partial"
                             if isinstance(completed, int) and completed > 0 else "failed")
            errors = dict.get(record, "errors")
            message = f"{INTERRUPTED_MESSAGE} {_REMEDY['ingest']}"
            if isinstance(errors, list):
                errors.append(message)
            else:
                dict.__setitem__(record, "errors", [message])
        else:
            dict.__setitem__(record, "status", "failed")
            dict.__setitem__(record, "error_code", "INTERRUPTED")
            dict.__setitem__(record, "error", f"{INTERRUPTED_MESSAGE} {_REMEDY[self.kind]}")


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


def _read_record(path: Path) -> dict:
    """The record in `path`, or ValueError/OSError saying why it is unusable."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("not a regular file")
    if not _ID.fullmatch(path.stem):
        raise ValueError("not a job id")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if not isinstance(data.get("status"), str):
        raise ValueError("no status")
    if "job_id" in data and data["job_id"] != path.stem:
        raise ValueError("its job_id doesn't match its name")
    return data


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
