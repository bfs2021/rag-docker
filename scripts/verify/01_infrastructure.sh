#!/usr/bin/env bash
# SPECIFICATIONS.md §10.5 — Infrastructure
#
# The restart and first-run timing checks are disruptive, so they are opt-in.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack
C="${PREFIX}Infra"

section "§10.5 Infrastructure"
# Synthetic loopback receiver inside an isolated container; no collector or
# external network is needed and no request payload from the stack is captured.
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_telemetry.py)
check "OTel configuration, safe OTLP export and bounded lifecycle" $?
python3 "$REPO_ROOT/scripts/tests/test_telemetry_implementation.py"
check "OTel embedded implementation stays synchronized" $?

RAG_INFRA_TMP=$(mktemp -d "${TMPDIR:-/tmp}/rag-infra.XXXXXX") || exit 2
export RAG_INFRA_TMP
# Also release the verify lock: this trap replaces the one lock.sh set.
trap 'rm -rf "$RAG_INFRA_TMP"; _rag_lock_release 2>/dev/null || true' EXIT
EXPECTED_PORT="${RAG_EXPECTED_PROXY_PORT:-8080}"
export EXPECTED_PORT
engine=$(docker version --format '{{.Server.Version}}')
python3 - "$engine" <<'ENDPY'
import re, sys
version = sys.argv[1]
match = re.match(r'^(\d+)\.(\d+)\.(\d+)', version)
ok = bool(match and tuple(map(int, match.groups())) >= (28, 0, 0)
          and not (version.startswith('28.0.0-') and any(x in version for x in ('alpha', 'beta', 'rc'))))
sys.exit(0 if ok else 1)
ENDPY
check "Docker Engine is 28.0.0 or newer for localhost port isolation" $? "$engine"

# ── resolved configuration and live bindings ─────────────────────────────────
# Inspect structured ports, including their host addresses. Matching only
# 0.0.0.0 made a loopback deployment look as though it published no ports.
(cd "$REPO_ROOT" && docker compose config --format json) > "$RAG_INFRA_TMP/vfy_compose.json"
python3 - <<'ENDPY'
import json, sys, os
services = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_compose.json'))['services']
ports = [(name, port) for name, service in services.items() for port in service.get('ports', [])]
ok = (len(ports) == 1 and ports[0][0] == 'proxy'
      and ports[0][1].get('host_ip') == '127.0.0.1'
      and str(ports[0][1]['published']) == os.environ['EXPECTED_PORT']
      and ports[0][1]['target'] == 80 and ports[0][1].get('protocol', 'tcp') == 'tcp')
sys.exit(0 if ok else 1)
ENDPY
check "resolved Compose publishes only the proxy on host loopback" $?

# ── cross-origin access, as SECURITY.md describes it ─────────────────────────
# SECURITY.md says a web page open in a browser on this machine can call the
# API, because CORS allows any origin (#25 tracks tightening it). Check that
# this is still true, so the policy and the code can't drift apart silently:
# when #25 changes CORS, this check and SECURITY.md change together.
cors=$(curl -s -D - -o /dev/null -m 10 -H "Origin: http://other.example" "$API/health" \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
check_eq "the API allows any origin, as SECURITY.md describes (#25)" "$cors" "*"

(cd "$REPO_ROOT" && python3 - <<'ENDPY'
import json, subprocess
ids = subprocess.check_output(['docker', 'compose', 'ps', '-q'], text=True).split()
containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids], text=True)) if ids else []
bindings = [{'service': container['Config']['Labels']['com.docker.compose.service'],
             'container_port': port, **binding}
            for container in containers
            for port, published in container['NetworkSettings']['Ports'].items()
            for binding in (published or [])]
print(json.dumps(bindings))
ENDPY
) > "$RAG_INFRA_TMP/vfy_bindings.json"
count=$(python3 -c "import json, os; print(len(json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_bindings.json'))))")
check_eq "only one port is published to the host" "$count" "1"
python3 - <<'ENDPY'
import json, sys, os
bindings = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_bindings.json'))
ok = (len(bindings) == 1 and bindings[0]['service'] == 'proxy'
      and bindings[0]['container_port'] == '80/tcp' and bindings[0]['HostIp'] == '127.0.0.1'
      and bindings[0]['HostPort'] == os.environ['EXPECTED_PORT'])
sys.exit(0 if ok else 1)
ENDPY
check "the live proxy port is bound only to host loopback" $?

for svc in api weaviate; do
  published=$( (cd "$REPO_ROOT" && docker compose ps --format "{{.Service}}|{{.Ports}}") \
    | grep "^$svc|" | grep -c -- '->[0-9]*/tcp' || true)
  check_eq "$svc publishes nothing to the host" "$published" "0"
