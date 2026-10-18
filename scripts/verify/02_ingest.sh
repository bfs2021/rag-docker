#!/usr/bin/env bash
# SPECIFICATIONS.md §10.1 — Ingest
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
# Regenerate when the newest fixture is missing, so a directory left by an
# older run does not hide a check behind a missing file.
[ -f "$FIX/plain.json" ] || python3 ./fixtures.py "$FIX" >/dev/null

require_stack
# Run the independent text-storage window acceptance without model work.
bash ./08_overlap.sh
check "bounded overlap text-storage acceptance" $?
C="${PREFIX}Ingest"

# Uploads a set of files and echoes the finished job document to a file.
ingest() {
  local collection="$1" strategy="$2" size="$3" minsize="$4"; shift 4
  local args=() f
  for f in "$@"; do args+=(-F "files=@$f"); done
  local http_status curl_status
  http_status=$(curl -s -m 600 -o /tmp/vfy_job.json -w '%{http_code}' -X POST "$API/ingest/upload" \
    -F "collection=$collection" -F "strategy=$strategy" -F "chunk_size=$size" \
    -F "chunk_overlap=60" -F "min_chunk_size=$minsize" "${args[@]}")
  curl_status=$?
  local job; job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_job.json'))['job_id'])" 2>/dev/null)
  if [ "$curl_status" -ne 0 ] || [[ "$http_status" != 2?? ]] || [ -z "$job" ]; then
    printf 'Upload failed: curl exit=%s HTTP=%s; response body follows:\n' "$curl_status" "$http_status" >&2
    cat /tmp/vfy_job.json >&2
    printf '\n' >&2
    return 1
  fi
  wait_for_job "/ingest/job/$job" 900 >/dev/null
  api_get "/ingest/job/$job" > /tmp/vfy_job.json
}

section "§10.1 Ingest"

# ── all six supported types ──────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 200 40 \
  "$FIX/policies.txt" "$FIX/policies.md" "$FIX/policies.csv" \
  "$FIX/policies.json" "$FIX/policies.pdf" "$FIX/policies.docx"
read -r status completed chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['chunks_stored'])")"
[ "$status" = completed ] && [ "$completed" = 6 ] && [ "$chunks" -gt 0 ]
check "all six file types ingest" $? "status=$status completed=$completed chunks=$chunks"

# Confirm the chunks actually landed, not merely that the job reported success.
stored=$(api_get "/collections" | python3 -c "
import json,sys
print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
[ "$stored" -ge 6 ]
check "every type produced at least one chunk" $? "collection holds $stored chunks"

# ── ZIP batch, with unsupported members reported ─────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 200 40 "$FIX/batch.zip" "$FIX/notes.xyz"
read -r status completed skipped_n <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], len(d.get('skipped',[])))")"
[ "$status" = completed ] && [ "$completed" = 3 ]
check "ZIP extracts and processes supported members" $? "status=$status completed=$completed (want 3)"
[ "$skipped_n" -ge 3 ]
check "unsupported files are reported, not dropped silently" $? "$skipped_n skipped"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_job.json'))
sys.exit(0 if all('unsupported type' in s for s in d.get('skipped',[])) else 1)"
check "each skip names the unsupported extension" $?

# ── unreadable archives and gzipped JSON members (#290) ──────────────────────
# Controlled cases: truncated archives, wrong member offsets, bad CRCs (also in
# skipped members), unsupported compression, encryption and unwritable members;
# JSON Lines skip reasons; gzip corruption, size bound and unparseable JSON.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_ingest_errors.py)
check "upload unpacking errors and gzipped JSON regressions" $?

# A truncated ZIP, like an incomplete download, once ended in a bare HTTP 500.
code=$(curl -s -m 120 -o /tmp/vfy_trunc.json -w '%{http_code}' -X POST "$API/ingest/upload" \
  -F "collection=$C" -F "strategy=fixed" -F "files=@$FIX/truncated.zip")
