#!/usr/bin/env bash
# Ingest, query, metrics and health API paths no other check covers (#399).
#
# Run by 01_infrastructure.sh, after its own checks: the opt-in first-start check
# at the end removes the verify project's volumes. The query checks need an LLM
# (skipped with RAG_SKIP_SLOW=1); the restart and first-start checks are opt-in
# (RAG_ALLOW_RESTART=1) and never reach the live rag-docker project.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack
C="${PREFIX}ApiPaths"

RAG_PATHS_TMP=$(mktemp -d "${TMPDIR:-/tmp}/rag-paths.XXXXXX") || exit 2
export RAG_PATHS_TMP
# Also release the verify lock: this trap replaces the one lock.sh set.
trap 'rm -rf "$RAG_PATHS_TMP"; _rag_lock_release 2>/dev/null || true' EXIT

section "API paths: controlled (#399)"
# Through the app with Weaviate and Ollama mocked, in the API image with no network.
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_api_paths.py)
check "ingest, query, retrieval-config, metrics and health paths through the app" $?
python3 "$REPO_ROOT/scripts/tests/test_ollama_entrypoint.py"
check "the Ollama entrypoint skips present models, pulls under a timeout and retries" $?

section "API paths: ingest errors and schema rules"
drop_collection "$C"; make_collection "$C"
printf 'not a supported type\n' > "$RAG_PATHS_TMP/notes.xyz"
_rag_require_lock_owner
code=$(curl -s -m 120 -o "$RAG_PATHS_TMP/unsupported.json" -w '%{http_code}' -X POST "$API/ingest/upload" \
  -F "collection=$C" -F "files=@$RAG_PATHS_TMP/notes.xyz")
check_eq "an upload with no supported file answers 400" "$code" "400"
check_eq "it carries NO_SUPPORTED_FILES" "$(jfield "['error']['code']" < "$RAG_PATHS_TMP/unsupported.json")" "NO_SUPPORTED_FILES"
check_eq "its message lists what was skipped" "$(jfield "['error']['message']" < "$RAG_PATHS_TMP/unsupported.json")" \
  "No supported files found in upload. Skipped: notes.xyz (unsupported type '.xyz')"

_rag_require_lock_owner
code=$(curl -s -m 120 -o "$RAG_PATHS_TMP/nojob.json" -w '%{http_code}' "$API/ingest/job/vfy-no-such-job")
check_eq "an unknown ingest job answers 404" "$code" "404"
check_eq "it carries JOB_NOT_FOUND and names the job" \
  "$(jfield "['error']['code'] + ' ' + d['error']['message']" < "$RAG_PATHS_TMP/nojob.json")" \
  "JOB_NOT_FOUND Job 'vfy-no-such-job' not found."

# The schema as Weaviate holds it, and an insert carrying a property it doesn't declare.
(cd "$REPO_ROOT" && docker compose exec -T api python - "$C" <<'ENDPY'
import json, sys
from services import weaviate_client as wc
name = sys.argv[1]
client = wc.get_client()
try:
    collection = client.collections.get(name)
    properties = {p.name: p for p in collection.config.get().properties}
    out = {"searchable": {n: properties[n].index_searchable
                          for n in ("content", "source_file", "source_type", "chunk_strategy")}}
    try:
        collection.data.insert(properties={"content": "undeclared property check",
                                           "undeclared_prop": "x"},
                               vector=[0.01] * 768)
        out["insert"] = "accepted"
    except Exception as exc:                          # noqa: BLE001
        out["insert"] = "refused"
    out["declared"] = "undeclared_prop" in {p.name for p in collection.config.get().properties}
    out["objects"] = collection.aggregate.over_all(total_count=True).total_count
    print(json.dumps(out))
finally:
    wc.close_client()
ENDPY
) > "$RAG_PATHS_TMP/schema.json"
check_eq "BM25 is off for source_file, source_type and chunk_strategy, on for content" \
  "$(jfield "['searchable']" < "$RAG_PATHS_TMP/schema.json")" \
  "{'content': True, 'source_file': False, 'source_type': False, 'chunk_strategy': False}"
check_eq "an insert with an undeclared property is refused, adding no property or object" \
  "$(jfield "['insert'] + ' ' + str(d['declared']) + ' ' + str(d['objects'])" < "$RAG_PATHS_TMP/schema.json")" \
  "refused False 0"

