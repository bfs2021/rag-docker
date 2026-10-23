#!/usr/bin/env bash
# Settings validation through the running API; the helper owns its collection.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
proxy_bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$proxy_bindings" || exit 2
require_stack
section "Concurrent settings publication and failed-write preservation"
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - SettingsPersistenceTests < scripts/tests/test_settings_persistence.py)
check "controlled retrieval/ingest concurrency and publication failures" $?
(cd ../.. && python3 scripts/tests/test_settings_implementation.py)
check "settings embedded implementation copies match runtime" $?
section "Settings refused before work, backends mocked (#386)"
(cd ../.. && docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_settings_validation.py)
check "test_settings_validation.py registered regression checks" $?
section "Settings validation before work"
RAG_API="$API" python3 ./settings_validation.py
check "live settings validation and owned-fixture cleanup" $?
summary