read -r err_code names_file <<<"$(python3 -c "
import json
try: e=json.load(open('/tmp/vfy_trunc.json'))['error']
except Exception: print('not-json no'); raise SystemExit
print(e['code'], 'yes' if \"'truncated.zip' is damaged and can't be read\" in e['message'] else 'no')")"
[ "$code" = 400 ] && [ "$err_code" = ARCHIVE_UNREADABLE ] && [ "$names_file" = yes ]
check "a truncated ZIP is refused with a readable 400 naming it" $? \
  "HTTP=$code code=$err_code names-file=$names_file"

drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 200 40 "$FIX/gz_batch.zip"
read -r status completed gz_errors <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], len(d['errors']))")"
[ "$status" = completed ] && [ "$completed" = 2 ] && [ "$gz_errors" = 0 ]
check "a ZIP's .json and .json.gz members both ingest" $? \
  "status=$status completed=$completed errors=$gz_errors (want completed 2)"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_job.json'))
sys.exit(0 if sorted(d.get('skipped', [])) == [
    \"gz_batch.zip:notes.txt.gz (unsupported type '.gz')\",
    \"gz_batch.zip:records.jsonl.gz (JSON Lines (.jsonl) isn't supported yet, see #291)\"] else 1)"
check "JSON Lines is skipped with its own reason, other gzip as unsupported" $?

# ── ordinary JSON as readable text (#295) ────────────────────────────────────
# Controlled cases: every conversion rule, file key order, non-ASCII text,
# element-schema rehydration, empty files and invalid JSON.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_json_ingest.py)
check "ordinary JSON conversion regressions" $?

# The stored chunks once held pretty-printed JSON with escaped non-ASCII text.
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 1000 0 "$FIX/plain.json"
read -r status chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['chunks_stored'])")"
[ "$status" = completed ] && [ "$chunks" -gt 0 ]
check "an ordinary nested JSON file ingests" $? "status=$status chunks=$chunks"
(cd "$REPO_ROOT" && docker compose exec -T api python - "$C" <<'ENDPY'
import sys
from services import weaviate_client as wc
try:
    collection = wc.get_client().collections.get(sys.argv[1])
    text = "\n".join(o.properties["content"] for o in collection.iterator())
finally:
    wc.close_client()
wanted = ["title: Remote work policy", "meta.author: Zoë Müller", "tags: policy, remote"]
missing = [w for w in wanted if w not in text]
syntax = [c for c in ("{", '"', "\\u") if c in text]
if missing or syntax:
    print(f"missing={missing} json-syntax={syntax}", file=sys.stderr)
    sys.exit(1)
ENDPY
)
check "its chunks hold key: value text with non-ASCII kept" $?

# ── upload size limit at the proxy ───────────────────────────────────────────
# nginx refuses request bodies over `client_max_body_size` (default 1 MB) with
# a 413 before the API sees them, so every real-world PDF failed through the
# UI while the few-KB fixtures here all passed. These go through $API, which is
# the proxy, on purpose. See issue #21.
size=$(python3 -c "import os;print(os.path.getsize('$FIX/large.pdf'))")
[ "$size" -gt 1048576 ]
check "the large fixture is over nginx's 1 MB default" $? "$size bytes"

drop_collection "$C"; make_collection "$C"
if ingest "$C" fixed 300 50 "$FIX/large.pdf"; then
  read -r status completed chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['chunks_stored'])")"
else
  status="rejected"; completed=0; chunks=0
fi
[ "$status" = completed ] && [ "$completed" = 1 ] && [ "$chunks" -gt 0 ]
check "an upload over 1 MB is accepted through the proxy and ingests" $? \
  "status=$status completed=$completed chunks=$chunks"

# Just over the 512 MB limit. A sparse file, so nothing is written to disk, and
# nginx answers from the Content-Length header without reading the body.
big_dir=$(mktemp -d)
python3 -c "open('$big_dir/oversize.txt','wb').truncate(513*1024*1024)"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X POST "$API/ingest/upload" \
  -F "collection=$C" -F "strategy=fixed" -F "files=@$big_dir/oversize.txt")
