#!/usr/bin/env bash
# SPECIFICATIONS.md §10.2 — Query
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
bash ./11_retrieval.sh
check "effective retrieval controls acceptance suite" $?
C="${PREFIX}Query"

section "§10.2 Query"
# A failed metrics write never fails a query, and metrics.jsonl is bounded (#374):
# controlled, in the API image with no network.
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_metrics.py)
check "a failed metrics write keeps the answer; metrics.jsonl is bounded and reloads (#374)" $?
# Citation scores are higher-is-better in every mode, and null when missing (#376):
# controlled in the API image with no network, then on the real Weaviate with each
# distance metric (models controlled, no LLM work).
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_citation_score.py)
check "citation scores convert each metric's distance, best first; missing reads as null (#376)" $?
(cd "$REPO_ROOT" && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/citation_score.py)
check "real Weaviate: cosine, dot and l2-squared scores, and a recreated collection's new metric (#376)" $?

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_q.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_q.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null

ask() {  # ask <mode> <format> <citations> -> writes /tmp/vfy_ans.json
  api_post "/query" "{\"question\":\"who approves overtime?\",\"collection\":\"$C\",\"retrieval_mode\":\"$1\",\"top_k\":3,\"alpha\":0.5,\"include_citations\":$3,\"response_format\":\"$2\"}" > /tmp/vfy_ans.json
}

if [ "$SKIP_SLOW" = "1" ]; then
  skip "LLM query assertions" "set RAG_SKIP_SLOW=0 to include model calls"
  summary; exit $?
fi

# ── a question returns an answer, with usable latencies ──────────────────────
ask hnsw end_user true
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
sys.exit(0 if d.get('answer','').strip() else 1)"
check "a question returns a non-empty answer" $?
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
sys.exit(0 if d.get('retrieval_latency_ms',0) > 0 and d.get('llm_latency_ms',0) > 0 else 1)"
check "latency fields present and non-zero" $? \
  "$(python3 -c "import json;d=json.load(open('/tmp/vfy_ans.json'));print(d.get('retrieval_latency_ms'),d.get('llm_latency_ms'))")"

# ── citations are shaped correctly ───────────────────────────────────────────
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
c = d.get('citations') or []
ok = bool(c) and all(
    isinstance(x.get('source_file'), str) and x['source_file']
    and (x.get('score') is None or isinstance(x.get('score'), (int, float)))
    and isinstance(x.get('chunk_index'), int)
    and isinstance(x.get('excerpt'), str) for x in c)
sys.exit(0 if ok else 1)"
check "citations carry source_file, chunk_index, score and excerpt" $?

ask hnsw end_user false
python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_ans.json'))
sys.exit(0 if not d.get('citations') else 1)"
check "citations are withheld when not requested" $?

# ── all four retrieval modes ─────────────────────────────────────────────────
for mode in hnsw flat hybrid semantic; do
  ask "$mode" end_user true
  python3 -c "
import json,sys
try: d=json.load(open('/tmp/vfy_ans.json'))
except Exception: sys.exit(1)
sys.exit(0 if 'error' not in d and d.get('chunks_retrieved',0) > 0 and d.get('answer','').strip() else 1)"
  check "retrieval mode '$mode' returns results" $?
  python3 -c "
import json,sys
try: c=json.load(open('/tmp/vfy_ans.json')).get('citations') or []
except Exception: sys.exit(1)
s=[x.get('score') for x in c]
sys.exit(0 if s and all(isinstance(v,(int,float)) for v in s) and s==sorted(s,reverse=True) else 1)"
  check "retrieval mode '$mode' citation scores are numbers, best (highest) first (#376)" $? \
    "$(python3 -c "import json;print([x.get('score') for x in json.load(open('/tmp/vfy_ans.json')).get('citations') or []])" 2>/dev/null)"
done

# ── end_user is shorter than engineer ────────────────────────────────────────
# The end_user prompt asks for a short answer (#109), but a CPU model still
# varies answer to answer, so a single pair is noise. Compare the mean length
# across trials.
trials="${RAG_FORMAT_TRIALS:-3}"
eu_total=0; en_total=0; eu_wins=0
for _ in $(seq 1 "$trials"); do
  ask flat end_user false;  eu=$(python3 -c "import json;print(len(json.load(open('/tmp/vfy_ans.json'))['answer']))")
  ask flat engineer false;  en=$(python3 -c "import json;print(len(json.load(open('/tmp/vfy_ans.json'))['answer']))")
  eu_total=$((eu_total+eu)); en_total=$((en_total+en))
  [ "$eu" -lt "$en" ] && eu_wins=$((eu_wins+1))
done
[ "$eu_total" -lt "$en_total" ]
check "end_user answers are shorter than engineer on average" $? \
  "mean end_user=$((eu_total/trials)) engineer=$((en_total/trials)); end_user shorter in $eu_wins/$trials trials"

# ── the query metrics reload after an API restart (#374; opt-in: it restarts the api) ──
# The queries above wrote metrics records; a restart goes through the
# lifespan's load_from_disk, so GET /metrics/latency must report the same records.
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  restart_refusal=$(restart_refusal_reason)
  if [ -n "$restart_refusal" ]; then
    check "query metrics reload after an API restart (#374)" 1 "$restart_refusal"
  else
    api_get "/metrics/latency?collection=$C&history_limit=500" > /tmp/vfy_metrics_before.json
    project="$COMPOSE_PROJECT_NAME"
    started=$(python3 -c "import time;print(time.time())")
    (cd "$REPO_ROOT" && docker compose -p "$project" restart api >/dev/null 2>&1)
    elapsed=$(wait_healthy_timed "$started" 300)
    api_get "/metrics/latency?collection=$C&history_limit=500" > /tmp/vfy_metrics_after.json
    python3 -c "
import json,sys
b=json.load(open('/tmp/vfy_metrics_before.json')); a=json.load(open('/tmp/vfy_metrics_after.json'))
sys.exit(0 if b['total_records'] > 0 and a['history'] == b['history'] else 1)"
    check "query metrics reload after an API restart (#374)" $? "healthy after ${elapsed:-never}s"
  fi
else
  skip "query metrics reload after an API restart (#374)" "set RAG_ALLOW_RESTART=1 to include it"
fi

drop_collection "$C"
cleanup_prefixed
summary
