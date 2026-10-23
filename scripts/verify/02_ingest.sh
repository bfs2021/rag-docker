#!/usr/bin/env bash
# SPECIFICATIONS.md §10.1 — Ingest
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
# Regenerate when the newest fixture is missing, so a directory left by an
# older run does not hide a check behind a missing file.
[ -f "$FIX/json_over_size_member.zip" ] || python3 ./fixtures.py "$FIX" >/dev/null

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
# which JSON Lines inputs are queued; gzip corruption, size bound and
# unparseable JSON.
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
[ "$status" = completed ] && [ "$completed" = 3 ] && [ "$gz_errors" = 0 ]
check "a ZIP's .json, .json.gz and .jsonl.gz members all ingest" $? \
  "status=$status completed=$completed errors=$gz_errors (want completed 3)"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_job.json'))
sys.exit(0 if d.get('skipped', []) == [\"gz_batch.zip:notes.txt.gz (unsupported type '.gz')\"] else 1)"
check "only the other gzip member is skipped, as unsupported" $?

# ── the expansion cap on an upload's ZIPs (#298) ────────────────────────────
# Controlled cases with a 1 MiB cap set through the environment: declared
# sizes, counted bytes, gzip output, several ZIPs in one upload, and ZIP work
# running off the event loop.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app -e MAX_ZIP_EXPANDED_BYTES=1048576 \
  api python - < scripts/tests/test_zip_expansion.py)
check "ZIP expansion cap and off-loop regressions (1 MiB cap from env)" $?

# docker-compose.yml sets the cap (#319), and the API reads that value.
cap_env=$(cd "$REPO_ROOT" && docker compose exec -T api python -c \
  'import os; from config import settings; print(os.environ.get("MAX_ZIP_EXPANDED_BYTES"), settings.max_zip_expanded_bytes)' | tr -d '\r')
[ "$cap_env" = "2147483648 2147483648" ]
check "docker-compose.yml sets MAX_ZIP_EXPANDED_BYTES to 2 GiB and the API uses it" $? "env and setting: $cap_env"

# The default 2 GiB cap through the proxy. Prints "<HTTP> <code> <message ok>".
too_large() {
  local name="$1" code
  code=$(curl -s -m 300 -o /tmp/vfy_cap.json -w '%{http_code}' -X POST "$API/ingest/upload" \
    -F "collection=$C" -F "strategy=fixed" -F "files=@$FIX/$name")
  python3 - "$code" "$name" <<'ENDPY'
import json, sys
code, name = sys.argv[1], sys.argv[2]
want = (f"'{name}' expands to more than 2,048 MB when unpacked, the most one upload's ZIPs "
        f"can expand to, so nothing from it was imported. Split it into smaller zips and "
        f"upload them in separate imports.")
try:
    e = json.load(open('/tmp/vfy_cap.json'))['error']
except Exception:
    print(code, 'not-json', 'no'); raise SystemExit
ok = e['message'] == want and e['detail'] == {'file': name, 'members': []}
print(code, e['code'], 'yes' if ok else 'no')
ENDPY
}
read -r code err_code text_ok <<<"$(too_large zip_over_declared.zip)"
[ "$code" = 400 ] && [ "$err_code" = ARCHIVE_TOO_LARGE ] && [ "$text_ok" = yes ]
check "a ZIP declaring more than 2 GiB is refused with ARCHIVE_TOO_LARGE" $? \
  "HTTP=$code code=$err_code exact-message=$text_ok"

# While a gzip member's output is counted, other requests are still answered.
# A smoke check: the count takes about a second, so the controlled test above
# is the one that tells off-loop from on-loop work.
too_large zip_over_gzip.zip > /tmp/vfy_cap_result.txt &
upload_pid=$!
probes=0; slow=0; worst=0
while kill -0 "$upload_pid" 2>/dev/null; do
  read -r pcode ptime <<<"$(curl -s -m 10 -o /dev/null -w '%{http_code} %{time_total}' "$API/ingest/job/vfy-none")"
  probes=$((probes + 1))
  if [ "$pcode" != 404 ] || python3 -c "import sys; sys.exit(0 if float('$ptime') >= 1.0 else 1)"; then
    slow=$((slow + 1))
  fi
  worst=$(python3 -c "print(max(float('$worst'), float('$ptime')))")
done
wait "$upload_pid"
read -r code err_code text_ok < /tmp/vfy_cap_result.txt
[ "$code" = 400 ] && [ "$err_code" = ARCHIVE_TOO_LARGE ] && [ "$text_ok" = yes ]
check "a ZIP whose gzip member expands past 2 GiB is refused with ARCHIVE_TOO_LARGE" $? \
  "HTTP=$code code=$err_code exact-message=$text_ok"
[ "$slow" = 0 ] && [ "$probes" -gt 0 ]
check "job polls are answered while a ZIP is being counted" $? \
  "$probes probes, $slow slow or failed, slowest ${worst}s"

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

# JSON input, records and text are bounded (#300): each over-limit file fails
# alone with its own reason, and the good file beside them is stored.
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 1000 0 "$FIX/json_over_size.json" "$FIX/json_many_records.json" \
  "$FIX/json_over_size_gz.zip" "$FIX/json_over_size_member.zip" "$FIX/plain.json"
read -r status chunks errors_ok <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
want = {
    'json_over_size.json: is larger than 30,000,000 bytes, the most one JSON file can hold; '
    'split the file or convert it to JSON Lines',
    'json_many_records.json: has more than 1,000,000 records, the most one file can hold; '
    'split the file',
    'json_over_size_gz.zip:big.json.gz: decompresses to more than 30,000,000 bytes, the most '
    'one JSON file can hold; split the file or convert it to JSON Lines',
    'json_over_size_member.zip:big.json: is larger than 30,000,000 bytes, the most one JSON '
    'file can hold; split the file or convert it to JSON Lines',
}
ok = len(d['errors']) == 4 and set(d['errors']) == want
if not ok:
    import sys; print(repr(d['errors']), file=sys.stderr)
print(d['status'], d['chunks_stored'], 'yes' if ok else 'no')")"
[ "$status" = partial ] && [ "$chunks" -gt 0 ] && [ "$errors_ok" = yes ]
check "over-size, over-cap, over-size .json.gz and over-size ZIP-member JSON fail alone with their reasons" $? \
  "status=$status chunks=$chunks exact-errors=$errors_ok"

# ── JSON Lines (#291) ────────────────────────────────────────────────────────
# Controlled cases: conversion per line, blank and malformed lines, the line
# and text limits, bounded reading, gzip errors, rechunk of a retained file.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_jsonl_ingest.py)
check "JSON Lines regressions" $?

drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 1000 0 "$FIX/records.jsonl" "$FIX/records.jsonl.gz"
read -r status completed chunks <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], d['files_completed'], d['chunks_stored'])")"
[ "$status" = completed ] && [ "$completed" = 2 ] && [ "$chunks" -ge 2 ]
check "a direct .jsonl and a direct .jsonl.gz both ingest" $? \
  "status=$status completed=$completed chunks=$chunks"