rm -rf "$big_dir"
check_eq "an upload over the 512 MB limit is refused with 413" "$code" "413"

# ── every chunking strategy against a PDF ────────────────────────────────────
for strategy in fixed overlap language context_aware semantic; do
  if [ "$SKIP_SLOW" = "1" ] && [ "$strategy" = "semantic" ]; then
    skip "strategy '$strategy' (loads a sentence-transformer)"; continue
  fi
  SC="${C}$(printf '%s' "$strategy" | tr -d '_')"
  drop_collection "$SC"; make_collection "$SC"
  ingest "$SC" "$strategy" 300 50 "$FIX/policies.pdf"
  read -r status chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json')); print(d['status'], d['chunks_stored'])")"
  [ "$status" = completed ] && [ "$chunks" -gt 0 ]
  check "strategy '$strategy' produces chunks from a PDF" $? "status=$status chunks=$chunks"
  drop_collection "$SC"
done

# ── min_chunk_size merging ───────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 60 100 "$FIX/tiny.txt"
under=$(curl -s -m 60 -X POST "$API/query" -H 'Content-Type: application/json' \
  -d "{\"question\":\"short\",\"collection\":\"$C\",\"retrieval_mode\":\"flat\",\"top_k\":20,\"include_citations\":true,\"response_format\":\"end_user\"}" \
  2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('?'); raise SystemExit
print(sum(1 for c in (d.get('citations') or []) if len(c['excerpt'].strip()) < 60))" 2>/dev/null)
python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json')); import sys
sys.exit(0 if d['chunks_stored'] == 1 else 1)"
check "chunks below min_chunk_size are merged" $? "stored $(python3 -c "import json;print(json.load(open('/tmp/vfy_job.json'))['chunks_stored'])") chunk(s), wanted 1"

# ── one bad file must not fail the batch ─────────────────────────────────────
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 300 50 "$FIX/broken.pdf" "$FIX/policies.txt" "$FIX/tiny.txt"
read -r status completed failed errs <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['files_failed'], len(d['errors']))")"
[ "$status" = partial ] && [ "$completed" = 2 ] && [ "$failed" = 1 ] && [ "$errs" -ge 1 ]
check "a parser failure does not stop the other files" $? \
  "status=$status completed=$completed failed=$failed errors=$errs"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_job.json'))
sys.exit(0 if any('broken.pdf' in e for e in d['errors']) else 1)"
check "the failure names the offending file" $?

# ── job status transitions ───────────────────────────────────────────────────
# `queued` is only observable when the executor is saturated, so this asserts
# the terminal transition and that the POST reports the initial state.
drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=300" -F "min_chunk_size=50" -F "files=@$FIX/policies.txt" > /tmp/vfy_start.json
initial=$(python3 -c "import json;print(json.load(open('/tmp/vfy_start.json'))['status'])")
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_start.json'))['job_id'])")
check_eq "a new job starts as 'queued'" "$initial" "queued"
final=$(wait_for_job "/ingest/job/$job" 900)
check_eq "job reaches 'completed'" "$final" "completed"

# ── deleting a collection while an ingest job runs (#40) ─────────────────────
# A DELETE sent during a job used to wait for it and then remove what it stored,
# and one that ran before a queued job let that job's inserts recreate the
# collection (Weaviate auto-schema). Controlled cases: every job kind, both
# request orderings, and the job's report when its collection disappears.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_delete_busy.py)
check "delete-while-busy: job kinds, request orderings and the job's report" $?

