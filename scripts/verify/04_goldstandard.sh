#!/usr/bin/env bash
# SPECIFICATIONS.md §10.3 — Gold Standard
#
# Every check here corresponds to a defect that was live in the codebase:
# lost pairs, counters that reported attempts as successes, a 500 where a 409
# belonged, and a PATCH that silently rewrote approved content.
cd "$(dirname "$0")" && . ./lib.sh
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
bash ./12_persistence.sh
check "durable session acceptance suite" $?
C="${PREFIX}Gold"

section "§10.3 Gold Standard"

# Selection needs the backend but no LLM work; include it even in slow-skip runs.
bash ./09_sampling.sh
check "UUID sampling acceptance" $?

# Review errors need no model: an unknown session is SESSION_NOT_FOUND, an
# unknown pair PAIR_NOT_FOUND, and an empty edit is refused with 422 (#381).
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_goldstandard_review.py)
check "review 404 codes and empty-edit refusal" $?
code=$(curl -s -o /tmp/vfy_gs404.json -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/gs_00000000/pair/p_1" -H 'Content-Type: application/json' \
  -d '{"status":"approved"}')
check_eq "PATCH on an unknown session returns 404 SESSION_NOT_FOUND" \
  "$code $(jfield "['error']['code']" < /tmp/vfy_gs404.json)" "404 SESSION_NOT_FOUND"
code=$(curl -s -o /tmp/vfy_gs404.json -m 120 -w '%{http_code}' -X POST \
  "$API/goldstandard/regenerate" -H 'Content-Type: application/json' \
  -d '{"session_id":"gs_00000000","pair_id":"p_1"}')
check_eq "regenerate on an unknown session returns 404 SESSION_NOT_FOUND" \
  "$code $(jfield "['error']['code']" < /tmp/vfy_gs404.json)" "404 SESSION_NOT_FOUND"

if [ "$SKIP_SLOW" = "1" ]; then
  skip "LLM generation/review/export" "set RAG_SKIP_SLOW=0 to include them"
  summary; exit $?
fi

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_g.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_g.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null

SAMPLE="${RAG_GS_SAMPLE:-3}"
api_post "/goldstandard/generate" "{\"collection\":\"$C\",\"sample_size\":$SAMPLE}" > /tmp/vfy_gen.json
SID=$(python3 -c "import json;print(json.load(open('/tmp/vfy_gen.json'))['session_id'])")

# ── 409 while still generating ───────────────────────────────────────────────
# Regenerating mid-flight raced with the generation loop and returned 500.
overlapped=0
for _ in $(seq 1 60); do
  read -r st n <<<"$(api_get "/goldstandard/session/$SID" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d['status'], len(d['pairs']))")"
  if [ "$st" = generating ] && [ "$n" -ge 1 ]; then
    pid=$(api_get "/goldstandard/session/$SID" | jfield "['pairs'][0]['pair_id']")
    code=$(api_post_code "/goldstandard/regenerate" "{\"session_id\":\"$SID\",\"pair_id\":\"$pid\"}")
    check_eq "regenerate during generation returns 409" "$code" "409"
    overlapped=1; break
  fi
  [ "$st" != generating ] && break
  sleep 1
done
[ "$overlapped" = 1 ] || skip "409-while-generating" "generation finished before a regenerate could overlap"

wait_for_job "/goldstandard/session/$SID" 1800 >/dev/null
api_get "/goldstandard/session/$SID" > /tmp/vfy_sess.json

# ── every sampled chunk is accounted for, including visible model failures ──────────────────────
read -r total attempted completed failed actual <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_sess.json'))
print(d['pairs_total'], d.get('pairs_attempted','?'), d['pairs_completed'],
      d.get('pairs_failed','?'), len(d['pairs']))")"
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_sess.json'))
completed,failed,total = d['pairs_completed'],d['pairs_failed'],d['pairs_total']
errors=d.get('errors',[])
ok=(d['status']=='completed' and completed>0 and completed+failed==total
    and d['pairs_attempted']==total and completed==len(d['pairs'])
    and len(errors)==failed and all(isinstance(e,str) and e.strip() for e in errors))
