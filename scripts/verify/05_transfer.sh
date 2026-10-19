#!/usr/bin/env bash
# RAG_EXPORT_SPECIFICATIONS.md §13 — export, import and tuning (E5-E20, E23, E26-E32)
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
bash ./13_identity.sh
check "imported evaluation identity acceptance suite" $?
bash ./14_reindex.sh
check "exact-record reindex acceptance suite" $?
C="${PREFIX}Transfer"
# The API's /app/exports on the host: the verify project's own folder (#152).
EXPORTS="${RAG_EXPORTS_DIR:-$REPO_ROOT/exports}"

section "Export, import and tuning"

(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_session_import.py)
check "evaluation import and generated-session regressions" $?
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_source_index_boundary.py)
check "retained-source index boundary regressions" $?
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_retrieval_import.py)
check "retrieval import and generated-script trust-boundary regressions" $?
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_batch_recovery.py)
check "import and tuning recovery regressions" $?
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_batch_recovery_fixture.py)
check "test_batch_recovery_fixture.py registered regression checks" $?

python3 "$REPO_ROOT/scripts/tests/test_batch_implementation.py"
check "test_batch_implementation.py registered regression checks" $?
python3 "$REPO_ROOT/scripts/tests/test_session_implementation.py"
check "embedded session/import verification sources match" $?

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_t.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_t.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null
chunks_before=$(api_get "/collections" | python3 -c "
import json,sys; print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")

# Saved ingest settings (the upload's own values) so the package carries
# ingest_config.json and import restores it through settings_store (#189).
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":150,\"min_chunk_size\":40}" >/dev/null
# Retrieval settings must exist for the package to carry retrieve.py.
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null

# ── export ───────────────────────────────────────────────────────────────────
api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_exp.json
ejob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_exp.json'))['job_id'])")
estatus=$(wait_for_job "/export/job/$ejob" 1800)
check_eq "export completes" "$estatus" "completed"
api_get "/export/job/$ejob" > /tmp/vfy_expjob.json
read -r PKG fidelity script <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_expjob.json'))
print(d['filename'], d['fidelity'], d['retrieve_script'])")"
check_eq "a collection with retained sources exports with-sources" "$fidelity" "with-sources"
check_eq "a tuned collection ships retrieve.py" "$script" "True"

python3 ./validate_package.py "$EXPORTS/$PKG" > /tmp/vfy_val.txt 2>&1
check "package satisfies every §4 clause" $? "$(tail -2 /tmp/vfy_val.txt | head -1)"

# ── E30: vectors are attributed to the collection's stored model (#196) ──────
EMBED_MODEL=$(cd "$REPO_ROOT" && docker compose exec -T api python -c 'from config import settings; print(settings.embed_model)' | tr -d '\r')
python3 - "$EXPORTS/$PKG" "$EMBED_MODEL" > /tmp/vfy_e30_match.txt 2>&1 <<'ENDPY'
import json, sys, tarfile
pkg, model = sys.argv[1], sys.argv[2]
with tarfile.open(pkg) as t:
    def read(name):
        m = next(x for x in t.getmembers() if x.isfile() and x.name.rsplit("/", 1)[-1] == name)
        return json.load(t.extractfile(m))
    coll, man = read("collection.json"), read("manifest.json")
print(f"collection.json={coll.get('embedding_model')!r} manifest={man['embedding']['model']!r} configured={model!r}")
sys.exit(0 if model and coll.get("embedding_model") == model == man["embedding"]["model"] else 1)
ENDPY
check "E30: a matching collection records its stored model in collection.json and the manifest" $? "$(tail -1 /tmp/vfy_e30_match.txt)"

# Collections the API would never create: an equal-width vector from another
# model's name, a named-vector schema, and no vectorizer. Each export must fail
# with the re-embedding remedy and leave nothing in the exports folder.
E30_DRIFT="${PREFIX}E30Drift"; E30_NAMED="${PREFIX}E30Named"; E30_NONE="${PREFIX}E30None"
e30_collections() {   # create | drop
  (cd "$REPO_ROOT" && docker compose exec -T api python - "$1" "$C" "$E30_DRIFT" "$E30_NAMED" "$E30_NONE") <<'ENDPY'
import sys
from weaviate.classes.config import Configure, DataType, Property
from config import settings
from services import weaviate_client as wc
action, source, drift, named, none = sys.argv[1:6]
client = wc.get_client()
for n in (drift, named, none):
    if client.collections.exists(n):
        client.collections.delete(n)
if action == "drop":
    sys.exit(0)
endpoint = f"http://{settings.ollama_host}:{settings.ollama_port}"
props = [Property(name="text", data_type=DataType.TEXT)]
# Same width as the configured model's vectors: provenance can't come from dimensions.
width = len(next(client.collections.get(source).iterator(include_vector=True)).vector["default"])
client.collections.create(drift, properties=props, vectorizer_config=Configure.Vectorizer.text2vec_ollama(
    api_endpoint=endpoint, model=settings.embed_model + "-e30-other", vectorize_collection_name=False))
client.collections.get(drift).data.insert({"text": "e30 drift"}, vector=[0.01] * width)
vectors = getattr(Configure, "Vectors", None)
named_cfg = (vectors.text2vec_ollama(name="default", api_endpoint=endpoint, model=settings.embed_model)
             if vectors else Configure.NamedVectors.text2vec_ollama(name="default", api_endpoint=endpoint, model=settings.embed_model))
client.collections.create(named, properties=props, vector_config=[named_cfg])
client.collections.create(none, properties=props, vectorizer_config=Configure.Vectorizer.none())
print(f"created {drift} (width {width}), {named}, {none}")
ENDPY
}
e30_collections create > /tmp/vfy_e30_setup.txt 2>&1
check "E30: mismatch fixtures created on the verify project" $? "$(tail -1 /tmp/vfy_e30_setup.txt)"
for e30c in "$E30_DRIFT" "$E30_NAMED" "$E30_NONE"; do
  before=$(ls -A "$EXPORTS" | sort)
  api_post "/export" "{\"collection\":\"$e30c\",\"include_models\":true}" > /tmp/vfy_e30_exp.json
  e30job=$(jfield "['job_id']" < /tmp/vfy_e30_exp.json)
  e30status=$(wait_for_job "/export/job/$e30job" 600)
  e30err=$(api_get "/export/job/$e30job" | jfield "['error']")
  [ "$e30status" = "failed" ] && [[ "$e30err" == PackageError:*Re-embed* ]]
  check "E30: export of $e30c refuses with the re-embedding remedy" $? "status '$e30status', error '$e30err'"
  check_eq "E30: refusal exposes EMBEDDING_MISMATCH" "$(api_get "/export/job/$e30job" | jfield "['error_code']")" "EMBEDDING_MISMATCH"
  after=$(ls -A "$EXPORTS" | sort)
  [ "$before" = "$after" ]
  check "E30: ... and leaves nothing in the exports folder ($e30c)" $? "$(diff <(echo "$before") <(echo "$after") | tail -3)"
done
e30_collections drop >/dev/null 2>&1
check "E30: mismatch fixtures removed" $?

# ── corruption is detected ───────────────────────────────────────────────────
python3 - "$EXPORTS/$PKG" <<'ENDPY'
import pathlib, shutil, subprocess, sys, tarfile, tempfile
src = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work)
    root = next(p for p in work.iterdir() if p.is_dir())
    chunks = root / "chunks.jsonl"
    chunks.write_bytes(chunks.read_bytes()[: len(chunks.read_bytes()) // 2])
    out = src.parent / (src.name.replace(".tar.gz", "") + "-corrupt.tar.gz")
    # Written under a .part name, then renamed into place: on Docker Desktop
    # the API can read a freshly written bind-mounted file as empty (#184).
    part = out.with_name("." + out.name + ".part")
    try:
        with tarfile.open(part, "w:gz") as t:
            t.add(root, arcname=root.name)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
ENDPY
CORRUPT=$(python3 - "$EXPORTS/$PKG" <<'ENDPY'
import pathlib, sys
src = pathlib.Path(sys.argv[1])
print(src.name.replace(".tar.gz", "") + "-corrupt.tar.gz")
ENDPY
)
api_post "/import" "{\"filename\":\"$CORRUPT\",\"on_conflict\":\"abort\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
code=$(api_get "/import/job/$ijob" | jfield "['error_code']")
check_eq "a truncated package is refused as PACKAGE_CORRUPT" "$code" "PACKAGE_CORRUPT"
api_get "/import/job/$ijob" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if 'chunks.jsonl' in (d.get('error') or '') else 1)"
check "the corruption error names the offending file" $?
rm -f "$EXPORTS/$CORRUPT"

# Digest-valid malformed retrieval settings must fail before every conflict path.
python3 ./retrieval_settings.py "$API" "$C" "$EXPORTS/$PKG"
check "invalid retrieval imports preserve live collections and settings" $?
# Settings saved before PR #108 (#173): a legacy ef exports and imports as null
# with a warning or note; other invalid saved settings fail the export early.
python3 ./legacy_retrieval.py "$API" "$C" "$EXPORTS" "$REPO_ROOT"
check "E28: legacy ef is cleared on export and import; invalid saved settings fail early" $?

# ── evaluation metadata is validated before mutation (E23) ──────────────────
# Add one evaluation sidecar to a copy of the package and re-sign the manifest,
# so the archive is digest-valid and only the session metadata decides.
GS_SID="gs_$(python3 -c 'import uuid;print(uuid.uuid4().hex[:8])')"
make_gs_pkg() {   # make_gs_pkg <session-id> <suffix>; prints the new filename
  python3 - "$EXPORTS/$PKG" "$1" "$2" "$C" <<'ENDPY'
import hashlib, json, pathlib, sys, tarfile, tempfile
src, sid, suffix, coll = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
session = {"session_id": 123 if sid == "__invalid_type__" else sid, "collection": coll, "status": "completed",
           "pairs_total": 1, "pairs_attempted": 1, "pairs_completed": 1, "pairs_failed": 0,
           "pairs": [{"pair_id": "p_0123abcd", "question": "Q?", "answer": "A",
                      "ground_truth": "A", "contexts": ["C"], "source_file": "policies.txt",
                      "chunk_index": 0, "status": "approved"}]}
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    root = next(p for p in work.iterdir() if p.is_dir())
    (root / "goldstandard").mkdir(exist_ok=True)
    side = root / "goldstandard" / "session.json"
    side.write_text(json.dumps(session))
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"]["goldstandard/session.json"] = \
        "sha256:" + hashlib.sha256(side.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out = src.parent / (src.name.replace(".tar.gz", "") + f"-{suffix}.tar.gz")
    # Written under a .part name, then renamed into place: on Docker Desktop
    # the API can read a freshly written bind-mounted file as empty (#184).
    part = out.with_name("." + out.name + ".part")
    try:
        with tarfile.open(part, "w:gz") as t:
            t.add(root, arcname=root.name)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    print(out.name)
ENDPY
}
count_of() { api_get "/collections" | python3 -c "
import json,sys; print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$1'][0])"; }

BADGS=$(make_gs_pkg "__invalid_type__" badgs)
api_post "/import" "{\"filename\":\"$BADGS\",\"on_conflict\":\"replace\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
api_get "/import/job/$ijob" > /tmp/vfy_gsjob.json
check_eq "E23: malformed evaluation metadata is refused as PACKAGE_CORRUPT" \
  "$(jfield "['error_code']" < /tmp/vfy_gsjob.json)" "PACKAGE_CORRUPT"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_gsjob.json'))
sys.exit(0 if 'goldstandard/session.json' in json.dumps(d) else 1)"
check "E23: the refusal names the offending sidecar" $?
check_eq "E23: replace left the existing collection untouched" "$(count_of "$C")" "$chunks_before"
check_eq "E23: no session was restored from the refused package" \
  "$(jfield "['restored_sessions']" < /tmp/vfy_gsjob.json)" "[]"
rm -f "$EXPORTS/$BADGS"

# A refused package must change no live state at all. A replace from this
# package would restore the same chunks, so the count above can't tell; the
# retrieval setting and the earlier, valid sidecar can.
MIX_SID="gs_$(python3 -c 'import uuid;print(uuid.uuid4().hex[:8])')"
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":7,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
MIXGS=$(python3 - "$EXPORTS/$PKG" "$MIX_SID" "$C" <<'ENDPY'
import hashlib, json, pathlib, sys, tarfile, tempfile
src, sid, coll = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def session(i, **over):
    s = {"session_id": i, "collection": coll, "status": "completed",
         "pairs_total": 1, "pairs_attempted": 1, "pairs_completed": 1, "pairs_failed": 0,
         "pairs": [{"pair_id": "p_0123abcd", "question": "Q?", "answer": "A",
                    "ground_truth": "A", "contexts": ["C"], "source_file": "policies.txt",
                    "chunk_index": 0, "status": "approved"}]}
    s.update(over); return s
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    root = next(p for p in work.iterdir() if p.is_dir())
    gold = root / "goldstandard"; gold.mkdir(exist_ok=True)
    manifest = json.loads((root / "manifest.json").read_text())
    # a_valid sorts first: a restore that isn't preflighted writes it before
    # it reaches the bad one.
    for name, body in (("a_valid.json", session(sid)),
                       ("b_bad.json", session("gs_0000beef", pairs_total="many"))):
        (gold / name).write_text(json.dumps(body))
        manifest["files"][f"goldstandard/{name}"] = \
            "sha256:" + hashlib.sha256((gold / name).read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out = src.parent / (src.name.replace(".tar.gz", "") + "-mixgs.tar.gz")
    # Written under a .part name, then renamed into place: on Docker Desktop
    # the API can read a freshly written bind-mounted file as empty (#184).
    part = out.with_name("." + out.name + ".part")
    try:
        with tarfile.open(part, "w:gz") as t:
            t.add(root, arcname=root.name)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    print(out.name)
ENDPY
)
api_post "/import" "{\"filename\":\"$MIXGS\",\"on_conflict\":\"replace\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
api_get "/import/job/$ijob" > /tmp/vfy_gsjob.json
check_eq "E23: a schema-invalid sidecar after a valid one is refused as PACKAGE_CORRUPT" \
  "$(jfield "['error_code']" < /tmp/vfy_gsjob.json)" "PACKAGE_CORRUPT"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_gsjob.json'))
sys.exit(0 if 'goldstandard/b_bad.json' in json.dumps(d) else 1)"
check "E23: the refusal names the invalid sidecar, not the valid one" $?
code=$(api_code "$API/goldstandard/session/$MIX_SID")
check_eq "E23: the valid sidecar in a refused package is not restored" "$code" "404"
topk=$(api_get "/retrieval/config/$C" | jfield "['top_k']")
check_eq "E23: a refused replace leaves the live retrieval settings alone" "$topk" "7"
check_eq "E23: ... and the collection's chunk count" "$(count_of "$C")" "$chunks_before"
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
rm -f "$EXPORTS/$MIXGS"
(cd "$REPO_ROOT" && docker compose exec -T api rm -f \
  "/app/uploads/goldstandard_sessions/$MIX_SID.json" \
  "/app/uploads/goldstandard_sessions/gs_0000beef.json") >/dev/null 2>&1 || true

GOODGS=$(make_gs_pkg "$GS_SID" goodgs)
api_post "/import" "{\"filename\":\"$GOODGS\",\"on_conflict\":\"rename\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 1800 >/dev/null
api_get "/import/job/$ijob" > /tmp/vfy_gsjob.json
read -r gstat gname <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_gsjob.json')); print(d['status'], d['collection'])")"
check_eq "E23: a package with valid evaluation metadata still imports" "$gstat" "completed"
api_get "/goldstandard/session/$GS_SID" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if d['collection']=='$gname' and d['pairs'][0]['status']=='approved' else 1)"
check "E23: the valid session is restored against the imported collection" $? "session $GS_SID -> $gname"
rm -f "$EXPORTS/$GOODGS"
drop_collection "$gname"
# The restored session file outlives its collection by design (spec §8 rule 4).
(cd "$REPO_ROOT" && docker compose exec -T api \
  rm -f "/app/uploads/goldstandard_sessions/$GS_SID.json") >/dev/null 2>&1 || true

# ── retained-source identities never select outside files (E29, #138) ───────
# A crafted, digest-valid package names an outside sentinel file in the API
# container through sources/index.json. Import must refuse it before any live
# mutation, and nothing exported afterwards may carry the sentinel's bytes.
E29_TAG="$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')"
E29_SENT="/tmp/e29-sentinel-$E29_TAG"
E29_TEXT="E29-OUTSIDE-SENTINEL-$E29_TAG"
(cd "$REPO_ROOT" && docker compose exec -T api sh -c "printf '%s' '$E29_TEXT' > '$E29_SENT'")
check "E29: the outside sentinel exists in the API container" $?
make_src_pkg() {   # make_src_pkg <abs|trav|mismatch> <suffix>; prints the new filename
  python3 - "$EXPORTS/$PKG" "$1" "$2" "$E29_SENT" <<'ENDPY'
import hashlib, json, pathlib, sys, tarfile, tempfile
src, mode, suffix, sent = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
sha = lambda b: hashlib.sha256(b).hexdigest()
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    root = next(p for p in work.iterdir() if p.is_dir())
    manifest = json.loads((root / "manifest.json").read_text())
    idx_path = root / "sources" / "index.json"
    index = json.loads(idx_path.read_text())
    digest, entry = next(iter(index["documents"].items()))   # the valid in-directory source
    if mode == "abs":
        key = sent
    elif mode == "trav":
        key = "../../.." + sent        # /app/sources/<collection>/../../.. is /
    else:                              # a digest-shaped key whose blob doesn't match it
        key = "0" * 64
        blob = root / "sources" / key
        blob.write_bytes(b"bytes that do not hash to the key")
        manifest["files"]["sources/" + key] = "sha256:" + sha(blob.read_bytes())
    index["documents"][key] = dict(entry, filenames=["leak.txt"])
    idx_path.write_text(json.dumps(index))
    assert "sources/index.json" in manifest["files"]
    manifest["files"]["sources/index.json"] = "sha256:" + sha(idx_path.read_bytes())
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out = src.parent / (src.name.replace(".tar.gz", "") + f"-{suffix}.tar.gz")
    # Written under a .part name, then renamed into place: on Docker Desktop
    # the API can read a freshly written bind-mounted file as empty (#184).
    part = out.with_name("." + out.name + ".part")
    try:
        with tarfile.open(part, "w:gz") as t:
            t.add(root, arcname=root.name)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    print(out.name)
ENDPY
}
no_sentinel_in_exports() {   # exit 0 when no file or archive member in exports holds the sentinel
  python3 - "$EXPORTS" "$E29_TEXT" <<'ENDPY'
import pathlib, sys, tarfile
root, needle = pathlib.Path(sys.argv[1]), sys.argv[2].encode()
hits = []
for p in root.rglob("*"):
    if not p.is_file():
        continue
    if needle in p.read_bytes():
        hits.append(str(p))
    if p.name.endswith(".tar.gz"):
        try:
            with tarfile.open(p) as t:
                for m in t.getmembers():
                    f = t.extractfile(m) if m.isfile() else None
                    if f is not None and needle in f.read():
                        hits.append(f"{p.name}:{m.name}")
        except tarfile.TarError:
            pass
print("\n".join(hits) or "no sentinel bytes in exports")
sys.exit(1 if hits else 0)
ENDPY
}
src_index_hash() {   # sha256 of the collection's retained index inside the API container
  (cd "$REPO_ROOT" && docker compose exec -T api python - "$1" <<'ENDPY'
import hashlib, sys
from services import sources
p = sources.collection_dir(sys.argv[1]) / "index.json"
print(hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "missing")
ENDPY
  )
}
collection_names() { api_get "/collections" | python3 -c "
import json,sys; print(' '.join(sorted(c['name'] for c in json.load(sys.stdin)['collections'])))"; }
run_import() {   # run_import <file> <on_conflict>; leaves the job in /tmp/vfy_e29job.json
  api_post "/import" "{\"filename\":\"$1\",\"on_conflict\":\"$2\"}" > /tmp/vfy_imp.json
  local j; j=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
  wait_for_job "/import/job/$j" 1800 >/dev/null
  api_get "/import/job/$j" > /tmp/vfy_e29job.json
}

# Import → export, rename: the PR's controlled regression in one chain on the
# live stack. If the import is (wrongly) accepted, export what it created and
# look for the sentinel there.
names_before=$(collection_names)
ABSPKG=$(make_src_pkg abs srcabs)
run_import "$ABSPKG" rename
check_eq "E29: an absolute source-index key is refused as PACKAGE_CORRUPT" \
  "$(jfield "['error_code']" < /tmp/vfy_e29job.json)" "PACKAGE_CORRUPT"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_e29job.json'))
sys.exit(0 if 'sources/index.json' in json.dumps(d) else 1)"
check "E29: the refusal names sources/index.json" $?
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_e29job.json'))
sys.exit(1 if '$E29_SENT' in json.dumps(d) or '$E29_TEXT' in json.dumps(d) else 0)"
check "E29: the refusal doesn't echo the outside path or its contents" $?
read -r e29stat e29name <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_e29job.json')); print(d['status'], d.get('collection') or '-')")"
if [ "$e29stat" = "completed" ] && [ "$e29name" != "-" ]; then
  api_post "/export" "{\"collection\":\"$e29name\",\"include_models\":false}" > /tmp/vfy_e29exp.json
  wait_for_job "/export/job/$(jfield "['job_id']" < /tmp/vfy_e29exp.json)" 1800 >/dev/null
fi
check_eq "E29: a refused rename import creates no collection" "$(collection_names)" "$names_before"
leak=$(no_sentinel_in_exports); check "E29: import → export never archives the outside sentinel" $? "$leak"
[ "$e29stat" = "completed" ] && [ "$e29name" != "-" ] && drop_collection "$e29name"

# Replace: refused before any live mutation, for every unsafe identity.
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":7,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
idx_before=$(src_index_hash "$C")
for mode in abs trav mismatch; do
  [ "$mode" = abs ] && P="$ABSPKG" || P=$(make_src_pkg "$mode" "src$mode")
  run_import "$P" replace
  check_eq "E29: replace with a $mode source identity is refused as PACKAGE_CORRUPT" \
    "$(jfield "['error_code']" < /tmp/vfy_e29job.json)" "PACKAGE_CORRUPT"
  check_eq "E29: ... leaves the chunk count alone ($mode)" "$(count_of "$C")" "$chunks_before"
  check_eq "E29: ... and the live retrieval settings ($mode)" \
    "$(api_get "/retrieval/config/$C" | jfield "['top_k']")" "7"
  check_eq "E29: ... and the retained source index ($mode)" "$(src_index_hash "$C")" "$idx_before"
  rm -f "$EXPORTS/$P"
done
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null

# The valid in-directory source still round-trips after the refusals.
api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_e29exp.json
e29job=$(jfield "['job_id']" < /tmp/vfy_e29exp.json)
check_eq "E29: the collection still exports after refused imports" \
  "$(wait_for_job "/export/job/$e29job" 1800)" "completed"
E29PKG=$(api_get "/export/job/$e29job" | jfield "['filename']")
python3 - "$EXPORTS/$E29PKG" <<'ENDPY'
import hashlib, json, re, sys, tarfile
with tarfile.open(sys.argv[1]) as t:
    names = {m.name.split("/", 1)[1]: m for m in t.getmembers() if "/" in m.name}
    index = json.load(t.extractfile(names["sources/index.json"]))
    docs = index["documents"]
    ok = bool(docs) and all(re.fullmatch(r"[0-9a-f]{64}", k) for k in docs)
    for k in docs:
        ok = ok and hashlib.sha256(t.extractfile(names[f"sources/{k}"]).read()).hexdigest() == k
sys.exit(0 if ok else 1)
ENDPY
check "E29: its package carries the valid source under its digest, and nothing else" $?
rm -f "$EXPORTS/$E29PKG"

# Read boundaries: an unsafe index already on disk (as a pre-fix import would
# have left it) must not let export or re-chunking read the sentinel.
plant() {   # plant <abs|link|restore>
  (cd "$REPO_ROOT" && docker compose exec -T api python - "$C" "$E29_SENT" "$1" <<'ENDPY'
import hashlib, json, os, sys
from services import sources
d = sources.collection_dir(sys.argv[1]); sent, mode = sys.argv[2], sys.argv[3]
idx, bak = d / "index.json", d / "index.json.e29bak"
if mode == "restore":
    keep = json.loads(bak.read_text())["documents"]
    for p in d.iterdir():
        if p.is_symlink():
            p.unlink()
    os.replace(bak, idx)
    sys.exit(0)
if not bak.exists():
    bak.write_bytes(idx.read_bytes())
index = json.loads(bak.read_text())
entry = next(iter(index["documents"].values()))
if mode == "abs":
    key = sent
else:
    key = hashlib.sha256(open(sent, "rb").read()).hexdigest()
    os.symlink(sent, d / key)
index["documents"][key] = dict(entry, filenames=["leak.txt"])
idx.write_text(json.dumps(index))
ENDPY
  )
}
for mode in abs link; do
  plant "$mode"
  check "E29: planted a $mode source entry on disk" $?
  api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_e29exp.json
  check_eq "E29: export refuses an on-disk $mode source entry" \
    "$(wait_for_job "/export/job/$(jfield "['job_id']" < /tmp/vfy_e29exp.json)" 1800)" "failed"
  leak=$(no_sentinel_in_exports); check "E29: ... and archives no outside bytes ($mode)" $? "$leak"
  api_post "/tune/rechunk" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80,\"min_chunk_size\":30}" > /tmp/vfy_tj.json
  check_eq "E29: re-chunking refuses an on-disk $mode source entry" \
    "$(wait_for_job "/tune/job/$(jfield "['job_id']" < /tmp/vfy_tj.json)" 1800)" "failed"
  check_eq "E29: ... and leaves the chunk count alone ($mode)" "$(count_of "$C")" "$chunks_before"
  if [ "$mode" = abs ]; then
    # An index that fails validation is a typed 409, not a 500, and doesn't
    # block tuning that never reads sources (reindex copies stored records).
    check_eq "E29: GET /tune reports an on-disk abs entry as HTTP 409" "$(api_code "$API/tune/$C")" "409"
    check_eq "E29: ... with error code SOURCE_INDEX_INVALID" \
      "$(api_get "/tune/$C" | jfield "['error']['code']")" "SOURCE_INDEX_INVALID"
    api_post "/tune/reindex" "{\"collection\":\"$C\"}" > /tmp/vfy_tj.json
    check_eq "E29: re-indexing from stored records still completes with an on-disk abs entry" \
      "$(wait_for_job "/tune/job/$(jfield "['job_id']" < /tmp/vfy_tj.json)" 1800)" "completed"
    check_eq "E29: ... and keeps the chunk count (abs)" "$(count_of "$C")" "$chunks_before"
    leak=$(no_sentinel_in_exports); check "E29: ... and nothing in exports holds outside bytes after it (abs)" $? "$leak"
  else
    echo "    (info) GET /tune/$C with the $mode entry planted: HTTP $(api_code "$API/tune/$C")"
  fi
  plant restore
  check "E29: restored the collection's own source index ($mode)" $?
done
check_eq "E29: the restored index is the original" "$(src_index_hash "$C")" "$idx_before"
(cd "$REPO_ROOT" && docker compose exec -T api rm -f "$E29_SENT") >/dev/null 2>&1 || true

# ── unindexed package blobs are never retained (E29, #179) ───────────────────
# A digest-valid package carries an extra sources/ file named after the SHA-256
# of a document not yet ingested, holding other bytes. Import must not retain
# it, so a later ingest of that document keeps the document's own bytes.
E29_FUTURE="$FIX/e29-future-$E29_TAG.txt"
printf 'E29 future document %s: the bytes a later ingest must retain.\n' "$E29_TAG" > "$E29_FUTURE"
E29_FDIG=$(python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$E29_FUTURE")
UNIDX=$(python3 - "$EXPORTS/$PKG" "$E29_FDIG" <<'ENDPY'
import hashlib, json, pathlib, sys, tarfile, tempfile
src, fdig = pathlib.Path(sys.argv[1]), sys.argv[2]
with tempfile.TemporaryDirectory() as td:
    work = pathlib.Path(td)
    with tarfile.open(src) as t:
        t.extractall(work, filter="data")
    root = next(p for p in work.iterdir() if p.is_dir())
    manifest = json.loads((root / "manifest.json").read_text())
    index = json.loads((root / "sources" / "index.json").read_text())
    assert index["documents"] and fdig not in index["documents"]
    planted = root / "sources" / fdig
    planted.write_bytes(b"E29 planted unindexed bytes")
    manifest["files"]["sources/" + fdig] = "sha256:" + hashlib.sha256(planted.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    out = src.parent / (src.name.replace(".tar.gz", "") + "-unindexed.tar.gz")
    part = out.with_name("." + out.name + ".part")
    try:
        with tarfile.open(part, "w:gz") as t:
            t.add(root, arcname=root.name)
        part.replace(out)
    finally:
        part.unlink(missing_ok=True)
    print(out.name)
ENDPY
)
run_import "$UNIDX" rename
read -r ustat uname <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_e29job.json')); print(d['status'], d.get('collection') or '-')")"
check_eq "E29: a digest-valid package with an unindexed source blob imports (rename)" "$ustat" "completed"
(cd "$REPO_ROOT" && docker compose exec -T api python - "$uname" "$E29_FDIG" <<'ENDPY'
import json, sys
from services import sources
d, fdig = sources.collection_dir(sys.argv[1]), sys.argv[2]
names = {p.name for p in d.iterdir()} if d.is_dir() else set()
docs = set(json.loads((d / "index.json").read_text())["documents"]) if (d / "index.json").is_file() else set()
print("    retained:", sorted(names))
sys.exit(0 if docs and names == docs | {"index.json"} and fdig not in names else 1)
ENDPY
)
check "E29: import retains only the indexed blobs and index.json, not the unindexed one" $?
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$uname" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$E29_FUTURE" > /tmp/vfy_e29ing.json
check_eq "E29: ingesting the document the planted name targets completes" \
  "$(wait_for_job "/ingest/job/$(jfield "['job_id']" < /tmp/vfy_e29ing.json)" 900)" "completed"
(cd "$REPO_ROOT" && docker compose exec -T api python - "$uname" "$E29_FDIG" <<'ENDPY'
import hashlib, sys
from services import sources
name, fdig = sys.argv[1], sys.argv[2]
p = sources.blob_path(name, fdig)
got = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "missing"
print("    retained blob hashes to", got)
sys.exit(0 if got == fdig and fdig in sources.load_index(name)["documents"] else 1)
ENDPY
)
check "E29: ... and its retained blob holds the document's own bytes" $?
[ "$uname" != "-" ] && [ "$uname" != "$C" ] && drop_collection "$uname"
rm -f "$EXPORTS/$UNIDX" "$E29_FUTURE"

# ── conflict handling ────────────────────────────────────────────────────────
api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"abort\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
wait_for_job "/import/job/$ijob" 900 >/dev/null
code=$(api_get "/import/job/$ijob" | jfield "['error_code']")
check_eq "abort refuses an existing collection" "$code" "COLLECTION_EXISTS"

api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"rename\"}" > /tmp/vfy_imp.json
ijob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_imp.json'))['job_id'])")
istatus=$(wait_for_job "/import/job/$ijob" 1800)
api_get "/import/job/$ijob" > /tmp/vfy_impjob.json
read -r istat iname irenamed iwritten <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_impjob.json'))
print(d['status'], d['collection'], d['renamed'], d['chunks_written'])")"
check_eq "rename imports alongside the original" "$istat" "completed"
[ "$irenamed" = "True" ] && [ "$iname" != "$C" ]
check "the renamed collection has a new name" $? "imported as $iname"
check_eq "every chunk is imported" "$iwritten" "$chunks_before"
api_get "/retrieval/config/$iname" | python3 -c '
import json,sys
config=json.load(sys.stdin)
expected={"retrieval_mode":"hybrid","top_k":6,"alpha":0.5,"ef":None,"response_format":"engineer"}
sys.exit(0 if all(config[k] == v for k,v in expected.items()) and not config["is_default"] else 1)'
check "renamed import preserves every saved retrieval setting" $?
api_get "/ingest/config/$iname" | python3 -c '
import json,sys
config=json.load(sys.stdin)
expected={"collection":sys.argv[1],"chunking_strategy":"fixed","chunk_size":150,"min_chunk_size":40}
sys.exit(0 if all(config[k] == v for k,v in expected.items()) and not config["is_default"] else 1)' "$iname"
check "renamed import restores the package's saved ingest settings (#189)" $?

# A successful destructive replace must be exercised as well as abort/rename.
api_post "/import" "{\"filename\":\"$PKG\",\"on_conflict\":\"replace\"}" > /tmp/vfy_replace.json
rjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_replace.json'))['job_id'])")
rstatus=$(wait_for_job "/import/job/$rjob" 1800)
check_eq "replace completes after verified final writes" "$rstatus" "completed"
check_eq "replace reports confirmed target objects" "$(api_get "/import/job/$rjob" | jfield "['chunks_written']")" "$chunks_before"
api_post "/export" "{\"collection\":\"$C\"}" > /tmp/vfy_replace_exp.json
rejob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_replace_exp.json'))['job_id'])")
wait_for_job "/export/job/$rejob" 1800 >/dev/null
RPKG=$(api_get "/export/job/$rejob" | jfield "['filename']")
[ "$RPKG" != "$PKG" ]
check "replace fidelity compares an independent re-export" $?

# ── import is lossless ───────────────────────────────────────────────────────
api_post "/export" "{\"collection\":\"$iname\"}" > /tmp/vfy_exp2.json
ejob2=$(python3 -c "import json;print(json.load(open('/tmp/vfy_exp2.json'))['job_id'])")
wait_for_job "/export/job/$ejob2" 1800 >/dev/null
PKG2=$(api_get "/export/job/$ejob2" | jfield "['filename']")
python3 - "$EXPORTS/$PKG" "$EXPORTS/$PKG2" "$EXPORTS/$RPKG" <<'ENDPY'
import json, sys, tarfile, tempfile, pathlib
def chunks(path):
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(path) as t:
            t.extractall(td)
        root = next(p for p in pathlib.Path(td).iterdir() if p.is_dir())
        return {r["id"]: r for r in
                (json.loads(l) for l in (root / "chunks.jsonl").read_text().splitlines() if l.strip())}
a = chunks(sys.argv[1])
comparisons = [chunks(path) for path in sys.argv[2:]]
same = all(set(a) == set(b) and all(a[k]["vector"] == b[k]["vector"]
                                and a[k]["properties"] == b[k]["properties"] for k in a)
           for b in comparisons)
sys.exit(0 if same else 1)
ENDPY
check "rename and replace preserve exported uuids, vectors and properties" $?
rm -f "$EXPORTS/$PKG2" "$EXPORTS/$RPKG"
drop_collection "$iname"

# ── models on import: damaged and namespaced (E26, E27) ──────────────────────
# Runs the importer inside the API process with its settings patched. E26 copies
# the embedding model into a temporary store and damages the copy; the live
# model store is only read. E27 uses the live store and a namespaced LLM name.
(cd "$REPO_ROOT" && docker compose exec -T api python - "$PKG") > /tmp/vfy_models.json 2>/tmp/vfy_models.err <<'ENDPY'
import json, shutil, sys, tempfile, uuid
from pathlib import Path
from unittest.mock import patch
from config import settings
from services import importer, model_bundle as models
pkg, out = sys.argv[1], {}

def run(conflict):
    jid = "vfy" + uuid.uuid4().hex[:6]
    importer._jobs[jid] = {"job_id": jid, "status": "queued", "filename": pkg,
                           "on_conflict": conflict, "collection": None,
                           "original_collection": None, "chunks_written": 0,
                           "fidelity": None, "renamed": False, "notes": [],
                           "error": None, "error_code": None, "error_detail": None}
    importer._active.add(pkg)
    importer._run(jid, pkg, conflict)
    return importer._jobs.pop(jid)

model = settings.embed_model
name = models.split_ref(model)[0]
out["live_intact"] = models.is_installed(model)
live_manifest = models.manifest_path(model)
digests = models._digests(json.loads(live_manifest.read_text()))
live_blobs = {d: models.blob_path(d) for d in digests}
live_stamps = {p: p.stat().st_mtime_ns for p in [live_manifest, *live_blobs.values()]}

# E26: installed but damaged -> MODEL_INTEGRITY_FAILED, the copy left as it was
with tempfile.TemporaryDirectory(prefix="vfy-model-", dir=settings.upload_dir) as td:
    with patch.object(settings, "ollama_models_dir", str(Path(td) / "store")):
        mp = models.manifest_path(model)
        mp.parent.mkdir(parents=True)
        shutil.copyfile(live_manifest, mp)
        for d, src in live_blobs.items():
            models.blob_path(d).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, models.blob_path(d))
        out["copy_present"] = models.is_installed(model)
        smallest = min(digests, key=lambda d: live_blobs[d].stat().st_size)
        damaged = models.blob_path(smallest)
        damaged.write_bytes(b"ordinary damaged review bytes")
        manifest_before = mp.read_bytes()
        job = run("rename")
        out["e26_status"] = job["status"]
        out["e26_code"] = job["error_code"]
        out["e26_message"] = job["error"] or ""
        out["e26_untouched"] = (damaged.read_bytes() == b"ordinary damaged review bytes"
                                and mp.read_bytes() == manifest_before)

# E27: a namespaced LLM that Ollama doesn't report -> import completes with a note
ns = "vfy-namespace/absent-llm"
reports = getattr(importer, "_ollama_reports", None)
if reports:                                              # real /api/tags; tag defaults to latest
    out["tags_have_embed"], out["ns_reported"] = reports(name), reports(ns)
with patch.object(settings, "llm_model", ns):
    job = run("rename")
out["e27_status"] = job["status"]
out["e27_error"] = f"{job['error_code']}: {job['error']}"
out["e27_collection"] = job["collection"]
out["e27_note"] = any(ns in n for n in job["notes"])
out["live_untouched"] = all(p.stat().st_mtime_ns == s for p, s in live_stamps.items())
print(json.dumps(out))
ENDPY
m() { python3 -c "import json,sys;print(json.load(open('/tmp/vfy_models.json'))[sys.argv[1]])" "$1" 2>/dev/null; }
check_eq "the live embedding model's files match their checksums" "$(m live_intact)" "True"
check_eq "the temporary copy of the embedding model starts intact" "$(m copy_present)" "True"
check_eq "E26 a damaged installed embedding model fails the import" "$(m e26_status)" "failed"
check_eq "E26 ... as MODEL_INTEGRITY_FAILED, not EMBEDDING_MODEL_MISSING" "$(m e26_code)" "MODEL_INTEGRITY_FAILED"
m e26_message | grep -qi "re-pull"
check "E26 the message says to restore or re-pull the model" $? "$(m e26_message)"
check_eq "E26 the damaged model's files are left untouched" "$(m e26_untouched)" "True"
check_eq "Ollama's /api/tags reports the embedding model by name (tag defaults to latest)" "$(m tags_have_embed)" "True"
check_eq "a namespaced model Ollama doesn't have is not reported" "$(m ns_reported)" "False"
check_eq "E27 with a namespaced LLM_MODEL, an import without bundled models completes" "$(m e27_status)" "completed"
[ "$(m e27_status)" = completed ] || printf '      %s\n' "$(m e27_error)"
check_eq "E27 the import notes the namespaced model" "$(m e27_note)" "True"
check_eq "the live model store was only read" "$(m live_untouched)" "True"
[ -s /tmp/vfy_models.json ] || sed 's/^/      /' /tmp/vfy_models.err | tail -5
e27c=$(m e27_collection); [ -n "$e27c" ] && [ "$e27c" != "None" ] && [ "$e27c" != "$C" ] && drop_collection "$e27c"

# ── tuning ───────────────────────────────────────────────────────────────────
api_get "/tune/$C" > /tmp/vfy_tune.json
check_eq "tune options report with-sources" "$(jfield "['fidelity']" < /tmp/vfy_tune.json)" "with-sources"
check_eq "re-chunking is offered" "$(jfield "['can_rechunk']" < /tmp/vfy_tune.json)" "True"

api_post "/tune/reindex" "{\"collection\":\"$C\",\"index_type\":\"flat\",\"distance_metric\":\"cosine\"}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
tstatus=$(wait_for_job "/tune/job/$tjob" 1800)
check_eq "re-index completes" "$tstatus" "completed"
api_get "/tune/job/$tjob" | python3 -c "
import json,sys; d=json.load(sys.stdin)
sys.exit(0 if any('unchanged' in n for n in d['notes']) else 1)"
check "re-index leaves gold-standard sessions alone" $?
after_index=$(api_get "/collections" | python3 -c "
import json,sys; print([c['index_type'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
check_eq "the index type actually changed" "$after_index" "flat"

api_post "/tune/rechunk" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80,\"min_chunk_size\":30}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
tstatus=$(wait_for_job "/tune/job/$tjob" 1800)
check_eq "re-chunk completes" "$tstatus" "completed"
after_chunks=$(api_get "/tune/job/$tjob" | jfield "['chunks_written']")
[ "$after_chunks" -gt "$chunks_before" ]
check "re-chunking with a smaller size yields more chunks" $? "$chunks_before -> $after_chunks"

# ── re-chunking after a replacement (#22, E17, E32) ──────────────────────────
# Re-ingesting replaces a name's chunks and retained original, so a rebuild
# brings back only the new version; a second name for the same content is
# rebuilt under both names (one chunk set per digest and name).
RC="${PREFIX}Rechunk"
RCFIX=$(mktemp -d)
printf '%s\n\n%s\n' \
  "Parking Permits. Facilities issue parking permits each January, and a permit is valid for one named vehicle only." \
  "Visitor Badges. Reception prints a visitor badge on arrival, and badges are returned at the end of the day." \
  > "$RCFIX/policies.txt"
cp "$RCFIX/policies.txt" "$RCFIX/copy.txt"
drop_collection "$RC"; make_collection "$RC"
for f in "$FIX/policies.txt" "$RCFIX/policies.txt" "$RCFIX/copy.txt"; do
  curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$RC" -F "strategy=fixed" \
    -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$f" > /tmp/vfy_rc.json
  rjob=$(jfield "['job_id']" < /tmp/vfy_rc.json)
  wait_for_job "/ingest/job/$rjob" 900 >/dev/null
done
# #311: the retained index can list a name the collection has no chunks for
# (a file that produced none, or a crafted package); rechunk must skip it.
(cd "$REPO_ROOT" && docker compose exec -T api python - "$RC") <<'ENDPY'
import sys
from services import sources
c = sys.argv[1]
docs = sources.load_index(c)["documents"]
digest = next(d for d, e in docs.items() if "copy.txt" in e["filenames"])
sources.store(c, "ghost.txt", sources.blob_path(c, digest).read_bytes(), replace=True)
sys.exit(0 if "ghost.txt" in sources.load_index(c)["documents"][digest]["filenames"] else 1)
ENDPY
check "E17: a name with no stored chunks is added to the retained index" $?
check_eq "E32: re-chunking is offered after a replacement and a second name" \
  "$(api_get "/tune/$RC" | jfield "['can_rechunk']")" "True"
api_post "/tune/rechunk" "{\"collection\":\"$RC\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80,\"min_chunk_size\":30}" > /tmp/vfy_tj.json
tjob=$(jfield "['job_id']" < /tmp/vfy_tj.json)
check_eq "E32: the re-chunk completes" "$(wait_for_job "/tune/job/$tjob" 1800)" "completed"
(cd "$REPO_ROOT" && docker compose exec -T api python - "$RC") > /tmp/vfy_rc_chunks.json <<'ENDPY'
import json, sys
from services import weaviate_client as wc
try:
    coll = wc.get_client().collections.get(sys.argv[1])
    print(json.dumps([[o.properties["source_file"], o.properties["content"]] for o in coll.iterator()]))
finally:
    wc.close_client()
ENDPY
python3 - <<'ENDPY'
import json, sys
chunks = json.load(open('/tmp/vfy_rc_chunks.json'))
texts = " ".join(c for _, c in chunks)
names = {n for n, _ in chunks}
print(sorted(names), "Travel Booking" in texts)
sys.exit(0 if names == {"policies.txt", "copy.txt"} and "Travel Booking" not in texts
         and "Parking Permits" in texts else 1)
ENDPY
check "E32: the rebuild holds only the new version, under both of its names" $?
rc_index=$( (cd "$REPO_ROOT" && docker compose exec -T api python - "$RC") <<'ENDPY'
import sys
from services import sources
print(sorted(n for e in sources.load_index(sys.argv[1])["documents"].values() for n in e["filenames"]))
ENDPY
)
python3 - "$rc_index" <<'ENDPY'
import json, sys
names = {n for n, _ in json.load(open('/tmp/vfy_rc_chunks.json'))}
sys.exit(0 if "ghost.txt" not in names and "'ghost.txt'" in sys.argv[1] else 1)
ENDPY
check "E17: a listed name with no stored chunks is not rebuilt, and the index still lists it" $? \
  "index: $rc_index"
drop_collection "$RC"
rm -rf "$RCFIX"

# ── chunks-only refuses re-chunking ──────────────────────────────────────────
SRCLESS="${PREFIX}Chunksonly"
drop_collection "$SRCLESS"; make_collection "$SRCLESS"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$SRCLESS" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_s.json
sjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_s.json'))['job_id'])")
wait_for_job "/ingest/job/$sjob" 900 >/dev/null
(cd "$REPO_ROOT" && docker compose exec -T api sh -c "rm -rf /app/sources/$SRCLESS") >/dev/null 2>&1
check_eq "a source-less collection reports chunks-only" \
  "$(api_get "/tune/$SRCLESS" | jfield "['fidelity']")" "chunks-only"
api_post "/tune/rechunk" "{\"collection\":\"$SRCLESS\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
wait_for_job "/tune/job/$tjob" 900 >/dev/null
check_eq "re-chunking a chunks-only collection is refused" \
  "$(api_get "/tune/job/$tjob" | jfield "['error_code']")" "SOURCES_REQUIRED"
# Use valid fixed settings so this tests source eligibility, not overlap validation.
api_post "/tune/reembed" "{\"collection\":\"$SRCLESS\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80}" > /tmp/vfy_tj.json
tjob=$(python3 -c "import json;print(json.load(open('/tmp/vfy_tj.json'))['job_id'])")
wait_for_job "/tune/job/$tjob" 900 >/dev/null
check_eq "re-embedding a chunks-only collection with new chunking is refused" \
  "$(api_get "/tune/job/$tjob" | jfield "['error_code']")" "SOURCES_REQUIRED"
drop_collection "$SRCLESS"

# ── #328 (reviewer): retained originals with no stored chunks ────────────────
# A file that produces no chunks is still retained at ingest. With nothing to
# rebuild, re-chunking isn't offered and is refused up front: no job is made.
NOCH="${PREFIX}Nochunks"
NOCHFIX=$(mktemp -d)
printf '   \n\n   \n' > "$NOCHFIX/blank.txt"
drop_collection "$NOCH"; make_collection "$NOCH"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$NOCH" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$NOCHFIX/blank.txt" > /tmp/vfy_nc.json
njob=$(jfield "['job_id']" < /tmp/vfy_nc.json)
[ -n "$njob" ] && wait_for_job "/ingest/job/$njob" 900 >/dev/null
nc_state=$( (cd "$REPO_ROOT" && docker compose exec -T api python - "$NOCH") <<'ENDPY'
import sys
from services import sources
from services import weaviate_client as wc
c = sys.argv[1]
try:
    stored = sum(1 for _ in wc.get_client().collections.get(c).iterator())
finally:
    wc.close_client()
names = sorted(n for e in sources.load_index(c)["documents"].values() for n in e["filenames"])
print(f"{stored} {names}")
ENDPY
)
check_eq "#328: a file that produced no chunks is retained, and the collection holds none" \
  "$nc_state" "0 ['blank.txt']"
api_get "/tune/$NOCH" > /tmp/vfy_nc_tune.json
check_eq "#328: GET /tune doesn't offer re-chunking with nothing to rebuild" \
  "$(jfield "['can_rechunk']" < /tmp/vfy_nc_tune.json)" "False"
check_eq "#328: GET /tune gives the nothing-to-rebuild reason" \
  "$(jfield "['note']" < /tmp/vfy_nc_tune.json | cut -c1-48)" "No retained original file has stored chunks, so "
for op in rechunk reembed; do
  code=$(curl -s -o /tmp/vfy_nc_start.json -m 120 -w '%{http_code}' -X POST "$API/tune/$op" \
    -H 'Content-Type: application/json' \
    -d "{\"collection\":\"$NOCH\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80}")
  check_eq "#328: POST /tune/$op with chunking answers 409" "$code" "409"
  check_eq "#328: POST /tune/$op with chunking is SOURCES_REQUIRED" \
    "$(jfield "['error']['code']" < /tmp/vfy_nc_start.json)" "SOURCES_REQUIRED"
  check_eq "#328: POST /tune/$op with chunking returns no job" \
    "$(jfield ".get('job_id')" < /tmp/vfy_nc_start.json)" "None"
done
drop_collection "$NOCH"
rm -rf "$NOCHFIX"

# ── the help page and the package README share a source ──────────────────────
api_get "/help/transfer" > /tmp/vfy_help.json
python3 - "$EXPORTS/$PKG" <<'ENDPY'
import json, pathlib, sys, tarfile, tempfile
help_md = json.load(open('/tmp/vfy_help.json'))['markdown']
partials = pathlib.Path('../../api/templates/partials')
with tempfile.TemporaryDirectory() as td:
    with tarfile.open(sys.argv[1]) as t:
        t.extractall(td)
    root = next(p for p in pathlib.Path(td).iterdir() if p.is_dir())
    readme = (root / "README.md").read_text()
missing = []
for part in sorted(partials.glob("*.md")):
    probe = max(part.read_text().split("\n"), key=len).strip()
    if "@@" in probe:
        probe = probe.split("@@")[0].strip()
    if not probe:
        continue
    if probe not in help_md:
        missing.append(f"{part.stem}: absent from help page")
    # retrieve_usage only appears in a README when that package ships a script
    elif probe not in readme and part.stem != "retrieve_usage":
        missing.append(f"{part.stem}: absent from package README")
sys.exit(0 if not missing else 1)
ENDPY
check "help page and package README render from the same partials" $?
python3 -c "
import json,re,sys
m=json.load(open('/tmp/vfy_help.json'))['markdown']
sys.exit(0 if not re.search(r'@@[A-Z_0-9]+@@', m) else 1)"
check "the help page has no unsubstituted placeholders" $?

# ── export job status survives an API restart (#23; opt-in: restarts the API) ──
# The export from the top of this suite must read back unchanged, and an export
# killed part-way must read `failed` with INTERRUPTED and its progress, not 404;
# so must an import killed part-way (#309). A synthetic
# 30,000-chunk collection (supplied vectors, no embedding) exports for about
# 40 s, so the restart lands early in it. `--timeout 0`: with the default 10 s
# stop timeout the worker could finish the export during shutdown.
XR="${PREFIX}ExportRestart"
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then restart_refusal=$(restart_refusal_reason); fi
if [ "${RAG_ALLOW_RESTART:-0}" != "1" ]; then
  skip "export job status across an API restart" "set RAG_ALLOW_RESTART=1 to include it"
elif [ -n "$restart_refusal" ]; then
  check "export job status across an API restart" 1 "$restart_refusal"
else
  api_get "/export/job/$ejob" > /tmp/vfy_xrestart_done.json
  drop_collection "$XR"
  (cd "$REPO_ROOT" && docker compose exec -T api python - "$XR" 30000 <<'ENDPY'
import random, sys
from datetime import datetime, timezone
from services import batch_write
from services import weaviate_client as wc
name, count = sys.argv[1], int(sys.argv[2])
try:
    wc._create_collection_sync(name, "hnsw", "cosine",
                               {"efConstruction": 128, "maxConnections": 64, "ef": 64})
    collection = wc.get_client().collections.get(name)
    rng, now = random.Random(23), datetime.now(timezone.utc).isoformat()
    for base in range(0, count, 2000):
        records = [{"properties": {"content": f"Synthetic chunk {i}. " + "lorem ipsum dolor " * 30,
                                   "source_file": f"synthetic_{i // 100}.txt", "source_type": "txt",
                                   "chunk_index": i % 100, "chunk_strategy": "fixed",
                                   "chunk_size": 1000, "chunk_overlap": 0, "created_at": now},
                    "vector": [rng.uniform(-1, 1) for _ in range(768)]}
                   for i in range(base, min(count, base + 2000))]
        batch_write.insert(collection, lambda r=records: iter(r), exact=False, cleanup_owned=True)
finally:
    wc.close_client()
ENDPY
  ) >/tmp/vfy_xrestart_prepare.log 2>&1
  check "a 30,000-chunk collection is built for the interrupted export" $? \
    "$(grep -E 'Error' /tmp/vfy_xrestart_prepare.log | tail -1)"
  builds_before=$(cd "$REPO_ROOT" && docker compose exec -T api sh -c 'ls -d /app/exports/.build-* 2>/dev/null' | sort)
  api_post "/export" "{\"collection\":\"$XR\",\"include_models\":false}" > /tmp/vfy_xrestart_start.json
  xjob=$(jfield "['job_id']" < /tmp/vfy_xrestart_start.json)
  state=""
  for _ in $(seq 1 600); do
    api_get "/export/job/$xjob" > /tmp/vfy_xrestart_before.json
    state=$(python3 -c "
import json; d=json.load(open('/tmp/vfy_xrestart_before.json'))
print(d['status'] + ('-writing' if d['chunks_written'] > 0 else ''))" 2>/dev/null)
    case "$state" in running-writing|completed*|failed*) break ;; esac
    sleep 0.5
  done
  if [ "$state" != running-writing ]; then
    check "an export is running with chunks written before the restart" 1 "last status '$state'; nothing restarted"
  else
    sleep 2
    _rag_require_lock_owner
    (cd "$REPO_ROOT" && docker compose restart --timeout 0 api) >/dev/null 2>&1
    for _ in $(seq 1 90); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
    api_get "/export/job/$ejob" > /tmp/vfy_xrestart_done_after.json
    python3 -c "
import json,sys
sys.exit(0 if json.load(open('/tmp/vfy_xrestart_done.json')) == json.load(open('/tmp/vfy_xrestart_done_after.json')) else 1)"
    check "a completed export job reads back unchanged after a restart" $?
    code=$(curl -s -m 120 -o /tmp/vfy_xrestart_after.json -w '%{http_code}' "$API/export/job/$xjob")
    python3 - "$code" <<'ENDPY'
import json, sys
after = json.load(open('/tmp/vfy_xrestart_after.json'))
ok = (sys.argv[1] == "200" and after.get("status") == "failed"
      and after.get("error_code") == "INTERRUPTED"
      and after.get("error") == "Interrupted: the API restarted while this job was running. "
                                "Start the export again."
      and after.get("chunks_written", 0) > 0)
sys.exit(0 if ok else 1)
ENDPY
    check "an interrupted export reads back 200 'failed' with INTERRUPTED and its progress" $? \
      "$(head -c 300 /tmp/vfy_xrestart_after.json)"
    # The killed build's leftovers are the container's (root, mode 0700): remove
    # only the build directories that appeared during this export.
    builds_after=$(cd "$REPO_ROOT" && docker compose exec -T api sh -c 'ls -d /app/exports/.build-* 2>/dev/null' | sort)
    stale=$(comm -13 <(printf '%s\n' "$builds_before") <(printf '%s\n' "$builds_after") | grep '^/app/exports/\.build-' || true)
    stem=$(printf '%s' "$XR" | tr '[:upper:]' '[:lower:]')
    (cd "$REPO_ROOT" && docker compose exec -T api sh -c 'rm -rf "$@"; rm -f /app/exports/.ragpkg-'"$stem"'-*.tar.gz.partial' sh $stale) >/dev/null 2>&1 || true
  fi

  # An import killed part-way reads `failed` with INTERRUPTED (#309): the same
  # collection, exported whole, dropped and imported back. The startup sweep
  # removes the half-built collection the killed import leaves.
  api_post "/export" "{\"collection\":\"$XR\",\"include_models\":false}" > /tmp/vfy_irestart_export.json
  xpkg_job=$(jfield "['job_id']" < /tmp/vfy_irestart_export.json)
  xpkg_status=$(wait_for_job "/export/job/$xpkg_job" 1800)
  xpkg=$(api_get "/export/job/$xpkg_job" | jfield "['filename']")
  if [ "$xpkg_status" != completed ] || [ -z "$xpkg" ] || [ "$xpkg" = None ]; then
    check "an import is running before the restart" 1 "the package export ended '$xpkg_status'; nothing restarted"
  else
    drop_collection "$XR"
    api_post "/import" "{\"filename\":\"$xpkg\",\"on_conflict\":\"abort\"}" > /tmp/vfy_irestart_start.json
    ijob_r=$(jfield "['job_id']" < /tmp/vfy_irestart_start.json)
    # Import reports chunks_written once, after its insert, so the restart
    # comes as soon as the job is running: unpacking, checks and the insert
    # of 30,000 chunks still lie ahead of it.
    state=""
    for _ in $(seq 1 600); do
      state=$(api_get "/import/job/$ijob_r" | jfield "['status']")
      case "$state" in running|completed|failed) break ;; esac
      sleep 0.2
    done
    if [ "$state" != running ]; then
      check "an import is running before the restart" 1 "last status '$state'; nothing restarted"
    else
      _rag_require_lock_owner
      (cd "$REPO_ROOT" && docker compose restart --timeout 0 api) >/dev/null 2>&1
      for _ in $(seq 1 90); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
      code=$(curl -s -m 120 -o /tmp/vfy_irestart_after.json -w '%{http_code}' "$API/import/job/$ijob_r")
      python3 - "$code" <<'ENDPY'
import json, sys
after = json.load(open('/tmp/vfy_irestart_after.json'))
ok = (sys.argv[1] == "200" and after.get("status") == "failed"
      and after.get("error_code") == "INTERRUPTED"
      and after.get("error") == "Interrupted: the API restarted while this job was running. "
                                "Start the import again.")
sys.exit(0 if ok else 1)
ENDPY
      check "an interrupted import reads back 200 'failed' with INTERRUPTED" $? \
        "$(head -c 300 /tmp/vfy_irestart_after.json)"
    fi
    rm -f "$EXPORTS/$xpkg"
  fi
  drop_collection "$XR"
fi

# ── verified recovery across an API restart (#43, #44; opt-in: restarts the API) ──
RP="${PREFIX}BatchRecovery"
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then restart_refusal=$(restart_refusal_reason); fi
if [ "${RAG_ALLOW_RESTART:-0}" != "1" ]; then
  skip "batch recovery across an API restart" "set RAG_ALLOW_RESTART=1 to include it"
elif [ -n "$restart_refusal" ]; then
  check "batch recovery across an API restart" 1 "$restart_refusal"
elif ! [[ "$RP" =~ ^Vfy[A-Za-z0-9_]+$ ]]; then
  # batch_recovery.py refuses any other prefix, as a guard on its destructive phases.
  skip "batch recovery across an API restart" "batch_recovery.py accepts only Vfy… prefixes; RAG_TEST_PREFIX='$PREFIX' gives '$RP'"
else
  (cd "$REPO_ROOT" && docker compose exec -T api python - prepare --prefix "$RP" \
    < scripts/verify/batch_recovery.py) > /tmp/vfy_recovery_prepare.log 2>&1
  check "batch faults fail truthfully and retain verified recovery" $? \
    "$(grep -E 'Error|AssertionError' /tmp/vfy_recovery_prepare.log | tail -1)"
  (cd "$REPO_ROOT" && docker compose restart api) >/dev/null 2>&1
  for _ in $(seq 1 90); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
  (cd "$REPO_ROOT" && docker compose exec -T api python - check --prefix "$RP" \
    < scripts/verify/batch_recovery.py) > /tmp/vfy_recovery_check.log 2>&1
  check "recovery survives restart; owned scratch swept, unowned names kept" $? \
    "$(grep -E 'Error|AssertionError' /tmp/vfy_recovery_check.log | tail -1)"
  (cd "$REPO_ROOT" && docker compose exec -T api python - cleanup --prefix "$RP" \
    < scripts/verify/batch_recovery.py) > /tmp/vfy_recovery_cleanup.log 2>&1
  check "recovery acceptance fixtures are removed" $?
fi

rm -f "$EXPORTS/$PKG"
drop_collection "$C"
cleanup_prefixed
bash ./10_validity.sh
check "retained-session validity acceptance suite" $?
summary