DC="${PREFIX}DeleteBusy"
drop_collection "$DC"; make_collection "$DC"
object_count() {   # the collection's object count, or "absent"
  api_get "/collections" | python3 -c "
import json,sys
found=[c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$1']
print(found[0] if found else 'absent')"
}
# About 180 chunks at chunk_size 300: some tens of seconds of embedding, so the
# job is still running when the DELETE follows the upload.
busy_dir=$(mktemp -d)
python3 - "$busy_dir/slow.txt" <<'ENDPY'
import random, sys
random.seed(40)
words = "policy remote office travel expense claim review approval budget quarter report".split()
paragraphs = (" ".join(random.choice(words) for _ in range(60)) for _ in range(150))
open(sys.argv[1], "w").write("\n\n".join(paragraphs))
ENDPY
curl -s -m 600 -o /tmp/vfy_busy_start.json -X POST "$API/ingest/upload" -F "collection=$DC" \
  -F "strategy=fixed" -F "chunk_size=300" -F "min_chunk_size=50" -F "files=@$busy_dir/slow.txt"
rm -rf "$busy_dir"
busy_job=$(jfield "['job_id']" < /tmp/vfy_busy_start.json)
before=$(object_count "$DC")
code=$(curl -s -m 120 -o /tmp/vfy_busy_delete.json -w '%{http_code}' -X DELETE "$API/collections/$DC?confirm=true")
state=$(api_get "/ingest/job/$busy_job" | jfield "['status']")
after=$(object_count "$DC")
read -r err_code err_kind err_job <<<"$(python3 -c "
import json
try: e=json.load(open('/tmp/vfy_busy_delete.json'))['error']
except Exception: print('not-json - -'); raise SystemExit
print(e['code'], e['detail']['job_kind'], e['detail']['job_id'])")"
[ "$code" = 409 ] && [ "$err_code" = COLLECTION_BUSY ] && [ "$err_kind" = ingest ] \
  && [ "$err_job" = "$busy_job" ] && { [ "$state" = queued ] || [ "$state" = running ]; }
check "DELETE during a running ingest job is refused with 409 COLLECTION_BUSY naming it" $? \
  "HTTP=$code code=$err_code kind=$err_kind job=$err_job (job $busy_job was $state)"
[ "$after" != absent ] && [ "$before" != absent ] && [ "$after" -ge "$before" ]
check "the refused DELETE leaves the collection and its objects in place" $? \
  "objects before=$before after=$after"
final=$(wait_for_job "/ingest/job/$busy_job" 900)
check_eq "the ingest job still completes" "$final" "completed"
code=$(curl -s -m 120 -o /dev/null -w '%{http_code}' -X DELETE "$API/collections/$DC?confirm=true")
check_eq "DELETE after the job completes succeeds" "$code" "200"
sleep 10
check_eq "the deleted collection is still gone 10 s later" "$(object_count "$DC")" "absent"

# ── job records survive an API restart (#23) ─────────────────────────────────
# Controlled cases: unbound stores, throttled and immediate writes, failed
# writes, startup marking of interrupted jobs, corrupt files and retention.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_jobstore.py)
check "job store: persistence, throttling, startup marking and retention" $?

# With auto-schema off, an insert into a deleted collection fails and creates
# nothing, instead of recreating it with inferred properties and no vectorizer.
(cd "$REPO_ROOT" && docker compose exec -T api python - "${PREFIX}NoAutoSchema" <<'ENDPY'
import sys
from services import weaviate_client as wc
name = sys.argv[1]
try:
    client = wc.get_client()
    if client.collections.exists(name):
        client.collections.delete(name)
    wc._create_collection_sync(name, "hnsw", "cosine", {})
    wc._delete_collection_sync(name)
    try:
        wc._insert_chunks_sync(name, [{"content": "must not recreate", "source_file": "x.txt",
                                       "chunk_index": 0}])
    except Exception:
        pass
    else:
        print("the insert into a deleted collection succeeded", file=sys.stderr)
        sys.exit(1)
    if client.collections.exists(name):
        client.collections.delete(name)
        print("the insert recreated the deleted collection", file=sys.stderr)
        sys.exit(1)
finally:
    wc.close_client()
ENDPY
) >/tmp/vfy_autoschema.txt 2>&1
check "an insert into a deleted collection fails and does not recreate it" $? \
  "$(tail -1 /tmp/vfy_autoschema.txt)"

# ── job status survives an API restart (#23; opt-in: restarts the API) ───────
# Before #23 every job answered 404 after a restart. A job that finished must
# read back unchanged, and one interrupted after storing a file must read
# `partial` with the interruption message. `--timeout 0`: with the default 10 s
# stop timeout the worker could finish the job during shutdown.
JR="${PREFIX}JobRestart"
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then restart_refusal=$(restart_refusal_reason); fi
if [ "${RAG_ALLOW_RESTART:-0}" != "1" ]; then
  skip "ingest job status across an API restart" "set RAG_ALLOW_RESTART=1 to include it"
elif [ -n "$restart_refusal" ]; then
  check "ingest job status across an API restart" 1 "$restart_refusal"
else
  drop_collection "$JR"; make_collection "$JR"
  ingest "$JR" fixed 300 50 "$FIX/policies.txt"
  done_job=$(jfield "['job_id']" < /tmp/vfy_job.json)
  api_get "/ingest/job/$done_job" > /tmp/vfy_restart_done.json
  # policies.txt stores in seconds; the slow file then embeds for tens of seconds.
  slow_dir=$(mktemp -d)
  python3 - "$slow_dir/slow.txt" <<'ENDPY'
import random, sys
random.seed(23)
words = "policy remote office travel expense claim review approval budget quarter report".split()
paragraphs = (" ".join(random.choice(words) for _ in range(60)) for _ in range(150))
open(sys.argv[1], "w").write("\n\n".join(paragraphs))
ENDPY
  curl -s -m 600 -o /tmp/vfy_restart_start.json -X POST "$API/ingest/upload" -F "collection=$JR" \
    -F "strategy=fixed" -F "chunk_size=300" -F "min_chunk_size=50" \
    -F "files=@$FIX/policies.txt" -F "files=@$slow_dir/slow.txt"
  rm -rf "$slow_dir"
  slow_job=$(jfield "['job_id']" < /tmp/vfy_restart_start.json)
  state=""
  for _ in $(seq 1 600); do
    api_get "/ingest/job/$slow_job" > /tmp/vfy_restart_before.json
    state=$(python3 -c "
import json; d=json.load(open('/tmp/vfy_restart_before.json'))
print(d['status'] + ('-stored' if d['files_completed'] >= 1 else ''))" 2>/dev/null)
    case "$state" in running-stored|completed*|partial*|failed*) break ;; esac
    sleep 0.5
  done
  if [ "$state" != running-stored ]; then
    check "an ingest is running with a file stored before the restart" 1 "last status '$state'; nothing restarted"
  else
    sleep 2
    _rag_require_lock_owner
    (cd "$REPO_ROOT" && docker compose restart --timeout 0 api) >/dev/null 2>&1
    for _ in $(seq 1 90); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
    api_get "/ingest/job/$done_job" > /tmp/vfy_restart_done_after.json
    python3 -c "
import json,sys
sys.exit(0 if json.load(open('/tmp/vfy_restart_done.json')) == json.load(open('/tmp/vfy_restart_done_after.json')) else 1)"
    check "a completed ingest job reads back unchanged after a restart" $?
    code=$(curl -s -m 120 -o /tmp/vfy_restart_after.json -w '%{http_code}' "$API/ingest/job/$slow_job")
    python3 - "$code" <<'ENDPY'
import json, sys
before = json.load(open('/tmp/vfy_restart_before.json'))
after = json.load(open('/tmp/vfy_restart_after.json'))
ok = (sys.argv[1] == "200" and after.get("status") == "partial"
      and any(e.startswith("Interrupted: the API restarted while this job was running.")
              for e in after.get("errors", []))
      and after.get("files_completed") == before["files_completed"])
print(sys.argv[1], after.get("status"), after.get("files_completed"), after.get("errors"))
sys.exit(0 if ok else 1)
ENDPY
    check "an interrupted ingest reads back 200 'partial' with the interruption message" $? \
      "$(head -c 300 /tmp/vfy_restart_after.json)"
  fi
  drop_collection "$JR"
fi

drop_collection "$C"
cleanup_prefixed
summary