print('reported failures:', errors)
sys.exit(0 if ok else 1)"
check "generate accounts for every sampled chunk with useful pairs and explicit failures" $? \
  "total=$total completed=$completed actual=$actual failed=$failed"
[ "$completed" = "$actual" ]
check "pairs_completed matches the pairs that exist" $? \
  "completed=$completed actual=$actual (this counted attempts before)"
[ "$attempted" = "$total" ]
check "pairs_attempted reaches the total so progress can finish" $? "attempted=$attempted/$total"

python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_sess.json'))
need = ('question','answer','ground_truth','contexts')
sys.exit(0 if d['pairs'] and all(all(p.get(f) for f in need) for p in d['pairs']) else 1)"
check "every pair has question, answer, ground_truth and contexts" $?

# ── the PATCH audit rule ─────────────────────────────────────────────────────
if [ "$actual" -lt 1 ]; then
  drop_collection "$C"
  cleanup_prefixed
  summary; exit $?
fi
PID=$(python3 -c "import json;print(json.load(open('/tmp/vfy_sess.json'))['pairs'][0]['pair_id'])")
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"question":"rewritten without declaring an edit"}')
check_eq "editing content without status='edited' is refused" "$code" "422"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"edited","question":"a properly declared edit"}')
check_eq "editing content with status='edited' is accepted" "$code" "200"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"approved"}')
check_eq "a status-only change is accepted" "$code" "200"
code=$(curl -s -o /tmp/vfy_gs404.json -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"edited","question":""}')
check_eq "an empty question is refused with 422 INVALID_PARAMETER" \
  "$code $(jfield "['error']['code']" < /tmp/vfy_gs404.json)" "422 INVALID_PARAMETER"
code=$(curl -s -o /tmp/vfy_gs404.json -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/p_unknown" -H 'Content-Type: application/json' \
  -d '{"status":"approved"}')
check_eq "PATCH on an unknown pair returns 404 PAIR_NOT_FOUND" \
  "$code $(jfield "['error']['code']" < /tmp/vfy_gs404.json)" "404 PAIR_NOT_FOUND"

# ── regenerate replaces exactly one pair ─────────────────────────────────────
if [ "$actual" -ge 2 ]; then
  TARGET=$(python3 -c "import json;print(json.load(open('/tmp/vfy_sess.json'))['pairs'][1]['pair_id'])")
  code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
    "$API/goldstandard/session/$SID/pair/$TARGET" -H 'Content-Type: application/json' \
    -d '{"status":"edited","question":"Owned regeneration replacement sentinel"}')
  check_eq "regeneration target edit is accepted" "$code" "200"
  # Snapshot after edits, so every non-target pair must remain byte-for-byte equal.
  api_get "/goldstandard/session/$SID" > /tmp/vfy_regen_before.json
  code=$(curl -s -m 1920 -o /tmp/vfy_regen.json -w '%{http_code}' -X POST \
    "$API/goldstandard/regenerate" -H 'Content-Type: application/json' \
    -d "{\"session_id\":\"$SID\",\"pair_id\":\"$TARGET\"}")
  check_eq "regenerate returns a successful API response" "$code" "200"
  api_get "/goldstandard/session/$SID" > /tmp/vfy_after.json
  python3 - "$TARGET" <<'ENDPY'
import json, sys
target = sys.argv[1]
before = {p['pair_id']: p for p in json.load(open('/tmp/vfy_regen_before.json'))['pairs']}
after = {p['pair_id']: p for p in json.load(open('/tmp/vfy_after.json'))['pairs']}
changed = [k for k in before if k in after and before[k] != after[k]]
# Only the requested pair may differ from the immediate pre-request snapshot.
unexpected = [k for k in changed if k != target]
sys.exit(0 if set(before) == set(after) and target in changed and not unexpected else 1)
ENDPY
  check "regenerate replaces only the targeted pair, ids stable" $?
else
  skip "regenerate-replaces-one" "needs at least two pairs"
