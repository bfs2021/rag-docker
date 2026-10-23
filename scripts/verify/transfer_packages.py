#!/usr/bin/env python3
"""Write one damaged copy of an export package, for the import refusal checks (#387).

    python3 transfer_packages.py <package.tar.gz> <kind> [suffix]

Writes `<stem>-<kind><suffix>.tar.gz` beside the package and prints its name. The copy is
written under a `.part` name and renamed into place: on Docker Desktop the API can read a
freshly written bind-mounted file as empty (#184). Standard library only; run on the host by
`15_transfer_paths.sh`.

Kinds, by the import check that refuses them:
  check 1, PACKAGE_UNREADABLE:  nongzip truncated symlink hardlink dotdot twodirs
  check 2, PACKAGE_FORMAT_UNSUPPORTED:  nomanifest badmanifest newformat badname
  records, PACKAGE_CORRUPT:  badline baduuid badprops novector wrongwidth countup countdown
The record kinds re-sign `chunks.jsonl` in the manifest, so the digests still match and only
the record decides; `countup`/`countdown` change only the manifest, which is not digested.
"""
import hashlib
import io
import json
import pathlib
import sys
import tarfile
import tempfile

KINDS = ("nongzip", "truncated", "symlink", "hardlink", "dotdot", "twodirs", "nomanifest",
         "badmanifest", "newformat", "badname", "badline", "baduuid", "badprops", "novector",
         "wrongwidth", "countup", "countdown")


def extract(src, work):
    with tarfile.open(src) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(work, filter="data")
        else:                                   # the suite's own package, before 3.9.17
            tar.extractall(work)
    return next(p for p in work.iterdir() if p.is_dir())


def edit_records(root, change):
    path = root / "chunks.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    lines = [json.dumps(row) for row in rows]
    change(rows, lines)
    path.write_text("\n".join(lines) + "\n")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"]["chunks.jsonl"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))


def edit_manifest(root, change):
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest, indent=2))


def record(index, change):
    def apply(rows, lines):
        row = rows[index]
        change(row)
        lines[index] = json.dumps(row)
    return apply


def member(tar, name, **fields):
    info = tarfile.TarInfo(name)
    data = b"" if fields else b"written outside the package\n"
    info.size = len(data)
    for key, value in fields.items():
        setattr(info, key, value)
    tar.addfile(info, io.BytesIO(data))


def write(src, kind, out):
    if kind == "nongzip":
        out.write_text("this is not a gzip archive\n")
        return
    if kind == "truncated":
        data = src.read_bytes()
        out.write_bytes(data[: len(data) // 2])
        return
    with tempfile.TemporaryDirectory() as td:
        root = extract(src, pathlib.Path(td))
        if kind == "nomanifest":
            (root / "manifest.json").unlink()
        elif kind == "badmanifest":
            (root / "manifest.json").write_text("{not json")
        elif kind == "newformat":
            edit_manifest(root, lambda m: m.update(package_format=m["package_format"] + 1))
        elif kind == "badname":
            edit_manifest(root, lambda m: m["collection"].update(name="1bad-name"))
        elif kind == "countup":
            edit_manifest(root, lambda m: m["collection"].update(chunk_count=m["collection"]["chunk_count"] + 1))
        elif kind == "countdown":
            edit_manifest(root, lambda m: m["collection"].update(chunk_count=m["collection"]["chunk_count"] - 1))
        elif kind == "badline":
            edit_records(root, lambda rows, lines: lines.__setitem__(1, "{not json"))
        elif kind == "baduuid":
            edit_records(root, record(0, lambda r: r.update(id="not-a-uuid")))
        elif kind == "badprops":
            edit_records(root, record(0, lambda r: r.update(properties=["content"])))
        elif kind == "novector":
            edit_records(root, record(0, lambda r: r.pop("vector")))
        elif kind == "wrongwidth":
            edit_records(root, record(0, lambda r: r.update(vector=r["vector"][:-1])))
        with tarfile.open(out, "w:gz") as tar:
            tar.add(root, arcname=root.name)
            if kind == "symlink":
                member(tar, root.name + "/link", type=tarfile.SYMTYPE, linkname="/etc/passwd")
            elif kind == "hardlink":
                member(tar, root.name + "/hard", type=tarfile.LNKTYPE,
                       linkname=root.name + "/manifest.json")
            elif kind == "dotdot":
                member(tar, "../escape-387.txt")
            elif kind == "twodirs":
                member(tar, "second/extra.txt")


def main(argv):
    if len(argv) not in (3, 4) or argv[2] not in KINDS:
        sys.exit(__doc__)
    src, kind = pathlib.Path(argv[1]), argv[2]
    suffix = argv[3] if len(argv) == 4 else ""
    out = src.parent / (src.name[: -len(".tar.gz")] + f"-{kind}{suffix}.tar.gz")
    part = out.with_name("." + out.name + ".part")
    try:
        write(src, kind, part)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    print(out.name)


if __name__ == "__main__":
    main(sys.argv)
