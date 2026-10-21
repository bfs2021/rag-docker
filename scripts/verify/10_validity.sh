#!/usr/bin/env bash
# Retained-session validity and explicit historical export, without model calls.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Retained evaluation validity and historical export"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/session_validity.py)
check "legacy defaults, real stale/orphan markers, export guard and historical file" $?

section "Gold-standard export files (#370)"
# Controlled: save and download reach only UPLOAD_DIR/goldstandard_exports; bad
# names get 400 INVALID_PATH and touch nothing.
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_goldstandard_exports.py)
check "export names, folder, overwrite and refusals" $?
# Through the proxy: other upload state is never served.
for name in metrics.jsonl goldstandard_sessions report.txt; do
  check_eq "download of '$name' is refused with 400" "$(api_code "$API/goldstandard/download/$name")" "400"
done
check_eq "download of a valid unknown name is 404" "$(api_code "$API/goldstandard/download/definitely_not_here.json")" "404"
summary