(cd "$REPO_ROOT" && docker compose exec -T api python - "$C" <<'ENDPY'
import sys
from services import weaviate_client as wc
try:
    collection = wc.get_client().collections.get(sys.argv[1])
    objects = [o.properties for o in collection.iterator()]
finally:
    wc.close_client()
wanted = ("bill: HR-1\ntitle: Remote work act\nsponsor.name: Zoë Müller\n\n"
          "bill: HR-2\ntitle: Expense filing act\ntags: finance, deadline")
problems = []
for name in ("records.jsonl", "records.jsonl.gz"):
    mine = sorted((o for o in objects if o["source_file"] == name), key=lambda o: o["chunk_index"])
    text = "".join(o["content"] for o in mine)
    if text != wanted:
        problems.append(f"{name}: text {text!r}")
    if {o["source_type"] for o in mine} != {"jsonl"}:
        problems.append(f"{name}: source_type {sorted({o['source_type'] for o in mine})}")
if problems:
    print("; ".join(problems), file=sys.stderr)
    sys.exit(1)
ENDPY
)
check "their chunks hold one key: value record per line, source_type jsonl" $?

drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 1000 0 "$FIX/bad.jsonl"
read -r status first_error <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
print(d['status'], 'yes' if d['errors'] == ['bad.jsonl: line 2 is not valid JSON: Expecting value at column 10'] else repr(d['errors']))")"
stored=$(api_get "/collections" | python3 -c "
import json,sys
print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
[ "$status" = failed ] && [ "$first_error" = yes ] && [ "$stored" = 0 ]
check "a malformed line fails the file with its line number and stores nothing" $? \
  "status=$status error=$first_error stored=$stored"

# A JSON Lines file holds at most 1,000,000 lines, records with no text
# included (#314): a few KB of gzipped `{}` lines fails and stores nothing.
drop_collection "$C"; make_collection "$C"
ingest "$C" fixed 1000 0 "$FIX/jsonl_many_records.jsonl.gz"
read -r status cap_error <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_job.json'))
want = ['jsonl_many_records.jsonl.gz: was decompressed, but it has more than 1,000,000 records, '
        'the most one file can hold; split the file']
print(d['status'], 'yes' if d['errors'] == want else repr(d['errors']))")"
stored=$(api_get "/collections" | python3 -c "
import json,sys
print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name']=='$C'][0])")
[ "$status" = failed ] && [ "$cap_error" = yes ] && [ "$stored" = 0 ]
check "a JSON Lines file over 1,000,000 lines fails with the record cap and stores nothing" $? \
  "status=$status error=$cap_error stored=$stored"

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

# Chunker limits and insert batching (#386), with the backends mocked, in an
# isolated container with the repository mounted.
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_chunk_limits.py)
check "chunker per-file limits, 6,000-character ceiling and semantic floor (#111)" $?
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_insert_chunks.py)
check "chunk inserts are batched and retried while the index isn't ready" $?

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

