# Verification suite

Integration tests that run the acceptance criteria in `SPECIFICATIONS.md` §10
and `RAG_EXPORT_SPECIFICATIONS.md` §13 against a running stack: the disposable
verify project, never the live stack (#152).

```bash
bash scripts/verify/stack.sh run                    # everything, ~20 min plus start-up
RAG_SKIP_SLOW=1 bash scripts/verify/stack.sh run    # skip LLM work, ~8 min, start-up included
bash scripts/verify/stack.sh run 02 04              # only the named suites
```

Exits non-zero if any check fails.

## Entry point and verify project

`stack.sh` runs everything on compose project `rag-verify`, a disposable copy of
the stack built from a checkout. `stack.sh` never builds, starts or stops the
live `rag-docker` stack, and only reads its model volume, to copy the models.
That guards against accidents, not hostile code (see "What it doesn't protect
against" below).

| Command | What it does |
|---|---|
| `stack.sh run [--checkout DIR] [suite ...]` | `up`, then that checkout's `all.sh` (only the named suites, if given; `RAG_SKIP_SLOW` and `RAG_ALLOW_RESTART` pass through), then `down`, always, even on failure or Ctrl-C. Exits with `all.sh`'s status, or 2 if the project didn't come up. |
| `stack.sh up [--checkout DIR] [--pull]` | Brings the project up and leaves it running, then prints the `export` lines that point `docker compose` and the suites at it. `--pull` pulls newer base images for the build. |
| `stack.sh down` | Removes the project: its containers, network, volumes, images and exports folder. |

`--checkout` is the checkout to build and test; the default is the one holding
`stack.sh`. The overlay `docker-compose.verify.yml` and `stack.sh` itself always
come from the checkout holding `stack.sh`, so a reviewer can run a trusted copy
against someone else's checkout.

What makes it separate from the live stack:

- **Its own project and port.** Compose project `rag-verify`, publishing only
  the proxy, on `127.0.0.1:8081` (`RAG_VERIFY_PORT`; 8080 is refused). Its
  network, containers and volumes (`rag-verify_weaviate_data`, ...) are its
  own, and empty at every `up`.
- **Its own images.** `rag-verify-api:latest` and `rag-verify-ui:latest`, and
  `rag-verify-<service>:latest` for any other service that builds. The
  configuration check refuses a build that would write any other tag, so the
  build doesn't move the base file's `rag-docker-api` and `rag-docker-ui`
  tags, or a third-party tag the live stack runs. `down` removes every image
  the project built.
- **Its own exports folder.** `${TMPDIR:-/tmp}/rag-verify-<uid>/exports`,
  inside a folder of the user's own with mode 700 (a symlink there, or a
  folder owned by someone else, is refused). It is passed to the suites as
  `RAG_EXPORTS_DIR`, in place of the checkout's `./exports`.
- **A copy of the models.** The Ollama models are copied once from the live
  `rag-docker_ollama_models` into the volume `rag-verify-ollama-models` (about
  2.5 GB). Every `up` checks the copy against the live store by sha256 and
  repairs any difference. The live volume is mounted read-only, in a throwaway
  container with no network, and only for that copy. The copy is kept by
  `down`, so later runs don't copy again. With no live model volume, the copy
  is skipped and the verify project's Ollama pulls the models into it, which
  needs the internet.
- **A configuration check.** Before anything is built, `up` reads the resolved
  configuration (`docker compose config`) and checks it against an allow-list
  of what the base file and the overlay need (#154). It refuses any other
  top-level or service key (for example `volumes_from`, `network_mode`,
  `privileged`, `cap_add`, `devices`, `pid`, `secrets`, `configs`); a build
  with options other than a context and Dockerfile inside the checkout, or
  whose image isn't `rag-verify-<service>`; a `rag-docker-*` image, under any
  registry name; a network other than the project's own bridge network, or
  joined with options; a volume that isn't the project's own or the model
  copy; anything but the proxy published, on loopback at the verify port; a
  mount other than a volume or a read-only bind from inside the checkout
  (never its `exports` folder), apart from the api's own exports folder; and
  the Docker socket. It can't see `env_file`, which compose merges into the
  environment.
- **Start-up.** `up` tears down anything left from an earlier run first, then
  builds, then starts with `up --wait` (15 minutes), and tries once more if that
  fails (Weaviate can be slow to report healthy, #130). If it still fails, it
  prints the last log lines of each service that isn't up. `up` then leaves
  the project running for inspection (`down` removes it); `run` removes it.
- **No suite outlives its run (#184).** `run` starts `all.sh` in a process
  group of its own. Whenever `run` ends, whether normally, on a failing suite,
  or on INT or TERM (which now act at once), it first stops that whole group
  (TERM, then KILL after 5 seconds), so nothing the suites started is left
  running, and only then tears the project down and releases the lock. A
  `stack.sh` killed outright (SIGKILL) can't do that. Then the verify lock
  stops the orphaned suite instead: `lock.sh` trusts an inherited
  `RAG_VERIFY_LOCK_HELD` only while the lock's pid is that shell or one of its
  ancestors, and `lib.sh`'s API helpers (`api_get`, `api_post`, `api_code`,
  `api_post_code`, `drop_collection`, `make_collection` and every
  `wait_for_job` poll) repeat that check before each request and end the
  suite, exit 3, when it fails. Not re-checked: direct `curl` and
  `docker compose exec` calls in the suites, and the Python helpers, which
  end on their own timeouts (up to 900 s).

**Memory.** The verify project runs next to the live stack on the same Docker
VM. With 12 GB allocated, both fit while one LLM is loaded; when both stacks
answer LLM questions at once, each loads its own model (about 3 GB), so
expect slower answers or a reload rather than a failure.

**What it doesn't protect against.** The suites, and anything else a checkout
runs on the host, have full access to Docker. The verify project keeps
verification away from the live stack by accident, not from hostile code:
reviewing what a branch runs is the security review's job.

### The live stack is refused

`all.sh` and every suite (`NN_*.sh`) refuse to run, exit 2, before any request
or Docker command, when their target is the live stack: when the compose
project they would act on is `rag-docker` (from `COMPOSE_PROJECT_NAME`, or
the folder name when it's unset), or when `RAG_API` uses port 8080, which is
also its default. `stack.sh` sets both for the verify project.

`RAG_VERIFY_LIVE=1` overrides the refusal with a warning, for someone verifying
their own deployment on purpose. The documented commands and the PR review
never use it. Restarts never reach the live project, even with it: with
`RAG_ALLOW_RESTART=1`, the restart checks in `01_infrastructure.sh`,
`04_goldstandard.sh` and `05_transfer.sh` run only when `COMPOSE_PROJECT_NAME`
is set and isn't `rag-docker`; otherwise they fail with the reason and restart
nothing.

`python3 scripts/tests/test_verify_stack.py` checks `stack.sh`, the
configuration check and both refusals, with Docker replaced by a stub, so it
needs no stack.

## Settings publication regressions

With API dependencies installed, run
`python3 scripts/tests/test_settings_persistence.py` for deterministic ingest and
retrieval save contention, unique temporary paths, first-save directory races,
and failed serialization/create/write/close/replace preservation. The test also
checks affected embedded sources against `IMPLEMENTATION.md`. It uses disposable
directories and does not connect to Weaviate or Ollama.

`07_settings.sh` runs the controlled persistence cases inside the disposable
verify-project API container, then runs its HTTP validation and round-trip checks. This coverage complements the required full
verification run; it does not establish full-stack acceptance by itself.

Suite 04 also registers `test_chunk_sampling.py` through `09_sampling.sh`, including guard release before model generation and after sampling/publication failures.

Suite 07 also runs `scripts/tests/test_settings_implementation.py` on the host
(no API dependencies), and 15 rounds of 12 concurrent live saves per ingest and
retrieval route. The persistence cases cover failed import-config publication.

## Focused import validation regressions

Run `python3 scripts/tests/test_session_implementation.py` from the repository root to check that the embedded session/import/package service examples retain the current validated implementation.

`05_transfer.sh` registers the four controlled regressions (`test_session_import.py`, `test_source_index_boundary.py`, `test_retrieval_import.py` and `test_batch_recovery.py`) and the source-contract check, so `all.sh` runs them. Its E23 live checks use digest-valid synthetic packages to verify malformed metadata is refused before replacement and valid metadata is restored on rename. The retained-source boundary group checks import refusal before backend/model work, valid digest identity, and export refusal of unsafe paths and links.

`scripts/tests/test_session_import.py` exercises the real package reader and
evaluation persistence with disposable fixtures. Model and database mutation
seams are mocked; this complements the live transfer suite and does not prove
Weaviate/Ollama acceptance. Run it using the API image's pinned dependencies, on
the verify project: `bash scripts/verify/stack.sh up`, paste the `export` lines
it prints, then

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts/tests:/tests:ro" -e RAG_TEST_API_DIR=/app \
  api python /tests/test_session_import.py
```

and `bash scripts/verify/stack.sh down` when done. Without those exports,
`docker compose run` from the main checkout starts a container in the live
`rag-docker` project, with the live volumes mounted.

Alternatively, with `uv` on the host:

```bash
uv run --no-project --python 3.11 \
  --with pydantic-settings==2.15.0 --with pydantic==2.13.5 \
  --with httpx==0.28.1 --with weaviate-client==4.23.1 \
  python scripts/tests/test_session_import.py
```

`scripts/tests/test_retrieval_import.py` adds controlled digest-valid malformed
retrieval package rejection before model, backend, recovery, or sidecar mutation;
historical defaults/coercion and `ef` round trips; and generated Python literal
regressions. Run it with the same API dependencies as the session import test.
`05_transfer.sh` registers it and `retrieval_settings.py` (E28), which submits
15 malformed settings imports across abort/rename/replace and verifies live
collection counts and saved settings are unchanged. The normal rename import
also checks all saved retrieval fields round-trip. `legacy_retrieval.py` (E28,
#184) writes a legacy `ef` and other invalid saved settings into the API
container, then checks through the real API that a legacy `ef` exports and
imports as `null` with a warning or note, and that other invalid saved settings
fail the export early with an actionable error and publish nothing. Crafted
packages are written under a `.part` name and renamed into place, because on
Docker Desktop the API can read a freshly written bind-mounted file as empty. Controlled tests complement,
and do not replace, this live acceptance.

`scripts/tests/test_ingest_errors.py` (#290) checks that damaged or unreadable ZIPs are
refused as a whole with readable 400s naming their unreadable members (truncated archive,
wrong member offsets, bad CRC, also in a member that would be skipped, unsupported
compression, encrypted member, filesystem error), unexpected errors with a JSON 500 and a
logged traceback, JSON Lines skip reasons, and that gzipped JSON members are decompressed
within their limit with clear per-file errors. Weaviate and source retention are mocked. `02_ingest.sh` runs it
in the verify project's API container, then checks a truncated ZIP and a ZIP of
`.json` and `.json.gz` members through the proxy.

`scripts/tests/test_json_ingest.py` (#295) checks that ordinary JSON becomes readable
`key: value` text: dotted and indexed paths, scalar arrays on one line, file key order,
non-ASCII text and numbers as written, one element per top-level record, empty values and
files giving no chunks, element-schema files still rehydrated, and readable errors for
invalid JSON in `.json` files and `.json.gz` members. `02_ingest.sh` runs it in the API
container, then ingests a nested `plain.json` through the proxy and reads its stored chunks
back to confirm they hold that text.

`scripts/tests/test_settings_validation.py` checks that invalid settings are
refused before backend, model or staging work, with every backend mocked. Its
embedded-source check reads `IMPLEMENTATION.md`, so mount the whole repository
(on the verify project, with the exports from `stack.sh up`, as above):

```bash
docker compose run --rm --no-deps -v "$PWD:/repo:ro" -w /repo \
  api python scripts/tests/test_settings_validation.py
```

With only `scripts/tests` mounted (as for `test_session_import.py` above) the
other tests still run and the embedded-source check is skipped.

## Why integration tests

Every defect this project has actually produced was invisible to a unit test of
the same code:

- `.md` files could never be ingested — the `markdown` package was missing from
  the image, so `unstructured.partition.md` failed at import time. The function
  was correct.
- `PATCH /goldstandard/.../pair/...` returned 200 and silently rewrote approved
  content. The handler did exactly what it was written to do.
- Explicit vectors survive Weaviate's vectorizer — the entire import design
  rests on that, and only the database can confirm it.
- A recreated collection inherited a deleted one's chunking settings, because
  deletion removed two of the three things it should have.

These tests talk to the running API, the real Weaviate and the real model, and
drive the UI in a real browser.

## Batch faults and recovery across restart

Run the controlled regressions with the API dependencies installed:

```bash
python -m unittest discover -s scripts/tests -p 'test_batch*.py'
```

`05_transfer.sh` runs `test_batch_recovery.py` in the API container, so `all.sh` covers it; `test_batch_implementation.py` is run by hand.

`batch_recovery.py` is the fault acceptance. It uses real Weaviate, synthetic
collections and the real embedding model. It injects final-create failures into
the test process, retains import/tuning recovery, restarts the API, then verifies
exact UUIDs, properties, vectors and sources. It also checks real completed-batch
rejection/partial acceptance, ingestion UUID rollback after a post-write read fault,
resumption of metadata cleanup after backend deletion, owned scratch cleanup and
retention of an unowned marker-like collection. It must not run against a
user's data stack: run it on the verify project.

`05_transfer.sh` runs it when `RAG_ALLOW_RESTART=1`: prepare, `docker compose
restart api`, check, then cleanup. It creates and deletes only the
`${RAG_TEST_PREFIX}BatchRecovery*` collections, archive and package folder it
records in its state file. The restart ends any job in progress on the stack.
Cleanup always runs, even after a failed check; the phase logs stay in
`/tmp/vfy_recovery_prepare.log`, `/tmp/vfy_recovery_check.log` and
`/tmp/vfy_recovery_cleanup.log`. The script accepts only a prefix starting with
`Vfy` (a guard on its destructive phases), so with any other `RAG_TEST_PREFIX`
the suite skips these checks and says why.

To run the phases by hand on a disposable stack:

```bash
bash scripts/verify/stack.sh up      # then paste the export lines it prints
docker compose exec -T api python - prepare < scripts/verify/batch_recovery.py
docker compose restart api
# Wait for /api/health to report healthy before the next phase.
docker compose exec -T api python - check < scripts/verify/batch_recovery.py
docker compose exec -T api python - cleanup < scripts/verify/batch_recovery.py
bash scripts/verify/stack.sh down
```

In a manual run, if interrupted, keep the recorded fixtures and run `check` after
restarting; run `cleanup` only after inspecting the result. Recovery journal and sidecar snapshots
live under `UPLOAD_DIR/collection_operations`, outside extraction workspaces.

## Layout

| File | Covers |
|---|---|
| `all.sh` | entry point; runs the suites and aggregates |
| `lib.sh` | shared helpers: checks, job polling, cleanup |
| `07_settings.sh` | registered live settings validation suite; invokes the standalone helper |
| `settings_validation.py` | standalone, on the verify project after `stack.sh up`: `RAG_API=http://localhost:8081/api python3 scripts/verify/settings_validation.py`; invalid settings, valid defaults and saved round trips on a unique disposable collection; no LLM work |
| `lock.sh` | one verify run at a time: a second `all.sh` or suite exits 3 while another is running, because runs share collection names, scratch files and fixtures. An inherited lock is trusted only while its holder is an ancestor, so a suite orphaned by a dead run exits 3 instead of acting on a later run's project (#184). Tested by `scripts/tests/test_verify_lock.sh` |
| `fixtures.py` | the test corpus — six file types plus edge cases, stdlib only |
| `01_infrastructure.sh` | §10.5 — ports, health, config lifecycle, startup sweeps |
| `02_ingest.sh` | §10.1 — six types, ZIP, unreadable archives and `.json.gz` members, ordinary JSON text, five strategies, merge rule, partial failure |
| `03_query.sh` | §10.2 — four retrieval modes, citations, latencies, answer style |
| `08_overlap.sh` | called by suite02 (and thus all.sh); real parser/ingest/Weaviate text-storage check on an owned fixture with vectorization disabled; optional `RAG_OVERLAP_REAL_EMBEDDING=1` model acceptance |
| `overlap_chunks.py` | helper for suite08; asserts nonempty text/windows, exact coverage/overlap, tail bounds and pre-storage output limits |
| `04_goldstandard.sh` | §10.3 — generation, the 409 and 422 guards, export schema |
| `05_transfer.sh` | export/import/tuning — E5–E20, E23 and E26–E30; destructive replace fidelity, live metadata and model checks, controlled regressions and source drift |
| `../tests/test_ingest_errors.py` | controlled upload-unpacking error and `.json.gz` member regressions, registered by ingest |
| `../tests/test_json_ingest.py` | controlled ordinary-JSON conversion regressions, registered by ingest |
| `../tests/test_session_import.py` | controlled import/persistence/generation regressions, registered by transfer |
| `../tests/test_source_index_boundary.py` | controlled source-index identity, early import refusal and export read-boundary regressions, registered by transfer |
| `../tests/test_batch_recovery.py` | controlled writer, import and tuning recovery regressions, registered by transfer |
| `../tests/test_session_implementation.py` | exact embedded source checks, registered by transfer |
| `06_ui.sh` + `browser/` | §10.4 — roles, gating, explainer, delete guard, help page |
| `14_reindex.sh` | exact-record reindex: 24 record/cutover/vectorizer/concurrency cases, fourteen writer/import/recovery cases, four async lifecycle/parent-cleanup cases, two polling-deadline cases and 44 real Weaviate/handler/restart checks with a refused embedding endpoint; run by `05_transfer.sh` |
| `reindex_cases.py` / `reindex.py` | owned controlled cases / actual backend and ASGI job handlers; only scoped synthetic fixtures, canonical local session IDs and exact successful-creation ownership. Verifier collection names deliberately lie outside the parent prefix-sweep namespace. Polling is bounded to 300s, cleanup settlement to 30s; a still-active job reports its ID/status and preserves a durable exact-name fixture receipt/directory before standalone exit; inspection must confirm terminal writer state before exact-name cleanup |
| `compose_target.py` | refuses a remote or mismatched API/Compose target before the new acceptance suite runs |
| `model_integrity.py` | bundled-model byte checks with the pulled embedding model, using a temporary package/store |
| `validate_package.py` | one export package against `RAG_EXPORT_SPECIFICATIONS.md` §4 |
| `09_sampling.sh` | called by suite04 (and thus all.sh), including slow-skip runs; owned synthetic UUID selection without model calls |
| `chunk_sampling.py` | standalone inside disposable API: `python - < scripts/verify/chunk_sampling.py`; real SDK seeded selection on owned synthetic UUIDs with supplied vectors, no model calls |

`10_validity.sh` is called by suite05 (and thus all.sh). It runs
`session_validity.py` inside the disposable API, using an owned real collection,
synthetic retained pairs and in-process HTTP without startup sweeps or model calls.
It checks warning metadata, actual deletion marking and explicit historical export.

## Environment

| Variable | Default | Effect |
|---|---|---|
| `RAG_VERIFY_PORT` | `8081` | `stack.sh`: the verify project's host port; 8080 is refused |
| `RAG_EXPECTED_PROXY_PORT` | `8080` | expected resolved/live proxy host port; `stack.sh` sets it to the verify port |
| `RAG_API` | `http://localhost:8080/api` | where the API is; `stack.sh` sets it to the verify project. Port 8080, the default, is refused as the live stack unless `RAG_VERIFY_LIVE=1` |
| `RAG_EXPORTS_DIR` | `<checkout>/exports` | the host folder behind the API's `/app/exports`; `stack.sh` sets it to the verify project's own folder |
| `RAG_VERIFY_LIVE` | `0` | `1` lets `all.sh` and the suites run against the live `rag-docker` stack, with a warning. Never used by the documented commands or the PR review, and never enables a restart of the live stack |
| `RAG_SKIP_SLOW` | `0` | `1` skips everything that needs an LLM call |
| `RAG_ALLOW_RESTART` | `0` | `1` allows suites to restart the stack (persistence checks, and the batch recovery acceptance in `05_transfer.sh`); never the `rag-docker` project |
| `RAG_RESTART_LIMIT_S` | `240` | seconds `01_infrastructure.sh`'s restart check allows from `down` to a healthy `/health`: Weaviate's `start_period` plus 60. The result line reports the measured time on a pass and on a fail (#130) |
| `RAG_GS_SAMPLE` | `3` | gold-standard pairs to generate |
| `RAG_FORMAT_TRIALS` | `3` | paired trials for the answer-length comparison |
| `RAG_NETWORK` | detected | compose network for the browser container |

## Writing a check

`12_persistence.sh` is called by `04_goldstandard.sh` before its slow-model skip, so `all.sh` includes durable session acceptance. It uses a unique real collection, supplied vectors, controlled model pairs, concurrent HTTP requests and a fresh API process reading the saved files. Only its owned fixtures are removed. The same registered script executes `session_persistence_cases.py` in the API image: owned filesystem/concurrency cases additionally hard-kill an owned writer at the replace boundary, pause archive inspection during a concurrent commit, retain generation failure codes and test marker-failure continuation. Native browser criteria verify pending, failed and out-of-order diagnostic refreshes using isolated HTTP responses. The in-container script rejects remote or mismatched `RAG_API` targets before health/backend execution.

The in-container retrieval check first verifies that `RAG_API` selects this Compose proxy on its published loopback port; remote or mismatched deployments are refused before execution. Its disposable collections use legacy staging markers, or persisted scratch ownership when the recovery service is present, so startup can finish cleanup after an interrupted verifier. Normal exit deletes only its own fixtures. Browser criteria cover Top-K 1/50 save payloads, metadata-read failures and out-of-order refresh completion with isolated HTTP responses.

`11_retrieval.sh` is called by 03/all.sh and runs `retrieval_controls.py` on owned real physical configurations/vector queries, controlling only model responses and avoiding startup sweeps.

`check <name> <exit-status> [detail]` — pass `$?` straight in:

```bash
[ "$status" = completed ] && [ "$chunks" -gt 0 ]
check "the job stored chunks" $? "status=$status chunks=$chunks"
```

Two rules the hard way:

- **Never pipe an API response through `echo`.** Shells interpret backslash
  escapes, and an LLM-generated answer containing `\n` becomes invalid JSON.
  Pipe `curl` straight into `python3`, or capture to a file.
- **Make a failing assertion fail loudly.** A check that compares two empty
  strings passes and proves nothing. Several early versions of these tests
  passed vacuously — asserting on a selector that matched nothing, or comparing
  a count to itself.

## Bundled-model integrity

On the verify project (`bash scripts/verify/stack.sh up`, then paste the
`export` lines it prints), whose model copy holds the embedding model, run:

```bash
docker compose exec -T api python - < scripts/verify/model_integrity.py
bash scripts/verify/stack.sh down
```

This verifies the real model's referenced bytes, copies them into a temporary
package/store, checks valid install and unchanged reuse, and refuses ordinary
mismatched bytes without changing the published manifest or healthy blobs.
The actual shared model store is only read; temporary content is removed. It
needs disk space for two copies of the embedding model and performs several
streamed hash passes. It does not invoke a model parser or claim trusted model
provenance. Controlled regression tests additionally cover digest grammar,
containment, interrupted publication and concurrent blob publication:

```bash
python -m unittest discover -s scripts/tests -p 'test_model_bundle.py'
```

## Cleaning up

Suites create collections prefixed `Vfy` (`RAG_TEST_PREFIX`) and remove them at
the end. On the verify project nothing outlives the run: `stack.sh run` removes
the whole project, volumes included, even when interrupted. If `stack.sh` itself
was killed, remove what is left with:

```bash
bash scripts/verify/stack.sh down
```

`13_identity.sh` is registered by `05_transfer.sh`/`all.sh`. It checks the embedded identity sources on the host, then executes twenty-one owned cache/disk/collision/redirected-slot/noncanonical-ID-refusal/concurrent-insertion/forced-duplicate-race/generation-503 cases in the API image, then a real export/edit/rename-import-twice roundtrip with supplied vectors and synthetic evaluation pairs. It verifies returned lookup mappings, independent four-field RAGAS downloads, fresh-process retention, re-export filenames/provenance, original-package byte equality and the `PACKAGE_CORRUPT` refusal of a noncanonical source session ID. Two controlled cases verify job-poll and cleanup deadlines. Backend/file/archive operations are delegated to worker threads. Successful collection creation records exact cleanup names; a similarly named protected fixture must survive that cleanup. Jobs have a 300-second poll deadline and 30-second cleanup deadline. If a job remains active, the standalone verifier exits 2 without executor joining or deleting its retained fixture directory, reporting the job/status/path. Only its exact owned fixtures are removed; in-container checks reject remote/mismatched targets before health/backend execution. Native browser criteria verify the existing Transfer notes expose source/local session IDs.

The infrastructure suite requires Docker Engine 28.0.0+ and checks both resolved Compose and live Docker bindings for a single loopback proxy publication. When deploying an alternate host port for testing, set `RAG_EXPECTED_PROXY_PORT` to that port as well as `RAG_API`. Its inspection files are kept in a private temporary directory removed on exit. The local profile assumes standard bridge/NAT routing.

Run `python3 scripts/tests/test_loopback_verification.py` from the repository root for the verifier's engine-version and adverse binding regressions. These exercise the actual assertion blocks and resolved default Compose; the live infrastructure suite still requires a running stack.


`reindex_verifier_cases.py` checks actual async failure cleanup and the parent shell cleanup predicate without a backend/model call. The registered suite transports its helper, the actual `lib.sh`, and these controlled tests into an owned temporary API directory. `test_collection_writes.py` checks waiting writers, case aliases, reentrancy, failures, independent collections and actual import/backend entry points, a paused complete replace-import cutover and sidecar restoration, positive ownership before import staging, and ordinary staging failure/recovery cleanup. The live suite refuses altered property vectorization and removes exact durably owned import scratch after an independent process exits without finally.

The concurrency HTTP check uses supplied-vector ingestion fixtures while keeping the upload handler, parser, chunker, worker, source retention and actual backend writes real. Its reindex source check pauses under the writer guard; the upload remains queued until final copy verification. Recovery is separately forced to fail at final creation and verified through an independent API lifespan. These cases do not claim generative model quality.

Tuning normalizes the backend first-character alias for active jobs and ownership, while preserving the caller-spelled identity for source/config/session sidecars. All tuning operations register positive staging ownership before creation and retain recovery before cutover. Explicit deletion of an exact positively owned recovery collection retires its matching journal and metadata snapshots; unrelated or invalid journals remain. Startup alone does not discard retained snapshots merely because a backend collection is missing. Interrupted explicit cleanup remains durable and is resumed at startup.

Issue #140 deletion coverage exercises canonical and accepted lowercase-alias HTTP deletion, both source/config sidecar spellings, durable orphan flags for both session spellings, and unrelated collection preservation. Controlled writer cases also cover backend deletion failure and retained recovery behavior; the controlled verifier invokes the actual deletion handler without a live backend.

## Deferred query configuration browser checks

`browser/query_config.js` runs against the real UI with all API calls stubbed
before startup. It explicitly holds and releases responses to cover a delayed
A save arriving before/after B's load, B's failed load, an A→B→A selection,
concurrent saves in both response orders, and save failure. Every case checks
the actual next Q&A request payload. These fixtures are included in `06_ui.sh`
through `ui_criteria.js`, and can also run without a backend or model:

```bash
# Start the UI separately: cd ui && npm ci && npm run dev -- --host 127.0.0.1
# With puppeteer-core available to Node and a local Chromium installation:
RAG_UI_BASE=http://127.0.0.1:3000 \
RAG_CHROMIUM_PATH=/path/to/chromium \
node scripts/verify/browser/query_config.js
```

The standalone runner uses the existing browser verification dependency
`puppeteer-core` (also available in the verification browser image); set
`NODE_PATH` if it is installed outside normal Node module resolution. This
isolated fixture run does not replace the full verification suite (`stack.sh run`).

## Deferred Chunking configuration checks

`browser/chunking_config.js`, registered in `06_ui.sh` through `ui_criteria.js`,
uses the rendered page with deferred API responses. Ten cases cover late loads,
failed loads, A→B→A selection, stale save completions, pending saves across
selection changes, duplicate-save prevention, late responses against current
edits and errors, the saved notice on a collection change, a configuration
returned for another collection, and a failed collections list. Each affected save checks
its actual collection and chunk-size payload. These fixtures make no backend
writes and complement the live settings suite.

Suite 05 also runs `scripts/tests/test_batch_recovery_fixture.py` in a network-isolated container from the built API image, with the repository mounted read-only so its sibling fixture is available.

Suite 05 also runs `scripts/tests/test_batch_implementation.py` on the host (Python and bash).

Retrieval deferred cases also cover superseded success/error notices, notice timer
ownership, both orders of an acknowledged success and newer failure, and a
three-save race that must not republish the same result over new edits.
