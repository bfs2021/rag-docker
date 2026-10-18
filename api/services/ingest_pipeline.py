from __future__ import annotations
from services import telemetry
import asyncio
import errno
import gzip
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import collection_writes
from config import settings
from models.schemas import IngestConfig
from services.chunker import MAX_TEXT_CHARACTERS, chunk as do_chunk
from services import sources
from services import weaviate_client as wc

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}
# Gzipped JSON is accepted inside a ZIP only (issue #290). It is decompressed
# when the job parses it, never more than this many bytes: the per-request
# upload limit, so gzip cannot bring in more than a plain upload could.
GZIP_JSON_SUFFIX = ".json.gz"
MAX_GZIP_DECOMPRESSED_BYTES = 512 * 1024 * 1024

_jobs: dict[str, dict] = {}
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


def _skip_reason(name: str) -> str:
    """Why a file is not ingested, for the job's `skipped` list."""
    lower = name.lower()
    if lower.endswith(".jsonl") or lower.endswith(".jsonl.gz"):
        return "JSON Lines (.jsonl) isn't supported yet, see #291"
    return f"unsupported type '{Path(lower).suffix or 'none'}'"


def source_type_of(path: Path) -> str:
    """The source_type stored with a file's chunks: what was parsed, so a
    gzipped JSON file is "json", like a plain one."""
    if _is_json_gz(path.name):
        return "json"
    return path.suffix.lower().lstrip(".")


def _save_upload(src, dest: Path) -> None:
    with dest.open("wb") as fh:
        shutil.copyfileobj(src, fh, length=1024 * 1024)


def _gunzip_bounded(src: Path, dest: Path) -> None:
    """Decompress src into dest in blocks, refusing more than the limit."""
    written = 0
    try:
        with gzip.open(src, "rb") as gz, dest.open("wb") as out:
            while True:
                block = gz.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if written > MAX_GZIP_DECOMPRESSED_BYTES:
                    raise ValueError(
                        f"decompresses to more than "
                        f"{MAX_GZIP_DECOMPRESSED_BYTES // (1024 * 1024)} MB, the limit for "
                        f"one compressed file; nothing from it was stored")
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
                records.append(text)
            else:
                budget.left = left  # a dropped record's text is not stored
    except RecursionError as exc:
        raise _JsonError("it is nested too deeply to read") from exc
    return records


def _parse_json(path: Path) -> tuple[str, list[Any]]:
    """Ordinary JSON becomes `key: value` text, one element per record. A file of
    serialized Unstructured elements is rehydrated by partition_json (#295)."""
    raw, value = _read_json(path)
    if not raw:
        return "", []
    from unstructured.partition.common.json_partitioning import is_element_shaped_dict
    if isinstance(value, list) and value and all(is_element_shaped_dict(i) for i in value):
        from unstructured.partition.json import partition_json
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
        except _JsonTooLarge as exc:
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