done

# ── five services, all reporting healthy where a healthcheck exists ──────────
running=$( (cd "$REPO_ROOT" && docker compose ps --services --filter status=running) | grep -c .)
check_eq "five services are running" "$running" "5"
unhealthy=$( (cd "$REPO_ROOT" && docker compose ps --format '{{.Status}}') | grep -c 'unhealthy' || true)
check_eq "no service reports unhealthy" "$unhealthy" "0"

# ── health endpoint reports each dependency ──────────────────────────────────
api_get "/health" > "$RAG_INFRA_TMP/vfy_health.json"
check_eq "health status is ok" "$(jfield "['status']" < "$RAG_INFRA_TMP/vfy_health.json")" "ok"
python3 -c "
import json,sys,os; d=json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_health.json'))
s=d['services']
ok = (s['weaviate']['status']=='ok' and s['ollama']['llm']['status']=='ok'
      and s['ollama']['embed']['status']=='ok'
      and all(v['latency_ms'] >= 0 for v in (s['weaviate'], s['ollama']['llm'], s['ollama']['embed'])))
sys.exit(0 if ok else 1)"
check "per-service status and latency are reported" $?

# ── memory resource reporting (Issue #98) ────────────────────────────────────
# Advisory only (see the comment in api/routers/health.py), so it never
# affects overall_ok, but the report itself must be right: the configured
# recommendation, and a flip to below_recommended -- with a note -- once the
# recommendation exceeds what's actually allocated.
check_eq "the recommended minimum defaults to 12.0 GB" \
  "$(jfield "['resources']['memory']['recommended_minimum_gb']" < "$RAG_INFRA_TMP/vfy_health.json")" "12.0"
# Whether this machine meets it depends on its Docker allocation, so check the
# status agrees with the allocation /health reports, not that it is 'ok'.
python3 -c "
import json, os
m = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_health.json'))['resources']['memory']
want = 'ok' if m['allocated_gb'] >= m['recommended_minimum_gb'] * 0.95 else 'below_recommended'
raise SystemExit(0 if m['status'] == want else 1)"
check "the memory status agrees with the reported allocation" $?

# Raise the recommendation past the actual allocation (in-process only --
# neither the running server nor docker-compose.yml is touched) and confirm
# the status flips and a note is attached.
(cd "$REPO_ROOT" && docker compose exec -T api env RECOMMENDED_MEMORY_GB=13 python3 -c "
import json
from config import Settings
from services import system_info
print(json.dumps(system_info.memory_info(Settings().recommended_memory_gb)))
") > "$RAG_INFRA_TMP/vfy_mem_override.json"
check_eq "a recommendation above the allocation reports below_recommended" \
  "$(jfield "['status']" < "$RAG_INFRA_TMP/vfy_mem_override.json")" "below_recommended"
python3 -c "
import json, os
d = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_mem_override.json'))
raise SystemExit(0 if d.get('note') and 'recommended' in d['note'] else 1)"
check "the below-recommended report includes an explanatory note" $?

# ── ingest config defaults ───────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
check_eq "a fresh collection reports is_default true" \
  "$(api_get "/ingest/config/$C" | jfield "['is_default']")" "True"
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":800,\"chunk_overlap\":150,\"similarity_threshold\":0.9,\"min_chunk_size\":80}" >/dev/null
api_get "/ingest/config/$C" > "$RAG_INFRA_TMP/vfy_cfg.json"
check_eq "after saving, is_default is false" "$(jfield "['is_default']" < "$RAG_INFRA_TMP/vfy_cfg.json")" "False"
check_eq "the saved strategy is returned" "$(jfield "['chunking_strategy']" < "$RAG_INFRA_TMP/vfy_cfg.json")" "semantic"

# ── deleting a collection must take its configs with it ──────────────────────
# Spec §8 rule 1. This was not happening for the ingest config, so a recreated
# collection silently inherited chunking settings the user never chose.
drop_collection "$C"
make_collection "$C"
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":900,\"chunk_overlap\":100,\"similarity_threshold\":0.9,\"min_chunk_size\":70}" >/dev/null
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":9,\"alpha\":0.4,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
drop_collection "$C"
make_collection "$C"
check_eq "a recreated collection does not inherit the ingest config" \
  "$(api_get "/ingest/config/$C" | jfield "['is_default']")" "True"
check_eq "a recreated collection does not inherit the retrieval config" \
  "$(api_get "/retrieval/config/$C" | jfield "['is_default']")" "True"

# ── persistence across a restart (opt-in: it stops the stack) ────────────────
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  restart_refusal=$(restart_refusal_reason)
  # The limit is checked before anything restarts (#130).
  restart_limit=$(restart_limit); restart_limit_ok=$?
fi
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ] && [ -n "$restart_refusal" ]; then
  check "restart, persistence and timing" 1 "$restart_refusal"