# ── re-ingesting a name replaces its chunks (#22) ────────────────────────────
# Re-ingesting used to add a second chunk set beside the first, so answers could
# cite a superseded version; ZIP members were named by basename, so a/report.txt
# and b/report.txt merged. Controlled cases: same-name inputs in one upload,
# ZIP path normalisation, insert-before-delete, stale marking, retention and
# rechunk coverage.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_reingest.py)
check "re-ingest: same-name inputs, replacement order, retention and rechunk regressions" $?

RI="${PREFIX}Reingest"
RIFIX=$(mktemp -d)
python3 - "$FIX/policies.txt" "$RIFIX" <<'ENDPY'
import pathlib, sys, zipfile
v1, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
(out / "v2").mkdir()
(out / "v2" / "policies.txt").write_text(
    "Parking Permits. Facilities issue parking permits each January, and a permit "
    "is valid for one named vehicle only.\n\n"
    "Visitor Badges. Reception prints a visitor badge on arrival, and badges are "
    "returned at the end of the day.\n")
with zipfile.ZipFile(out / "folders.zip", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("a/report.txt", "Alpha report. The cafeteria opens at seven every weekday.")
    z.writestr("b/report.txt", "Beta report. The library closes at nine on Fridays.")
assert "Travel Booking" in v1.read_text()
ENDPY
# What the collection holds: (source_file, content) pairs, and the retained index.
ri_state() {
  (cd "$REPO_ROOT" && docker compose exec -T api python - "$RI") <<'ENDPY'
import json, sys
from services import sources, weaviate_client as wc
try:
    coll = wc.get_client().collections.get(sys.argv[1])
    chunks = sorted([o.properties["source_file"], o.properties["content"]] for o in coll.iterator())
    d = sources.collection_dir(sys.argv[1])
    blobs = sorted(p.name for p in d.iterdir() if p.name != "index.json") if d.is_dir() else []
    print(json.dumps({"chunks": chunks, "index": sources.load_index(sys.argv[1])["documents"],
                      "blobs": blobs}))
finally:
    wc.close_client()
ENDPY
}
drop_collection "$RI"; make_collection "$RI"
ingest "$RI" fixed 200 40 "$FIX/policies.txt"
n1=$(jfield "['chunks_stored']" < /tmp/vfy_job.json)
ingest "$RI" fixed 200 40 "$FIX/policies.txt"
cp /tmp/vfy_job.json /tmp/vfy_ri_again.json
ri_state > /tmp/vfy_ri_state.json
python3 - "$n1" <<'ENDPY'
import json, sys
n1 = int(sys.argv[1]); job = json.load(open('/tmp/vfy_ri_again.json')); st = json.load(open('/tmp/vfy_ri_state.json'))
print(job["status"], job.get("chunks_replaced"), len(st["chunks"]), n1)
sys.exit(0 if n1 > 0 and job["status"] == "completed" and job.get("chunks_replaced") == n1
         and len(st["chunks"]) == n1 else 1)
ENDPY
check "ingesting the same file twice keeps one chunk set and reports chunks_replaced" $? \
  "$(cat /tmp/vfy_ri_again.json | head -c 300)"

ingest "$RI" fixed 200 40 "$RIFIX/v2/policies.txt"
cp /tmp/vfy_job.json /tmp/vfy_ri_v2.json
ri_state > /tmp/vfy_ri_state.json
python3 - "$n1" "$RIFIX/v2/policies.txt" <<'ENDPY'
import hashlib, json, sys
n1 = int(sys.argv[1]); v2 = hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()
job = json.load(open('/tmp/vfy_ri_v2.json')); st = json.load(open('/tmp/vfy_ri_state.json'))
texts = " ".join(c for _, c in st["chunks"])
checks = {
    "completed": job["status"] == "completed",
    "replaced all of v1": job.get("chunks_replaced") == n1,
    "holds only v2's chunks": len(st["chunks"]) == job["chunks_stored"],
    "v1 text gone": "Travel Booking" not in texts,
    "v2 text present": "Parking Permits" in texts,
    "one digest names it": [d for d, e in st["index"].items() if "policies.txt" in e["filenames"]] == [v2],
    "v1 blob gone": st["blobs"] == [v2],
}
print({k: v for k, v in checks.items() if not v} or "ok")
sys.exit(0 if all(checks.values()) else 1)
ENDPY
check "a modified file replaces its chunks and its retained original" $? "$(head -c 300 /tmp/vfy_ri_v2.json)"

ingest "$RI" fixed 200 40 "$RIFIX/folders.zip"
ri_state > /tmp/vfy_ri_state.json
python3 - <<'ENDPY'
import json, sys
st = json.load(open('/tmp/vfy_ri_state.json'))
names = {n: c for n, c in st["chunks"]}
ok = ("Alpha report" in names.get("a/report.txt", "")
      and "Beta report" in names.get("b/report.txt", "") and "report.txt" not in names)
print(sorted(names))
sys.exit(0 if ok else 1)
ENDPY
check "ZIP members with one basename in two folders stay two documents, named by path" $?

# A Finder "Compress" ZIP (#378): __MACOSX/finder/._report.txt is the file's
# AppleDouble resource fork with the same suffix, and was queued as a document.
python3 - "$RIFIX/finder.zip" <<'ENDPY'
import sys, zipfile
double = b"\x00\x05\x16\x07\x00\x02\x00\x00" + b"Mac OS X        " + b"\x00" * 58
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for name, data in [("finder/", b""), ("finder/report.txt",
                        b"Finder report. The pool closes at eight on Sundays."),
                       ("__MACOSX/", b""), ("__MACOSX/finder/", b""),
                       ("__MACOSX/finder/._report.txt", double)]:
        z.writestr(zipfile.ZipInfo(name), data)
ENDPY
ingest "$RI" fixed 200 40 "$RIFIX/finder.zip"
cp /tmp/vfy_job.json /tmp/vfy_ri_finder.json
ri_state > /tmp/vfy_ri_state.json
python3 - <<'ENDPY'
import json, sys
job = json.load(open('/tmp/vfy_ri_finder.json')); st = json.load(open('/tmp/vfy_ri_state.json'))
names = {n: c for n, c in st["chunks"]}
checks = {
    "completed": job["status"] == "completed",
    "no errors": job["errors"] == [],
    "one file": job["files_total"] == 1,
    "skipped names the AppleDouble member": job["skipped"] == [
        "finder.zip:__MACOSX/finder/._report.txt (macOS metadata (AppleDouble), not a document)"],
    "real file stored": "Finder report" in names.get("finder/report.txt", ""),
    "no metadata stored": not any("__MACOSX" in n or "/._" in n for n in names),
}
print({k: v for k, v in checks.items() if not v} or "ok", sorted(names))
sys.exit(0 if all(checks.values()) else 1)
ENDPY
check "a Finder ZIP stores its file, not the __MACOSX AppleDouble member, which is listed in skipped" $? \
  "$(head -c 300 /tmp/vfy_ri_finder.json)"

# Two parts with one filename: the second's bytes are stored, the first reported.
ingest "$RI" fixed 200 40 "$FIX/policies.txt;filename=dupe.txt" "$RIFIX/v2/policies.txt;filename=dupe.txt"
cp /tmp/vfy_job.json /tmp/vfy_ri_dupe.json
ri_state > /tmp/vfy_ri_state.json
python3 - <<'ENDPY'
import json, sys
job = json.load(open('/tmp/vfy_ri_dupe.json')); st = json.load(open('/tmp/vfy_ri_state.json'))
texts = " ".join(c for n, c in st["chunks"] if n == "dupe.txt")
ok = (job["files_total"] == 1 and job["skipped"] == [
          "dupe.txt (replaced by dupe.txt later in this upload, same name 'dupe.txt')"]
      and "Parking Permits" in texts and "Travel Booking" not in texts)
print(job["files_total"], job["skipped"])
sys.exit(0 if ok else 1)
ENDPY
check "the same name twice in one upload ingests the last and reports the first" $? \
  "$(head -c 300 /tmp/vfy_ri_dupe.json)"

# Gold-standard sessions are marked stale only when a re-ingest replaces earlier
# chunks, never for a brand-new name (#22). A real session, not a mocked marker.
RIGS="gs_$(python3 -c 'import uuid; print(uuid.uuid4().hex[:8])')"
(cd "$REPO_ROOT" && docker compose exec -T api python - "$RI" "$RIGS") <<'ENDPY'
import sys
from services import goldstandard as gs
pairs = [{"pair_id": "ri_pair", "question": "Q", "answer": "A", "contexts": ["C"],
          "ground_truth": "T", "source_file": "policies.txt", "chunk_index": 0, "status": "approved"}]
gs.store_session({"session_id": sys.argv[2], "collection": sys.argv[1], "status": "completed",
                  "pairs_total": 1, "pairs_completed": 1, "pairs": pairs})
ENDPY
check "re-ingest: a synthetic gold-standard session is planted for the collection" $?
# The API reads sessions written by another process at its next scan.
api_get "/goldstandard/diagnostics" >/dev/null
printf 'Fresh document. The gym opens at six.\n' > "$RIFIX/fresh.txt"
ingest "$RI" fixed 200 40 "$RIFIX/fresh.txt"
api_get "/goldstandard/session/$RIGS" > /tmp/vfy_ri_gs.json
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ri_gs.json')); j=json.load(open('/tmp/vfy_job.json'))
print(d.get('stale'), d.get('stale_reason'), j['status'], j.get('chunks_replaced'))
sys.exit(0 if d.get('stale') is False and j['status']=='completed' and j.get('chunks_replaced')==0 else 1)"
check "ingesting a brand-new name leaves gold-standard sessions current" $? "$(head -c 300 /tmp/vfy_ri_gs.json)"
ingest "$RI" fixed 200 40 "$FIX/policies.txt"
api_get "/goldstandard/session/$RIGS" > /tmp/vfy_ri_gs.json
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ri_gs.json')); j=json.load(open('/tmp/vfy_job.json'))
r=d.get('stale_reason') or ''
print(d.get('stale'), r, j['status'], j.get('chunks_replaced'))
sys.exit(0 if d.get('stale') is True and 're-ingested' in r and 'policies.txt' in r
         and j['status']=='completed' and j.get('chunks_replaced',0)>0 else 1)"
check "a replacing re-ingest marks the collection's gold-standard sessions stale, naming the file" $? \
  "$(head -c 300 /tmp/vfy_ri_gs.json)"

# Two concurrent uploads of one name (#22): the job that runs last wins, each
# job is applied whole, and arrival order is not promised, so either may win.
for v in 1 2; do
  src="$FIX/policies.txt"; [ "$v" = 2 ] && src="$RIFIX/v2/policies.txt"
  curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$RI" -F "strategy=fixed" \
    -F "chunk_size=200" -F "chunk_overlap=60" -F "min_chunk_size=40" \
    -F "files=@$src;filename=race.txt" > "/tmp/vfy_ri_race$v.json" &
done
wait
for v in 1 2; do
  rj=$(jfield "['job_id']" < "/tmp/vfy_ri_race$v.json")
  [ -n "$rj" ] && wait_for_job "/ingest/job/$rj" 900 >/dev/null
  api_get "/ingest/job/$rj" > "/tmp/vfy_ri_race$v.done.json"
done
ri_state > /tmp/vfy_ri_state.json
python3 - "$FIX/policies.txt" "$RIFIX/v2/policies.txt" <<'ENDPY'
import hashlib, json, sys
sha = [hashlib.sha256(open(p, "rb").read()).hexdigest() for p in sys.argv[1:3]]
jobs = [json.load(open(f"/tmp/vfy_ri_race{v}.done.json")) for v in (1, 2)]
st = json.load(open("/tmp/vfy_ri_state.json"))
texts = [c for n, c in st["chunks"] if n == "race.txt"]
joined = " ".join(texts)
v1, v2 = "Travel Booking" in joined, "Parking Permits" in joined
win = 0 if v1 else 1
digests = [d for d, e in st["index"].items() if "race.txt" in e["filenames"]]
checks = {
    "both completed": all(j.get("status") == "completed" for j in jobs),
    "exactly one version present": v1 != v2,
    "winner's whole chunk set": len(texts) == jobs[win].get("chunks_stored"),
    "winner replaced the loser's set": jobs[win].get("chunks_replaced") == jobs[1 - win].get("chunks_stored"),
    "one digest, the winner's": digests == [sha[win]],
}
print({k: v for k, v in checks.items() if not v} or "ok", [j.get("status") for j in jobs])
sys.exit(0 if all(checks.values()) else 1)
ENDPY
check "two concurrent uploads of one name leave exactly one job's version, whole" $?
drop_collection "$RI"
rm -rf "$RIFIX"

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
# Submitted, accepted and stored chunks are counted apart (#302); a clean job's
# three counts agree, and stored is the checked count it always was.
read -r n_sub n_acc n_sto <<<"$(api_get "/ingest/job/$job" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d.get('chunks_submitted'), d.get('chunks_accepted'), d.get('chunks_stored'))")"
[ "$n_sub" = "$n_sto" ] && [ "$n_acc" = "$n_sto" ] && [ "${n_sto:-0}" -gt 0 ] 2>/dev/null
check "a clean job reports chunks_submitted = chunks_accepted = chunks_stored" $? \
  "submitted=$n_sub accepted=$n_acc stored=$n_sto"

