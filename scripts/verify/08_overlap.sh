#!/usr/bin/env bash
# Real parser/window/ingest/storage acceptance on an owned text-only fixture.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Overlap window and tuning-budget regressions"
# With the repository mounted, so its IMPLEMENTATION.md mirror check runs too.
(cd ../.. && docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_overlap_chunks.py)
check "test_overlap_chunks.py registered regression checks" $?
section "Bounded overlap text storage"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" -e RAG_OVERLAP_REAL_EMBEDDING="${RAG_OVERLAP_REAL_EMBEDDING:-0}" api python - < scripts/verify/overlap_chunks.py)
check "overlap coverage, bounds, budget rejection and owned-fixture cleanup" $?
summary