elif [ "${RAG_ALLOW_RESTART:-0}" = "1" ] && [ "$restart_limit_ok" != 0 ]; then
  check "restart, persistence and timing" 1 "$restart_limit"
elif [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  # Save a known config here, right before the restart: the section above ends
  # by recreating $C with no saved config, so relying on earlier state made
  # this check fail on every run (#73).
  api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":800,\"chunk_overlap\":150,\"similarity_threshold\":0.9,\"min_chunk_size\":80}" >/dev/null
  check_eq "a config saved before the restart reads back" \
    "$(api_get "/ingest/config/$C" | jfield "['chunking_strategy']")" "semantic"
  api_get "/collections" > "$RAG_INFRA_TMP/vfy_before.json"
  started=$(python3 -c "import time;print(time.time())")
  # Name the project explicitly; restart_refusal_reason has already made sure
  # it is set and is not the live rag-docker project.
  project="$COMPOSE_PROJECT_NAME"
  (cd "$REPO_ROOT" && docker compose -p "$project" down >/dev/null 2>&1 && docker compose -p "$project" up -d >/dev/null 2>&1)
  # Wait up to twice the limit, so a slow restart is still timed (#130).
  elapsed=$(wait_healthy_timed "$started" $((restart_limit * 2)))
  restart_timing_check "$restart_limit" "$elapsed"
  api_get "/collections" > "$RAG_INFRA_TMP/vfy_after.json"
  python3 -c "
import json,sys,os
b={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_before.json'))['collections']}
a={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_after.json'))['collections']}
ok = set(b)==set(a) and all(b[k]['object_count']==a[k]['object_count'] for k in b)
sys.exit(0 if ok else 1)"
  check "Weaviate data survives down/up" $?
  python3 -c "
import json,sys,os
b={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_before.json'))['collections']}
a={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_after.json'))['collections']}
sys.exit(0 if all(b[k]['created_at']==a[k]['created_at'] for k in b if k in a) else 1)"
  check "created_at is preserved across restart" $?
  api_get "/ingest/config/$C" > "$RAG_INFRA_TMP/vfy_cfg_after.json"
  check_eq "saved ingest config survives restart" \
    "$(jfield "['chunking_strategy']" < "$RAG_INFRA_TMP/vfy_cfg_after.json")" "semantic"
  check_eq "it is still the saved config, not the default" \
    "$(jfield "['is_default']" < "$RAG_INFRA_TMP/vfy_cfg_after.json")" "False"
else
  skip "restart, persistence and timing" "set RAG_ALLOW_RESTART=1 to include them"
fi

# ── restart from a Raft snapshot (issue #178; opt-in: it stops the stack) ────
# The restart above comes too early for a snapshot: few schema changes, under
# a minute of uptime. Here Weaviate takes one, a tail of changes follows it,
# and a down/up must restore both: collections and objects from before the
# snapshot, a create and a delete after it.
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ] && [ -z "$restart_refusal" ] && [ "$restart_limit_ok" = 0 ]; then
  SNAP_KEEP="${C}SnapKeep"; SNAP_GONE="${C}SnapGone"; SNAP_TAIL="${C}SnapTail"
  for n in "$SNAP_KEEP" "$SNAP_GONE" "$SNAP_TAIL"; do drop_collection "$n"; done
  # put_objects <collection> <count>: objects with their own vectors, so no
  # embedding call is involved.
  put_objects() {
    (cd "$REPO_ROOT" && docker compose exec -T api python - "$1" "$2" <<'ENDPY'
import sys
from weaviate.classes.data import DataObject
from services import weaviate_client as wc
name, count = sys.argv[1], int(sys.argv[2])
try:
    result = wc.get_client().collections.get(name).data.insert_many([
        DataObject(properties={"content": f"snapshot check {i}", "chunk_index": i},
                   vector=[(i + 1) / (j + 1) for j in range(768)])
        for i in range(count)])
    sys.exit(1 if result.has_errors else 0)
finally:
    wc.close_client()
ENDPY
    ) >/dev/null 2>&1
  }
  snapshots() { (cd "$REPO_ROOT" && docker compose exec -T weaviate ls /var/lib/weaviate/raft/snapshots) 2>/dev/null | sort; }
  make_collection "$SNAP_KEEP"; put_objects "$SNAP_KEEP" 5; keep_ok=$?
  make_collection "$SNAP_GONE"; put_objects "$SNAP_GONE" 3; gone_ok=$?
  [ "$keep_ok$gone_ok" = 00 ]
  check "objects written before the snapshot" $?
  before_snaps=$(snapshots)
  # 70 create/delete pairs: 140 Raft entries, over the threshold of 128.
  (cd "$REPO_ROOT" && docker compose exec -T api python - "${C}Churn" <<'ENDPY'
import sys
from services import weaviate_client as wc
client = wc.get_client()
try:
    for i in range(70):
        client.collections.create(f"{sys.argv[1]}{i}")
        client.collections.delete(f"{sys.argv[1]}{i}")
finally:
    wc.close_client()
ENDPY
  ) >/dev/null 2>&1
  check "140 schema changes made" $?
  # The interval is checked every 30-60s; allow 150s.
  new_snap=""
  for _ in $(seq 1 30); do
    new_snap=$(comm -13 <(printf '%s\n' "$before_snaps") <(snapshots) | grep . | tail -1)
    [ -n "$new_snap" ] && break
    sleep 5
  done
  [ -n "$new_snap" ]
  check "Weaviate snapshots its Raft log after 140 schema changes (within 150s)" $? "no new snapshot in /var/lib/weaviate/raft/snapshots"
  snap_index=$(printf '%s' "$new_snap" | cut -d- -f2)
  # The tail after the snapshot: one collection created, one deleted.
  make_collection "$SNAP_TAIL"; put_objects "$SNAP_TAIL" 4
  check "objects written after the snapshot" $?
  drop_collection "$SNAP_GONE"
  started=$(python3 -c "import time;print(time.time())")
  project="$COMPOSE_PROJECT_NAME"
  (cd "$REPO_ROOT" && docker compose -p "$project" down >/dev/null 2>&1 && docker compose -p "$project" up -d >/dev/null 2>&1)
  elapsed=$(wait_healthy_timed "$started" $((restart_limit * 2)))
  [ -n "$elapsed" ]
  check "healthy again after a restart from the snapshot (took ${elapsed:-unknown}s)" $? "not healthy after $((restart_limit * 2))s"
  # Weaviate logs the snapshot it started from on "raft node constructed".
  restored=$( (cd "$REPO_ROOT" && docker compose -p "$project" logs weaviate 2>/dev/null) | python3 -c "
import json, sys
for line in sys.stdin:
    _, _, body = line.partition('|')
    try:
        d = json.loads(body)
    except ValueError:
        continue
    if d.get('msg') == 'raft node constructed':
        print(d.get('last_snapshot_index', 0))")
  [ -n "$snap_index" ] && [ "${restored:-0}" -ge "$snap_index" ] 2>/dev/null
  check "the restart starts from the snapshot" $? "last_snapshot_index on start: ${restored:-none}, snapshot taken at: ${snap_index:-none}"
  api_get "/collections" | python3 -c "
import json, sys
a = {c['name']: c['object_count'] for c in json.load(sys.stdin)['collections']}
ok = (a.get('$SNAP_KEEP') == 5 and a.get('$SNAP_TAIL') == 4 and '$SNAP_GONE' not in a
      and not any(n.startswith('${C}Churn') for n in a))
print(a if not ok else '')
sys.exit(0 if ok else 1)" > "$RAG_INFRA_TMP/vfy_snap.txt"
  check "collections and objects before and after the snapshot survive the restart, deletes stay deleted" $? "$(cat "$RAG_INFRA_TMP/vfy_snap.txt")"
  for n in "$SNAP_KEEP" "$SNAP_TAIL"; do drop_collection "$n"; done
elif [ "${RAG_ALLOW_RESTART:-0}" != "1" ]; then
  skip "restart from a Raft snapshot" "set RAG_ALLOW_RESTART=1 to include it"
fi

# ── startup sweeps leave a clean instance alone ──────────────────────────────
leftover=$( (cd "$REPO_ROOT" && docker compose exec -T api sh -c \
  'ls -d /app/uploads/import-* /app/uploads/rechunk-* 2>/dev/null | wc -l') | tr -d ' ')
check_eq "no abandoned extraction directories" "${leftover:-0}" "0"
staging=$( (cd "$REPO_ROOT" && docker compose exec -T api python -c '
import json
from services import collection_recovery as recovery, weaviate_client as wc
try:
    records = [json.loads(path.read_text()) for path in recovery._root().glob("*.json")]
    print(sum(record.get("state") == "scratch" and wc.get_client().collections.exists(record["staging"])
              for record in records))
finally:
    wc.close_client()
'))
check_eq "no abandoned owned scratch collections" "$staging" "0"

drop_collection "$C"
cleanup_prefixed
summary