@telemetry.traced("rag.parse")
def _parse_file(path: Path) -> tuple[str, list[Any]]:
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
) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    try:
        for path in file_paths:
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

                now = datetime.now(timezone.utc).isoformat()
                weaviate_chunks = [
                    {
                        "content": c,
                        "source_file": path.name,
                        "source_type": source_type,
                        "chunk_index": i,
                        "chunk_strategy": strategy,
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "created_at": now,
                    }
                    for i, c in enumerate(chunks)
                ]

                wc._insert_chunks_sync(collection, weaviate_chunks)

                # Retain the original only after the file has been parsed,
                # chunked and stored. A file that fails any of those steps
                # leaves no orphan source behind.
                try:
                    sources.store(
                        collection,
                        path.name,
                        path.read_bytes(),
                        mimetypes.guess_type(path.name)[0],
                    )
                except (OSError, ValueError) as exc:
                    # Retention failing must not fail an otherwise good ingest;
                    # the chunks are already stored. It does cost this
                    # collection its full-fidelity export, so it is logged loudly.
                    _log.error("Could not retain source %s for %r: %s", path.name, collection, exc)

                job["chunks_stored"] += len(chunks)
                job["files_completed"] += 1
            except Exception as exc:
                telemetry.outcome("partial", exc)
                # Name the file the way the user knows it (a ZIP member by its
                # archive and path), and never leave the reason blank.
                label = (labels or {}).get(path, path.name)
                job["files_failed"] += 1
                job["errors"].append(f"{label}: {str(exc) or type(exc).__name__}")
                _log.warning("ingest job %s: %s failed", job_id, label, exc_info=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if job["files_failed"] == 0:
        job["status"] = "completed"
    elif job["files_failed"] == job["files_total"]:
        job["status"] = "failed"
        telemetry.outcome("error")
    else:
        job["status"] = "partial"


def _os_reason(exc: OSError) -> str:
    return exc.strerror or str(exc) or type(exc).__name__


def _rejected(code: str, message: str, file: str,
              members: list[dict[str, str]] | None = None) -> UploadRejected:
    _log.warning("Upload refused (%s): %s", code, message)
    return UploadRejected(code, message, file, members)


# How many unreadable members a refusal names in its message; detail lists all.
_MAX_NAMED_MEMBERS = 10


def _member_problem(z: zipfile.ZipFile, member: zipfile.ZipInfo) -> tuple[str, bool] | None:
    """Why this member can't be read, and whether that is damage; None if it reads."""
    if member.flag_bits & 0x1:
        return "password-protected", False
    try:
        with z.open(member) as data:
            while data.read(1024 * 1024):
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


def _extract_zip(dest: Path, safe_name: str, tmp_dir: Path, file_paths: list[Path],
                 skipped: list[str], labels: dict[Path, str]) -> None:
    """Validate one uploaded ZIP, then extract its supported members into tmp_dir.

    Every member is read to its end first, including the ones that will be
    skipped. A zip with any unreadable member is refused as a whole, naming
    them; nothing is repaired, and nothing from it is ingested (#290).
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

    resolved_tmp = tmp_dir.resolve()
    with archive as z:
        members = []
        for member in z.infolist():
            if member.is_dir():
                continue
            member_path = (tmp_dir / member.filename).resolve()
            if not member_path.is_relative_to(resolved_tmp):
                continue
            members.append((member, member_path))

        problems = []
        for member, _ in members:
            problem = _member_problem(z, member)
            if problem is not None:
                problems.append((member.filename, *problem))
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

        for member, member_path in members:
            if not (member_path.suffix.lower() in SUPPORTED_EXTENSIONS
                    or _is_json_gz(member_path.name)):
                skipped.append(f"{safe_name}:{member.filename} ({_skip_reason(member.filename)})")
                continue
            try:
                z.extract(member, tmp_dir)
            except OSError as exc:
                reason = _os_reason(exc)
                raise _rejected(
                    "UPLOAD_WRITE_FAILED",
                    f"Could not extract '{member.filename}' from '{safe_name}' on the "
                    f"server: {reason}.",
                    safe_name, [{"member": member.filename, "reason": reason}]) from exc
            file_paths.append(member_path)
            labels[member_path] = f"{safe_name}:{member.filename}"


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

    tmp_dir = Path(tempfile.mkdtemp(dir=settings.upload_dir))
    file_paths: list[Path] = []
    # Files the caller sent that this build cannot parse. Dropping them without
    # a word makes a ZIP of 8 files silently report 6, with nothing saying which
    # two were ignored or why.
    skipped: list[str] = []
    # How each queued file is named in the job's errors.
    labels: dict[Path, str] = {}

    try:
        for upload in files:
            raw_name = upload.filename or ""
            safe_name = Path(raw_name).name
            if not safe_name:
                continue
            dest = tmp_dir / safe_name
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
                _extract_zip(dest, safe_name, tmp_dir, file_paths, skipped, labels)
                os.remove(dest)
            elif Path(safe_name).suffix.lower() in SUPPORTED_EXTENSIONS:
                file_paths.append(dest)
                labels[dest] = safe_name
            else:
                skipped.append(f"{safe_name} ({_skip_reason(safe_name)})")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if not file_paths:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        detail = "; ".join(skipped)
        raise ValueError(
            "No supported files found in upload."
            + (f" Skipped: {detail}" if detail else ""))

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "files_total": len(file_paths),
        "files_completed": 0,
        "files_failed": 0,
        "chunks_stored": 0,
        "errors": [],
        "skipped": skipped,
    }

    @telemetry.admitted
    def _on_done(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            with telemetry.span("rag.failure_report"):
                telemetry.outcome("error", exc)
                _log.error("ingest job %s failed: %s", job_id, exc)
                job = _jobs.get(job_id)
                if job and job["status"] not in ("completed", "partial", "failed"):
                    job["status"] = "failed"
                    job["errors"].append(str(exc))

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
    )
    future.add_done_callback(_on_done)

    return job_id