fi

# ── export filtering, schema and filename ────────────────────────────────────
python3 - "$SID" "$API" <<'ENDPY'
import json, sys, urllib.request
sid, api = sys.argv[1], sys.argv[2]
pairs = json.load(urllib.request.urlopen(f"{api}/goldstandard/session/{sid}"))['pairs']
plan = ["approved", "edited", "rejected", "pending", "approved"]
for pair, status in zip(pairs, plan):
    body = {"status": status}
    if status == "edited":
        body["question"] = "edited for export"
    req = urllib.request.Request(
        f"{api}/goldstandard/session/{sid}/pair/{pair['pair_id']}",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)
ENDPY
api_post "/goldstandard/save" "{\"session_id\":\"$SID\"}" > /tmp/vfy_save.json
SAVE_INFO=$(python3 - "$C" <<'ENDPY'
import json, re, sys
collection = sys.argv[1]
d = json.load(open('/tmp/vfy_save.json'))
kept = json.load(open('/tmp/vfy_sess.json'))['pairs']
expected = sum(1 for p, s in zip(kept, ["approved","edited","rejected","pending","approved"])
               if s in ("approved", "edited"))
print(d['pairs_saved'], d['pairs_excluded'], expected, d['filename'],
      1 if re.match(rf'^{collection}_\d{{8}}_\d{{6}}\.json$', d['filename']) else 0)
ENDPY
)
read -r saved excluded expected fname name_ok <<<"$SAVE_INFO"
[ "$saved" = "$expected" ]
check "export keeps only approved and edited pairs" $? "saved=$saved expected=$expected excluded=$excluded"
[ "$((saved + excluded))" = "$actual" ]
check "saved + excluded accounts for every pair" $? "$saved + $excluded vs $actual"
check_eq "default filename is {collection}_{YYYYMMDD_HHMMSS}.json" "$name_ok" "1"

curl -s -m 120 "$API/goldstandard/download/$fname" -o /tmp/vfy_export.json
python3 -c "
import json,sys
d = json.load(open('/tmp/vfy_export.json'))
RAGAS = {'question','answer','contexts','ground_truth'}
ok = isinstance(d, list) and d and all(
    RAGAS <= set(r) and isinstance(r['contexts'], list) and r['contexts']
    and all(isinstance(x,str) and x for x in r['contexts'])
    and all(isinstance(r[k],str) and r[k].strip() for k in ('question','answer','ground_truth'))
    for r in d)
sys.exit(0 if ok else 1)"
check "exported file is valid JSON in the RAGAS schema" $?

code=$(api_code "$API/goldstandard/download/definitely_not_here.json")
check_eq "download of an unknown filename returns 404" "$code" "404"

# A caller-chosen name reaches only the exports folder (#370).
for name in metrics.jsonl . .. goldstandard_sessions; do
  api_post "/goldstandard/save" "{\"session_id\":\"$SID\",\"filename\":\"$name\"}" > /tmp/vfy_badsave.json
  check_eq "save as '$name' is refused with INVALID_PATH" \
    "$(jfield "['error']['code']" < /tmp/vfy_badsave.json)" "INVALID_PATH"
done
code=$(api_post_code "/goldstandard/save" "{\"session_id\":\"$SID\",\"filename\":\"collection_registry.json\"}")
check_eq "save as 'collection_registry.json' writes an export" "$code" "200"
check_eq "collections still list after that save" "$(api_code "$API/collections")" "200"

# ── sessions survive a restart ───────────────────────────────────────────────
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then restart_refusal=$(restart_refusal_reason); fi
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ] && [ -n "$restart_refusal" ]; then
  check "sessions survive an API restart" 1 "$restart_refusal"
