"""The import job: validate a package, then build a collection from it.

Reads the package through `packager.py` so the format has exactly one
implementation. Validation order is spec §6.2 and stops at the first failure.

**Atomicity, and where it departs from the plan.** The plan said to build into a
temporary collection and rename it on success. Weaviate has no rename:
`client.collections` offers create/delete/exists/get/list_all and nothing else,
confirmed against 4.23.1. So the guarantee in spec §6.5 — a failed import leaves
no partial collection and never destroys the target — is met differently
depending on whether there is anything to protect:

* `abort` and `rename` produce a collection name that does not yet exist, so the
  build goes straight into it and is deleted on failure. Nothing pre-existing is
  at risk, and there is no second pass.
* `replace` builds into a temporary collection first, to prove the package
  inserts cleanly, and only then deletes the existing collection and builds the
  real one. That costs a second insert pass, which is the price of a
  staged replace with a recoverable failure path in a database that cannot rename. If the second pass
  fails, the temporary collection is *kept* and named in the error, so the data
  is recoverable rather than lost.
"""
from __future__ import annotations
from services import telemetry

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from contextlib import nullcontext

from config import settings
from services import settings_store
from models.schemas import SEARCH_EF_MAX, SEARCH_EF_MIN
from services import goldstandard
from services import model_bundle
from services import packager
from services import retrieval_config
from services import sources
from services import weaviate_client as wc
from services import batch_write, collection_recovery, collection_writes, jobstore
from services.packager import PackageError

_log = logging.getLogger(__name__)

ON_CONFLICT = ("abort", "rename", "replace")

# Job records survive an API restart (#23); see jobstore.py.
_jobs = jobstore.JobStore("import")
# Package names being imported: the file the worker opens, whatever the path
# spelling sent (#391).
_active: set[str] = set()
# Target collections claimed by an import job, canonical name -> job id. A second
# import into the same target is refused whatever its conflict mode, so two
# imports never build, mark or clean up one collection at once (#391).
_targets: dict[str, str] = {}
_lock = threading.Lock()

# Weaviate capitalises the first character of a collection name and rejects
# anything outside [A-Za-z0-9_]. Both were confirmed against the live server.
_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


# ── Surviving a hard kill ─────────────────────────────────────────────────────
#
# `abort` and `rename` build straight into the target collection, so there is no
# staging name for the startup sweep to recognise. A SIGKILL during the insert
# would leave a half-filled collection that looks like a real one. A marker
# written before the build, and removed after it, lets the next start tell the
# two apart. The marker's instance token is also written into the collection's
# schema description, so a collection created later under the same name is
# never mistaken for the interrupted import.

def _markers_dir() -> Path:
    d = Path(settings.upload_dir) / "imports_in_progress"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path(collection: str) -> Path:
    return _markers_dir() / f"{_safe_file(collection)}.json"


def _instance_description(instance: str) -> str:
    return f"rag-import:{instance}"