section "API paths: proxy timeout and metrics route"
# The configuration the running proxy loaded, not only the file.
(cd "$REPO_ROOT" && docker compose exec -T proxy nginx -T 2>/dev/null) > "$RAG_PATHS_TMP/nginx.txt"
python3 - "$RAG_PATHS_TMP/nginx.txt" <<'ENDPY'
import re, sys
text = open(sys.argv[1]).read()
start = text.index("location /api/ {")
block = text[start:text.index("}", start)]
ok = (re.search(r"^\s*proxy_read_timeout\s+300s;", block, re.M)
      and re.search(r"^\s*proxy_connect_timeout\s+10s;", block, re.M))
sys.exit(0 if ok else 1)
ENDPY
check "the proxy's /api/ location reads for 300 s and connects for 10 s" $?

_rag_require_lock_owner
code=$(curl -s -m 120 -o "$RAG_PATHS_TMP/latency.json" -w '%{http_code}' "$API/metrics/latency?history_limit=0")
check_eq "GET /metrics/latency answers 200" "$code" "200"
python3 - "$RAG_PATHS_TMP/latency.json" <<'ENDPY'
import json, sys
d = json.load(open(sys.argv[1]))
stats = ("p50", "p95", "p99", "mean", "count")
ok = (set(d) == {"total_records", "retrieval_latency", "llm_latency", "total_latency", "history"}
      and all(set(d[k]) == set(stats) for k in ("retrieval_latency", "llm_latency", "total_latency"))
      and d["history"] == [])
sys.exit(0 if ok else 1)
ENDPY
check "it reports the three latency summaries, and history_limit=0 an empty history" $?
_rag_require_lock_owner
code=$(curl -s -m 120 -o "$RAG_PATHS_TMP/latency501.json" -w '%{http_code}' "$API/metrics/latency?history_limit=501")
check_eq "history_limit above 500 answers 422 INVALID_PARAMETER" \
  "$code $(jfield "['error']['code']" < "$RAG_PATHS_TMP/latency501.json")" "422 INVALID_PARAMETER"

section "API paths: a query with the defaults (LLM)"
queried=0
if [ "$SKIP_SLOW" = "1" ]; then
  skip "a default query and its metric" "set RAG_SKIP_SLOW=0 to include model calls"
else
  python3 - "$RAG_PATHS_TMP/overtime.txt" <<'ENDPY'
import sys
topics = ["overtime approval", "holiday requests", "expense claims", "travel booking",
          "remote work", "sick leave", "training budgets", "equipment loans",
          "parking permits", "visitor badges", "parental leave", "jury duty"]
with open(sys.argv[1], "w") as f:
    for t in topics:
        f.write(f"The policy on {t} says a manager must approve every request about {t} "
                f"before the end of the month, and the HR team records the decision.\n\n")
ENDPY
  _rag_require_lock_owner
  curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
    -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$RAG_PATHS_TMP/overtime.txt" > "$RAG_PATHS_TMP/ingest.json"
  job=$(jfield "['job_id']" < "$RAG_PATHS_TMP/ingest.json")
  check_eq "a text of more than five chunks is ingested" "$(wait_for_job "/ingest/job/$job" 900)" "completed"
  api_post "/query" "{\"question\":\"who approves overtime?\",\"collection\":\"$C\"}" > "$RAG_PATHS_TMP/answer.json"
  check_eq "a query with only question and collection retrieves the default 5 chunks, without citations" \
    "$(jfield "['chunks_retrieved'], d['citations'], bool(d['answer'].strip())" < "$RAG_PATHS_TMP/answer.json")" \
    "5 None True"
  api_get "/metrics/latency?collection=$C" > "$RAG_PATHS_TMP/latency_c.json"
  python3 - "$RAG_PATHS_TMP/latency_c.json" "$RAG_PATHS_TMP/answer.json" <<'ENDPY'
import json, sys
d, a = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
h = d["history"]
ok = (d["total_records"] == 1 and len(h) == 1 and h[0]["retrieval_mode"] == "hnsw"
      and h[0]["retrieval_ms"] == a["retrieval_latency_ms"] and h[0]["llm_ms"] == a["llm_latency_ms"]
      and h[0]["total_ms"] == a["retrieval_latency_ms"] + a["llm_latency_ms"])
sys.exit(0 if ok else 1)
ENDPY
  check "the query recorded one hnsw metric with its latencies" $? "$(cat "$RAG_PATHS_TMP/latency_c.json")"
  queried=1
fi

section "API paths: restart and first start (opt-in)"
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  restart_refusal=$(restart_refusal_reason)
  # The limit is checked before anything restarts (#130).
  restart_limit=$(restart_limit); restart_limit_ok=$?
