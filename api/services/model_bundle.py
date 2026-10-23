"""Copying Ollama models into and out of an export package.

Ollama stores a model as a manifest plus content-addressed blobs:

    models/manifests/registry.ollama.ai/library/<name>/<tag>   (JSON)
    models/blobs/sha256-<hex>                                  (config + layers)

The manifest names every blob the model needs, so bundling resolves blobs
*through the manifest* rather than copying the blob store. The store here holds
2.3 GB across 9 blobs for two models; a naive copy would sweep up unrelated
weights, and on a machine with other models pulled it would be far worse.

In a package each model gets its own folder, `models/<name>/<tag>/`, so two
tags of one name (an embedding model `foo:a` and an LLM `foo:b`) never share
files. Packages built before #487 used `models/<name>/`; such a folder is used
only for the one tag the package manifest gives that name.

Copying the files verbatim is deliberate. Offline there is no registry to pull
from, and rebuilding a model through Ollama's HTTP API would mean reconstructing
its template, params and license from the config blob — a reproduction, not the
model that produced the vectors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import logging
from pathlib import Path

from config import settings

_log = logging.getLogger(__name__)

REGISTRY = "registry.ollama.ai"
NAMESPACE = "library"
DEFAULT_TAG = "latest"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_READ_SIZE = 1024 * 1024


def _root() -> Path:
    return Path(settings.ollama_models_dir) / "models"


def split_ref(model: str) -> tuple[str, str]:
    """'phi3.5' -> ('phi3.5', 'latest'); 'phi3.5:3.8b' -> ('phi3.5', '3.8b')."""
    name, _, tag = model.partition(":")
    tag = tag or DEFAULT_TAG
    if not _COMPONENT.fullmatch(name) or not _COMPONENT.fullmatch(tag):
        raise ValueError("Model name and tag must be simple path components")
    return name, tag


def model_ref(model: str) -> str:
    """'phi3.5' -> 'phi3.5:latest': the name and tag a package folder holds."""
    name, tag = split_ref(model)
    return f"{name}:{tag}"


def manifest_path(model: str) -> Path:
    name, tag = split_ref(model)
    return _contained(_root(), "manifests", REGISTRY, NAMESPACE, name, tag)


def blob_path(digest: str) -> Path:
    # Manifests write 'sha256:<hex>'; the filename on disk is 'sha256-<hex>'.
    _validate_digest(digest)
    return _contained(_root(), "blobs", digest.replace(":", "-"))


def store_available() -> bool:
    """False when the model store is not mounted, e.g. an older compose file."""
    return _root().is_dir()


def _contained(root: Path, *parts: str) -> Path:
    """Reject symlinks and paths outside the configured store/package root."""
    root = root.resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Model path leaves its storage root")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Model storage path is a symlink: {current.name}")
    return path


def _validate_digest(digest) -> None:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Model blob digest must be sha256 followed by 64 lowercase hex digits")


def _digests(manifest: dict) -> list[str]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
        raise ValueError("Model manifest must contain a config object")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or any(not isinstance(layer, dict) for layer in layers):
        raise ValueError("Model manifest layers must be an array of objects")
    digests = [manifest["config"].get("digest"), *(layer.get("digest") for layer in layers)]
    for digest in digests:
        _validate_digest(digest)
    return list(dict.fromkeys(digests))


def _check_blob(path: Path, digest: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"Model blob is not a regular file: {path.name}")
    actual = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_READ_SIZE):
            actual.update(chunk)
    if actual.hexdigest() != digest.split(":", 1)[1]:
        raise ValueError(f"Model blob bytes disagree with {digest}; restore the content before importing")


def supports_name(model: str) -> bool:
    """Whether the model has a path in this store layout.

    Namespaced names (`user/model`, `hf.co/org/model`) don't: they can still be
    pulled and served by Ollama, but they can't be checked or installed here.
    """
    try:
        split_ref(model)
    except ValueError:
        return False
    return True


def installed_state(model: str) -> str:
    """'present', 'absent' or 'corrupt'.

    'corrupt' means a manifest exists but a referenced blob is missing or its
    bytes disagree with its address. Import treats that as its own outcome:
    telling the user to pull a model they already have would send them the
    wrong way.
    """
    split_ref(model)  # a name with no path here is the caller's to handle (supports_name)
    try:
        mp = manifest_path(model)
    except ValueError:
        # A path that leaves the store or passes through a symlink is not a
        # model this store can vouch for.
        return "corrupt"
    if not mp.is_file():
        return "absent"
    try:
        for digest in _digests(json.loads(mp.read_text())):
            _check_blob(blob_path(digest), digest)
    except (OSError, ValueError):
        return "corrupt"
    return "present"


def is_installed(model: str) -> bool:
    """A model is installed only if its referenced bytes match their addresses."""
    try:
        return installed_state(model) == "present"
    except (OSError, ValueError):
        return False


def export_model(model: str, dest: Path) -> list[tuple[str, Path]]:
    """Files to place under `models/<name>/<tag>/`, as (relative path, source).

    Each tag has its own folder, so two tags of one name never overwrite each
    other's manifest (#487). Returns them rather than copying so the caller can digest each file into the
    package manifest as it is written.
    """
    mp = manifest_path(model)
    if not mp.is_file():
        raise FileNotFoundError(f"Ollama has no manifest for '{model}' at {mp}")
    manifest = json.loads(mp.read_text())

    name, tag = split_ref(model)
    files: list[tuple[str, Path]] = [(f"models/{name}/{tag}/manifest.json", mp)]
    for digest in _digests(manifest):
        blob = blob_path(digest)
        if not blob.is_file():
            raise FileNotFoundError(
                f"Model '{model}' references blob {digest} which is not in the store")
        files.append((f"models/{name}/{tag}/blobs/{digest.replace(':', '-')}", blob))
    return files


def _legacy_tag(name: str, package_manifest) -> str | None:
    """The tag a pre-#487 `models/<name>/` folder holds, or None if unknown.

    Those packages didn't record it. The package manifest names the embedding
    model and the LLM; when exactly one tag of `name` is among them, the folder
    holds that tag. Two tags of one name shared the folder, the last copy
    overwriting the first's manifest, so the folder can't be trusted for either.
    """
    if not isinstance(package_manifest, dict):
        return None
    tags = set()
    for section in ("embedding", "llm"):
        entry = package_manifest.get(section)
        model = entry.get("model") if isinstance(entry, dict) else None
        if isinstance(model, str) and supports_name(model):
            model_name, tag = split_ref(model)
            if model_name == name:
                tags.add(tag)
    return tags.pop() if len(tags) == 1 else None


def bundled_source(pkg: Path, model: str, package_manifest=None) -> Path | None:
    """The package folder holding exactly this name and tag, or None."""
    name, tag = split_ref(model)
    folder = _contained(pkg, "models", name, tag)
    if _contained(folder, "manifest.json").is_file():
        return folder
    legacy = _contained(pkg, "models", name)
    if _contained(legacy, "manifest.json").is_file() and _legacy_tag(name, package_manifest) == tag:
        return legacy
    return None


def bundled_models(pkg: Path, package_manifest=None) -> list[str]:
    """`name:tag` of each model the package bundles, by the rules of `bundled_source`."""
    d = pkg / "models"
    if not d.is_dir():
        return []
    refs = set()
    for folder in d.iterdir():
        if not folder.is_dir():
            continue
        if (folder / "manifest.json").is_file():
            tag = _legacy_tag(folder.name, package_manifest)
            if tag is not None:
                refs.add(f"{folder.name}:{tag}")
        refs.update(f"{folder.name}:{sub.name}" for sub in folder.iterdir()
                    if sub.is_dir() and (sub / "manifest.json").is_file())
    return sorted(refs)


def _publish(path: Path, write, *, replace: bool = True) -> None:
    """Write a unique private temporary file and publish only verified bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    tmp = Path(filename)
    try:
        with os.fdopen(fd, "wb") as output:
            write(output)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            tmp.replace(path)
        else:
            try:
                os.link(tmp, path)  # An independent installer may have won; never overwrite its blob.
            except FileExistsError:
                pass               # The caller verifies the winning content before publishing a manifest.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