# ── deleting a collection while an ingest job runs (#40) ─────────────────────
# A DELETE sent during a job used to wait for it and then remove what it stored,
# and one that ran before a queued job let that job's inserts recreate the
# collection (Weaviate auto-schema). Controlled cases: every job kind, both
# request orderings, and the job's report when its collection disappears.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_delete_busy.py)
check "delete-while-busy: job kinds, request orderings and the job's report" $?
# Confirmation, a delete in progress refusing new jobs, and a cancelled
# generation start (#302); then the job write counts and the MCP delete tool.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_write_counts.py)
check "ingest jobs count submitted, accepted and stored chunks apart" $?
python3 "$REPO_ROOT/scripts/tests/test_mcp_delete.py"
check "MCP rag_delete_collection sends confirm=true and still takes only a name" $?
# One spelling for per-collection state (#372): every route takes a collection
# name as Weaviate stores it, so a lowercase first letter keys nothing apart.
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_collection_spelling.py)
check "routes key per-collection state by the stored collection name (#372)" $?

# DELETE needs ?confirm=true, checked before the collection is looked up (#302).
CC="${PREFIX}Confirm"
drop_collection "$CC"; make_collection "$CC"
confirm_code() {   # HTTP status and error code of a DELETE with this query
  _rag_require_lock_owner
  local http; http=$(curl -s -m 120 -o /tmp/vfy_confirm.json -w '%{http_code}' -X DELETE "$API/collections/$1$2")
  printf '%s %s' "$http" "$(python3 -c "
import json
try: print(json.load(open('/tmp/vfy_confirm.json'))['error']['code'])
except Exception: print('-')")"
}
check_eq "DELETE without confirm is 400 CONFIRMATION_REQUIRED" "$(confirm_code "$CC" "")" "400 CONFIRMATION_REQUIRED"
check_eq "DELETE with confirm=false is 400 CONFIRMATION_REQUIRED" "$(confirm_code "$CC" "?confirm=false")" "400 CONFIRMATION_REQUIRED"
check_eq "a refused DELETE leaves the collection in place" "$(api_get "/collections" | python3 -c "
import json,sys; print(any(c['name']=='$CC' for c in json.load(sys.stdin)['collections']))")" "True"
check_eq "DELETE of a missing collection without confirm is 400, not 404" \
  "$(confirm_code "${PREFIX}ConfirmMissing" "")" "400 CONFIRMATION_REQUIRED"
