#!/usr/bin/env bash
# UUID selection through the real SDK; owned fixtures and supplied vectors.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Sampling and generation guard regressions"
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_chunk_sampling.py)
check "sampling, publication and generation guard regressions" $?
section "Evaluation sampling across iterator pages"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/chunk_sampling.py)
check "seeded selection, iterator paging, bounds and owned-fixture cleanup" $?
section "Generation request validation before backend/model work"
for body in \
  '{"collection":"MissingSamplingFixture","sample_size":0}' \
  '{"collection":"MissingSamplingFixture","sample_size":101}' \
  '{"collection":"MissingSamplingFixture","sample_size":true}' \
  '{"collection":"MissingSamplingFixture","sample_size":1.5}' \
  '{"collection":"MissingSamplingFixture","seed":false}' \
  '{"collection":"MissingSamplingFixture","seed":1.5}' \
  '{"collection":"MissingSamplingFixture","seed":NaN}' \
  '{"collection":"MissingSamplingFixture","sample_size":Infinity}'
do
  code=$(api_post_code /goldstandard/generate "$body")
  check_eq "invalid sampling request rejected with 422: $body" "$code" "422"
done
section "Gold-standard API paths without a model (#398)"
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_goldstandard_api_paths.py)
check "empty-collection outcome, reply parser, 409 GENERATION_IN_PROGRESS, 502 PAIR_GENERATION_FAILED" $?
summary
