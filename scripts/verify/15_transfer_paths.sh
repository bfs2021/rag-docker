#!/usr/bin/env bash
# Transfer and tuning paths with no check before (#387): import refusals, on_conflict,
# unknown job ids, /packages, the package file left alone, export without the model store,
# retrieve.py (E8-E10), equivalent retrieval after import (E11) and a re-chunk that produces
# no chunks. Run by 05_transfer.sh.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
C="${PREFIX}Paths"
SLUG=$(printf '%s' "$C" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]\{1,\}/-/g')
# The API's /app/exports on the host: the verify project's own folder (#152).
EXPORTS="${RAG_EXPORTS_DIR:-$REPO_ROOT/exports}"
QUESTION="who approves overtime?"

section "Transfer and tuning paths (#387)"

(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_package_refusals.py)
check "package refusal regressions (checks 1 and 2, collection name, chunk records)" $?
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_transfer_paths.py)
check "transfer and tuning route regressions" $?
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_retrieve_script.py)
check "retrieve.py flag and exit-code regressions (E8-E10)" $?
python3 "$REPO_ROOT/scripts/tests/test_distribution.py"
check "package.sh, package-offline.sh and install-offline.sh regressions" $?

count_of() { api_get "/collections" | python3 -c "
import json,sys; print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$1'][0])"; }
collection_names() { api_get "/collections" | python3 -c "
import json,sys; print(' '.join(sorted(c['name'] for c in json.load(sys.stdin)['collections'])))"; }
run_import() {   # run_import <file> <on_conflict>; leaves the job in /tmp/vfy_p_job.json
  api_post "/import" "{\"filename\":\"$1\",\"on_conflict\":\"$2\"}" > /tmp/vfy_p_imp.json
  local j; j=$(jfield "['job_id']" < /tmp/vfy_p_imp.json)
  # No job (a refused request): record that instead of polling a job that isn't there.
  if [ -z "$j" ] || [ "$j" = None ]; then cp /tmp/vfy_p_imp.json /tmp/vfy_p_job.json; return; fi
  wait_for_job "/import/job/$j" 900 >/dev/null
  api_get "/import/job/$j" > /tmp/vfy_p_job.json
}

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_p_ing.json
check_eq "the suite's collection is ingested" \
  "$(wait_for_job "/ingest/job/$(jfield "['job_id']" < /tmp/vfy_p_ing.json)" 900)" "completed"
chunks=$(count_of "$C")
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":150,\"min_chunk_size\":40}" >/dev/null
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":6,\"alpha\":0.5,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_p_exp.json
check_eq "... and exported" \
  "$(wait_for_job "/export/job/$(jfield "['job_id']" < /tmp/vfy_p_exp.json)" 1800)" "completed"
PKG=$(api_get "/export/job/$(jfield "['job_id']" < /tmp/vfy_p_exp.json)" | jfield "['filename']")

# -- unknown job ids (TR:A10, TR:C5, TU:34) -----------------------------------
for kind in export import tune; do
  code=$(api_code "$API/$kind/job/vfy-no-such-job")
  ecode=$(api_get "/$kind/job/vfy-no-such-job" | jfield "['error']['code']")
  [ "$code" = 404 ] && [ "$ecode" = JOB_NOT_FOUND ]
  check "an unknown $kind job id is 404 JOB_NOT_FOUND" $? "HTTP $code, code '$ecode'"
done

# -- on_conflict is required and checked (TR:C2) ------------------------------
for body in "{\"filename\":\"$PKG\"}" "{\"filename\":\"$PKG\",\"on_conflict\":null}" \
            "{\"filename\":\"$PKG\",\"on_conflict\":\"merge\"}"; do
  code=$(api_post_code "/import" "$body")
  ecode=$(api_post "/import" "$body" | jfield "['error']['code']")
  [ "$code" = 422 ] && [ "$ecode" = INVALID_PARAMETER ]
  check "POST /import $body is 422 INVALID_PARAMETER" $? "HTTP $code, code '$ecode'"
done

# -- GET /packages, readable and not (TR:D1) ----------------------------------
JUNK="ragpkg-$SLUG-junk.tar.gz"
printf 'not a package\n' > "$EXPORTS/.$JUNK.part" && mv "$EXPORTS/.$JUNK.part" "$EXPORTS/$JUNK"
api_get "/packages" > /tmp/vfy_p_list.json
python3 - "$EXPORTS" "$PKG" "$JUNK" "$C" "$chunks" <<'ENDPY'
import json, os, sys
exports, pkg, junk, coll, chunks = sys.argv[1:6]
listed = {p["filename"]: p for p in json.load(open("/tmp/vfy_p_list.json"))["packages"]}
want = {pkg: {"filename": pkg, "size_bytes": os.path.getsize(os.path.join(exports, pkg)),
              "collection": coll, "chunk_count": int(chunks), "fidelity": "with-sources",
              "readable": True},
        junk: {"filename": junk, "size_bytes": os.path.getsize(os.path.join(exports, junk)),
               "collection": None, "chunk_count": None, "fidelity": None, "created_at": None,
               "readable": False}}
bad = [name for name, fields in want.items()
       if name not in listed or any(listed[name].get(k) != v for k, v in fields.items())]
bad += [] if (listed.get(pkg) or {}).get("created_at") else [pkg + " has no created_at"]
print("    listed:", json.dumps({k: listed.get(k) for k in want}))
sys.exit(1 if bad else 0)
ENDPY
check "GET /packages lists the package readable, with its fields, and junk as readable: false" $?
rm -f "$EXPORTS/$JUNK"

# -- import refusals (TR:C7-C10, C16, C34) ------------------------------------
# Each crafted copy of the package is refused with its code, and no collection appears.
names_before=$(collection_names)
run_import "ragpkg-$SLUG-absent.tar.gz" rename
check_eq "a package that does not exist is refused as PACKAGE_UNREADABLE" \
  "$(jfield "['error_code']" < /tmp/vfy_p_job.json)" "PACKAGE_UNREADABLE"
for spec in nongzip:PACKAGE_UNREADABLE truncated:PACKAGE_UNREADABLE symlink:PACKAGE_UNREADABLE \
            hardlink:PACKAGE_UNREADABLE dotdot:PACKAGE_UNREADABLE twodirs:PACKAGE_UNREADABLE \
            nomanifest:PACKAGE_FORMAT_UNSUPPORTED badmanifest:PACKAGE_FORMAT_UNSUPPORTED \
            newformat:PACKAGE_FORMAT_UNSUPPORTED badname:PACKAGE_FORMAT_UNSUPPORTED \
            badline:PACKAGE_CORRUPT baduuid:PACKAGE_CORRUPT badprops:PACKAGE_CORRUPT \
            novector:PACKAGE_CORRUPT wrongwidth:PACKAGE_CORRUPT countup:PACKAGE_CORRUPT \
            countdown:PACKAGE_CORRUPT; do
  kind=${spec%%:*}; want=${spec#*:}
  P=$(python3 ./transfer_packages.py "$EXPORTS/$PKG" "$kind")
  run_import "$P" rename
  got=$(jfield "['error_code']" < /tmp/vfy_p_job.json)
  names_after=$(collection_names)
  [ "$got" = "$want" ] && [ "$names_after" = "$names_before" ]
  check "a $kind package is refused as $want and creates no collection" $? \
    "code '$got': $(jfield "['error']" < /tmp/vfy_p_job.json | cut -c1-120)"
  rm -f "$EXPORTS/$P"
done
P=$(python3 ./transfer_packages.py "$EXPORTS/$PKG" badline -replace)
run_import "$P" replace
check_eq "replace with a malformed chunk record is refused as PACKAGE_CORRUPT" \
  "$(jfield "['error_code']" < /tmp/vfy_p_job.json)" "PACKAGE_CORRUPT"
check_eq "... and leaves the existing collection's chunks alone" "$(count_of "$C")" "$chunks"
rm -f "$EXPORTS/$P"

# -- the package file is left unchanged (TR:C28) ------------------------------
package_state() { python3 - "$EXPORTS/$PKG" <<'ENDPY'
import hashlib, os, sys
st = os.stat(sys.argv[1])
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest(), st.st_size, st.st_mtime_ns)
ENDPY
}
state_before=$(package_state)
run_import "$PKG" rename
read -r istat IMP <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_p_job.json')); print(d['status'], d.get('collection') or '-')")"
check_eq "the package imports under a new name" "$istat" "completed"
check_eq "the import leaves the package's bytes, size and mtime unchanged" "$(package_state)" "$state_before"

# -- E11: the import retrieves what the original does -------------------------
# /query rewrites each question with the LLM first, so two answers can differ even for
# identical collections; retrieval is compared directly, for each path /query uses.
(cd "$REPO_ROOT" && docker compose exec -T api python - "$C" "$IMP" "$QUESTION") <<'ENDPY'
import asyncio, sys
from services import ollama_client
from services import weaviate_client as wc
original, imported, question = sys.argv[1:4]

async def retrieve(name, vector):
    return {"vector": await wc.near_vector_query(name, vector, 6),
            "hybrid": await wc.hybrid_query(name, question, 0.5, 6),
            "near-text": await wc.near_text_query(name, question, 6)}

async def main():
    vector = await ollama_client.embed(question)
    return await retrieve(original, vector), await retrieve(imported, vector)

try:
    a, b = asyncio.run(main())
finally:
    wc.close_client()
ok = True
for path in a:
    key = lambda rows: sorted((r["source_file"], r["chunk_index"], r["score"]) for r in rows)
    x, y = key(a[path]), key(b[path])
    same = len(x) == len(y) > 0 and all(
        p[:2] == q[:2] and abs(p[2] - q[2]) < 1e-4 for p, q in zip(x, y))
    print(f"    {path}: {len(x)} and {len(y)} chunk(s), {'the same' if same else 'DIFFERENT'}")
    ok = ok and same
sys.exit(0 if ok else 1)
ENDPY
check "E11: the imported collection retrieves the same chunks and scores as the original" $?

# -- export without the model store (TR:A19) ----------------------------------
(cd "$REPO_ROOT" && docker compose exec -T api python - "$C") > /tmp/vfy_p_models.json 2>/tmp/vfy_p_models.err <<'ENDPY'
import json, sys, tarfile
from unittest.mock import patch
from config import settings
from services import packager
from services import weaviate_client as wc
try:
    with patch.object(settings, "ollama_models_dir", "/nonexistent-ollama-store-387"):
        result = packager.build(sys.argv[1], include_models=True)
finally:
    wc.close_client()
with tarfile.open(packager.exports_dir() / result["filename"]) as tar:
    names = tar.getnames()
    manifest = json.load(tar.extractfile(next(n for n in names if n.endswith("/manifest.json"))))
print(json.dumps({"filename": result["filename"], "models_bundled": result["models_bundled"],
                  "warnings": result["warnings"], "manifest": [manifest["models_bundled"],
                                                               manifest["bundled_models"]],
                  "models": [n for n in names if "/models" in n]}))
ENDPY
python3 - <<'ENDPY'
import json, sys
try:
    d = json.load(open("/tmp/vfy_p_models.json"))
except ValueError:
    print("    " + open("/tmp/vfy_p_models.err").read()[-300:]); sys.exit(1)
ok = (d["models_bundled"] is False and d["manifest"] == [False, []] and d["models"] == []
      and "models were requested but the Ollama model store is not mounted; the package was "
          "built without them" in d["warnings"])
print("    " + json.dumps(d))
sys.exit(0 if ok else 1)
ENDPY
check "export with models but no model store warns, bundles nothing and says so in the manifest" $?
NOSTORE=$(jfield "['filename']" < /tmp/vfy_p_models.json)
[ -n "$NOSTORE" ] && [ "$NOSTORE" != "None" ] && rm -f "$EXPORTS/$NOSTORE"

# -- retrieve.py from the package (E8-E10) ------------------------------------
RTMP=$(mktemp -d)
python3 - "$EXPORTS/$PKG" "$RTMP" <<'ENDPY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1]) as tar:
    member = next(m for m in tar.getmembers() if m.isfile() and m.name.endswith("/retrieve.py"))
    pathlib.Path(sys.argv[2], "retrieve.py").write_bytes(tar.extractfile(member).read())
ENDPY
check "the package carries retrieve.py" $?
python3 -I -S "$RTMP/retrieve.py" --api-url "http://127.0.0.1:9/api" "$QUESTION" \
  > /tmp/vfy_p_r.out 2> /tmp/vfy_p_r.err
rc=$?
[ "$rc" = 3 ] && grep -q '^error: Cannot reach the API at http://127.0.0.1:9/api' /tmp/vfy_p_r.err \
  && ! grep -q Traceback /tmp/vfy_p_r.err
check "E10: retrieve.py exits 3 with one sentence when nothing answers" $? \
  "exit $rc: $(head -1 /tmp/vfy_p_r.err)"
if [ "$SKIP_SLOW" = "1" ]; then
  skip "E8/E9/E11: retrieve.py answers from the original and the import" "RAG_SKIP_SLOW=1"
else
  want_citations=$(( chunks < 6 ? chunks : 6 ))
  for run in "default:$want_citations" "top2:2"; do
    name=${run%%:*}; want=${run#*:}
    args=(--api-url "$API" --json --citations)
    [ "$name" = top2 ] && args+=(--top-k 2)
    python3 -I -S "$RTMP/retrieve.py" "${args[@]}" "$QUESTION" > /tmp/vfy_p_r.json 2> /tmp/vfy_p_r.err
    rc=$?
    python3 - "$want" <<'ENDPY'
import json, sys
try:
    d = json.load(open("/tmp/vfy_p_r.json"))
except ValueError:
    sys.exit(1)
sys.exit(0 if d["answer"].strip() and len(d["citations"] or []) == int(sys.argv[1]) else 1)
ENDPY
    answered=$?
    [ "$rc" = 0 ] && [ "$answered" = 0 ]
    check "E8/E9: retrieve.py ($name) answers with $want citation(s)" $? \
      "exit $rc: $(head -c 200 /tmp/vfy_p_r.err)"
  done
  python3 -I -S "$RTMP/retrieve.py" --api-url "$API" --collection "$IMP" "$QUESTION" \
    > /tmp/vfy_p_r.out 2> /tmp/vfy_p_r.err
  rc=$?
  [ "$rc" = 0 ] && [ -n "$(tr -d '[:space:]' < /tmp/vfy_p_r.out)" ]
  check "E11: retrieve.py answers from the imported collection" $? "exit $rc: $(head -c 200 /tmp/vfy_p_r.err)"
fi
rm -rf "$RTMP"

# -- a re-chunk that produces no chunks (TU:16) -------------------------------
# The retained original is replaced with blank text; its stored chunks stay.
(cd "$REPO_ROOT" && docker compose exec -T api python - "$C") <<'ENDPY'
import sys
from services import sources
c = sys.argv[1]
sources.store(c, "policies.txt", b"   \n\n   \n", replace=True)
docs = sources.load_index(c)["documents"]
sys.exit(0 if [e["filenames"] for e in docs.values()] == [["policies.txt"]] else 1)
ENDPY
check "the retained policies.txt is replaced with blank text" $?
api_post "/tune/rechunk" "{\"collection\":\"$C\",\"chunking_strategy\":\"fixed\",\"chunk_size\":80,\"min_chunk_size\":30}" > /tmp/vfy_p_tune.json
tjob=$(jfield "['job_id']" < /tmp/vfy_p_tune.json)
if [ -n "$tjob" ] && [ "$tjob" != None ]; then
  tstatus=$(wait_for_job "/tune/job/$tjob" 900)
  api_get "/tune/job/$tjob" > /tmp/vfy_p_tjob.json
else
  tstatus="not started: $(head -c 200 /tmp/vfy_p_tune.json)"; echo '{}' > /tmp/vfy_p_tjob.json
fi
[ "$tstatus" = failed ] && [ "$(jfield "['error_code']" < /tmp/vfy_p_tjob.json)" = SOURCES_REQUIRED ] \
  && jfield "['error']" < /tmp/vfy_p_tjob.json | grep -q "produced no chunks"
check "a re-chunk that produces no chunks fails SOURCES_REQUIRED" $? \
  "status '$tstatus': $(jfield "['error']" < /tmp/vfy_p_tjob.json)"
check_eq "... and leaves the collection's chunks alone" "$(count_of "$C")" "$chunks"

[ "$IMP" != "-" ] && [ "$IMP" != "$C" ] && drop_collection "$IMP"
drop_collection "$C"
rm -f "$EXPORTS/$PKG"
summary