fi
if [ "${RAG_ALLOW_RESTART:-0}" != "1" ]; then
  skip "retrieval settings and metrics across a restart, and the first start" "set RAG_ALLOW_RESTART=1 to include them"
elif [ -n "$restart_refusal" ]; then
  check "retrieval settings and metrics across a restart, and the first start" 1 "$restart_refusal"
elif [ "$restart_limit_ok" != 0 ]; then
  check "retrieval settings and metrics across a restart, and the first start" 1 "$restart_limit"
else
  project="$COMPOSE_PROJECT_NAME"
  saved="{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":7,\"alpha\":0.3,\"ef\":null,\"response_format\":\"engineer\"}"
  api_post "/retrieval/config" "$saved" > /dev/null
  api_get "/retrieval/config/$C" > "$RAG_PATHS_TMP/retrieval_before.json"
  [ "$queried" = 1 ] && api_get "/metrics/latency?collection=$C" > "$RAG_PATHS_TMP/metrics_before.json"
  started=$(python3 -c "import time;print(time.time())")
  (cd "$REPO_ROOT" && docker compose -p "$project" down >/dev/null 2>&1 && docker compose -p "$project" up -d >/dev/null 2>&1)
  elapsed=$(wait_healthy_timed "$started" $((restart_limit * 2)))
  if [ -z "$elapsed" ]; then
    check "the API is healthy again after the restart" 1 "not healthy after $((restart_limit * 2))s"
  else
    api_get "/retrieval/config/$C" > "$RAG_PATHS_TMP/retrieval_after.json"
    python3 - "$RAG_PATHS_TMP/retrieval_before.json" "$RAG_PATHS_TMP/retrieval_after.json" <<'ENDPY'
import json, sys
before, after = (json.load(open(p)) for p in sys.argv[1:3])
want = {"retrieval_mode": "hybrid", "top_k": 7, "alpha": 0.3, "ef": None,
        "response_format": "engineer", "is_default": False}
ok = all(before.get(k) == v for k, v in want.items()) and after == before
sys.exit(0 if ok else 1)
ENDPY
    check "saved retrieval settings survive an API restart" $? "$(cat "$RAG_PATHS_TMP/retrieval_after.json")"
    if [ "$queried" = 1 ]; then
      api_get "/metrics/latency?collection=$C" > "$RAG_PATHS_TMP/metrics_after.json"
      python3 - "$RAG_PATHS_TMP/metrics_before.json" "$RAG_PATHS_TMP/metrics_after.json" <<'ENDPY'
import json, sys
before, after = (json.load(open(p)) for p in sys.argv[1:3])
sys.exit(0 if before["total_records"] == 1 and after["history"] == before["history"] else 1)
ENDPY
      check "the query's metric survives an API restart" $? "$(cat "$RAG_PATHS_TMP/metrics_after.json")"
    else
      skip "the query's metric across a restart" "set RAG_SKIP_SLOW=0 to include model calls"
    fi

    # First start: no volumes but the model copy, which is external and kept by
    # `down -v`, so the models are already present, as SPECIFICATIONS.md's
    # criterion says; the pull from empty is a recorded manual measurement (#465).
    # Only on the verify project: elsewhere `down -v` would remove every volume.
    removal_refusal=$(volume_removal_refusal_reason)
    if [ -n "$removal_refusal" ]; then
      check "a first start from empty volumes brings every service healthy within 300s" 1 "$removal_refusal"
    else
      (cd "$REPO_ROOT" && docker compose -p "$project" down -v >/dev/null 2>&1)
      started=$(python3 -c "import time;print(time.time())")
      (cd "$REPO_ROOT" && docker compose -p "$project" up -d --wait --wait-timeout 600 >/dev/null 2>&1)
      up_ok=$?
      took=$(python3 -c "import time;print(int(time.time()-$started))")
      [ "$up_ok" = 0 ] && [ "$took" -le 300 ]
      check "a first start from empty volumes brings every service healthy within 300s (took ${took}s)" $? \
        "$([ "$up_ok" = 0 ] || printf 'not healthy after 600s')"
      wait_healthy_timed "$(python3 -c "import time;print(time.time())")" 60 > /dev/null
      # It started from nothing: the collection made above is gone with its volume.
      check_eq "that first start began with no collections" \
        "$(api_get "/collections" | jfield "['collections']")" "[]"
    fi
  fi
fi

drop_collection "$C"
summary