def _read_marker(path: Path) -> tuple[dict, Path]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("Import ownership must be a regular metadata file of at most 4096 bytes")
    data = json.loads(path.read_text())
    collection, count = data["collection"], data["expected_chunks"]
    snapshot = data["expected_snapshot"]
    # Version 3 predates the instance identity: it is read so that its cleanup
    # can finish and its snapshot stays named, but it never authorizes deletion.
    if (type(data.get("version")) is not int or data["version"] not in (3, 4)
            or not _NAME_OK.fullmatch(collection) or collection != canonical(collection)
            or path.name != f"{collection}.json" or type(count) is not int or count < 0
            or data["state"] not in ("building", "cleanup")
            or (data["version"] == 4 and (type(data["instance"]) is not str
                                          or not re.fullmatch(r"[0-9a-f]{32}", data["instance"])))
            or not re.fullmatch(r"[0-9a-f]{32}\.sqlite3", snapshot["file"])
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"])):
        raise ValueError("Invalid import ownership")
    return data, _markers_dir() / snapshot["file"]


def _mark_started(collection: str, expected_chunks: int, job_id: str, records) -> str:
    """Publish the marker; returns the instance token the target must carry."""
    instance = uuid.uuid4().hex
    snapshot = _markers_dir() / f"{uuid.uuid4().hex}.sqlite3"
    try:
        with batch_write.ExpectedRecords() as expected:
            try:
                expected.capture(records, expected_chunks)
            except (ValueError, KeyError, TypeError) as exc:
                raise PackageError("PACKAGE_CORRUPT", str(exc), {"file": "chunks.jsonl"}) from exc
            expected.snapshot(snapshot)
        with snapshot.open("rb") as data:
            os.fsync(data.fileno())
        collection_recovery._sync_dir(_markers_dir())
        collection_recovery._sync_dir(Path(settings.upload_dir))
        collection_recovery.atomic_json(_marker_path(collection), {
            "version": 4, "collection": collection, "expected_chunks": expected_chunks,
            "job_id": job_id, "state": "building", "instance": instance,
            "expected_snapshot": {"file": snapshot.name, "sha256": packager.sha256_file(snapshot)},
        })
    except Exception:
        try:
            snapshot.unlink(missing_ok=True)
        except OSError:
            _log.exception("Could not remove unpublished expectation snapshot %s", snapshot)
        raise
    return instance


def _mark_finished(collection: str) -> None:
    marker = _marker_path(collection)
    if not marker.exists():
        return
    record, snapshot = _read_marker(marker)
    if record["state"] != "cleanup":
        record["state"] = "cleanup"
        collection_recovery.atomic_json(marker, record)
    snapshot.unlink(missing_ok=True)
    marker.unlink()
    collection_recovery._sync_dir(_markers_dir())


# Extraction workspaces created by an import or a re-chunk, and an ingest's
# upload staging (#377). Each removes its own directory in a `finally`, which a
# hard kill skips -- twelve of these were found holding 152 MB after the kill
# tests, a with-models package would leave 2.3 GB behind each time, and an
# ingest up to 512 MB of upload plus 2 GiB of extracted ZIP members.
_WORKDIR_PREFIXES = ("import-", "rechunk-", "batch-verify-", "ingest-")


def sweep_stale_workdirs() -> list[str]:
    """Remove work directories abandoned by a killed job; never follows a link."""
    root = Path(settings.upload_dir)
    removed: list[str] = []
    if not root.is_dir():
        return removed
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith(_WORKDIR_PREFIXES):
            continue
        size = packager.remove_leftover(entry, directory=True)
        if size is not None:
            removed.append(f"{entry.name} ({size // (1024 * 1024)} MB)")
    return removed


def sweep_interrupted_imports() -> list[str]:
    """Remove collections left half-built by a killed import.

    Compare the expected identities, properties and vectors, not just count.
    Only the collection instance the import created, identified by the token
    in its schema description, can be deleted. Unreadable or legacy ownership
    cannot authorize destructive cleanup.
    """
    removed: list[str] = []
    for marker in sorted(_markers_dir().glob("*.json")):
        try:
            data, snapshot = _read_marker(marker)
            collection = data["collection"]
            if data["state"] == "building" and data["version"] == 3:
                # No instance identity: it cannot tell this import's collection
                # from a later one under the same name, so it never deletes.
                _log.warning("Import marker %s has no instance identity; kept, and never "
                             "used to delete %r", marker, collection)
                continue
            if data["state"] == "building":
                if snapshot.is_symlink() or not snapshot.is_file():
                    raise ValueError("Expected-record snapshot is not a regular file")
                if packager.sha256_file(snapshot) != data["expected_snapshot"]["sha256"]:
                    raise ValueError("Expected-record snapshot integrity mismatch")
                with batch_write.ExpectedRecords() as expected:
                    expected.load_snapshot(snapshot, data["expected_chunks"])
                    if wc._collection_exists_sync(collection):
                        col = wc.get_client().collections.get(collection)
                        if telemetry.call("weaviate.config", col.config.get).description != _instance_description(data["instance"]):
                            # Created after the import stopped, for example while
                            # an earlier start could not resolve this marker.
                            _log.warning("Collection %r is not the one the interrupted import "
                                         "created; kept, and its import marker retired", collection)
                        else:
                            try:
                                expected.verify(col, exact=True)
                            except batch_write.BatchVerificationError:
                                telemetry.call("weaviate.delete", wc.get_client().collections.delete, collection)
                                removed.append(f"{collection} (persisted records did not match import)")
            # Cleanup is an explicit durable phase: failure here never converts
            # a verified target back into a candidate for backend deletion.
            _mark_finished(collection)
        except Exception:
            _log.exception("Could not resolve import ownership %s; preserved", marker)
            continue
    _sweep_orphaned_snapshots()
    return removed


def _sweep_orphaned_snapshots() -> None:
    """Remove expectation snapshots that no marker names.

    A kill between writing the snapshot and publishing its marker, or a new
    import overwriting a marker, leaves one behind. A marker that cannot be
    parsed might name any of them, so then nothing is removed.
    """
    directory = _markers_dir()
    referenced: set[str] = set()
    for marker in directory.glob("*.json"):
        try:
            if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
                raise ValueError("not a bounded regular file")
            data = json.loads(marker.read_text())
        except (OSError, ValueError):
            _log.warning("Unreadable import marker %s; expectation snapshots preserved", marker)
            return
        snapshot = data.get("expected_snapshot") if isinstance(data, dict) else None
        if isinstance(snapshot, dict) and isinstance(snapshot.get("file"), str):
            referenced.add(snapshot["file"])
    for path in sorted(directory.glob("*.sqlite3")):
        if (path.name in referenced or not re.fullmatch(r"[0-9a-f]{32}\.sqlite3", path.name)
                or path.is_symlink() or not path.is_file()):
            continue
        try:
            path.unlink()
        except OSError:
            _log.exception("Could not remove orphaned expectation snapshot %s", path)
            continue
        _log.warning("Removed orphaned import expectation snapshot %s", path.name)


def canonical(name: str) -> str:
    """The name Weaviate will actually store, so collision checks are honest."""
    return name[:1].upper() + name[1:] if name else name


def _package_key(filename: str) -> str:
    """The package the worker opens, so every spelling of one file is one key."""
    return Path(filename).name


def _claim_target(job_id: str, target: str, instead_of: str | None = None) -> str:
    """Claim `target` for this import, or refuse while another import holds it.

    `instead_of`, a name this job claimed earlier, is released in the same step:
    a `rename` no longer writes the name it moved away from.
    """
    name = canonical(target)
    with _lock:
        owner = _targets.get(name)
        if owner is not None and owner != job_id:
            raise PackageError(
                "IMPORT_IN_PROGRESS",
                f"Another import into '{name}' is running. Start this import again "
                "once it finishes.",
                {"collection": name})
        if instead_of is not None and _targets.get(canonical(instead_of)) == job_id:
            del _targets[canonical(instead_of)]
        _targets[name] = job_id
    return name


def _release_targets(job_id: str) -> None:
    with _lock:
        for name in [name for name, owner in _targets.items() if owner == job_id]:
            del _targets[name]


def _safe_file(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _rename_target(name: str, id8: str) -> str:
    """Spec §6.4's `rename`, adjusted to a name Weaviate accepts.

    The spec says `<name>-imported-<id8>`; Weaviate rejects hyphens with a 422,
    so the separator is an underscore. A numeric suffix is added only if that
    name is taken too, which happens when the same package is imported twice.
    """
    base = f"{canonical(name)}_imported_{id8}"
    if not wc._collection_exists_sync(base):
        return base
    for n in range(2, 100):
        candidate = f"{base}_{n}"
        if not wc._collection_exists_sync(candidate):
            return candidate
    raise PackageError("COLLECTION_EXISTS",
                       f"Could not find a free name based on '{base}'.")


# ── Validation (spec §6.2) ────────────────────────────────────────────────────

def _check_embedding(manifest: dict) -> None:
    """Check 4. A refusal, not a warning — see spec §6.2."""
    embedding = manifest.get("embedding") or {}
    pkg_model = embedding.get("model")
    pkg_dims = embedding.get("dimensions")
    our_model = settings.embed_model

    if pkg_model != our_model:
        fidelity = manifest.get("fidelity")
        # Nothing re-embeds a refused package, whatever its fidelity (#390).
        remedy = (f"Use an instance running '{pkg_model}', or re-export from one."
                  if fidelity == "with-sources" else
                  "This package is `chunks-only`, so it cannot be re-embedded from "
                  "the original documents. Use an instance running "
                  f"'{pkg_model}', or re-export from one.")
        actual = _probe_dimensions()
        ours = (f"{actual} dimensions" if actual is not None else
                "whose dimension count couldn't be read from Ollama")
        raise PackageError(
            "EMBEDDING_MISMATCH",
            f"The package was embedded with '{pkg_model}' ({pkg_dims} dimensions) "
            f"but this instance uses '{our_model}' ({ours}). Vectors from a different "
            f"model are meaningless here, not merely different, so the import is "
            f"refused. " + remedy,
            {"package_model": pkg_model, "package_dimensions": pkg_dims,
             "instance_model": our_model, "instance_dimensions": actual})

    # Same model name but a different width means one side is not what it claims.
    if pkg_dims is not None:
        actual = _probe_dimensions()
        if actual is not None and actual != pkg_dims:
            raise PackageError(
                "EMBEDDING_MISMATCH",
                f"The package reports {pkg_dims}-dimension vectors from "
                f"'{pkg_model}', but this instance's '{our_model}' produces "
                f"{actual}. The models share a name but not a vector space.",
                {"package_model": pkg_model, "package_dimensions": pkg_dims,
                 "instance_model": our_model, "instance_dimensions": actual})


_probed_dimensions: int | None = None


def _probe_dimensions() -> int | None:
    """Embed a token once to learn this instance's real vector width."""
    global _probed_dimensions
    if _probed_dimensions is None:
        try:
            from services import ollama_client
            vector = asyncio.run(ollama_client.embed("dimension probe"))
            _probed_dimensions = len(vector)
        except Exception as exc:                      # noqa: BLE001
            _log.warning("Could not probe embedding dimensions: %s", exc)
            return None
    return _probed_dimensions


def _ollama_reports(model: str) -> bool | None:
    """Whether Ollama lists the model, matching `name:tag` (tag defaults to latest).

    None means Ollama couldn't be asked. That isn't "absent": saying the model is
    missing would send the user to pull one they may already have.
    """
    from services import ollama_client
    want = model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"
    try:
        return want in asyncio.run(ollama_client.list_models())
    except Exception as exc:                      # noqa: BLE001
        _log.warning("Could not list Ollama models: %s", exc)
        return None


def _ensure_models(pkg: Path, manifest: dict) -> list[str]:
    """Spec §6.3. The embedding model is the one that decides the import.

    Present, bytes intact -> skip; an existing model is assumed deliberate.
    Present, bytes differ -> MODEL_INTEGRITY_FAILED (embedding) or a note (LLM).
    Absent, bundled       -> install, then verify Ollama actually reports it.
    Absent, unbundled     -> EMBEDDING_MODEL_MISSING.
    Namespaced name       -> ask Ollama; it can't be checked or installed here.
    """
    notes: list[str] = []
    if not model_bundle.store_available():
        # Nothing can be installed or checked; say so rather than guess.
        notes.append("the Ollama model store is not mounted, so models were not checked")
        return notes

    embed_model = settings.embed_model
    llm_model = settings.llm_model
    # `name:tag`; a folder counts only for the tag it holds (#487).
    bundled = model_bundle.bundled_models(pkg, manifest)

    for model, required in ((embed_model, True), (llm_model, False)):
        if not model_bundle.supports_name(model):
            # `user/model` and the like have no path in the store layout that
            # bundles use. Refusing every import over the name would block
            # packages that don't bundle models at all, so defer to Ollama.
            reported = _ollama_reports(model)
            if reported:
                notes.append(f"model '{model}' reported by Ollama; a namespaced "
                             "model's files can't be checked, so they weren't")
                continue
            if reported is None:
                if not required:
                    notes.append(f"model '{model}' wasn't checked: Ollama couldn't be "
                                 "reached to confirm it is there")
                    continue
                raise PackageError(
                    "IMPORT_FAILED",
                    f"Couldn't reach Ollama to confirm the embedding model '{model}' is "
                    f"there. A namespaced model can only be checked through Ollama. "
                    f"Check that the ollama service is running, then import again.",
                    {"model": model})
            if not required:
                notes.append(f"model '{model}' is absent; a namespaced model can't be "
                             "installed from a package, so pull it before querying")
                continue
            pkg_model = (manifest.get("embedding") or {}).get("model")
            raise PackageError(
                "EMBEDDING_MODEL_MISSING",
                f"This instance does not have the embedding model '{model}'. It is "
                f"a namespaced model, which can't be installed from a package. Pull "
                f"it with `docker compose exec ollama ollama pull {model}`.",
                {"model": model, "package_model": pkg_model, "bundled": bundled})

        name = model_bundle.split_ref(model)[0]
        state = model_bundle.installed_state(model)
        if state == "present":
            notes.append(f"model '{name}' already present; left untouched")
            continue
        if state == "corrupt":
            # Installing over it could break other models that share the
            # blobs, so restoring it stays an owner action (§6.3).
            if not required:
                notes.append(f"model '{name}' is installed but its files don't match "
                             "their checksums; restore or re-pull it before querying")
                continue
            raise PackageError(
                "MODEL_INTEGRITY_FAILED",
                f"The embedding model '{name}' is installed, but its files don't "
                f"match their checksums. Restore it, or re-pull it with "
                f"`docker compose exec ollama ollama pull {name}`, then import again.",
                {"model": name})
        if model_bundle.model_ref(model) not in bundled:
            if not required:
                notes.append(f"model '{name}' is absent and not bundled; "
                             "queries will fail until it is pulled")
                continue
            pkg_model = (manifest.get("embedding") or {}).get("model")
            raise PackageError(
                "EMBEDDING_MODEL_MISSING",
                f"This instance does not have the embedding model '{name}' and the "
                f"package does not bundle it. The vectors in this package were "
                f"produced by '{pkg_model}', so nothing can embed a query against "
                f"them. Pull it with `docker compose exec ollama ollama pull {name}`, "
                f"or import a package exported with include_models=true.",
                {"model": name, "package_model": pkg_model, "bundled": bundled})
        try:
            model_bundle.install_model(pkg, model, manifest)
        except (OSError, ValueError) as exc:
            raise PackageError(
                "EMBEDDING_MODEL_MISSING" if required else "IMPORT_FAILED",
                f"Could not install bundled model '{name}': {exc}",
                {"model": name}) from exc
        # Spec §6.3: confirm through Ollama itself, not the files just written (#390).
        reported = _ollama_reports(model)
        if reported is None:
            raise PackageError(
                "IMPORT_FAILED",
                f"Installed '{name}' from the package but couldn't reach Ollama to "
                f"confirm it is there. Check that the ollama service is running, "
                f"then import again.",
                {"model": name})
        if not reported:
            raise PackageError(
                "EMBEDDING_MODEL_MISSING" if required else "IMPORT_FAILED",
                f"Installed '{name}' from the package but Ollama does not report it.",
                {"model": name})
        notes.append(f"model '{name}' installed from the package")
    return notes


# ── Building ──────────────────────────────────────────────────────────────────

def _create_from_package(name: str, pkg: Path, instance: str | None = None) -> None:
    cfg_path = pkg / "collection.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    wc._create_collection_sync(
        name,
        cfg.get("index_type", "hnsw"),
        cfg.get("distance_metric", "cosine"),
        cfg.get("hnsw_config") or {},
        preserve_hnsw=True,
        description=_instance_description(instance) if instance else None,
    )


def _package_records(pkg: Path, manifest: dict):
    expected_dims = (manifest.get("embedding") or {}).get("dimensions")
    for record in packager.iter_chunks_file(pkg):
        try:
            uuid.UUID(record["id"])
            if not isinstance(record["properties"], dict):
                raise ValueError("Chunk properties must be an object")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PackageError("PACKAGE_CORRUPT", f"Invalid chunk identity or properties: {exc}",
                               {"file": "chunks.jsonl"}) from exc
        vector = record.get("vector")
        if not batch_write._valid_vector(vector):
            raise PackageError("PACKAGE_CORRUPT", f"Chunk {record.get('id')} has no valid vector.")
        if expected_dims and len(vector) != expected_dims:
            raise PackageError("PACKAGE_CORRUPT", f"Chunk {record.get('id')} has the wrong vector width.")
        yield record


def _insert_chunks(name: str, pkg: Path, manifest: dict, progress) -> int:
    """Insert every chunk with its original uuid and vector.

    The uuid is preserved deliberately: gold-standard sessions reference chunks
    by id, and that is the reason sessions are exportable at all.
    """
    client = wc.get_client()
    col = client.collections.get(name)
    try:
        written = batch_write.insert(col, lambda: _package_records(pkg, manifest),
                                     expected_count=manifest["collection"]["chunk_count"])
    except (ValueError, KeyError, TypeError) as exc:
        raise PackageError("PACKAGE_CORRUPT", str(exc), {"file": "chunks.jsonl"}) from exc
    if progress:
        progress(written)
    return written


@telemetry.traced("rag.validate")
def _validate_package_sources(pkg: Path, manifest: dict) -> None:
    """Check source identities after verify_digests, before any live mutation."""
    source_dir = pkg / "sources"
    if not source_dir.exists():
        if manifest.get("fidelity") == "with-sources":
            raise PackageError("PACKAGE_CORRUPT", "Retained sources are missing.",
                               {"file": "sources/index.json"})
        return
    index_path = source_dir / sources.INDEX_NAME
    try:
        if source_dir.is_symlink() or not source_dir.is_dir():
            raise ValueError("Retained source directory is not a regular directory")
        if (not index_path.exists() and not index_path.is_symlink()
                and manifest.get("fidelity") != "with-sources"):
            return
        if index_path.is_symlink() or not index_path.is_file():
            raise ValueError("Retained source index is missing or is not a regular file")
        index = sources.validate_index(json.loads(index_path.read_text()))
        for digest in index["documents"]:
            blob = source_dir / digest
            if blob.is_symlink() or not blob.is_file():
                raise ValueError("Retained source blob is missing or is not a regular file")
            # Check 3 already hashed listed files in this private extraction.
            # Match that verified digest to the content-addressed filename;
            # manifest omissions still need their own identity hash.
            files = manifest.get("files", {})
            rel = f"sources/{digest}"
            actual = files[rel] if rel in files else "sha256:" + packager.sha256_file(blob)
            if actual != f"sha256:{digest}":
                raise ValueError("Retained source blob does not match its identity")
    except (OSError, ValueError, TypeError) as exc:
        raise PackageError("PACKAGE_CORRUPT", "Invalid retained source metadata.",
                           {"file": "sources/index.json"}) from exc


def _read_goldstandard_sessions(pkg: Path, original: str) -> list[dict]:
    """Preflight every evaluation sidecar before touching live state.

    A valid archive and matching digests prove neither metadata validity nor
    safe persistence destinations. Retain these parsed snapshots so restoration
    cannot discover an invalid later session after a replacement has begun.
    """
    gold = pkg / "goldstandard"
    if not gold.exists():
        return []
    if not gold.is_dir():
        raise PackageError("PACKAGE_CORRUPT", "goldstandard must be a directory.",
                           {"file": "goldstandard"})
    sessions: list[dict] = []
    identities: set[str] = set()
    for path in sorted(gold.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Evaluation session must be a regular file.")
            data = json.loads(path.read_text())
            # Source IDs must use the generated grammar. An occupied one is
            # kept as provenance at restore, so only the guarded root is
            # checked here, not the source ID's own file.
            goldstandard.validate_session(data)
            if canonical(data["collection"]) != canonical(original):
                raise ValueError("Evaluation session belongs to a different collection.")
            if data["session_id"] in identities:
                raise ValueError("Duplicate evaluation session identity.")
            goldstandard._session_storage_root()
        except (OSError, ValueError, RuntimeError) as exc:
            raise PackageError(
                "PACKAGE_CORRUPT", "Invalid evaluation session metadata.",
                {"file": f"goldstandard/{path.name}"}) from exc
        identities.add(data["session_id"])
        sessions.append(data)
    return sessions


def _read_retrieval_config(pkg: Path, original: str,
                           notes: list[str] | None = None) -> dict | None:
    """Validate once before live mutation; restore this normalized snapshot.

    A legacy ef (an integer outside the save bounds, from before PR #108) is
    cleared to null, and `notes` records it for the completed job.
    """
    path = pkg / "retrieval_config.json"
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Retrieval settings must be a regular file")
        validated, cleared = retrieval_config.normalize(json.loads(path.read_text()), original)
    except (OSError, ValueError) as exc:
        raise PackageError("PACKAGE_CORRUPT", "Invalid retrieval settings.",
                           {"file": "retrieval_config.json"}) from exc
    if cleared is not None and notes is not None:
        notes.append(f"the package's retrieval setting ef={cleared} is outside "
                     f"{SEARCH_EF_MIN}-{SEARCH_EF_MAX} and was restored as null; "
                     "ef is no longer used.")
    return validated


def _restore_sidecars(target: str, pkg: Path, original: str,
                      validated_sessions: list[dict],
                      restored_sessions: list[dict] | None = None,
                      validated_retrieval: dict | None = None) -> list[str]:
    """Sources, configs and gold-standard sessions. Returns notes for the job."""
    notes: list[str] = []

    sources.restore_package(pkg, target)

    ingest_cfg = pkg / "ingest_config.json"
    if ingest_cfg.is_file():
        data = json.loads(ingest_cfg.read_text())
        data["collection"] = target
        out = Path(settings.upload_dir) / "ingest_configs"
        out.mkdir(parents=True, exist_ok=True)
        settings_store.publish(out / f"{_safe_file(target)}.json", data)

    if validated_retrieval is not None:
        data = {**validated_retrieval, "collection": target}
        try:
            retrieval_config.save(data)
        except Exception as exc:                      # noqa: BLE001
            notes.append(f"retrieval settings could not be restored: {exc}")

    if validated_sessions:
        restored = 0
        for session in validated_sessions:
            data = dict(session)
            # The session points at the collection by name; after a rename that
            # name is different, and a session pointing at nothing is worse than
            # no session.
            data["collection"] = target
            # A restored session is valid again for this collection, so any
            # orphan flag from the collection it replaced no longer applies.
            data.pop("orphaned", None)
            data.pop("orphaned_reason", None)
            data.pop("orphaned_at", None)
            # A package can carry any value here, and a write failure on the
            # source system says nothing about this one's storage.
            data.pop("persistence_error", None)
            # Write through the service: a direct file write leaves the
            # in-memory cache holding the old version, which the next flagging
            # pass would write straight back over this one.
            saved = goldstandard.store_imported_session(data, original)
            mapping = {"source_session_id": session["session_id"],
                       "session_id": saved["session_id"], "collection": target}
            if restored_sessions is not None:
                restored_sessions.append(mapping)
            notes.append(f"evaluation session '{mapping['source_session_id']}' "
                         f"restored as local '{mapping['session_id']}' for '{target}'")
            restored += 1
        if restored:
            notes.append(f"{restored} gold-standard session(s) restored")
            if target != original:
                notes.append(f"their collection was rewritten from '{original}' to '{target}'")
    return notes


@telemetry.traced("rag.rebuild")
@collection_writes.serialized("target")
def _build(target: str, pkg: Path, manifest: dict, progress, instance: str | None = None) -> int:
    """Create and fill `target`. Removes it again if anything fails."""
    _create_from_package(target, pkg, instance)
    try:
        return _insert_chunks(target, pkg, manifest, progress)
    except Exception as original:
        # Spec §6.5: a failure part-way leaves no partial collection.
        try:
            telemetry.call("weaviate.delete", wc.get_client().collections.delete, target)
        except Exception as cleanup:
            raise PackageError("IMPORT_FAILED", f"{type(original).__name__}: {original}; "
                               f"partial target cleanup failed ({cleanup})",
                               {"collection": target, "cleanup_pending": True}) from original
        raise


@telemetry.traced("rag.import")
def _run(job_id: str, filename: str, on_conflict: str) -> None:
    job = _jobs[job_id]
    job["status"] = "running"
    work = Path(tempfile.mkdtemp(prefix="import-", dir=settings.upload_dir))
    temp_collection: str | None = None
    marked: str | None = None
    # True only while a *successfully built* staging collection is on disk.
    # _build deletes its own collection on failure, so temp_collection being
    # set is not by itself evidence that anything survived to recover.
    staged = False
    ownership: dict | None = None

    def progress(n: int) -> None:
        job["chunks_written"] = n

    def finish(**fields) -> None:
        # The registration ends before the status turns terminal, so a client
        # that sees the import finish can delete the collection at once (#40).
        collection_writes.end_job("import", job_id)
        job.update(fields)

    try:
        archive = packager.exports_dir() / Path(filename).name
        pkg, manifest = packager.open_package(archive, work)        # checks 1, 2
        packager.verify_digests(pkg, manifest)                      # check 3
        _validate_package_sources(pkg, manifest)
        _check_embedding(manifest)                                  # check 4

        original = manifest["collection"]["name"]
        id8 = packager.sha256_file(pkg / "manifest.json")[:8]
        job["collection"] = original
        job["fidelity"] = manifest.get("fidelity")

        replace_notes: list[str] = []
        target = canonical(original)
        if not _NAME_OK.match(target):
            raise PackageError("PACKAGE_FORMAT_UNSUPPORTED",
                               f"'{original}' is not a usable collection name.",
                               {"name": original})

        validated_sessions = _read_goldstandard_sessions(pkg, original)
        retrieval_notes: list[str] = []
        validated_retrieval = _read_retrieval_config(pkg, original, retrieval_notes)

        model_notes = _ensure_models(pkg, manifest)                 # spec §6.3

        # One import per target, whatever its conflict mode, claimed before the
        # existence check and held until the job ends (#391).
        _claim_target(job_id, target)
        # Hold one target guard across conflict check, replacement, and sidecars.
        with (collection_writes.guard(target) if on_conflict == "replace" else nullcontext()):
            # Before the existence check: a target the delete hasn't removed yet
            # would otherwise answer COLLECTION_EXISTS (#324).
            if on_conflict == "abort" and collection_writes.delete_in_progress(target):
                raise PackageError("COLLECTION_DELETING", collection_writes.deleting_message(target),
                                   {"collection": target})
            exists = wc._collection_exists_sync(target)                 # check 5
            if exists and on_conflict == "abort":
                raise PackageError(
                    "COLLECTION_EXISTS",
                    f"A collection named '{target}' already exists. Import with "
                    "on_conflict='rename' to keep both, or 'replace' to overwrite it.",
                    {"collection": target})

            if exists and on_conflict == "rename":
                target = _claim_target(job_id, _rename_target(original, id8), instead_of=target)

            # The target is final: DELETE /collections refuses it from here until
            # this job ends (#40). Replace's own delete of the old target below
            # goes through _delete_collection_sync, which the registry never refuses.
            # A target being deleted refuses the import (#302).
            try:
                collection_writes.register_job(target, "import", job_id)
            except collection_writes.CollectionDeletingError:
                raise PackageError("COLLECTION_DELETING", collection_writes.deleting_message(target),
                                   {"collection": target}) from None

            if exists and on_conflict == "replace":
                # Prove the package inserts cleanly before destroying anything.
                ownership = collection_recovery.begin(target, "import", wc.get_client())
                temp_collection = ownership["staging"]
                _build(temp_collection, pkg, manifest, progress)
                job["chunks_written"] = 0
                collection_recovery.retain(ownership, package=pkg)
                staged = True
                # Counted before the delete, because the delete is what orphans them.
                orphaned = len({session["session_id"]
                                for spelling in collection_writes.aliases(target)
                                for session in goldstandard.sessions_for(spelling)})
                wc._delete_collection_sync(target)   # also drops its sources + config
                if orphaned:
                    # Spec §8 rule 4: silently destroying evaluation work is worse
                    # than reporting it, and refusing the import would block a
                    # legitimate operation over data the user may not care about.
                    replace_notes.append(
                        f"{orphaned} gold-standard session(s) from the replaced "
                        "collection were kept; inspect session recovery diagnostics "
                        "if an orphan marker could not be persisted")

            expected = manifest.get("collection", {}).get("chunk_count", -1)
            instance = _mark_started(target, expected, job_id, lambda: _package_records(pkg, manifest))
            marked = target
            written = _build(target, pkg, manifest, progress, instance)
            _mark_finished(target)
            marked = None
            job.setdefault("restored_sessions", [])
            notes = model_notes + replace_notes + _restore_sidecars(
                target, pkg, original, validated_sessions, job["restored_sessions"],
                validated_retrieval) + retrieval_notes

            if staged and temp_collection:
                try:
                    collection_recovery.discard(ownership, wc.get_client())
                except Exception:                         # noqa: BLE001
                    _log.exception("Could not remove staging collection %r", temp_collection)
                staged = False

            finish(status="completed", collection=target, original_collection=original,
                   chunks_written=written, renamed=(target != canonical(original)),
                   notes=notes)

    except PackageError as exc:
        telemetry.outcome("error", exc)
        finish(status="failed", error_code=exc.code, error=exc.message,
               error_detail=exc.detail)
        if staged and temp_collection:
            # The real build failed after the target was deleted. Keeping the
            # staging collection means the data is recoverable, not lost.
            job["error"] = (exc.message + f" The imported data is available as "
                            f"'{temp_collection}'.")
            job["error_detail"] = {**(exc.detail or {}), "recovered_as": temp_collection,
                                   "sidecar_snapshots": collection_recovery.sidecar_reference(ownership)}
    except Exception as exc:                          # noqa: BLE001
        telemetry.outcome("error", exc)
        _log.exception("Import of %r failed", filename)
        finish(status="failed", error_code="IMPORT_FAILED",
               error=f"{type(exc).__name__}: {exc}")
        if staged and temp_collection:
            job["error"] = (job["error"] + f" The imported data is available as "
                            f"'{temp_collection}'.")
            job["error_detail"] = {"recovered_as": temp_collection,
                                   "sidecar_snapshots": collection_recovery.sidecar_reference(ownership)}
    finally:
        # A handled failure already removed the partial collection, so the
        # marker has nothing left to describe. Only a hard kill leaves one
        # behind, which is the case the startup sweep exists for.
        if marked and not (job.get("error_detail") or {}).get("cleanup_pending"):
            try:
                _mark_finished(marked)
            except Exception:
                _log.exception("Could not finish import marker for %r; startup will retry", marked)
        if ownership and ownership["state"] == "scratch":
            try:
                collection_recovery.discard(ownership, wc.get_client())
            except Exception:
                _log.exception("Could not remove owned scratch collection %r", ownership["staging"])
        shutil.rmtree(work, ignore_errors=True)
        collection_writes.end_job("import", job_id)
        with _lock:
            _active.discard(_package_key(filename))
        _release_targets(job_id)


async def start_import_job(filename: str, on_conflict: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    key = _package_key(filename)
    with _lock:
        if key in _active:
            raise RuntimeError(filename)
        _active.add(key)

    # The first write is durable, so it runs off the event loop (#309). A start
    # cancelled meanwhile releases its guard; the store removes the record.
    try:
        await _jobs.create(job_id, {
            "job_id": job_id,
            "status": "queued",
            "filename": filename,
            "on_conflict": on_conflict,
            "collection": None,
            "original_collection": None,
            "chunks_written": 0,
            "fidelity": None,
            "renamed": False,
            "notes": [],
            "restored_sessions": [],
            "error": None,
            "error_code": None,
            "error_detail": None,
        })
    except BaseException:
        with _lock:
            _active.discard(key)
        raise
    asyncio.create_task(asyncio.to_thread(telemetry.admitted(_run), job_id, filename, on_conflict))
    return job_id
