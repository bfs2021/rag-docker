"""Validate one export package against RAG_EXPORT_SPECIFICATIONS.md §4.

    python3 validate_package.py <path-to-ragpkg-*.tar.gz>

Exits non-zero if any clause fails. Checks the filename convention, that <id8>
really is the manifest digest, every listed digest, that nothing in the archive
is unlisted, the chunks.jsonl record shape, source content-addressing, and the
generated README and retrieve.py.
"""
import ast, hashlib, json, re, sys, tarfile, tempfile
from pathlib import Path

archive = Path(sys.argv[1])
fails = []
def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not ok: fails.append(name)

# E5 — filename (§4.1)
NAME_RE = re.compile(r"^ragpkg-([a-z0-9]+(?:-[a-z0-9]+)*)-(\d{8}T\d{6}Z)-([0-9a-f]{8})\.tar\.gz$")
m = NAME_RE.match(archive.name)
check("E5 filename matches the §4.1 pattern", m is not None, archive.name)
if not m:
    sys.exit(1)
slug, ts, id8 = m.groups()

with tempfile.TemporaryDirectory() as td:
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        tar.extractall(td)
    root = Path(td)
    tops = {n.split("/")[0] for n in names}
    check("archive expands to a single directory", len(tops) == 1, str(tops))
    check("that directory is the filename minus .tar.gz",
          tops == {archive.name[:-len(".tar.gz")]}, str(tops))
    # §4.2 — the manifest comes right after the directory entry (#383)
    check("§4.2 manifest.json is the archive's first file",
          names[1:2] == [archive.name[:-len(".tar.gz")] + "/manifest.json"], str(names[:3]))
    pkg = root / archive.name[:-len(".tar.gz")]

    manifest = json.loads((pkg / "manifest.json").read_text())

    # §4.1 — id8 is the manifest digest prefix
    digest = hashlib.sha256((pkg / "manifest.json").read_bytes()).hexdigest()
    check("<id8> is the first 8 hex of the manifest digest",
          digest[:8] == id8, f"manifest={digest[:8]} filename={id8}")

    # §4.1 — slug is derived from the authoritative name
    name = manifest["collection"]["name"]
    expect = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    check("slug derives from manifest collection name", slug == expect,
          f"{name!r} -> {expect!r}, filename has {slug!r}")

    # E6 — every listed digest matches
    bad = []
    for rel, want in manifest["files"].items():
        f = pkg / rel
        if not f.is_file():
            bad.append(f"{rel}: listed but missing"); continue
        got = "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()
        if got != want: bad.append(f"{rel}: digest mismatch")
    check(f"E6 all {len(manifest['files'])} listed digests verify", not bad, "; ".join(bad[:3]))

    # No file in the package is silently unlisted
    UNDIGESTED = {"manifest.json", "README.md", "retrieve.py"}
    on_disk = {str(p.relative_to(pkg)) for p in pkg.rglob("*") if p.is_file()}
    unlisted = on_disk - set(manifest["files"]) - UNDIGESTED
    check("no file is unlisted and undigested", not unlisted, str(sorted(unlisted)))
    overlap = UNDIGESTED & set(manifest["files"])
    check("generated files are not in the digest map (would be circular)",
          not overlap, str(sorted(overlap)))

    # §4.2 — required layout
    for req in ("manifest.json", "collection.json", "chunks.jsonl",
                "retrieval_config.json", "README.md"):
        check(f"§4.2 contains {req}", (pkg / req).is_file())

    # §4.4 — manifest shape
    for key in ("package_format", "created_at", "produced_by", "collection",
                "embedding", "llm", "chunking", "fidelity", "models_bundled", "files"):
        check(f"§4.4 manifest has {key}", key in manifest)
    check("package_format is 1", manifest.get("package_format") == 1)
    check("created_at is UTC ISO-8601 with Z",
          bool(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", manifest["created_at"])),
          manifest["created_at"])
    check("embedding.dimensions is an int", isinstance(manifest["embedding"]["dimensions"], int),
          str(manifest["embedding"]))

    # §4.3 — fidelity
    fid = manifest["fidelity"]
    check("fidelity is one of the two spec values", fid in ("with-sources", "chunks-only"), fid)
    has_sources_dir = (pkg / "sources").is_dir()
    check("sources/ present iff fidelity is with-sources",
          has_sources_dir == (fid == "with-sources"), f"{fid}, dir={has_sources_dir}")

    # §4.5 — chunks.jsonl
    lines = (pkg / "chunks.jsonl").read_text().strip().split("\n")
    lines = [l for l in lines if l]
    check("chunk_count matches chunks.jsonl lines",
          len(lines) == manifest["collection"]["chunk_count"],
          f"{len(lines)} lines vs {manifest['collection']['chunk_count']}")
    EIGHT = {"content","source_file","source_type","chunk_index",
             "chunk_strategy","chunk_size","chunk_overlap","created_at"}
    probs = []
    for i, line in enumerate(lines):
        rec = json.loads(line)
        if set(rec) != {"id","vector","properties","source_sha256"}:
            probs.append(f"line {i+1}: keys {sorted(rec)}")
        elif set(rec["properties"]) != EIGHT:
            probs.append(f"line {i+1}: properties {sorted(rec['properties'])}")
        elif len(rec["vector"]) != manifest["embedding"]["dimensions"]:
            probs.append(f"line {i+1}: vector len {len(rec['vector'])}")
        elif not all(isinstance(v,(int,float)) for v in rec["vector"]):
            probs.append(f"line {i+1}: vector not all numbers")
    check("§4.5 every chunk has the 4 fields, 8 properties and a full vector",
          not probs, "; ".join(probs[:3]))

    if fid == "with-sources":
        idx = json.loads((pkg / "sources" / "index.json").read_text())
        stored = {p.name for p in (pkg / "sources").iterdir() if p.name != "index.json"}
        check("every indexed source document is present",
              set(idx["documents"]) == stored,
              f"index={len(idx['documents'])} files={len(stored)}")
        bad = [d for d in stored if hashlib.sha256((pkg/"sources"/d).read_bytes()).hexdigest() != d]
        check("source files are content-addressed correctly", not bad, str(bad[:2]))

    # Generated files
    readme = (pkg / "README.md").read_text()
    check("README has no unsubstituted placeholders",
          not re.search(r"@@[A-Z_0-9]+@@", readme),
          str(set(re.findall(r"@@[A-Z_0-9]+@@", readme))))
    check("E19 README states the collection name", name in readme)
    check("E19 README states the fidelity", fid in readme)
    check("E19 README carries the encryption warning", "not encrypted" in readme.lower())

    if manifest.get("retrieve_script"):
        rp = pkg / "retrieve.py"
        check("retrieve.py present when retrieve_script is true", rp.is_file())
        src = rp.read_text()
        check("retrieve.py has no unsubstituted placeholders",
              not re.search(r"@@[A-Z_0-9]+@@", src),
              str(set(re.findall(r"@@[A-Z_0-9]+@@", src))))
        try:
            ast.parse(src); ok = True; err = ""
        except SyntaxError as e:
            ok = False; err = str(e)
        check("retrieve.py is valid Python", ok, err)
        check("retrieve.py records its provenance (id8 + created_at)",
              id8 in src and manifest["created_at"] in src)
        imports = {n.split(".")[0] for node in ast.walk(ast.parse(src))
                   if isinstance(node, (ast.Import, ast.ImportFrom))
                   for n in ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module or ""])}
        STDLIB = {"argparse","json","sys","urllib","os","re"}
        check("retrieve.py imports stdlib only", imports <= STDLIB, str(sorted(imports)))
    else:
        check("retrieve.py absent when retrieve_script is false",
              not (pkg / "retrieve.py").exists())

print(f"\n{'PACKAGE VALID' if not fails else str(len(fails)) + ' CHECK(S) FAILED'}")
sys.exit(1 if fails else 0)