check_eq "DELETE with confirm=TRUE (any case) deletes" "$(confirm_code "$CC" "?confirm=TRUE" | cut -d' ' -f1)" "200"
check_eq "the confirmed delete removed the collection" "$(api_get "/collections" | python3 -c "
import json,sys; print(any(c['name']=='$CC' for c in json.load(sys.stdin)['collections']))")" "False"

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
# A killed job's leftovers (#377): ingest staging in UPLOAD_DIR, and an export's
# `.build-*` directory and `.*.tar.gz.partial` archive in EXPORTS_DIR.
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_startup_sweep.py)
check "startup sweep: ingest staging and export build leftovers, exact names, links never followed" $?

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
# read back unchanged, one interrupted after storing a file must read `partial`
# with the interruption message, and one interrupted before storing any file
# must read `failed` with it (#309). `--timeout 0`: with the default 10 s stop
# timeout the worker could finish the job during shutdown.
JR="${PREFIX}JobRestart"
JN="${PREFIX}JobRestartNone"
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then restart_refusal=$(restart_refusal_reason); fi
if [ "${RAG_ALLOW_RESTART:-0}" != "1" ]; then
  skip "ingest job status across an API restart" "set RAG_ALLOW_RESTART=1 to include it"
elif [ -n "$restart_refusal" ]; then
  check "ingest job status across an API restart" 1 "$restart_refusal"