def install_model(pkg: Path, model: str, package_manifest=None) -> None:
    """Verify all referenced bytes before publishing the captured manifest.

    Existing content is checked and reused unchanged. A corrupt existing blob
    is refused rather than replacing shared content behind other model names.
    An interrupted install can leave valid unreferenced blobs, never a newly
    published manifest whose bytes were not checked. Only a folder holding this
    exact name and tag is installed (#487); `package_manifest` tells which tag
    a pre-#487 folder holds.
    """
    src = bundled_source(pkg, model, package_manifest)
    if src is None:
        raise FileNotFoundError(f"Package does not bundle '{model}'")
    src_manifest = _contained(src, "manifest.json")
    manifest_bytes = src_manifest.read_bytes()
    digests = _digests(json.loads(manifest_bytes))
    destination = manifest_path(model)
    references = []
    for digest in digests:
        source = _contained(src, "blobs", digest.replace(":", "-"))
        target = blob_path(digest)
        _check_blob(source, digest)
        if target.exists():
            _check_blob(target, digest)
        references.append((digest, source, target))

    for digest, source, target in references:
        if target.exists():
            _check_blob(target, digest)
            continue
        def copy_verified(output, source=source, digest=digest):
            actual = hashlib.sha256()
            with source.open("rb") as data:
                while chunk := data.read(_READ_SIZE):
                    actual.update(chunk)
                    output.write(chunk)
            if actual.hexdigest() != digest.split(":", 1)[1]:
                raise ValueError(f"Bundled blob changed or disagrees with {digest}")
        _publish(target, copy_verified, replace=False)

    # Recheck reused files after writes too; manifest data itself is the exact
    # snapshot whose grammar and references were preflighted, not a reread.
    for digest, _, target in references:
        _check_blob(blob_path(digest), digest)
    _publish(destination, lambda output: output.write(manifest_bytes))
    _log.info("Installed model %r from package", model)
