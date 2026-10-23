from __future__ import annotations
import asyncio
import json
import logging
import os
import statistics
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import settings

MAX_RECORDS = 500
_ring: deque[dict] = deque(maxlen=MAX_RECORDS)

# metrics.jsonl is bounded (#374): once an append brings it to COMPACT_AT lines it
# is compacted to its last MAX_RECORDS records, and startup reads at most its last
# _TAIL_BYTES, which hold far more than MAX_RECORDS records.
COMPACT_AT = 2 * MAX_RECORDS
_TAIL_BYTES = 1024 * 1024
_TMP_PREFIX = ".metrics.jsonl."
_TMP_SUFFIX = ".tmp"

_METRICS_FILE = None
# Appends and compactions run in worker threads, one per query; the lock keeps
# an append from landing in a file that is being replaced.
_file_lock = threading.Lock()
_file_lines = 0         # lines in the file since the last load or compaction

_log = logging.getLogger(__name__)


def _metrics_path() -> Path:
    global _METRICS_FILE
    if _METRICS_FILE is None:
        p = Path(settings.upload_dir)
        p.mkdir(parents=True, exist_ok=True)
        _METRICS_FILE = p / "metrics.jsonl"
    return _METRICS_FILE


def _metrics_file_name() -> Path:
    """The metrics file's path for log messages, known even when creating its folder failed."""
    return _METRICS_FILE or Path(settings.upload_dir) / "metrics.jsonl"


def _read_tail(path: Path) -> tuple[list[str], bool]:
    """The last MAX_RECORDS valid lines of `path`, read from at most its last
    _TAIL_BYTES, and whether they are all the file holds."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - _TAIL_BYTES)
        f.seek(start)
        data = f.read(_TAIL_BYTES)
    if start:
        # The read began inside a line: drop that partial line.
        data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
    lines, extra = [], start > 0
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            line = raw.decode("utf-8")
            json.loads(line)
        except ValueError:
            extra = True                    # not a record: compacted away
            continue
        lines.append(line)
    if len(lines) > MAX_RECORDS:
        lines, extra = lines[-MAX_RECORDS:], True
    return lines, not extra


def _compact_locked(path: Path, lines: list[str]) -> None:
    """Atomically replace `path` with `lines`. Raises on failure, leaving the old
    file whole and no temporary file. Call with _file_lock held."""
    global _file_lines
    fd, tmp = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write("".join(line + "\n" for line in lines))
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _file_lines = len(lines)


def load_from_disk() -> None:
    """Load the last MAX_RECORDS records into the ring. Never raises: a metrics
    file that can't be read or compacted must not stop the API starting."""
    global _file_lines
    with _file_lock:
        try:
            p = _metrics_path()
            for leftover in p.parent.glob(_TMP_PREFIX + "*" + _TMP_SUFFIX):
                # A compaction interrupted by a crash; metrics.jsonl is whole.
                leftover.unlink(missing_ok=True)
            if not p.exists():
                return
            lines, whole = _read_tail(p)
        except Exception:                                 # noqa: BLE001
            _log.exception("Could not load the query metrics from %s", _metrics_file_name())
            return
        for line in lines:
            _ring.append(json.loads(line))
        _file_lines = len(lines)
        if not whole:
            try:
                _compact_locked(p, lines)
            except Exception:                             # noqa: BLE001
                _log.exception("Could not compact %s; it is tried again after later queries", p)


def _append_sync(line: str) -> None:
    global _file_lines
    with _file_lock:
        p = _metrics_path()
        with open(p, "a+b") as f:
            payload = line.encode("utf-8") + b"\n"
            if f.seek(0, os.SEEK_END):
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    # An earlier write stopped part-way: start a line of our own.
                    payload = b"\n" + payload
            f.write(payload)
        _file_lines += 1
        if _file_lines >= COMPACT_AT:
            try:
                _compact_locked(p, _read_tail(p)[0])
            except Exception:                             # noqa: BLE001
                _log.exception("Could not compact %s; it is tried again after the next query", p)


async def record(
    collection: str,
    retrieval_mode: str,
    retrieval_ms: int,
    llm_ms: int,
    chunks_retrieved: int,
) -> None:
    """Add a query's record to the ring and append it to metrics.jsonl. Never
    raises: the query has its answer, and a failed write must not lose it (#374)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "collection": collection,
        "retrieval_mode": retrieval_mode,
        "retrieval_ms": retrieval_ms,
        "llm_ms": llm_ms,
        "total_ms": retrieval_ms + llm_ms,
        "chunks_retrieved": chunks_retrieved,
    }
    _ring.append(entry)
    try:
        await asyncio.to_thread(_append_sync, json.dumps(entry))
    except Exception:                                     # noqa: BLE001
        _log.warning("Could not write the query metrics record for %r to %s; "
                     "the answer is still returned", collection, _metrics_file_name(),
                     exc_info=True)


def _percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    data_sorted = sorted(data)
    k = (len(data_sorted) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(data_sorted) - 1)
    frac = k - lo
    return round(data_sorted[lo] + frac * (data_sorted[hi] - data_sorted[lo]), 1)


def _stats(values: list[float]) -> dict:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "count": 0}
    return {
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "mean": round(statistics.mean(values), 1),
        "count": len(values),
    }


def get_summary(
    collection: str | None = None,
    retrieval_mode: str | None = None,
    history_limit: int = 100,
) -> dict:
    records = list(_ring)
    if collection:
        records = [r for r in records if r.get("collection") == collection]
    if retrieval_mode:
        records = [r for r in records if r.get("retrieval_mode") == retrieval_mode]

    retrieval = [r["retrieval_ms"] for r in records]
    llm = [r["llm_ms"] for r in records]
    total = [r["total_ms"] for r in records]

    # Most recent `history_limit` records, kept in chronological order. The ring
    # buffer is already oldest-first, so the tail is the newest slice.
    recent = records[-history_limit:] if history_limit > 0 else []
    history = [
        {
            "timestamp": r.get("ts", ""),
            "collection": r.get("collection", ""),
            "retrieval_mode": r.get("retrieval_mode", ""),
            "retrieval_ms": r.get("retrieval_ms", 0),
            "llm_ms": r.get("llm_ms", 0),
            "total_ms": r.get("total_ms", 0),
        }
        for r in recent
    ]

    return {
        "total_records": len(records),
        "retrieval_latency": _stats(retrieval),
        "llm_latency": _stats(llm),
        "total_latency": _stats(total),
        "history": history,
    }