else
  drop_collection "$JR"; make_collection "$JR"
  drop_collection "$JN"; make_collection "$JN"
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
    rm -rf "$slow_dir"
    check "an ingest is running with a file stored before the restart" 1 "last status '$state'; nothing restarted"
  else
    # Only the slow file, so nothing is stored before the restart (#309).
    curl -s -m 600 -o /tmp/vfy_restart_none_start.json -X POST "$API/ingest/upload" -F "collection=$JN" \
      -F "strategy=fixed" -F "chunk_size=300" -F "min_chunk_size=50" -F "files=@$slow_dir/slow.txt"
    rm -rf "$slow_dir"
    none_job=$(jfield "['job_id']" < /tmp/vfy_restart_none_start.json)
    sleep 2
    api_get "/ingest/job/$none_job" > /tmp/vfy_restart_none_before.json
    # The running jobs' upload staging, which the restart kills in place (#377).
    staged=$(cd "$REPO_ROOT" && docker compose exec -T api sh -c 'ls -d /app/uploads/ingest-* 2>/dev/null' | sort)
    check "the interrupted ingests have ingest-* staging for the restart to sweep" \
      "$([ -n "$staged" ] && echo 0 || echo 1)" "no /app/uploads/ingest-* while the jobs ran"
    _rag_require_lock_owner
    (cd "$REPO_ROOT" && docker compose restart --timeout 0 api) >/dev/null 2>&1
    for _ in $(seq 1 90); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
    left=$(cd "$REPO_ROOT" && docker compose exec -T api sh -c '
      for p in "$@"; do if [ -e "$p" ] || [ -L "$p" ]; then echo "$p"; fi; done; echo checked' sh $staged)
    check "the restart removes the interrupted ingests' upload staging" \
      "$([ -n "$staged" ] && [ "$left" = checked ] && echo 0 || echo 1)" "still there: $left"
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
    code=$(curl -s -m 120 -o /tmp/vfy_restart_none_after.json -w '%{http_code}' "$API/ingest/job/$none_job")
    python3 - "$code" <<'ENDPY'
import json, sys
try:
    before = json.load(open('/tmp/vfy_restart_none_before.json'))
    after = json.load(open('/tmp/vfy_restart_none_after.json'))
except (OSError, ValueError):
    sys.exit(1)
ok = (before.get("status") in ("queued", "running") and before.get("files_completed") == 0
      and sys.argv[1] == "200" and after.get("status") == "failed"
      and any(e.startswith("Interrupted: the API restarted while this job was running.")
              for e in after.get("errors", []))
      and after.get("files_completed") == 0)
print(before.get("status"), sys.argv[1], after.get("status"), after.get("files_completed"), after.get("errors"))
sys.exit(0 if ok else 1)
ENDPY
    check "an ingest interrupted before storing a file reads back 200 'failed' with the message" $? \
      "before: $(head -c 200 /tmp/vfy_restart_none_before.json); after: $(head -c 300 /tmp/vfy_restart_none_after.json)"
  fi
  drop_collection "$JR"
  drop_collection "$JN"
fi

drop_collection "$C"
cleanup_prefixed
summary
