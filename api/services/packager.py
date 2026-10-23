"""The on-disk export package format: naming, digests, manifest, assembly.

Deliberately separate from the export *job* (`exporter.py`): import reuses the
reader here rather than reimplementing it, which is what keeps the two halves
from drifting.

Format reference: RAG_EXPORT_SPECIFICATIONS.md §4.
"""
from __future__ import annotations
from services import telemetry

import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from pydantic import ValidationError

from config import settings
from models.schemas import SEARCH_EF_MAX, SEARCH_EF_MIN
from services import goldstandard
from services import ingest_config
from services import model_bundle
from services import retrieval_config
from services import sources
from services import weaviate_client as wc

_log = logging.getLogger(__name__)

PACKAGE_FORMAT = 1
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Files generated *from* the manifest, so they cannot appear in its `files` map:
# the manifest cannot contain its own digest, and `<id8>` is the manifest digest,
# which README.md and retrieve.py both embed. Including them would be circular.
UNDIGESTED = ("manifest.json", "README.md", "retrieve.py")


def exports_dir() -> Path:
    p = Path(settings.exports_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Leftovers of a killed job (#377) ─────────────────────────────────────────
# A build removes its staging directory and temporary archive in a `finally`,
# which a hard kill skips: each killed export left them in ./exports for good,
# hidden and root-owned, about 2.3 GB with models. Matched by exact name pattern
# and by kind, never by age; anything else in the folder is left alone.
_BUILD_DIR = ".build-*"
_PARTIAL_ARCHIVE = ".*.tar.gz.partial"


def remove_leftover(entry: Path, *, directory: bool = False,
                    regular_file: bool = False) -> int | None:
    """Remove one thing a killed job left behind and return the bytes it held.

    `directory` and `regular_file` say what its name promises. Anything else,
    a symlink included, is kept, and no link is followed: not to check it, size
    it or remove it. None when it was kept or could not be removed (logged).
    """
    try:
        info = entry.lstat()
        if directory and stat.S_ISDIR(info.st_mode):
            size = 0
            for parent, _dirs, files in os.walk(entry):     # does not follow links
                for name in files:
                    found = os.lstat(os.path.join(parent, name))
                    if stat.S_ISREG(found.st_mode):
                        size += found.st_size
            # Removes links inside without following them, and refuses a link.
            shutil.rmtree(entry)
            return size
        if regular_file and stat.S_ISREG(info.st_mode):
            entry.unlink()
            return info.st_size
        return None
    except OSError:
        _log.exception("Could not remove %s", entry)
        return None


def sweep_build_leftovers() -> list[str]:
    """Remove the staging directories and temporary archives of killed exports.

    Startup only, before this process can have an export of its own running.
    A missing exports folder is not created.
    """
    root = Path(settings.exports_dir)
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for entry in sorted(root.iterdir()):
        as_dir = fnmatch.fnmatchcase(entry.name, _BUILD_DIR)
        as_file = fnmatch.fnmatchcase(entry.name, _PARTIAL_ARCHIVE)
        if not (as_dir or as_file):
            continue
        size = remove_leftover(entry, directory=as_dir, regular_file=as_file)
        if size is not None:
            removed.append(f"{entry.name} ({size // (1024 * 1024)} MB)")
    return removed


# ── Naming (spec §4.1) ────────────────────────────────────────────────────────

def slug(collection: str) -> str:
    """Lossy label for the filename. Never parsed back — see spec §4.1."""
    s = re.sub(r"[^a-z0-9]+", "-", collection.lower()).strip("-")
    return s or "collection"


def timestamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def package_stem(collection: str, when: datetime, id8: str) -> str:
    return f"ragpkg-{slug(collection)}-{timestamp(when)}-{id8}"


# ── Digests ───────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json_default(value: Any) -> Any:
    """Weaviate returns DATE properties as datetime, which json cannot encode."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


# ── Reading the collection (spec §4.5) ────────────────────────────────────────

def read_chunks(collection: str) -> Iterator[dict]:
    """One record per chunk, streamed. Never materialises the collection.

    A 100k-chunk corpus is roughly 880 MB of JSON, so nothing here accumulates.
    """
    client = wc.get_client()
    col = client.collections.get(collection)

    # digests_for_filename() reloads the index on every call, which would be
    # O(chunks x documents). Build the reverse map once.
    by_filename: dict[str, list[str]] = {}
    for digest, entry in sources.load_index(collection)["documents"].items():
        for name in entry.get("filenames", []):
            by_filename.setdefault(name, []).append(digest)

    for obj in telemetry.iterate(col.iterator, include_vector=True):
        vector = obj.vector
        # Verified against the live stack: iterator() yields a dict keyed by
        # vector name, not a bare list. Code written for a list breaks here.
        if isinstance(vector, dict):
            vector = vector.get("default") or next(iter(vector.values()), None)

        props = dict(obj.properties or {})
        # source_sha256 is a sibling field, not a stored property: spec §4.5
        # keeps the eight chunk properties unchanged. Resolve it from retention.
        digests = by_filename.get(props.get("source_file"), [])
        yield {
            "id": str(obj.uuid),
            "vector": vector,
            "properties": props,
            # More than one digest means the same filename was ingested with
            # different content. Recording null is honest; picking one is not.
            "source_sha256": digests[0] if len(digests) == 1 else None,
        }


# ── Assembly ──────────────────────────────────────────────────────────────────

class _Builder:
    """Accumulates files and their digests as they are written."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, str] = {}

    def add(self, rel: str, write: Callable[[Path], None]) -> Path:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        write(target)
        self.files[rel] = "sha256:" + sha256_file(target)
        return target

    def add_json(self, rel: str, data: Any) -> Path:
        return self.add(rel, lambda p: p.write_text(
            json.dumps(data, indent=2, sort_keys=True, default=_json_default)))

    def discard(self, rels: list[str]) -> None:
        """Remove files already written, and directories that leaves empty."""
        for rel in rels:
            self.files.pop(rel, None)
            target = self.root / rel
            target.unlink(missing_ok=True)
            parent = target.parent
            while parent != self.root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent


def _resolve_includes(text: str, depth: int = 0) -> str:
    """Expand `@@INCLUDE:name@@` from templates/partials/<name>.md.

    Partials are the single source the package README and the in-app help page
    both render from (spec §10). Without that, the two drift — this project has
    already found five places where documentation and implementation had.
    """
    if depth > 5:
        raise RuntimeError("template includes nested too deeply")
    def swap(match: re.Match) -> str:
        name = match.group(1)
        path = _TEMPLATE_DIR / "partials" / f"{name}.md"
        if not path.is_file():
            raise RuntimeError(f"no such template partial: {name}")
        return _resolve_includes(path.read_text().rstrip("\n"), depth + 1)
    return re.sub(r"@@INCLUDE:([a-z_0-9]+)@@", swap, text)


def _render(template: str, values: dict[str, str]) -> str:
    text = _resolve_includes((_TEMPLATE_DIR / template).read_text())
    # Substitute only original template tokens. Inserted data can itself contain
    # token-shaped text and must never be interpreted as another substitution.
    def substitute(match):
        key = match.group()[2:-2]
        if key not in values:
            raise RuntimeError(f"{template}: unsubstituted placeholder {match.group()}")
        return str(values[key])
    return re.sub(r"@@[A-Z_0-9]+(?::[a-z_0-9]+)?@@", substitute, text)


def _render_retrieve(collection: str, cfg: dict, metadata: dict) -> str:
    """Only validated, encoded Python literals may cross into script source."""
    cfg = retrieval_config.validate(cfg, collection)
    return _render("retrieve.py.tmpl", {
        "PACKAGE_METADATA": repr(metadata),
        "COLLECTION_NAME": repr(collection),
        "RETRIEVAL_MODE": repr(cfg["retrieval_mode"]),
        "TOP_K": repr(cfg["top_k"]),
        "ALPHA": repr(cfg["alpha"]),
        "RESPONSE_FORMAT": repr(cfg["response_format"]),
    })


def render_help(embed_dimensions: int | str) -> str:
    """The /help/transfer page, from the same partials as a package README.

    Dimensions are passed in rather than assumed: the only honest source is an
    actual embedding call, which the caller makes.
    """
    return _render("help_transfer.md.tmpl", {
        "EMBED_MODEL": settings.embed_model,
        "EMBED_DIMENSIONS": embed_dimensions,
        "LLM_MODEL": settings.llm_model,
    })


def _export_retrieval_settings(collection: str, warnings: list[str]) -> tuple[dict, bool]:
    """The settings to package, and whether they are defaults.

    Runs before any chunk is read, so bad settings fail the export early with
    a next step instead of a raw validation message. A legacy ef is cleared
    with a warning; the saved file itself is left alone.
    """
    try:
        cfg, is_default = retrieval_config.merged(collection)
        cfg, cleared = retrieval_config.normalize(cfg, collection)
    except TypeError as exc:          # merged(): the saved JSON is not an object
        problems, cause = "the settings file is not a JSON object", exc
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                             for error in exc.errors())
        cause = exc
    else:
        if cleared is not None:
            warnings.append(f"saved retrieval setting ef={cleared} is outside "
                            f"{SEARCH_EF_MIN}-{SEARCH_EF_MAX} and was exported as null; ef is "
                            "no longer used. Save this collection's settings on the Retrieval "
                            "page to clear it.")
        return cfg, is_default
    raise ValueError(f"Saved retrieval settings for '{collection}' are invalid: {problems}. "
                     "Open the Retrieval page, save this collection's settings, then "
                     "export again.") from cause


def _ingest_config(collection: str) -> dict | None:
    """The collection's saved chunking settings, or None."""
    return ingest_config.load(collection)


def _goldstandard_sessions(collection: str) -> list[dict]:
    # Preserve export's detached on-disk snapshots. The cache contains live
    # generation/edit objects, which must not change during JSON serialization.
    # Disk reads still share schema, identity and regular-file validation.
    return [session for session in goldstandard._sessions_on_disk()
            if session["collection"] == collection]


def _fidelity_note(fidelity: str) -> str:
    if fidelity == "with-sources":
        return ("The original documents are included, so the collection can be "
                "re-chunked and re-embedded exactly after import.")
    return ("The original documents are not included. This is normal for a "
            "collection built before source retention existed. Import and query "
            "work; re-chunking does not.")


def _retrieve_section(has_script: bool, cfg: dict, collection: str) -> str:
    if not has_script:
        return ("`retrieve.py` is **not** included in this package, because the "
                "collection had no saved retrieval settings when it was exported. "
                "A script carrying stock defaults while claiming to carry tuned "
                "ones would be worse than no script. Query the API directly at "
                "`POST /api/query`, or save retrieval settings and export again.")
    usage = _resolve_includes("@@INCLUDE:retrieve_usage@@")
    return (f"`retrieve.py` queries this collection through a running rag-docker "
            f"API, using the settings it was tuned with.\n\n"
            f"{usage}\n\n"
            f"Baked-in defaults for this package: collection `{collection}`, mode "
            f"`{cfg['retrieval_mode']}`, top-k {cfg['top_k']}, alpha {cfg['alpha']}, "
            f"answer style `{cfg['response_format']}`.")


def _write_archive(stage: Path, stem: str, dest: Path) -> None:
    """Archive the staged package as `<stem>/`, with manifest.json first (#383).

    The directory entry, then the manifest, then everything else in the sorted
    order `tar.add(stage, arcname=stem)` gives, so the listing can stop reading
    at the manifest instead of decompressing chunks.jsonl to reach it.
    """
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(stage, arcname=stem, recursive=False)
        tar.add(stage / "manifest.json", arcname=f"{stem}/manifest.json")
        for entry in sorted(os.listdir(stage)):
            if entry != "manifest.json":
                tar.add(stage / entry, arcname=f"{stem}/{entry}")


@telemetry.traced("rag.package")
def build(
    collection: str,
    include_models: bool = False,
    progress: Callable[[int], None] | None = None,
) -> dict:
    """Write one package and return a summary. Blocking; run it in a thread.

    Assembly order matters (spec §6.2): content first, digests as we go, then
    the manifest from those digests, then README.md and retrieve.py which embed
    the manifest digest. Manifest last is what makes <id8> derivable.
    """
    created = datetime.now(timezone.utc)
    created_at = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = exports_dir()
    warnings: list[str] = []

    # Staged inside exports_dir so the final rename is on the same filesystem.
    stage = Path(tempfile.mkdtemp(dir=out_dir, prefix=".build-"))
    tmp_archive: Path | None = None
    try:
        b = _Builder(stage)

        # Settings first: a bad config fails before any chunk is streamed.
        retrieval_cfg, is_default = _export_retrieval_settings(collection, warnings)

        collection_cfg = wc._collection_config_sync(collection)
        stored_model = collection_cfg.get("embedding_model")
        if not isinstance(stored_model, str) or not stored_model or stored_model != settings.embed_model:
            raise PackageError(
                "EMBEDDING_MISMATCH",
                f"Collection '{collection}' has embedding model {stored_model!r}, "
                f"but this instance is configured for {settings.embed_model!r}. "
                "Export cannot attribute its stored vectors to the configured model. "
                "Re-embed the collection with the configured model before exporting; "
                "unknown or named-vector configurations are not supported.",
                {"collection": collection, "stored_model": stored_model,
                 "configured_model": settings.embed_model})

        # 1. chunks.jsonl — streamed, one line at a time.
        chunk_count = 0
        dimensions: int | None = None
        first_props: dict | None = None
        unresolved = 0

        def write_chunks(path: Path) -> None:
            nonlocal chunk_count, dimensions, first_props, unresolved
            with path.open("w", encoding="utf-8") as fh:
                for record in read_chunks(collection):
                    if dimensions is None and record["vector"]:
                        dimensions = len(record["vector"])
                    if first_props is None:
                        first_props = record["properties"]
                    if record["source_sha256"] is None:
                        unresolved += 1
                    fh.write(json.dumps(record, default=_json_default))
                    fh.write("\n")
                    chunk_count += 1
                    if progress and chunk_count % 500 == 0:
                        progress(chunk_count)

        b.add("chunks.jsonl", write_chunks)
        if progress:
            progress(chunk_count)

        # 2. collection.json
        b.add_json("collection.json", collection_cfg)

        # 3. configs
        ingest_cfg = _ingest_config(collection)
        if ingest_cfg is not None:
            b.add_json("ingest_config.json", ingest_cfg)

        has_saved_retrieval = not is_default
        b.add_json("retrieval_config.json", retrieval_cfg)

        # 4. gold standard sessions
        for session in _goldstandard_sessions(collection):
            b.add_json(f"goldstandard/{session['session_id']}.json", session)

        # 5. sources, when they exist
        index = sources.load_index(collection)
        shipped = {}
        for digest, entry in index["documents"].items():
            blob = sources.blob_path(collection, digest)
            if not blob.exists():
                warnings.append(f"retained source {digest[:12]} is missing on disk")
                continue
            shipped[digest] = entry
        fidelity = "with-sources" if shipped else "chunks-only"
        source_document_count = len(shipped)
        if fidelity == "with-sources":
            b.add_json("sources/index.json", {**index, "documents": shipped})
            for digest in shipped:
                blob = sources.blob_path(collection, digest)
                b.add(f"sources/{digest}", lambda p, s=blob: shutil.copyfile(s, p))

        # 5b. bundled models, resolved through each model's manifest
        bundled: list[str] = []
        if include_models:
            # Models are compared by name and tag, so `x` and `x:latest` are one
            # model, copied once (#494). Each copy then has its own
            # models/<name>/<tag>/ folder (#487), so removing a failed copy
            # never removes another model's files.
            wanted: dict[str, str] = {}
            for model in (settings.embed_model, settings.llm_model):
                try:
                    key = model_bundle.model_ref(model)
                except ValueError:
                    key = model  # no path in the store; export_model refuses it below
                wanted.setdefault(key, model)
            if not model_bundle.store_available():
                warnings.append(
                    "models were requested but the Ollama model store is not mounted; "
                    "the package was built without them")
            else:
                for model in wanted.values():
                    attempted: list[str] = []
                    try:
                        for rel, source in model_bundle.export_model(model, stage):
                            attempted.append(rel)
                            b.add(rel, lambda p, s=source: shutil.copyfile(s, p))
                    except (OSError, ValueError) as exc:
                        # Import treats a model whose manifest.json is in the
                        # package as bundled, so leave no part of it behind (#449).
                        b.discard(attempted)
                        warnings.append(f"model '{model}' could not be bundled: {exc}")
                    else:
                        bundled.append(model_bundle.model_ref(model))
        models_bundled = bool(bundled)

        if unresolved:
            warnings.append(
                f"{unresolved} chunk(s) could not be linked to a single source "
                "document; their source_sha256 is null"
            )

        # 6. manifest — written from the digests collected above
        chunking = None
        if ingest_cfg:
            chunking = {
                "strategy": ingest_cfg.get("chunking_strategy"),
                "chunk_size": ingest_cfg.get("chunk_size"),
                "chunk_overlap": ingest_cfg.get("chunk_overlap"),
            }
        elif first_props:
            # No saved config: report what the chunks themselves say.
            chunking = {
                "strategy": first_props.get("chunk_strategy"),
                "chunk_size": first_props.get("chunk_size"),
                "chunk_overlap": first_props.get("chunk_overlap"),
            }

        manifest = {
            "package_format": PACKAGE_FORMAT,
            "created_at": created_at,
            "produced_by": {
                "platform": "rag-docker",
                "weaviate": str(wc._meta_sync().get("version", "unknown")),
            },
            "collection": {
                "name": collection,
                "chunk_count": chunk_count,
                "source_document_count": source_document_count,
            },
            "embedding": {
                "model": settings.embed_model,
                "dimensions": dimensions,
            },
            "llm": {"model": settings.llm_model},
            "chunking": chunking,
            "fidelity": fidelity,
            # What the package actually carries, not what was asked for: a
            # request that could not be honoured is recorded in `warnings`.
            "models_bundled": models_bundled,
            # Only the models whose files were all added; a model that failed
            # is recorded in `warnings` instead.
            "bundled_models": sorted(set(bundled)),
            # False means the collection had no saved retrieval settings, so a
            # script would have carried stock defaults while implying tuned ones.
            "retrieve_script": has_saved_retrieval,
            "files": dict(sorted(b.files.items())),
            "warnings": warnings,
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        id8 = sha256_file(manifest_path)[:8]

        # 7. the generated pair, which embed <id8>
        stem = package_stem(collection, created, id8)
        filename = f"{stem}.tar.gz"

        if has_saved_retrieval:
            (stage / "retrieve.py").write_text(_render_retrieve(collection, retrieval_cfg, {
                "package_filename": filename,
                "id8": id8,
                "created_at": created_at,
                "embed_model": settings.embed_model,
                "embed_dimensions": dimensions,
            }))
            (stage / "retrieve.py").chmod(0o755)

        (stage / "README.md").write_text(_render("package_readme.md.tmpl", {
            "COLLECTION_NAME": collection,
            "CHUNK_COUNT": chunk_count,
            "SOURCE_DOCUMENT_COUNT": source_document_count,
            "FIDELITY": fidelity,
            "FIDELITY_NOTE": _fidelity_note(fidelity),
            "EMBED_MODEL": settings.embed_model,
            "EMBED_DIMENSIONS": dimensions if dimensions is not None else "unknown",
            "CREATED_AT": created_at,
            "ID8": id8,
            "PACKAGE_FILENAME": filename,
            "RETRIEVE_SECTION": _retrieve_section(has_saved_retrieval, retrieval_cfg, collection),
        }))

        # 8. archive under a temporary name, then rename, so a partial file is
        #    never mistaken for a package.
        tmp_archive = out_dir / f".{stem}.tar.gz.partial"
        _write_archive(stage, stem, tmp_archive)
        final = out_dir / filename
        tmp_archive.replace(final)
        tmp_archive = None

        return {
            "filename": filename,
            "size_bytes": final.stat().st_size,
            "chunk_count": chunk_count,
            "source_document_count": source_document_count,
            "fidelity": fidelity,
            "models_bundled": models_bundled,
            "retrieve_script": has_saved_retrieval,
            "id8": id8,
            "warnings": warnings,
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if tmp_archive is not None:
            tmp_archive.unlink(missing_ok=True)


# ── Reading a package back ────────────────────────────────────────────────────

class PackageError(Exception):
    """A package that cannot be used, carrying the spec §11 error code."""

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


# The listing reads only the manifest (#383). It holds one `files` entry per file
# in the package, so 16 MiB is far more than any real package needs; a larger
# manifest is refused unread and the package is listed as unreadable.
MANIFEST_MAX_BYTES = 16 * 1024 * 1024


def _is_top_manifest(name: str) -> bool:
    """True for `<top-level directory>/manifest.json`, not one deeper down."""
    parts = PurePosixPath(name).parts
    return len(parts) == 2 and parts[1] == "manifest.json"


def read_manifest(archive: Path) -> dict:
    """Read the top-level manifest.json out of a package, for the listing.

    Members are read one at a time and nothing after the manifest is touched,
    so a package is not decompressed in full (#383). New packages put the
    manifest first; in older ones chunks.jsonl comes before it and is read past.
    Raises when the package has no readable manifest, or one larger than
    MANIFEST_MAX_BYTES or not a JSON object.
    """
    with tarfile.open(archive, "r:gz") as tar:
        while (member := tar.next()) is not None:
            if not (_is_top_manifest(member.name) and member.isfile()):
                continue
            if member.size > MANIFEST_MAX_BYTES:
                raise ValueError(f"manifest.json is {member.size} bytes, over the "
                                 f"{MANIFEST_MAX_BYTES}-byte limit")
            fh = tar.extractfile(member)
            if fh is None:
                break
            manifest = json.loads(fh.read(member.size).decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest.json is not a JSON object")
            return manifest
    raise ValueError("no manifest.json in package")


def listing_fields(manifest: dict) -> dict:
    """The four values the listing shows, from a manifest read_manifest returned.

    An absent or null value is listed as None. A value of the wrong JSON type
    (spec §4.4) raises instead of being coerced, so the package is listed as
    unreadable rather than the whole listing failing (#451).
    """
    collection = manifest.get("collection")
    if collection is None:
        collection = {}
    elif not isinstance(collection, dict):
        raise ValueError("manifest collection is not a JSON object")
    fields = {"collection": collection.get("name"),
              "chunk_count": collection.get("chunk_count"),
              "fidelity": manifest.get("fidelity"),
              "created_at": manifest.get("created_at")}
    for key in ("collection", "fidelity", "created_at"):
        if fields[key] is not None and not isinstance(fields[key], str):
            raise ValueError(f"manifest {key} is not a string")
    count = fields["chunk_count"]
    if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
        raise ValueError("manifest chunk_count is not an integer")
    return fields


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing any member that would land outside dest.

    A package is a file someone was handed, so it is untrusted input. Python
    3.12 has `filter="data"` for this; doing it explicitly keeps the guarantee
    on 3.11 and makes the refusal auditable.
    """
    root = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise PackageError(
                "PACKAGE_UNREADABLE",
                f"Package contains a link ('{member.name}'), which is not allowed.",
                {"member": member.name})
        if not (member.isfile() or member.isdir()):
            raise PackageError(
                "PACKAGE_UNREADABLE",
                "Package contains a non-regular archive member.",
                {"member": member.name})
        target = (dest / member.name).resolve()
        if target != root and root not in target.parents:
            raise PackageError(
                "PACKAGE_UNREADABLE",
                f"Package contains a path outside the archive ('{member.name}').",
                {"member": member.name})
    tar.extractall(dest)


@telemetry.traced("rag.package")
def open_package(archive: Path, dest: Path) -> tuple[Path, dict]:
    """Checks 1 and 2 of spec §6.2: readable archive, understood format.

    Returns (package root directory, manifest).
    """
    if not archive.is_file():
        raise PackageError("PACKAGE_UNREADABLE",
                           f"No package named '{archive.name}' in the exports directory.")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, dest)
    except PackageError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise PackageError("PACKAGE_UNREADABLE",
                           f"'{archive.name}' is not a readable .tar.gz archive.",
                           {"reason": f"{type(exc).__name__}: {exc}"}) from exc

    roots = [p for p in dest.iterdir() if p.is_dir()]
    # §4.1 requires exactly one top-level directory.
    if len(roots) != 1:
        raise PackageError("PACKAGE_UNREADABLE",
                           f"'{archive.name}' does not expand to a single directory.",
                           {"found": sorted(p.name for p in roots)})
    pkg = roots[0]

    manifest_path = pkg / "manifest.json"
    if not manifest_path.is_file():
        raise PackageError("PACKAGE_FORMAT_UNSUPPORTED",
                           f"'{archive.name}' has no manifest.json, so it is not a RAG package.")
    try:
        manifest = json.loads(manifest_path.read_text())
    except ValueError as exc:
        raise PackageError("PACKAGE_FORMAT_UNSUPPORTED",
                           f"The manifest in '{archive.name}' is not valid JSON.",
                           {"reason": str(exc)}) from exc

    fmt = manifest.get("package_format")
    if not isinstance(fmt, int) or fmt > PACKAGE_FORMAT:
        raise PackageError(
            "PACKAGE_FORMAT_UNSUPPORTED",
            f"Package format {fmt!r} is newer than this instance understands "
            f"(supported: {PACKAGE_FORMAT}). Upgrade rag-docker to import it.",
            {"package_format": fmt, "supported": PACKAGE_FORMAT})
    return pkg, manifest


def _contained_file_key(root: Path, rel: str) -> bool:
    """True when a manifest `files` key names a file inside the package.

    The manifest is untrusted: an absolute key, a `..` key or a key through a
    link would otherwise make check 3 hash a file outside the package and
    report its digest (#371). `root` is the resolved package directory.
    """
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False
    if PurePosixPath(rel).as_posix() != rel:
        return False
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return root in target.parents


@telemetry.traced("rag.validate")
def verify_digests(pkg: Path, manifest: dict) -> None:
    """Check 3 of spec §6.2. Names the first file that fails.

    Every key is checked to be inside the package before anything is hashed.
    """
    files = manifest.get("files", {})
    root = pkg.resolve()
    if not all(_contained_file_key(root, rel) for rel in files):
        raise PackageError(
            "PACKAGE_CORRUPT",
            "The manifest lists a file outside the package; the package is damaged "
            "or was modified after export.",
            {"file": "manifest.json"})
    for rel, expected in sorted(files.items()):
        target = pkg / rel
        if not target.is_file():
            raise PackageError("PACKAGE_CORRUPT",
                               f"'{rel}' is listed in the manifest but missing from the package.",
                               {"file": rel})
        actual = "sha256:" + sha256_file(target)
        if actual != expected:
            raise PackageError(
                "PACKAGE_CORRUPT",
                f"'{rel}' does not match its manifest digest; the package is damaged "
                "or was modified after export.",
                {"file": rel, "expected": expected, "actual": actual})


def iter_chunks_file(pkg: Path) -> Iterator[dict]:
    """Stream chunks.jsonl back. Mirrors read_chunks() on the way in."""
    path = pkg / "chunks.jsonl"
    with path.open("r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise PackageError("PACKAGE_CORRUPT",
                                   f"chunks.jsonl line {number} is not valid JSON.",
                                   {"file": "chunks.jsonl", "line": number}) from exc
