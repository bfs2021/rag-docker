from __future__ import annotations
from services import telemetry
import asyncio
import errno
import gzip
import io
import json
import logging
import mimetypes
import os
import posixpath
import shutil
import tempfile
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from services import batch_write, collection_writes, jobstore
from config import settings
from models.schemas import IngestConfig
from services.chunker import MAX_TEXT_CHARACTERS, chunk as do_chunk
from services import sources
from services import weaviate_client as wc

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".jsonl"}
# Gzipped JSON is accepted inside a ZIP only (issue #290); gzipped JSON Lines
# directly or inside a ZIP (#291). Either is decompressed when the job parses
# it: a .json.gz never past MAX_JSON_INPUT_BYTES (#300), a .jsonl.gz never past
# MAX_GZIP_DECOMPRESSED_BYTES. The bounds are per file, so several gzipped
# members could each reach them: a ZIP's members, their gzip output included,
# are bounded together by settings.max_zip_expanded_bytes (#298). A directly
# uploaded .jsonl.gz is bounded by its own limit alone.
GZIP_JSON_SUFFIX = ".json.gz"
GZIP_JSONL_SUFFIX = ".jsonl.gz"
MAX_GZIP_DECOMPRESSED_BYTES = 512 * 1024 * 1024
# The most of one JSON Lines line that is read (#291). A line is parsed whole,
# so this bounds the memory one record can take (a 10 MB line of small numbers
# parses to about 230 MB). Files beyond the per-file limits are #312's subject.
# It counts neither the line ending (`\n` or `\r\n`) nor line 1's byte-order
# mark, so a CRLF file and an LF file behave the same (#314).
MAX_JSONL_LINE_BYTES = 10_000_000

# Job records survive an API restart (#23); see jobstore.py.
_jobs = jobstore.JobStore("ingest")
_log = logging.getLogger(__name__)