elif [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  api_get "/goldstandard/session/$SID" > /tmp/vfy_after.json
  (cd "$(git rev-parse --show-toplevel 2>/dev/null || echo ../..)" && docker compose restart api >/dev/null 2>&1)
  for _ in $(seq 1 60); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 3; done
  api_get "/goldstandard/session/$SID" > /tmp/vfy_post.json
  python3 -c "
import json,sys
a=json.load(open('/tmp/vfy_after.json')); b=json.load(open('/tmp/vfy_post.json'))
sys.exit(0 if [p['pair_id'] for p in a['pairs']] == [p['pair_id'] for p in b['pairs']] else 1)"
  check "sessions survive an API restart" $?
else
  skip "session survives a restart" "set RAG_ALLOW_RESTART=1 to include it"
fi

# ── hard-stop recovery retains pairs and unblocks regeneration ───────────────
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ] && [ -z "$restart_refusal" ]; then
  api_post "/goldstandard/generate" "{\"collection\":\"$C\",\"sample_size\":5}" > /tmp/vfy_gen2.json
  SID2=$(python3 -c "import json;print(json.load(open('/tmp/vfy_gen2.json'))['session_id'])")
  st=""; n=0
  for _ in $(seq 1 240); do
    api_get "/goldstandard/session/$SID2" > /tmp/vfy_gen2_pre.json
    read -r st n <<<"$(python3 -c "import json;d=json.load(open('/tmp/vfy_gen2_pre.json'));print(d['status'],len(d['pairs']))")"
    { [ "$st" != generating ] || [ "$n" -ge 1 ]; } && break
    sleep 1
  done
  if [ "$st" = generating ] && [ "$n" -ge 1 ]; then
    # Recheck ownership immediately before the destructive disposable-project action.
    _rag_require_lock_owner
    if refusal=$(restart_refusal_reason) && [ -z "$refusal" ] && [ "${COMPOSE_PROJECT_NAME:-}" = rag-verify ]; then
      (cd "$(git rev-parse --show-toplevel)" && docker compose kill -s SIGKILL api >/dev/null 2>&1 && docker compose start api >/dev/null 2>&1)
      check "hard API stop and start succeeds" $?
      for _ in $(seq 1 60); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 3; done
      api_get "/goldstandard/session/$SID2" > /tmp/vfy_gen2_post.json
      python3 - <<'ENDPY'
import json,sys
before=json.load(open('/tmp/vfy_gen2_pre.json')); after=json.load(open('/tmp/vfy_gen2_post.json'))
retained={p['pair_id']:p for p in after['pairs']}
ok=(after['status']=='failed' and all(retained.get(p['pair_id'])==p for p in before['pairs'])
    and any(e.startswith('Generation interrupted by API restart.') for e in after.get('errors',[])))
print(after['status'], after.get('errors'))
sys.exit(0 if ok else 1)
ENDPY
      check "hard stop settles generation with a plain reason and retained pairs" $?
      pid2=$(python3 -c "import json;print(json.load(open('/tmp/vfy_gen2_post.json'))['pairs'][0]['pair_id'])")
      code=$(curl -s -m 1920 -o /tmp/vfy_gen2_regen.json -w '%{http_code}' -X POST \
        "$API/goldstandard/regenerate" -H 'Content-Type: application/json' \
        -d "{\"session_id\":\"$SID2\",\"pair_id\":\"$pid2\"}")
      check_eq "regeneration succeeds after startup settlement" "$code" "200"
      api_get "/goldstandard/session/$SID2" > /tmp/vfy_gen2_final.json
      python3 - "$pid2" <<'ENDPY'
import json,sys
pair=json.load(open('/tmp/vfy_gen2_regen.json'))
state=json.load(open('/tmp/vfy_gen2_final.json'))
sys.exit(0 if pair.get('pair_id')==sys.argv[1] and pair in state['pairs'] else 1)
ENDPY
      check "regenerated retained pair is present in session" $?
    else
      check "hard-stop target is disposable" 1 "${refusal:-expected compose project rag-verify}"
    fi
  else
    skip "hard stop with retained pair" "no interrupted session with a pair within 240s (status=$st pairs=$n)"
  fi
elif [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  check "hard-stop target is disposable" 1 "$restart_refusal"
else
  skip "hard stop during generation" "set RAG_ALLOW_RESTART=1 to include it"
fi

drop_collection "$C"
cleanup_prefixed
summary