class UploadRejected(Exception):
    """An upload that cannot be unpacked. The whole request is refused with a
    readable 4xx instead of the bare 500 an unhandled exception became (#290)."""

    def __init__(self, code: str, message: str, file: str,
                 members: list[dict[str, str]] | None = None, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = {"file": file, "members": members or []}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _is_json_gz(name: str) -> bool:
    return name.lower().endswith(GZIP_JSON_SUFFIX)


def _is_jsonl_gz(name: str) -> bool:
    return name.lower().endswith(GZIP_JSONL_SUFFIX)


def _is_jsonl(name: str) -> bool:
    return name.lower().endswith(".jsonl") or _is_jsonl_gz(name)


def _skip_reason(name: str) -> str:
    """Why a file is not ingested, for the job's `skipped` list."""
    return f"unsupported type '{Path(name.lower()).suffix or 'none'}'"


def source_type_of(path: Path) -> str:
    """The source_type stored with a file's chunks: what was parsed, so a
    gzipped JSON file is "json" and gzipped JSON Lines "jsonl", like plain ones."""
    if _is_json_gz(path.name):
        return "json"
    if _is_jsonl_gz(path.name):
        return "jsonl"
    return path.suffix.lower().lstrip(".")


def _save_upload(src, dest: Path) -> None:
    with dest.open("wb") as fh:
        shutil.copyfileobj(src, fh, length=1024 * 1024)


def _gzip_too_large() -> ValueError:
    return ValueError(
        f"decompresses to more than "
        f"{MAX_GZIP_DECOMPRESSED_BYTES // (1024 * 1024)} MB, the limit for "
        f"one compressed file; nothing from it was stored")


def _gunzip_bounded(src: Path, dest: Path) -> None:
    """Decompress a .json.gz into dest in blocks, stopping as soon as the output
    passes the JSON input limit (#300)."""
    written = 0
    try:
        with gzip.open(src, "rb") as gz, dest.open("wb") as out:
            while True:
                block = gz.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if written > MAX_JSON_INPUT_BYTES:
                    raise ValueError(_json_input_too_large("decompresses to more than"))
                out.write(block)
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise ValueError(
            f"is not a valid gzip file; it may be truncated or corrupt ({exc})") from exc


class _JsonError(ValueError):
    """A .json file that can't be read as JSON; `detail` is the parser's reason."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"is not valid JSON: {detail}")
        self.detail = detail


class _JsonTooLarge(_JsonError):
    """Valid JSON whose converted text would be over the per-file text limit."""

    def __init__(self) -> None:
        detail = (f"converts to more than {MAX_JSON_TEXT_CHARACTERS:,} characters of text, "
                  f"the per-file limit for every chunking strategy; split the file")
        ValueError.__init__(self, detail)
        self.detail = detail


# The most text one JSON file may convert to (#295 review). Each line repeats its
# full key path, so a small file with a long key over many values could otherwise
# expand to gigabytes. This is the chunker's own per-file limit: a file whose
# records hold more text than this is refused by every chunking strategy anyway,
# so the bound refuses nothing that could have been ingested; it only refuses
# before the text is built.
MAX_JSON_TEXT_CHARACTERS = MAX_TEXT_CHARACTERS
# The most bytes of one JSON file that are parsed (#300). A .json file is parsed
# whole, before the text bound can be checked, and parsing takes up to about 34
# times the file's size (`[[1],[1],...]`), so this bounds one file's parse at
# about 1 GB. A .json.gz member stops decompressing here. JSON Lines files are
# read a line at a time and have their own limits.
MAX_JSON_INPUT_BYTES = 30_000_000
# The most records one JSON file may keep (#300). Each kept record of a
# top-level array becomes its own element, and the text bound alone still
# allows millions of tiny ones (`[[1],[1],...]`, 3 characters each with the
# blank line between records). A file of more than this many records averages
# under 8 characters a record; any other passes the text bound first.
# It also bounds a JSON Lines file's lines (#314): every line counts, blank ones
# and records with no text included, since those spend none of the text bound.
# An element-schema file's items count too (#321): partition_json builds each
# into an element, whatever its text.
MAX_JSON_RECORDS = 1_000_000


def _json_input_too_large(verb: str) -> str:
    return (f"{verb} {MAX_JSON_INPUT_BYTES:,} bytes, the most one JSON file can hold; "
            f"split the file or convert it to JSON Lines")


def _too_many_records() -> str:
    # One message for the JSON record cap and the JSON Lines line cap (#330).
    return (f"has more than {MAX_JSON_RECORDS:,} records, the most one file can hold; "
            f"split the file")


class _JsonTooManyRecords(_JsonError):
    """Valid JSON with more kept records than one file may hold (#300)."""

    def __init__(self) -> None:
        detail = _too_many_records()
        ValueError.__init__(self, detail)
        self.detail = detail


class _JsonNumber:
    """A JSON number kept as written in the file, so 1.50 stays 1.50. Not a str,
    so element-shape checks see a number, as partition_json does."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __str__(self) -> str:
        return self.text


def _reject_constant(name: str) -> Any:
    raise _JsonError(f"{name} is not a JSON value")


def _read_json(path: Path) -> tuple[str, Any]:
    """The file's text without a byte-order mark, and its JSON value; the value is
    None for an empty file (and for JSON `null`, which the text tells apart)."""
    try:
        # utf-8-sig: a leading byte-order mark, as some Windows tools write, is dropped.
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _JsonError(f"it is not UTF-8 text ({exc.reason} at byte {exc.start})") from exc
    if not raw.strip():
        return "", None
    try:
        return raw, json.loads(raw, parse_int=_JsonNumber, parse_float=_JsonNumber,
                                parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise _JsonError(f"{exc.msg} at line {exc.lineno}, column {exc.colno}") from exc
    except RecursionError as exc:
        raise _JsonError("it is nested too deeply to read") from exc


def _is_json_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


def _json_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


class _JsonBudget:
    """Characters of converted text left for this file."""

    def __init__(self) -> None:
        self.left = MAX_JSON_TEXT_CHARACTERS

    def spend(self, characters: int) -> None:
        self.left -= characters
        if self.left < 0:
            raise _JsonTooLarge()


# A key path is a chain of (parent, segment, length) links, written out only for a
# line that fits the budget, so no path longer than the text it produces is built.
def _path_child(path: Any, segment: str) -> Any:
    return (path, segment, len(segment) + (path[2] if path else 0))


def _path_text(path: Any) -> str:
    parts = []
    while path is not None:
        parts.append(path[1])
        path = path[0]
    return "".join(reversed(parts))


def _json_line(path: Any, text: str, out: list[str], budget: _JsonBudget) -> None:
    # A newline joins this line to the one before it in the record.
    budget.spend((path[2] + 2 if path else 0) + len(text) + (1 if out else 0))
    out.append(f"{_path_text(path)}: {text}" if path else text)


def _json_lines(value: Any, path: Any, out: list[str], budget: _JsonBudget) -> None:
    """Append `path: value` lines for value: dotted keys, indexed arrays (#295)."""
    if isinstance(value, dict):
        for key, item in value.items():
            _json_lines(item, _path_child(path, f".{key}" if path else key), out, budget)
    elif isinstance(value, list):
        if not value:
            return
        if all(_is_json_scalar(item) for item in value):
            items = [_json_scalar(item) for item in value]
            # Checked before joining, so an over-long line is never built.
            if sum(len(item) for item in items) + 2 * (len(items) - 1) > budget.left:
                raise _JsonTooLarge()
            _json_line(path, ", ".join(items), out, budget)
        else:
            for i, item in enumerate(value):
                _json_lines(item, _path_child(path, f"[{i}]"), out, budget)
    else:
        _json_line(path, _json_scalar(value), out, budget)


def _json_records(value: Any) -> list[str]:
    """Ordinary JSON as readable text: one record per item of a top-level array
    that holds any object or array, otherwise one record. Records with no text
    are dropped."""
    if isinstance(value, list) and not all(_is_json_scalar(item) for item in value):
        groups = value
    else:
        groups = [value]
    records = []
    budget = _JsonBudget()
    try:
        for group in groups:
            lines: list[str] = []
            left = budget.left
            _json_lines(group, None, lines, budget)
            text = "\n".join(lines)
            if text.strip():
                if records:
                    budget.spend(2)  # the blank line before this record (#300)
                if len(records) == MAX_JSON_RECORDS:
                    raise _JsonTooManyRecords()
                records.append(text)
            else:
                budget.left = left  # a dropped record's text is not stored
    except RecursionError as exc:
        raise _JsonError("it is nested too deeply to read") from exc
    return records


def _parse_json(path: Path) -> tuple[str, list[Any]]:
    """Ordinary JSON becomes `key: value` text, one element per record. A file of
    serialized Unstructured elements is rehydrated by partition_json (#295)."""
    # Checked before anything is read: the parse is the cost (#300).
    if path.stat().st_size > MAX_JSON_INPUT_BYTES:
        raise ValueError(_json_input_too_large("is larger than"))
    raw, value = _read_json(path)
    if not raw:
        return "", []
    from unstructured.partition.common.json_partitioning import is_element_shaped_dict
    if isinstance(value, list) and value and all(is_element_shaped_dict(i) for i in value):
        # Each item becomes one element, so the record cap applies to the items,
        # counted before any element is built (#321).
        if len(value) > MAX_JSON_RECORDS:
            raise _JsonTooManyRecords()
        from unstructured.partition.json import partition_json
        # partition_json parses the text again: drop the first parse first, so
        # the two are not held together (#300).
        value = None
        # The text already read, so a byte-order mark is handled the same way.
        elements = partition_json(text=raw)
        return "\n".join(str(el) for el in elements), elements
    from unstructured.documents.elements import Text
    records = _json_records(value)
    # A blank line between records, so the language splitter prefers to cut there.
    return "\n\n".join(records), [Text(text=record) for record in records]


def _parse_json_gz(path: Path) -> tuple[str, list[Any]]:
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".json")
    os.close(fd)
    plain = Path(name)
    try:
        _gunzip_bounded(path, plain)
        try:
            return _parse_json(plain)
        except (_JsonTooLarge, _JsonTooManyRecords) as exc:
            raise ValueError(f"was decompressed, but it {exc.detail}") from exc
        except _JsonError as exc:
            raise ValueError(
                f"was decompressed, but its JSON could not be parsed: {exc.detail}") from exc
        except Exception as exc:
            raise ValueError(
                f"was decompressed, but its JSON could not be parsed: "
                f"{str(exc) or type(exc).__name__}") from exc
    finally:
        plain.unlink(missing_ok=True)


class _JsonlError(ValueError):
    """A JSON Lines file that can't be ingested; `detail` names the line (#291)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _JsonlTooLarge(_JsonlError):
    """A JSON Lines file whose text passes the per-file limit, or with more lines
    than one file may hold (#314)."""


class _BoundedLines:
    """Lines of a decompressing stream, refusing more than the gzip limit."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.read = 0

    def readline(self, size: int) -> bytes:
        line = self.stream.readline(size)
        self.read += len(line)
        if self.read > MAX_GZIP_DECOMPRESSED_BYTES:
            raise _gzip_too_large()
        return line


_JSONL_BLANK = b" \t\r"
_BOM = b"\xef\xbb\xbf"


def _jsonl_records(stream: Any) -> list[str]:
    """The records of a JSON Lines file, one per line with text (#291).

    Read one line at a time, at most MAX_JSONL_LINE_BYTES of it besides its line
    ending and line 1's byte-order mark, and stopped at the first bad line, at
    the line past MAX_JSON_RECORDS, or when the file's text passes the per-file
    limit, so memory is bounded by one line and that limit, never by the file's
    size, and work by the line count.
    """
    budget = _JsonBudget()
    records: list[str] = []
    number = 0
    blank = None  # the first blank line not yet followed by a record
    while True:
        # Room for line 1's byte-order mark and a CRLF beyond the limit (#314).
        raw = stream.readline(MAX_JSONL_LINE_BYTES + len(_BOM) + 2)
        if not raw:
            break
        number += 1
        if number > MAX_JSON_RECORDS:
            # Every line counts, so lines with no text can't run on to the gzip bound.
            raise _JsonlTooLarge(_too_many_records())
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        shift = 0
        if number == 1 and raw.startswith(_BOM):
            raw, shift = raw[len(_BOM):], len(_BOM)
        # A read cut short has no newline and is at least 2 bytes over the limit.
        too_long = len(raw) > MAX_JSONL_LINE_BYTES
        if not too_long and not raw.strip(_JSONL_BLANK):
            if blank is None:
                blank = number
            continue
        # Trailing blank lines are accepted; one followed by a record is not.
        if blank is not None:
            raise _JsonlError(f"line {blank} is blank")
        if too_long:
            raise _JsonlError(
                f"line {number} is longer than {MAX_JSONL_LINE_BYTES:,} bytes, the most one "
                f"line can hold; split the record across lines or files")
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _JsonlError(f"line {number} is not UTF-8 text "
                              f"({exc.reason} at byte {exc.start + shift})") from exc
        try:
            value = json.loads(line, parse_int=_JsonNumber, parse_float=_JsonNumber,
                               parse_constant=_reject_constant)
            lines: list[str] = []
            left = budget.left
            _json_lines(value, None, lines, budget)
            text = "\n".join(lines)
            if text.strip():
                if records:
                    budget.spend(2)  # the blank line before this record
                records.append(text)
            else:
                budget.left = left  # a record with no text is not stored
        except _JsonTooLarge as exc:
            raise _JsonlTooLarge(
                f"converts to more than {MAX_JSON_TEXT_CHARACTERS:,} characters of text by "
                f"line {number}, the per-file limit for every chunking strategy; "
                f"split the file") from exc
        except _JsonError as exc:
            raise _JsonlError(f"line {number} is not valid JSON: {exc.detail}") from exc
        except json.JSONDecodeError as exc:
            raise _JsonlError(
                f"line {number} is not valid JSON: {exc.msg} at column {exc.colno}") from exc
        except RecursionError as exc:
            raise _JsonlError(
                f"line {number} is not valid JSON: it is nested too deeply to read") from exc
    return records


def _parse_jsonl(path: Path) -> tuple[str, list[Any]]:
    """JSON Lines, plain or gzipped: each line is one record, converted like
    ordinary JSON (#295), records separated by a blank line (#291). Lines are
    never treated as serialized Unstructured elements."""
    if _is_jsonl_gz(path.name):
        try:
            # Decompressed while read; nothing decompressed is written to disk.
            with gzip.open(path, "rb") as gz:
                records = _jsonl_records(_BoundedLines(gz))
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise ValueError(
                f"is not a valid gzip file; it may be truncated or corrupt ({exc})") from exc
        except _JsonlError as exc:
            subject = "it " if isinstance(exc, _JsonlTooLarge) else ""
            raise ValueError(f"was decompressed, but {subject}{exc.detail}") from exc
    else:
        try:
            with path.open("rb") as fh:
                records = _jsonl_records(fh)
        except _JsonlError as exc:
            raise ValueError(exc.detail) from exc
    from unstructured.documents.elements import Text
    return "\n\n".join(records), [Text(text=record) for record in records]


@telemetry.traced("rag.parse")
def _parse_file(path: Path) -> tuple[str, list[Any]]:
    if _is_jsonl(path.name):
        return _parse_jsonl(path)
    if _is_json_gz(path.name):
        return _parse_json_gz(path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        from unstructured.partition.pdf import partition_pdf
        elements = partition_pdf(filename=str(path))
    elif ext == ".docx":
        from unstructured.partition.docx import partition_docx
        elements = partition_docx(filename=str(path))
    elif ext == ".txt":
        from unstructured.partition.text import partition_text
        elements = partition_text(filename=str(path))
    elif ext == ".md":
        from unstructured.partition.md import partition_md
        elements = partition_md(filename=str(path))
    elif ext == ".csv":
        from unstructured.partition.csv import partition_csv
        elements = partition_csv(filename=str(path))
    elif ext == ".json":
        return _parse_json(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    text = "\n".join(str(el) for el in elements)
    return text, elements


class _CollectionDeleted(Exception):
    """Storing failed because the job's collection no longer exists (#40)."""


def _collection_gone(collection: str) -> bool:
    """True only when the backend answers that the collection is missing. A
    failed check is not evidence of deletion, so the original error stands."""
    try:
        return not wc._collection_exists_sync(collection)
    except Exception:
        _log.warning("Could not check whether %r still exists", collection, exc_info=True)
        return False


def _store_chunks(collection: str, chunks: list[dict]) -> None:
    try:
        wc._insert_chunks_sync(collection, chunks)
    except Exception as exc:
        # Only a failed store asks whether the collection is gone: a parse or
        # chunking failure says nothing about it. With auto-schema off, Weaviate
        # refuses inserts into a deleted class, and the batch, verification or
        # cleanup error it causes would not tell the user what happened.
        if _collection_gone(collection):
            _log.warning("Collection %r disappeared while storing chunks: %s",
                         collection, exc, exc_info=True)
            raise _CollectionDeleted(collection) from exc
        raise


def _count_writes(job: dict, submitted: int, accepted: int, stored: int = 0) -> None:
    """Add a file's insert to the job's sent, acknowledged and stored counts (#302).
    Records made before the first two existed start them at 0. The counts change
    in one update, so a status read never sees part of a file's counts (#324)."""
    job.update({
        "chunks_submitted": (job.get("chunks_submitted") or 0) + submitted,
        "chunks_accepted": (job.get("chunks_accepted") or 0) + accepted,
        "chunks_stored": job["chunks_stored"] + stored,
    })


def _has_chunks(collection: str, name: str) -> bool:
    """Whether the name already has chunks, which this file will replace (#22).
    It is the job's first backend call for a file, so a deleted collection is
    reported here the way _store_chunks reports it (#40)."""
    try:
        return wc._source_has_chunks_sync(collection, name)
    except Exception as exc:
        if _collection_gone(collection):
            _log.warning("Collection %r disappeared before storing chunks: %s",
                         collection, exc, exc_info=True)
            raise _CollectionDeleted(collection) from exc
        raise


class _StaleMarkFailed(Exception):
    """Gold-standard sessions could not be marked stale before a replacement."""


class _StaleLookupFailed(Exception):
    """Which later files in the job also replace earlier chunks could not be
    looked up, so the stale reason could not be written (#311)."""


# How many replaced names a stale reason lists before "and N more".
_MAX_NAMED_FILES = 10


def _mark_replacement_stale(collection: str, name: str, rest: list[str]) -> None:
    """Mark the collection's gold-standard sessions stale before `name` replaces
    its earlier chunks (#22). Their pairs cite chunks by source_file and
    chunk_index, which the replacement changes, so they would no longer describe
    what is stored. Saved before anything is stored: if it can't be saved, the
    file fails and the collection is left as it was.

    The reason names this file and the job's later files that also have earlier
    chunks, because a job marks at most once. A failed lookup of those raises
    _StaleLookupFailed and only a failed marking _StaleMarkFailed, so the error
    names the step that failed (#311); either way nothing has been marked.
    """
    try:
        names = [name] + [other for other in rest if wc._source_has_chunks_sync(collection, other)]
    except Exception as exc:
        raise _StaleLookupFailed(str(exc) or type(exc).__name__) from exc
    listed = ", ".join(names[:_MAX_NAMED_FILES])
    if len(names) > _MAX_NAMED_FILES:
        listed += f" and {len(names) - _MAX_NAMED_FILES} more"
    try:
        # Imported here: goldstandard imports weaviate_client, as tuning notes.
        from services import goldstandard
        goldstandard.mark_stale(
            collection, f"documents were re-ingested, replacing their earlier chunks: {listed}",
            require_durable=True)
    except Exception as exc:
        raise _StaleMarkFailed(str(exc) or type(exc).__name__) from exc


def _remove_earlier(collection: str, name: str, created_at: datetime,
                    expected: int) -> tuple[int, str | None]:
    """Remove a name's earlier chunks once its new ones are stored.

    Returns how many were removed and, when not all of them could be, why. A
    failure here never undoes the insert: duplicates are recoverable by uploading
    again, lost chunks are not.
    """
    try:
        return wc._delete_earlier_chunks_sync(collection, name, created_at, expected), None
    except Exception as exc:
        _log.error("Could not remove the earlier chunks of %r in %r", name, collection,
                   exc_info=True)
        return getattr(exc, "deleted", 0), str(exc) or type(exc).__name__


@telemetry.traced("rag.ingest")
@collection_writes.serialized("collection")
def _process_job_sync(
    job_id: str,
    file_paths: list[Path],
    tmp_dir: Path,
    collection: str,
    strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    similarity_threshold: float,
    min_chunk_size: int,
    labels: dict[Path, str] | None = None,
    names: dict[Path, str] | None = None,
) -> None:
    job = _jobs[job_id]
    job["status"] = "running"
    deleted = False
    # Logical names (#22): what each file is stored and replaced under.
    names = names or {}
    logical = [names.get(p, p.name) for p in file_paths]
    # Gold-standard sessions are marked stale at most once per job.
    marked = False

    try:
        for index, path in enumerate(file_paths):
            name = logical[index]
            label = (labels or {}).get(path, name)
            try:
                text, elements = _parse_file(path)
                source_type = source_type_of(path)

                chunks = do_chunk(
                    text=text,
                    strategy=strategy,
                    chunk_size=chunk_size,
                    chunk_overlap_size=chunk_overlap,
                    similarity_threshold=similarity_threshold,
                    min_chunk_size=min_chunk_size,
                    elements=elements if strategy == "context_aware" else None,
                )

                created = datetime.now(timezone.utc)
                now = created.isoformat()
                weaviate_chunks = [
                    {
                        "content": c,
                        "source_file": name,
                        "source_type": source_type,
                        "chunk_index": i,
                        "chunk_strategy": strategy,
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "created_at": now,
                    }
                    for i, c in enumerate(chunks)
                ]

                # The last ingest of a name wins (#22). Everything before the
                # insert only reads, so a failure up to there changes nothing.
                replacing = _has_chunks(collection, name)
                if replacing and not marked:
                    _mark_replacement_stale(collection, name, logical[index + 1:])
                    marked = True

                # Insert first, then remove the earlier chunks, never the other
                # way round: a failed insert leaves the earlier version whole.
                try:
                    _store_chunks(collection, weaviate_chunks)
                except Exception as exc:
                    _count_writes(job, *(batch_write.write_counts(exc) or (0, 0)))
                    raise
                # Stored counts here too, with the other two: counted later, a clean
                # job would show its counts apart until then (#324).
                _count_writes(job, len(chunks), len(chunks), len(chunks))
                removed, problem = 0, None
                if replacing:
                    removed, problem = _remove_earlier(collection, name, created, len(chunks))
                    job["chunks_replaced"] = job.get("chunks_replaced", 0) + removed

                # Retain the original only after the file has been parsed,
                # chunked and stored. A file that fails any of those steps
                # leaves no orphan source behind. replace=True releases the name
                # from the version it replaces, so rechunk can't rebuild that.
                try:
                    # A gzipped file's bytes are gzip, whatever it decompresses to.
                    media_type, encoding = mimetypes.guess_type(name)
                    sources.store(
                        collection,
                        name,
                        path.read_bytes(),
                        "application/gzip" if encoding else media_type,
                        replace=True,
                    )
                except (OSError, ValueError) as exc:
                    # Retention failing must not fail an otherwise good ingest;
                    # the chunks are already stored. It does cost this
                    # collection its full-fidelity export, so it is logged loudly.
                    # Names are logged with %r: one can hold a newline (#298).
                    _log.error("Could not retain source %r for %r: %s", name, collection, exc)

                if problem is None:
                    job["files_completed"] += 1
                else:
                    job["files_failed"] += 1
                    job["errors"].append(
                        f"{label}: its new chunks were stored, but earlier chunks could not "
                        f"all be removed ({problem}); upload it again to retry")
            except _CollectionDeleted:
                # Nothing more can be stored: this file and the rest fail.
                job["files_failed"] += len(file_paths) - index
                deleted = True
                break
            except _StaleMarkFailed as exc:
                telemetry.outcome("partial", exc)
                job["files_failed"] += 1
                job["errors"].append(
                    f"{label}: could not mark gold-standard sessions stale before replacing "
                    f"its chunks, so nothing was changed: {exc}")
                _log.warning("ingest job %s: %r not replaced", job_id, label, exc_info=True)
            except _StaleLookupFailed as exc:
                job["files_failed"] += 1
                job["errors"].append(
                    f"{label}: could not check which later files in this upload replace "
                    f"earlier chunks, so nothing was changed: {exc}")
                _log.warning("ingest job %s: %r not replaced", job_id, label, exc_info=True)
            except Exception as exc:
                telemetry.outcome("partial", exc)
                # Name the file the way the user knows it (a ZIP member by its
                # archive and path), and never leave the reason blank.
                job["files_failed"] += 1
                job["errors"].append(f"{label}: {str(exc) or type(exc).__name__}")
                _log.warning("ingest job %s: %r failed", job_id, label, exc_info=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Before the status turns terminal: a client that sees the job end
        # can delete the collection at once (#40).
        collection_writes.end_job("ingest", job_id)

    if deleted:
        telemetry.outcome("error")
        job["errors"].append(f"collection '{collection}' was deleted while this job was running")
        job["status"] = "failed"
        return

    if job["files_failed"] == 0:
        job["status"] = "completed"
    elif job["files_failed"] == job["files_total"]:
        job["status"] = "failed"
        telemetry.outcome("error")
    else:
        telemetry.outcome("partial")
        job["status"] = "partial"


def _os_reason(exc: OSError) -> str:
    return exc.strerror or str(exc) or type(exc).__name__


def _rejected(code: str, message: str, file: str,
              members: list[dict[str, str]] | None = None) -> UploadRejected:
    # %r: the message names the upload and its members, and a name can hold a
    # newline that would otherwise forge a log line (#298).
    _log.warning("Upload refused (%s): %r", code, message)
    return UploadRejected(code, message, file, members)


# How many unreadable members a refusal names in its message; detail lists all.
_MAX_NAMED_MEMBERS = 10

_BLOCK = 1024 * 1024


class _OverCap(Exception):
    """An upload's ZIPs expand past max_zip_expanded_bytes (#298)."""


class _ExpansionBudget:
    """Bytes the ZIPs of one upload request may still expand to (#298).

    One total for the whole request: every ZIP in it adds to it, files
    uploaded directly add nothing. Each member's bytes count once, as
    validation reads them; extraction reads supported members again and does
    not count them a second time.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def fits(self, size: int) -> bool:
        return self.used + size <= self.limit

    def spend(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise _OverCap()


class _CountedMember:
    """A ZIP member's data, counting every block read toward the cap (#298).

    It is what the gzip reader reads from, so an error raised here is about the
    ZIP member itself (damage), never about its gzip data; it is kept so the
    caller can tell the two apart.
    """

    def __init__(self, data: Any, budget: _ExpansionBudget) -> None:
        self.data = data
        self.budget = budget
        self.error: BaseException | None = None

    def read(self, size: int = -1) -> bytes:
        try:
            block = self.data.read(_BLOCK if size is None or size < 0 else min(size, _BLOCK))
        except BaseException as exc:
            self.error = exc
            raise
        self.budget.spend(len(block))
        return block


class _RawMember(io.RawIOBase):
    """A _CountedMember as raw I/O, for io.BufferedReader to read (#319).

    The gzip reader reads header fields such as FNAME, and the zero padding
    after a stream, one byte per call. Unbuffered, each byte was one read of
    the ZIP member, so a few KB of zip could hold a thread for minutes. Every
    byte still goes through the _CountedMember, so it counts once and an error
    reading the member is still recorded there.
    """

    def __init__(self, member: _CountedMember) -> None:
        self.member = member

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        block = self.member.read(len(buffer))
        buffer[:len(block)] = block
        return len(block)


def _count_gzip_output(member: _CountedMember, budget: _ExpansionBudget) -> None:
    """Count what a gzipped member decompresses to, keeping none of it.

    The job decompresses .json.gz and .jsonl.gz members later, with gzip.open,
    so their output would not otherwise count toward the cap. They are read
    here with the same reader, gzip.GzipFile, so the count is what the job
    would decompress: the same headers accepted (zlib's own gzip mode refuses
    some the job reads in full, which let them count nothing), concatenated
    streams and zero padding followed. Gzip data the reader refuses stops the
    count, and the job reports that file as it always has.

    The member is read through a buffer, a block at a time (#319). Bytes the
    buffer reads ahead are counted as they are read from the member, and the
    caller reads the rest of the member after the count.
    """
    try:
        source = io.BufferedReader(_RawMember(member), _BLOCK)
        with gzip.GzipFile(fileobj=source, mode="rb") as gz:
            while True:
                block = gz.read(_BLOCK)
                if not block:
                    break
                budget.spend(len(block))
    except (OSError, EOFError, zlib.error):
        if member.error is not None:
            raise member.error
        # Invalid gzip data: its count stops here.


def _member_problem(z: zipfile.ZipFile, member: zipfile.ZipInfo, budget: _ExpansionBudget,
                    gunzip: bool = False) -> tuple[str, bool] | None:
    """Why this member can't be read, and whether that is damage; None if it reads.

    Every block read is counted toward the request's expansion cap, and a
    gzipped member's decompressed output too (#298); _OverCap stops the read.
    """
    if member.flag_bits & 0x1:
        return "password-protected", False
    try:
        with z.open(member) as data:
            counted = _CountedMember(data, budget)
            if gunzip:
                _count_gzip_output(counted, budget)
            # The rest of the member (all of it, if not gzip) is still read to
            # its end, so damage anywhere in it is found.
            while counted.read(_BLOCK):
                pass
    except NotImplementedError:
        return f"unsupported compression method {member.compress_type}", False
    except OSError as exc:
        if exc.errno == errno.EINVAL:
            # A negative seek: the zip's directory points before the file starts.
            return "damaged: its recorded position in the zip is invalid", True
        return f"damaged: {_os_reason(exc)}", True
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        return f"damaged: {str(exc) or type(exc).__name__}", True
    return None


class _Queue:
    """The files an upload queues, one per logical name (#22).

    Every input is staged in a directory of its own, so no upload or ZIP member
    can overwrite another's bytes on disk, and the parser still sees the file's
    own extension. When a later input has the same logical name, the earlier one
    is dropped and reported in `skipped`: the last one wins, never silently.
    """

    def __init__(self, tmp_dir: Path, skipped: list[str]) -> None:
        self.tmp_dir = tmp_dir
        self.skipped = skipped
        self.entries: dict[str, tuple[Path, str]] = {}
        self._slots = 0

    def slot(self, basename: str) -> Path:
        self._slots += 1
        directory = self.tmp_dir / str(self._slots)
        directory.mkdir()
        return directory / basename

    def add(self, path: Path, logical: str, label: str) -> None:
        earlier = self.entries.pop(logical, None)
        if earlier is not None:
            earlier[0].unlink(missing_ok=True)
            self.skipped.append(
                f"{earlier[1]} (replaced by {label} later in this upload, same name '{logical}')")
        self.entries[logical] = (path, label)


def _member_name(filename: str) -> str | None:
    """A ZIP member's logical name: its normalised POSIX path inside the archive
    (#22), so a/report.pdf and b/report.pdf stay two documents and a/../b.txt is
    b.txt. None for a path that leaves the archive, which is not ingested."""
    name = posixpath.normpath(filename)
    if name.startswith("/") or name in (".", "..") or name.startswith("../"):
        return None
    return name


def _is_macos_metadata(logical: str) -> bool:
    """Whether a ZIP member is macOS metadata rather than a document (#378).

    Finder's Compress stores each file's resource fork and extended attributes
    as an AppleDouble file, __MACOSX/<folder>/._<name>, and other macOS tools
    write ._<name> beside the file. Either keeps the real file's suffix, so the
    suffix check would queue it as a document of its own."""
    path = PurePosixPath(logical)
    return path.parts[0] == "__MACOSX" or path.name.startswith("._")


def _limit_text(limit: int) -> str:
    mib = 1024 * 1024
    if limit >= mib and limit % mib == 0:
        return f"{limit // mib:,} MB"
    return f"{limit:,} bytes"


def _too_large(safe_name: str, limit: int) -> UploadRejected:
    return _rejected(
        "ARCHIVE_TOO_LARGE",
        f"'{safe_name}' expands to more than {_limit_text(limit)} when unpacked, the most one "
        f"upload's ZIPs can expand to, so nothing from it was imported. Split it into smaller "
        f"zips and upload them in separate imports.",
        safe_name)


def _declared_size(members: list[tuple[zipfile.ZipInfo, str]]) -> int:
    """What the ZIP's directory says these members decompress to."""
    return sum(member.file_size for member, _ in members)


def _extract_zip(dest: Path, safe_name: str, queue: _Queue, budget: _ExpansionBudget) -> None:
    """Validate one uploaded ZIP, then queue its supported members.

    Every member is read to its end first, including the ones that will be
    skipped. A zip with any unreadable member is refused as a whole, naming
    them; nothing is repaired, and nothing from it is ingested (#290). macOS
    metadata members (__MACOSX/, ._*) are skipped like unsupported ones (#378).

    The request's ZIPs may expand to at most the budget (#298): the sizes this
    ZIP declares are checked before anything is read, then the bytes actually
    decompressed, gzipped members' output included, are counted while reading,
    and the request is refused as soon as the total passes the cap. This runs
    in a worker thread: it can take seconds, and on the event loop it stalled
    every other request.
    """
    remedy = "Re-create the zip and upload it again."
    try:
        archive = zipfile.ZipFile(dest, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise _rejected(
            "ARCHIVE_UNREADABLE",
            f"'{safe_name}' is damaged and can't be read, so nothing from it was imported. "
            f"It may be truncated (for example an incomplete download or copy) or corrupt. "
            f"{remedy}",
            safe_name) from exc

    with archive as z:
        members = []
        for member in z.infolist():
            if member.is_dir():
                continue
            logical = _member_name(member.filename)
            if logical is None:
                continue
            members.append((member, logical))

        if not budget.fits(_declared_size(members)):
            raise _too_large(safe_name, budget.limit)

        problems = []
        try:
            for member, logical in members:
                basename = PurePosixPath(logical).name
                problem = _member_problem(z, member, budget,
                                          _is_json_gz(basename) or _is_jsonl_gz(basename))
                if problem is not None:
                    problems.append((member.filename, *problem))
        except _OverCap:
            # As soon as the total passes the cap, whatever else was found.
            raise _too_large(safe_name, budget.limit) from None
        if problems:
            damaged = any(is_damage for _, _, is_damage in problems)
            named = "; ".join(f"{name} ({reason})"
                              for name, reason, _ in problems[:_MAX_NAMED_MEMBERS])
            more = len(problems) - _MAX_NAMED_MEMBERS
            if more > 0:
                named += f"; and {more} more"
            reasons = {reason for _, reason, _ in problems}
            if "password-protected" in reasons:
                remedy = "Re-create the zip without a password and upload it again."
            elif not damaged:
                remedy = ("Re-create the zip with standard (Deflate) compression and "
                          "upload it again.")
            raise _rejected(
                "ARCHIVE_UNREADABLE" if damaged else "ARCHIVE_UNSUPPORTED",
                f"'{safe_name}' {'is damaged and ' if damaged else ''}can't be read, so "
                f"nothing from it was imported. Unreadable members: {named}. {remedy}",
                safe_name,
                [{"member": name, "reason": reason} for name, reason, _ in problems])

        for member, logical in members:
            basename = PurePosixPath(logical).name
            if _is_macos_metadata(logical):
                queue.skipped.append(
                    f"{safe_name}:{member.filename} (macOS metadata (AppleDouble), not a document)")
                continue
            if not (PurePosixPath(logical).suffix.lower() in SUPPORTED_EXTENSIONS
                    or _is_json_gz(basename) or _is_jsonl_gz(basename)):
                queue.skipped.append(
                    f"{safe_name}:{member.filename} ({_skip_reason(member.filename)})")
                continue
            # Copied out by our own code, not ZipFile.extract, whose own path
            # cleaning would put a/../b.txt somewhere other than where it is
            # queued.
            try:
                target = queue.slot(basename)
                with z.open(member) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
            except OSError as exc:
                reason = _os_reason(exc)
                raise _rejected(
                    "UPLOAD_WRITE_FAILED",
                    f"Could not extract '{member.filename}' from '{safe_name}' on the "
                    f"server: {reason}.",
                    safe_name, [{"member": member.filename, "reason": reason}]) from exc
            queue.add(target, logical, f"{safe_name}:{member.filename}")


async def start_ingest_job(
    files: list[Any],
    collection: str,
    strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    similarity_threshold: float,
    min_chunk_size: int,
) -> str:
    config = IngestConfig(chunking_strategy=strategy, chunk_size=chunk_size,
                          chunk_overlap=chunk_overlap, similarity_threshold=similarity_threshold,
                          min_chunk_size=min_chunk_size)
    strategy, chunk_size, chunk_overlap, min_chunk_size = (
        config.chunking_strategy, config.chunk_size, config.chunk_overlap, config.min_chunk_size)
    similarity_threshold = config.similarity_threshold if config.similarity_threshold is not None else 0.85
    job_id = str(uuid.uuid4())[:8]

    # Registered before the upload is saved, so the collection can't be deleted
    # out from under a job being admitted (#40). Every way out of admission that
    # starts no worker ends the registration; the worker ends its own.
    collection_writes.register_job(collection, "ingest", job_id)
    try:
        return await _admit_ingest_job(job_id, files, collection, strategy, chunk_size,
                                       chunk_overlap, similarity_threshold, min_chunk_size)
    except BaseException:
        collection_writes.end_job("ingest", job_id)
        raise


async def _admit_ingest_job(job_id: str, files: list[Any], collection: str, strategy: str,
                            chunk_size: int, chunk_overlap: int,
                            similarity_threshold: float, min_chunk_size: int) -> str:
    # The prefix is how the startup sweep finds a killed job's staging (#377).
    tmp_dir = Path(tempfile.mkdtemp(prefix="ingest-", dir=settings.upload_dir))
    # Files the caller sent that this build cannot parse, and inputs replaced by
    # a later one with the same name. Dropping them without a word makes a ZIP
    # of 8 files silently report 6, with nothing saying which two or why.
    skipped: list[str] = []
    queue = _Queue(tmp_dir, skipped)
    # One expansion total for every ZIP in this request (#298).
    budget = _ExpansionBudget(settings.max_zip_expanded_bytes)

    try:
        for upload in files:
            raw_name = upload.filename or ""
            safe_name = Path(raw_name).name
            if not safe_name:
                continue
            dest = queue.slot(safe_name)
            # Copy in blocks rather than `await upload.read()`, which held the
            # whole file in memory. Uploads can now reach 512 MB (issue #21),
            # and this container already runs close to its memory budget. The
            # copy blocks, so it runs off the event loop.
            try:
                await asyncio.to_thread(_save_upload, upload.file, dest)
            except OSError as exc:
                raise _rejected(
                    "UPLOAD_WRITE_FAILED",
                    f"Could not save '{safe_name}' on the server: {_os_reason(exc)}.",
                    safe_name) from exc

            if safe_name.lower().endswith(".zip"):
                # Off the event loop, like the copy above (#298).
                await asyncio.to_thread(_extract_zip, dest, safe_name, queue, budget)
                os.remove(dest)
            elif (Path(safe_name).suffix.lower() in SUPPORTED_EXTENSIONS
                  or _is_jsonl_gz(safe_name)):
                # A directly uploaded .json.gz is still skipped (#291 scope).
                queue.add(dest, safe_name, safe_name)
            else:
                skipped.append(f"{safe_name} ({_skip_reason(safe_name)})")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # One file per logical name, in the order of each winning input. How each
    # is named in the job's errors, and what it is stored under.
    file_paths = [path for path, _ in queue.entries.values()]
    labels = {path: label for path, label in queue.entries.values()}
    names = {path: logical for logical, (path, _) in queue.entries.items()}

    if not file_paths:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        detail = "; ".join(skipped)
        raise ValueError(
            "No supported files found in upload."
            + (f" Skipped: {detail}" if detail else ""))

    # The first write is durable, so it runs off the event loop (#309). A start
    # cancelled meanwhile removes its uploads here; start_ingest_job ends the
    # registration, and the store removes the record.
    try:
        await _jobs.create(job_id, {
            "job_id": job_id,
            "status": "queued",
            "files_total": len(file_paths),
            "files_completed": 0,
            "files_failed": 0,
            "chunks_stored": 0,
            # Chunks sent to Weaviate, and those it acknowledged; chunks_stored
            # counts only the checked ones (#302).
            "chunks_submitted": 0,
            "chunks_accepted": 0,
            # Earlier chunks of the same names removed by this job (#22).
            "chunks_replaced": 0,
            "errors": [],
            "skipped": skipped,
        })
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    @telemetry.admitted
    def _on_done(future: asyncio.Future) -> None:
        # The worker ends its registration itself; this covers a job cancelled
        # before it ran and an error that escaped the worker.
        collection_writes.end_job("ingest", job_id)
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            with telemetry.span("rag.failure_report"):
                telemetry.outcome("error", exc)
                _log.error("ingest job %s failed: %s", job_id, exc)
                job = _jobs.get(job_id)
                # This write stays on the event loop (#309): a done callback is
                # synchronous and can't await a thread, it runs only when an error
                # escaped the worker, and it saves one record of about 1 KB.
                if job and job["status"] not in ("completed", "partial", "failed"):
                    # The error first: the status change is the write that saves
                    # the record (#23), and an in-place append is not seen alone.
                    job["errors"].append(str(exc))
                    job["status"] = "failed"

    future = asyncio.get_running_loop().run_in_executor(
        None,
        telemetry.admitted(_process_job_sync),
        job_id,
        file_paths,
        tmp_dir,
        collection,
        strategy,
        chunk_size,
        chunk_overlap,
        similarity_threshold,
        min_chunk_size,
        labels,
        names,
    )
    future.add_done_callback(_on_done)

    return job_id
