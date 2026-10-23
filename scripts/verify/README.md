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
`02_ingest.sh`, `03_query.sh`, `04_goldstandard.sh`, `05_transfer.sh` and `16_api_paths.sh` run only when `COMPOSE_PROJECT_NAME`
is set and isn't `rag-docker`; otherwise they fail with the reason and restart
nothing. The first-start check in `16_api_paths.sh` removes the project's
volumes (`down -v`), so it asks more (#465, `volume_removal_refusal_reason` in
`lib.sh`): `COMPOSE_PROJECT_NAME` must be `rag-verify` and `COMPOSE_FILE` must
include `docker-compose.verify.yml`, whose model volume is external and kept, as
`stack.sh` sets them. Otherwise that check fails with the reason and removes
nothing.

`python3 scripts/tests/test_verify_stack.py` checks `stack.sh`, the
configuration check and both refusals, with Docker replaced by a stub, so it
needs no stack.

## Settings publication regressions

With API dependencies installed, run
`python3 scripts/tests/test_settings_persistence.py` for deterministic ingest and
retrieval save contention, unique temporary paths, first-save directory races,
failed serialization/create/write/close/replace preservation, and the folder
fsync after the replacement, including import publication and a failed folder
fsync (#445). The test also
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

`05_transfer.sh` registers the seven controlled regressions (`test_session_import.py`, `test_source_index_boundary.py`, `test_retrieval_import.py`, `test_import_embedding.py`, `test_import_guard.py`, `test_batch_recovery.py` and `test_package_listing.py`) and the source-contract check, so `all.sh` runs them. Its E23 live checks use digest-valid synthetic packages to verify malformed metadata is refused before replacement and valid metadata is restored on rename. The retained-source boundary group checks import refusal before backend/model work, valid digest identity, and export refusal of unsafe paths and links.

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

`15_transfer_paths.sh` (#387), run by `05_transfer.sh`, covers transfer and tuning paths that
had no check. It runs four controlled tests, then live checks on a collection of its own.
`scripts/tests/test_package_refusals.py` (API container) checks that `open_package` refuses a
missing file, a file that isn't gzip and a truncated archive (`PACKAGE_UNREADABLE`), a symlink,
a hardlink and a `../` member, naming the member and extracting nothing, an archive with two
top-level directories, and a package with no manifest, a manifest that isn't JSON or a newer
`package_format` (`PACKAGE_FORMAT_UNSUPPORTED`). Through `importer._run`, with Weaviate and the
model checks mocked, a digest-valid package naming a collection Weaviate can't use is
`PACKAGE_FORMAT_UNSUPPORTED` before any lookup, and one with a `chunks.jsonl` line that isn't
JSON, a bad UUID, properties that aren't an object, a missing or short vector, or a chunk count
above or below the manifest's is `PACKAGE_CORRUPT` before any collection is created, with
`abort` and `rename`. `scripts/tests/test_transfer_paths.py` (API container, through the app)
checks that a missing, `null` or unknown `on_conflict` is 422 `INVALID_PARAMETER`, a second
import of a running file 409 `IMPORT_IN_PROGRESS`, unknown export, import and tune job ids 404
`JOB_NOT_FOUND`, that a query is answered while an export build is held in its worker thread,
`GET /packages` with a readable and an unreadable package, the help page's "its own number of"
fallback when the embedding probe fails and `HELP_RENDER_FAILED` for a missing partial or an
unsubstituted placeholder, 409 `TUNE_IN_PROGRESS` for a second tune start under either
spelling, and a re-chunk that produces no chunks failing `SOURCES_REQUIRED` before any rebuild.
`scripts/tests/test_retrieve_script.py` (API container) renders `retrieve.py` with the exporter
and runs it with `python -I -S` against a stub API: standard-library imports only, the baked
defaults sent, every flag overriding its default, and the exit codes of
`RAG_EXPORT_SPECIFICATIONS.md` section 5.1 (2 bad usage; 3 refused connection, 502/503/504 and
timeout; 4 enveloped `COLLECTION_NOT_FOUND`; 5 bare 404, other errors and non-JSON), each
failure one `error:` sentence and a hint. `scripts/tests/test_distribution.py` (host, standard
library) builds `package.sh`'s zip and `package-offline.sh`'s bundle from a copy of the tracked
tree with excluded files planted and reads them back, runs `package-offline.sh` and
`install-offline.sh` with `docker` and `sleep` replaced by logging stubs (nothing is saved,
loaded or started), and checks the bundled image list against
`docker compose config --images`. The copy is a real git repository with the tracked files
committed (git ignoring the user's and the system's configuration), because both scripts
archive `HEAD` with `git archive` (#384); the excluded files are planted untracked, as in a
real checkout, so the tests show `git archive` leaves them out, a top-level `.env.<suffix>`
such as `.env.local` included (SPECIFICATIONS.md section 11.2 excludes `.env*`). Its
`install-offline.sh` wait test records that the wait counts running services, not healthy ones.

The live checks: unknown job ids, `on_conflict` 422s, `GET /packages` with a junk
`ragpkg-*.tar.gz`, seventeen crafted copies of the suite's package written by
`transfer_packages.py` (one per refusal above) and a missing file, each refused with its code
and no collection created, a malformed copy refused by `replace` with the collection intact,
the package's bytes, size and mtime unchanged by a `rename` import, the same chunks and scores
from the original and the import for vector, hybrid and near-text retrieval (E11; `/query`
rewrites the question with the LLM first, so answers are not compared), an export with models
requested and the model store setting pointed at a missing folder (warning,
`models_bundled: false`, no `models/`), the packaged `retrieve.py` exiting 3 against a closed
port (E10) and, unless `RAG_SKIP_SLOW=1`, answering with the baked top-k and with `--top-k 2`
(E8, E9) and from the import, and a re-chunk after the retained original is replaced with blank
text failing `SOURCES_REQUIRED`. `sweep_stale_workdirs` (TR:C30) is covered by
`test_startup_sweep.py`.

`scripts/tests/test_import_embedding.py` (#390) runs import check 4 for real (E12):
an import job of a digest-valid package embedded with another model is refused
`EMBEDDING_MISMATCH` before any model, backend or sidecar step, naming both models
and both dimension counts (or saying this instance's count couldn't be read), with
no promise of re-embedding; a same-name package of another width is refused too.
It also checks that a model installed from a package is confirmed through Ollama's
model list: reported, not reported, and Ollama unreachable. Only Ollama is
replaced. `05_transfer.sh` runs it in the verify project's API container.

`scripts/tests/test_import_guard.py` (#391) runs concurrent import jobs with
Weaviate mocked: two spellings of one package file (`./x.tar.gz`, `a/../x.tar.gz`)
answer 409 `IMPORT_IN_PROGRESS`; a second import into a target another import is
building fails `IMPORT_IN_PROGRESS` naming the collection, whatever either job's
`on_conflict`, never builds, and leaves the first job's in-progress marker in place;
imports into different targets run side by side; a `rename` claims its new name and
frees the original. With the target created at the start of the build, as Weaviate
does, a second `abort`, `rename` or `replace` import is still refused
`IMPORT_IN_PROGRESS`, so the claim is checked to come before the existence check; a
refused job's `POST /import` answers 202 and registers nothing against the
collection; a target is free again after its import fails; and a start whose first
job record can't be stored releases the package name. `05_transfer.sh` runs it in
the verify project's API container.

`scripts/tests/test_ingest_errors.py` (#290) checks that damaged or unreadable ZIPs are
refused as a whole with readable 400s naming their unreadable members (truncated archive,
wrong member offsets, bad CRC, also in a member that would be skipped, unsupported
compression, encrypted member, filesystem error), unexpected errors with a JSON 500 and a
logged traceback, which inputs are queued (JSON Lines direct and as members, #291; a direct
`.json.gz` and other gzip types skipped), and that gzipped JSON members are decompressed
within their limit with clear per-file errors. Since #298 it also checks a ZIP with damaged and
unsupported members together (`ARCHIVE_UNREADABLE`, generic remedy), `UPLOAD_WRITE_FAILED` when
saving the upload itself fails, names with a newline logged escaped in every ingest log line that
carries one, the retained `media_type` of gzipped files (`application/gzip`), and rechunk of a
retained `.json.gz`. Weaviate and source retention are mocked. `02_ingest.sh` runs it
in the verify project's API container, then checks a truncated ZIP and a ZIP of
`.json`, `.json.gz` and `.jsonl.gz` members through the proxy. `browser/ui_criteria.js`
(suite 06) checks that the Import page shows the API's message for a truncated ZIP and a
failing status poll, and that a new ingest and the next successful poll clear that error.
Since #394 it also checks, with the list, upload and polls stubbed, that the job ID and a
queued panel show as soon as an upload is accepted, before the first poll, and stay through a
failed poll; that the first answered poll replaces the panel while the job ID stays; and that
"Start Ingest" with no collection selected says so and sends nothing.

`scripts/tests/test_zip_expansion.py` (#298) checks the total expanded-size cap on an
upload's ZIPs with a 1 MiB cap set through `MAX_ZIP_EXPANDED_BYTES` (`02_ingest.sh` passes it
to the container; the test sets it when absent): declared sizes over the cap refused before
any member is read, a total at the cap accepted and one byte over refused, one total for every
ZIP in the upload, counting when declared sizes understate, `.json.gz` and `.jsonl.gz` output
counted (concatenated streams and zero padding too), a corrupt gzip accepted and a truncated
one counted, a direct `.jsonl.gz` not counted, the cap passed after an unreadable member, no
member read and gzip counting stopped within one block once the cap is passed, gzip headers
that zlib refuses but the job reads (FHCRC with a bogus CRC, a reserved flag bit) still
counted, the count equal to what the job's gzip reader produces, zero padding and a long gzip
file name read in blocks, bytes read ahead past a stream's end counted once, the cap still
stopping a read through that buffer, ZIP damage found during the gzip count refused as
`ARCHIVE_UNREADABLE`, the exact message and `detail`, the 2 GiB default, and that ZIP work runs
in a worker thread while the event loop serves other work. `02_ingest.sh` runs it in the API
container, checks that the API takes the 2 GiB cap from `docker-compose.yml`'s
`MAX_ZIP_EXPANDED_BYTES`, then, at the default cap, sends `zip_over_declared.zip` (declares
2,049 MiB) and `zip_over_gzip.zip` (a gzip member expanding to 2,112 MiB) through the proxy,
checks each is refused with `ARCHIVE_TOO_LARGE` and the exact message, and that job polls sent
while the second is counted are answered within a second (a smoke check; the controlled test is
the one that tells off-loop from on-loop work).

`scripts/tests/test_json_ingest.py` (#295) checks that ordinary JSON becomes readable
`key: value` text: dotted and indexed paths, scalar arrays on one line, file key order,
non-ASCII text and numbers as written, one element per top-level record, empty values and
files giving no chunks, element-schema files still rehydrated, a byte-order mark accepted,
converted text bounded by the chunker's per-file limit (a long key over many values fails
fast with bounded memory), and readable errors for invalid JSON in `.json` files and
`.json.gz` members. For #300 it also checks the bounds on what one JSON file may cost: the
blank lines between records charged to the text limit (exactly at the limit, and one dropped
record refunded), the 1,000,000-record cap (records with no text not counted; a file of
1,000,001 tiny records fails fast with a bounded peak and builds no elements; an
element-schema file's items counted too, items with no text included, before `partition_json`
runs, and the cap applied at and one past the edge when a retained element file is rechunked,
#321), the 30,000,000-byte input limit at and one past the edge (element-schema files and a
byte-order mark included), a `.json.gz` member that stops decompressing at that limit, the
exact messages, a job where over-limit files fail alone, the traced peak of deep long keys and
of an over-long scalar array, and that an element-schema file's first parse is freed before
`partition_json` runs. `02_ingest.sh` runs it in the API container, then ingests a nested
`plain.json` through the proxy and reads its stored chunks back to confirm they hold that text.
It then uploads `json_over_size.json` (30,000,001 bytes), `json_many_records.json` (1,000,001
records), `json_over_size_gz.zip` (a `.json.gz` member one byte over the limit when
decompressed), `json_over_size_member.zip` (a `.json` member one byte over the limit) and
`plain.json` together, and checks that the job is `partial`, each over-limit file fails with
its exact message, and `plain.json` is stored.

`scripts/tests/test_jsonl_ingest.py` (#291) checks that JSON Lines (`.jsonl`, `.jsonl.gz`)
becomes one `key: value` record per line, separated by blank lines, with one element per
record: object, array and scalar lines, lines with no text dropped, CRLF, a byte-order mark,
no final newline, trailing blank lines accepted and U+2028 kept inside its line. Every line
error names its line and only the first is reported (blank, invalid JSON, `NaN`, deep nesting,
non-UTF-8 with its byte offset); the 10,000,000-byte line limit and the 10,000,000-character
text limit are checked at and one past their edges, and a file far over the limit stops
being read at the line that passes it, with a small traced peak. The line limit counts neither
the line ending nor line 1's byte-order mark (#314): it is checked at and one past its edge
with `\n` and `\r\n` endings, with and without the mark, and a CRLF file gives the same
errors as an LF one. Lines 1 to 5 bytes over the limit, at the edge of the read window, fail
on their own line with `\n`, `\r\n` or no ending, with and without the mark. The
1,000,000-line cap (#314) is checked at and one past its edge, with
blank lines and lines with no text counting, and reading stops at the line past it. Gzip: the
same text as plain, the `was decompressed, but` prefix (line, line-limit, text-limit and cap
errors), corrupt, truncated and over-size data. It also checks `source_type` `jsonl`, a job
that stores nothing for a bad file beside a good one, job-level outcomes (#314: empty files
complete with 0 chunks under every strategy, a file over the 10,000-chunk limit or the cap
fails and stores nothing, `context_aware` keeps one chunk per record), and rechunk of a
retained `.jsonl.gz`. `02_ingest.sh` runs it in the API container, then ingests a direct
`records.jsonl` and `records.jsonl.gz` through the proxy and reads their records back, checks
that `bad.jsonl` (malformed line 2) fails naming the line and stores nothing, and that
`jsonl_many_records.jsonl.gz` (1,000,001 `{}` lines, a few KB gzipped) fails with the
record-cap message and stores nothing.
`browser/ui_criteria.js` (suite 06) checks that the Import page's file picker accepts
`.jsonl` and `.jsonl.gz` and that its hint names JSONL.

`scripts/tests/test_delete_busy.py` (#40) checks that `DELETE /collections/{name}` answers
`409 COLLECTION_BUSY`, naming the job, while an ingest, export, import, tune or gold-standard
generation job is active, and that each kind's registration ends before its status turns
terminal. The request orderings are arranged with events: a job registered before the
delete, one registered while the delete waits for the writer guard (refused within a
second, not after the job), one registered once it holds the guard, and a job admitted
after a completed delete, which fails with "collection '<name>' was deleted while this job
was running" at its first store. Weaviate is mocked. `02_ingest.sh` runs it in the API
container, then deletes a collection while a real ingest is still embedding (`409`, data
in place), again after the job completes (`200`, still gone 10 s later), and checks that an
insert into a deleted collection fails without recreating it (auto-schema is off).

`test_delete_busy.py` also covers #302. A `DELETE` without `?confirm=true` (case-insensitive)
answers `400 CONFIRMATION_REQUIRED` before the collection is looked up. A `DELETE` held inside
its backend delete refuses new jobs: the ingest, export, tune and generate routes answer
`409 COLLECTION_DELETING` and leave nothing behind, generation without its one-second guard
wait, and an import of the collection ends `failed` with that code, with `abort` even before the
backend collection is gone, and at registration when the delete starts after its first check,
while a `rename` import of a collection still present completes under its new name; a delete
still waiting for another writer is not in progress, and one that waits behind a writer with no
job deletes once it lets go. A gold-standard start cancelled mid-preparation ends its
registration and leaves its session `cancelled`, and one whose preparation fails settles
nothing. Import-replace's own delete runs through `importer._run`.
`scripts/tests/test_write_counts.py` (#302, API container) checks `chunks_submitted` and
`chunks_accepted`: the writer's counts on each kind of failure, a retried file counted once, job
totals, a clean job's counts equal after each file's insert, and `null` on an older record.
`scripts/tests/test_mcp_delete.py` runs on the host with the MCP SDK stubbed and checks
`rag_delete_collection` sends `confirm=true` with `name` still its only input, and that
`rag_ingest_status` passes the write counts through unchanged. `02_ingest.sh` runs all three,
then checks on the live verify stack the `400` cases, a `confirm=TRUE` delete, and that a clean
job's three counts agree.
`browser/ui_criteria.js` (suite 06) stubs job responses to check the Import page's
"Submitted N · accepted N · stored N" line appears only when the counts differ, and an upload
answered `409 COLLECTION_DELETING` to check its message is shown.

`scripts/tests/test_jobstore.py` (#23) checks the job store that keeps ingest, export, import
and tune job records across an API restart: an unbound store (no lifespan) is a plain dict
that writes nothing; status changes and terminal records are written at once, owner-only;
10,000 progress updates cause a handful of writes and the last one still reaches the disk; a
failed write raises nothing and keeps the previous file; ids can't name another path; startup
marks `queued`/`running` records interrupted (ingest `partial` or `failed`, the others
`failed` with `INTERRUPTED`), keeps unreadable files and logs them, and keeps the newest 200;
a real lifespan run marks a stored running export. While the API runs (#309), storing a
record past the limit removes the finished records saved longest ago, from memory and disk,
never a `queued` or `running` one, in the order of each record's own last write; and each of
the four job starts writes its first record from a worker thread, and when cancelled during
that write releases its guards and leaves no record (or, for ingest, upload folder), also when
that abandoned write fails (logged). A second export of a collection started during the
first one's write waits for it, then answers with the first job's id, which can be read, or,
if the first start was cancelled or its write failed, starts its own (#326); it answers at
once when the record is already stored, holds nothing if cancelled while it waits, and
exports of other collections never wait. The wait is bounded (#338): past it, the second
start gives up holding nothing and the route answers `409 EXPORT_STARTING` with no job id.
The bound counts once, from the second start's first wait: when the first start's write fails
and another start takes the slot, waiting again on that one doesn't restart it (#347).
`02_ingest.sh` runs it in the API container. With
`RAG_ALLOW_RESTART=1`, `02_ingest.sh` also restarts the API
(`docker compose restart --timeout 0 api`) while two uploads are part-way and checks that a
completed job reads back unchanged, the one that stored a file reads `partial` and the one
that stored none reads `failed`, both with the interruption message; `05_transfer.sh` does the
same for a completed export, an export interrupted part-way through a synthetic 30,000-chunk
collection (`failed`, `INTERRUPTED`, `chunks_written` kept) and an import of that collection
interrupted part-way (`failed`, `INTERRUPTED`). Tune has no live restart check: no tune
operation runs reliably long enough to interrupt, so `test_jobstore.py` covers it.

`scripts/tests/test_startup_sweep.py` (#377) checks the startup sweeps of what a killed job
leaves behind, in disposable folders. In `UPLOAD_DIR`, ingest staging is created as
`ingest-*` and removed with the `import-*`, `rechunk-*` and `batch-verify-*` work directories;
in `EXPORTS_DIR`, `.build-*` directories and `.*.tar.gz.partial` files are removed. Only those
names, and only a real directory or regular file as the name says: completed packages,
`ingest_configs/`, look-alike names, a file where a directory is expected and the reverse, and
anything in a sub-folder are kept. A symlink is never followed, at the top or inside a
removed directory, and sizes don't count what links point at. A failed removal is logged and
not reported as removed, and a missing exports folder is not created. A real lifespan run in a
child process removes both kinds and logs them by name and size. A second one, whose export
sweep fails, logs the error and still starts, with the ingest staging removed (#423).
`02_ingest.sh` runs it in the API container. With `RAG_ALLOW_RESTART=1`, `02_ingest.sh` checks
that its two killed ingests' `ingest-*` staging existed before its restart and is gone after it
(#423), and `05_transfer.sh` checks after its export restart that the interrupted export's build
directory and temporary archive are gone, without removing anything itself, and that the
completed package from the top of the suite is still there.

`scripts/tests/test_reingest.py` (#22) checks that re-ingesting a name replaces it and that
same-named inputs don't collide. Admission: a name twice in one request keeps the last bytes,
repeated ZIP entries, the same path in two ZIPs and a direct upload against a top-level member
keep the last and list each earlier one in `skipped`; members in different folders stay
separate, `a/../b.txt` and `./c.txt` are queued with their real bytes under their normalised
names, paths leaving the archive are dropped, and labels keep the archive's own spelling.
macOS metadata in a ZIP (#378): a Finder-style ZIP queues its file and lists the
`__MACOSX/` AppleDouble member in `skipped`, a `._*` file beside its file and any other
`__MACOSX/` member are skipped the same way, names that only look like them are queued, and a
ZIP of nothing else is refused with `NO_SUPPORTED_FILES`. These members are still validated
(#455): a damaged `__MACOSX/` member refuses the ZIP with `ARCHIVE_UNREADABLE` and their bytes
count toward the expansion cap; `./__MACOSX/._r.txt`, `x/../__MACOSX/notes.txt` and
`d/./._e.txt` are matched by their normalised names and listed under the archive's spelling;
and a direct upload named `._notes.txt` is still queued. The
job: new names mark nothing; a replacement marks gold-standard sessions stale once, naming
the replaced files, before any insert, and deletes earlier chunks only after the insert; a
marking failure fails only that file and changes nothing; a failed lookup of the later files
is reported as the lookup, not the marking, and also changes nothing (#311); a failed insert keeps the earlier
version; a removal failure keeps the new chunks and reports `partial`; a deleted collection is
reported at the first lookup (#40). Also the delete rounds, precondition and final count,
retention releasing the name (also when the blob write fails, keeping shared digests), and
rechunk coverage and per-(digest, name) rebuilds, rebuilding only names with stored chunks
while a missing blob is still refused (#311), also for one blob under 50 names with one
stored and a digest with none (only the stored name is parsed). A collection whose retained
names have no stored chunks is refused as having nothing to rebuild, before staging and, for
a rechunk or a re-embed with chunking parameters, before any job is created, with the route
answering 409 (#328). That check runs only for those two, after the `TUNE_IN_PROGRESS` and
`COLLECTION_DELETING` guards; a re-index, an invalid index and a collection with a stored
listed name still start a job. A Weaviate error during the check answers 503
`SERVICE_UNAVAILABLE` with no job, the guards released and the cause logged, not sent (#340),
also when the connection itself fails and for a re-embed with chunking parameters through its
route, while a re-embed without them still starts. A Weaviate error checking that the
collection exists answers 503 from each tune start, with no job, and `GET /tune/{collection}`
answers 503 when either that check or its re-chunk check can't read Weaviate (#349).
The same existence check in `POST /query`, `POST /goldstandard/generate`, `POST /collections`,
`DELETE /collections/{name}`, `POST /ingest/upload` and `POST /export` answers 503
`SERVICE_UNAVAILABLE` when Weaviate fails, naming the collection, with the cause logged, not
sent, and nothing run, created or saved; a collection Weaviate reports missing is still 404
`COLLECTION_NOT_FOUND` there, and one it reports present is still 409 `COLLECTION_EXISTS` at
create (#366).
Each of those 503s, the tune ones included, is also checked over HTTP through the app, so
the JSON 503 reaches the client (#369), also the tune starts' re-chunk check one (#409).
Weaviate and sessions are mocked.
`02_ingest.sh` runs it in the API container, then, through the proxy, ingests a file twice
(one chunk set, `chunks_replaced`), a changed version (only its text remains, one digest
names it, the old blob is gone), a ZIP with `a/report.txt` and `b/report.txt`, a Finder-style
ZIP (it completes with no errors, stores only its file and lists the AppleDouble member in
`skipped`, #378), and two parts with one filename. With a real gold-standard session planted for the collection, it checks
that ingesting a new name leaves the session current and a replacing re-ingest marks it
stale, naming the file. Two concurrent uploads of one name must leave exactly one job's
version, whole: its chunk set, its digest, and `chunks_replaced` equal to the other job's
chunks. `browser/ui_criteria.js` (suite 06) stubs the job response and checks that the Import
page shows "Replaced N chunk(s) from a previous version" only when `chunks_replaced` is above
0, and the plain "N file(s) skipped:" heading with each entry's own reason.
`05_transfer.sh` (E32) replaces a file, adds a second name for the new content, re-chunks,
and checks that only the new text comes back, under both names. It also adds a name with no
stored chunks to the retained index first, and checks that the rechunk doesn't rebuild it and
the index still lists it (E17, #311). It then ingests a whitespace-only file, which is retained with no
chunks, and checks that `GET /tune` doesn't offer re-chunking and gives the nothing-to-rebuild
note, and that `POST /tune/rechunk` and `/tune/reembed` with chunking fields answer 409
`SOURCES_REQUIRED` with no job (#328).

`scripts/tests/test_settings_validation.py` checks that invalid settings are
refused before backend, model or staging work, with every backend mocked.
`07_settings.sh` runs it in the API image with the repository mounted read-only
and no network (#386). Its embedded-source check reads `IMPLEMENTATION.md`, so
to run it by hand mount the whole repository (on the verify project, with the
exports from `stack.sh up`, as above):

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
| `../tests/test_error_envelope.py` | controlled JSON error envelope and dependency-failure 503 regressions through the app (#373), registered by suite 01 |
| `../tests/test_package_archives.py` | host-side: `package.sh` and `package-offline.sh` in a scratch git repository with `docker` stubbed hold exactly the tracked files of `HEAD`, and refuse outside a git top level (#384), run in a checkout reached through a symlink, and ignore the user's system and global git config (#447); embedded copies; registered by suite 01 |
| `../tests/test_health.py` | controlled `/health` 503 body for a missing model and an unreachable Ollama or Weaviate, alone or together (#375, #415), and advisory memory (#415), registered by suite 01 |
| `../tests/test_collection_registry.py` | controlled collection-registry regressions (#388, #445): atomic save, a failed save keeps the old file, an unreadable registry is kept and logged, the folder is fsynced after the replace, startup removes interrupted saves' temporary files; registered by suite 01 |
| `16_api_paths.sh` | ingest, query, metrics and health API paths (#399), run last by suite 01: `NO_SUPPORTED_FILES`, `JOB_NOT_FOUND`, schema rules, the proxy's `/api/` timeouts, `/metrics/latency`, a default query and its metric (LLM); opt-in restart and first-start checks |
| `../tests/test_api_paths.py` | controlled ingest error, `/query` default, §6 prompt, query metric, retrieval-config file, `/metrics/latency` and `/health` probe regressions (#399), registered by `16_api_paths.sh` |
| `../tests/test_ollama_entrypoint.py` | `ollama/entrypoint.sh` with stub commands, on the host: skip present models, timed pull, retries (#399), registered by `16_api_paths.sh` |
| `02_ingest.sh` | §10.1 — six types, ZIP, unreadable archives and `.json.gz` members, the ZIP expansion cap, ordinary JSON text and its input, record and text limits, JSON Lines, five strategies, merge rule, partial failure |
| `03_query.sh` | §10.2 — four retrieval modes, citations, latencies, answer style |
| `../tests/test_metrics.py` | controlled metrics-file regressions (#374): a failed write keeps the answer, the bound, crash-safe compaction, reload after a restart; registered by suite 03 |
| `../tests/test_citation_score.py` | controlled citation-score regressions (#376): each metric's distance-to-score conversion, best first, missing values and unknown metrics as `null`, a failed metric read as the 503, the metric read on every query; registered by suite 03 |
| `citation_score.py` | helper for suite 03 (#376), run in the API container: citation scores of known vectors on a real Weaviate for each distance metric, and after a collection is recreated with another metric |
| `08_overlap.sh` | called by suite02 (and thus all.sh); runs `test_overlap_chunks.py` in the API image with the repository mounted read-only and no network; real parser/ingest/Weaviate text-storage check on an owned fixture with vectorization disabled; optional `RAG_OVERLAP_REAL_EMBEDDING=1` model acceptance |
| `overlap_chunks.py` | helper for suite08; asserts nonempty text/windows, exact coverage/overlap, tail bounds and pre-storage output limits |
| `../tests/test_overlap_chunks.py` | controlled overlap-window, per-file limit, ingest-worker and tuning-budget regressions and the `IMPLEMENTATION.md` mirror check, registered by suite08 (#340) |
| `04_goldstandard.sh` | §10.3 — generation, the 409 and 422 guards, review 404 codes and empty-edit refusal (#381), export schema, export name refusals (#370) |
| `../tests/test_goldstandard_review.py` | controlled PATCH/regenerate 404 codes for unknown sessions and pairs, and the empty-edit refusal (#381), registered by `04_goldstandard.sh` outside the slow gate |
| `../tests/test_goldstandard_exports.py` | controlled gold-standard export folder, name rule, overwrite and refusal regressions (#370), exports-folder refusal and replace-not-follow write (#402), registered by `10_validity.sh` |
| `05_transfer.sh` | export/import/tuning — E5–E20, E23 and E26–E34; destructive replace fidelity, live metadata and model checks, controlled regressions and source drift; a lowercase-first collection name end to end (#372) |
| `../tests/test_ingest_errors.py` | controlled upload-unpacking error and `.json.gz` member regressions, registered by ingest |
| `../tests/test_zip_expansion.py` | controlled ZIP expansion-cap (1 MiB cap from env) and off-event-loop regressions, registered by ingest |
| `../tests/test_json_ingest.py` | controlled ordinary-JSON conversion regressions, registered by ingest |
| `../tests/test_jsonl_ingest.py` | controlled JSON Lines reading, conversion, limit and gzip regressions, registered by ingest |
| `../tests/test_delete_busy.py` | controlled delete-while-busy registry, request-ordering and job-report regressions, registered by ingest |
| `../tests/test_jobstore.py` | controlled job-record persistence, throttling, startup marking and retention regressions, registered by ingest |
| `../tests/test_reingest.py` | controlled re-ingest replacement, same-name upload, macOS ZIP metadata, retention and rechunk-coverage regressions, registered by ingest |
| `../tests/test_collection_spelling.py` | controlled regressions for per-collection state keyed by the stored collection name (#372), registered by ingest |
| `../tests/test_session_import.py` | controlled import/persistence/generation regressions, registered by transfer |
| `../tests/test_source_index_boundary.py` | controlled source-index identity, early import refusal and export read-boundary regressions, registered by transfer |
| `../tests/test_batch_recovery.py` | controlled writer, import and tuning recovery regressions, registered by transfer |
| `../tests/test_package_listing.py` | controlled package-listing regressions (#383): the listing reads each archive only up to its manifest, new archives put the manifest first, an oversized manifest and other unreadable files are listed `readable: false`, and so is a manifest with a wrongly typed listed value (#451); run in the API image with no network, registered by transfer |
| `../tests/test_session_implementation.py` | exact embedded source checks, registered by transfer |
| `../tests/test_chunk_limits.py` / `../tests/test_insert_chunks.py` | controlled chunker per-file limits, chunk ceiling and semantic floor (#111) / insert batching and the "index not ready" retry, registered by ingest in the API image with the repository mounted (#386) |
| `../tests/test_model_bundle.py` | controlled bundled-model install safety, E21 and E22 with a temporary store, registered by transfer in the API image with the repository mounted (#386) |
| `../tests/test_settings_validation.py` | controlled settings refusals before work, registered by settings in the API image with the repository mounted (#386) |
| `../tests/test_retrieval_controls.py` | controlled collection list rows and retrieval-mode dispatch, registered by retrieval (suite 11) in the API image with the repository mounted (#386) |
| `../tests/test_suite_registration.py` | host-only: the suites above run the files #386 registered and `check` their status, registered by infrastructure |
| `15_transfer_paths.sh` | transfer and tuning paths with no check before (#387): import refusals, `on_conflict`, unknown job ids, `/packages`, the package file unchanged, export without the model store, `retrieve.py` (E8-E10), E11 retrieval, a re-chunk with no chunks; run by `05_transfer.sh` |
| `transfer_packages.py` | helper for suite 15; writes one damaged copy of a package per refusal kind |
| `../tests/test_package_refusals.py` | controlled import checks 1 and 2, collection-name and `chunks.jsonl` record refusals, registered by suite 15 |
| `../tests/test_transfer_paths.py` | controlled transfer and tuning route regressions through the app, registered by suite 15 |
| `../tests/test_retrieve_script.py` | controlled `retrieve.py` defaults, flags and exit codes against a stub API, registered by suite 15 |
| `../tests/test_distribution.py` | `package.sh`, `package-offline.sh` and `install-offline.sh` with stubbed `docker`, and the image list, on the host, registered by suite 15 |
| `06_ui.sh` + `browser/` | §10.4 — roles, gating, explainer, delete guard, help page, Import page file types, and the page controls of #400 (see "UI control browser checks") |
| `14_reindex.sh` | exact-record reindex: 24 record/cutover/vectorizer/concurrency cases, fourteen writer/import/recovery cases, four async lifecycle/parent-cleanup cases, two polling-deadline cases and 44 real Weaviate/handler/restart checks with a refused embedding endpoint; run by `05_transfer.sh` |
| `reindex_cases.py` / `reindex.py` | owned controlled cases / actual backend and ASGI job handlers; only scoped synthetic fixtures, canonical local session IDs and exact successful-creation ownership. Verifier collection names deliberately lie outside the parent prefix-sweep namespace. Polling is bounded to 300s, cleanup settlement to 30s; a still-active job reports its ID/status and preserves a durable exact-name fixture receipt/directory before standalone exit; inspection must confirm terminal writer state before exact-name cleanup |
| `compose_target.py` | refuses a remote or mismatched API/Compose target before the new acceptance suite runs |
| `model_integrity.py` | bundled-model byte checks with the pulled embedding model, using a temporary package/store; registered by transfer (#386) |
| `validate_package.py` | one export package against `RAG_EXPORT_SPECIFICATIONS.md` §4 |
| `09_sampling.sh` | called by suite04 (and thus all.sh), including slow-skip runs; owned synthetic UUID selection without model calls |
| `../tests/test_goldstandard_api_paths.py` | controlled empty-collection session outcome, model reply parser, regenerate 409 `GENERATION_IN_PROGRESS` while generating and 502 `PAIR_GENERATION_FAILED` with the pair unchanged (#398), registered by `09_sampling.sh` |
| `chunk_sampling.py` | standalone inside disposable API: `python - < scripts/verify/chunk_sampling.py`; real SDK seeded selection on owned synthetic UUIDs with supplied vectors, no model calls |

`10_validity.sh` is called by suite05 (and thus all.sh). It runs
`session_validity.py` inside the disposable API, using an owned real collection,
synthetic retained pairs and in-process HTTP without startup sweeps or model calls.
It checks warning metadata, actual deletion marking and explicit historical export.
It then runs `../tests/test_goldstandard_exports.py` in the API container and checks
through the proxy that download refuses other upload state with 400 (#370).

## Environment

| Variable | Default | Effect |
|---|---|---|
| `RAG_VERIFY_PORT` | `8081` | `stack.sh`: the verify project's host port; 8080 is refused |
| `RAG_EXPECTED_PROXY_PORT` | `8080` | expected resolved/live proxy host port; `stack.sh` sets it to the verify port |
| `RAG_API` | `http://localhost:8080/api` | where the API is; `stack.sh` sets it to the verify project. Port 8080, the default, is refused as the live stack unless `RAG_VERIFY_LIVE=1` |
| `RAG_EXPORTS_DIR` | `<checkout>/exports` | the host folder behind the API's `/app/exports`; `stack.sh` sets it to the verify project's own folder |
| `RAG_VERIFY_LIVE` | `0` | `1` lets `all.sh` and the suites run against the live `rag-docker` stack, with a warning. Never used by the documented commands or the PR review, and never enables a restart of the live stack |
| `RAG_SKIP_SLOW` | `0` | `1` skips everything that needs an LLM call |
| `RAG_ALLOW_RESTART` | `0` | `1` allows suites to restart the stack (persistence checks, job status across a restart in `02_ingest.sh` and `05_transfer.sh`, the query metrics reload in `03_query.sh`, the batch recovery acceptance in `05_transfer.sh`, and retrieval settings and metrics across a restart and the first start from empty volumes in `16_api_paths.sh`); never the `rag-docker` project |
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

`05_transfer.sh` runs `model_integrity.py` in the verify project's API
container, and `test_model_bundle.py` in the API image with the repository
mounted (#386). To run them by hand, on the verify project
(`bash scripts/verify/stack.sh up`, then paste the `export` lines it prints),
whose model copy holds the embedding model, run:

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

## Health page refresh checks

`browser/health_page.js` (#415), registered in `06_ui.sh` through `ui_criteria.js`,
stubs every API call the Health page makes and holds each health and metrics answer
until the check releases it, driving each refresh through the captured 30 s timer. Ten
cases cover a failed metrics call (an API error, and no answer) keeping the charts last
shown; a 200 without a health body (JSON of another shape, an empty body, text) and a 503
carrying the API's error envelope, each shown as "Service health could not be read:" with
no cards; and late responses from an older refresh, which never replace a newer health
result (success or failure) or newer charts, while a late metrics result after a newer
failed one is still shown. Every case also fails on a React page error. These fixtures
make no backend calls; like the other modules, the file runs standalone with
`RAG_UI_BASE` and `RAG_CHROMIUM_PATH`.

## Transfer page polling and failure checks

`browser/transfer_page.js`, registered in `06_ui.sh` through `ui_criteria.js`, renders the
Transfer page with every API call stubbed in the browser and the page's 2 s poll delay cut to
20 ms (#382). Fourteen cases: for each half, a failed poll (a proxy 502) is shown and retried
with the button still busy and cleared by the next good poll; 30 failures in a row stop polling,
show the last error and enable the button again, and a new start clears the message; a poll in
flight when the page is left schedules no further poll. An export start answered after the page
is left starts no polling. A 409 `SOURCE_INDEX_INVALID`, 503 or 404 from `/tune` is labelled
"Fidelity: unknown", a good lookup shows the fidelity, and a late answer for a collection no
longer selected is ignored. A failed export shows its `error_code`, and a failed package list
is shown without the empty-folder hint until a good Refresh. Like `query_config.js`, it can run
on its own against a UI with no backend (`RAG_UI_BASE`, `RAG_CHROMIUM_PATH`):

```bash
node scripts/verify/browser/transfer_page.js
```

## Gold Standard page details

`browser/goldstandard_page.js`, registered in `06_ui.sh` through `ui_criteria.js`,
answers every `/api/` call from the Gold Standard page by request interception
(#393). It checks that the export filename is pre-filled as
`{collection}_{YYYYMMDD_HHMMSS}.json` after a generation and after a lookup, made
safe and shortened to 128 characters as the API's default is; that Export opens the
download URL with the filename encoded; that a review row expands to the first 300
characters of the context followed by `…`, then its answer and its ground truth (a
fixture pair whose two differ, so one cannot stand in for the other); that a session with
no collection pre-fills `export_{YYYYMMDD_HHMMSS}.json` and a collection with no safe
characters pre-fills `_{YYYYMMDD_HHMMSS}.json`, as the API names them; and that a sample
size of 0, 101, 2.5 or an empty field disables Generate with a message and sends no
request, while 1 and 100 enable it. Its #380 half
checks how a generation ends and the guards on actions in flight: the outcome shown for a
completed, partially failed, failed, cancelled and empty-collection session (headline, failed
pairs, distinct reasons with counts, at most five); a poll answered 503 or 404 stops, shows
the message, enables Generate and is cleared by Load / refresh; a second Generate click in the
window before the first poll sends no request and asks no confirm; Regenerate is disabled for
the running pair only and repeated clicks send one request; and a refused edit (422) keeps the
modal open with the typed values and the message, and each edit sends only the changed fields.
A second
Chromium, launched without Puppeteer's `--disable-popup-blocking`, clicks Export for
real: a save answered in 1 s opens the download tab, and a control opens none. The
control holds its save answer until the page reports
`navigator.userActivation.isActive` is false (polled every 100 ms, at most 15 s), so it
follows Chromium's user-activation window, not a fixed delay, and it fails unless the
activation was still active when the save request arrived. That tab sends one
read-only download request for the fixture name to the stack under test, which
refuses it as an invalid export name. It runs on its own too, like
`query_config.js`:

```bash
RAG_UI_BASE=http://127.0.0.1:3000 RAG_CHROMIUM_PATH=/path/to/chromium \
node scripts/verify/browser/goldstandard_page.js
```

## UI control browser checks

`browser/ui_controls.js` and `browser/ui_pages.js`, registered in `06_ui.sh` through
`ui_criteria.js` (#400), check the page controls that had no browser check. They share
`browser/stub.js`, which opens the built UI in a fresh browser context and answers every
`/api/` request itself; a request a case did not stub is answered 599 and fails the case, so
nothing reaches the backend or a model. 67 cases:

- `ui_controls.js`: the Collections table (columns, a dash for a null created date) and create
  modal (HNSW slider defaults, bounds and labels, Flat hiding them, the create payload); the Import
  queue, slider visibility per strategy and role, Chunk Size pulling Overlap down, the upload
  form fields and the "+ New" modal; the Developer nav and the redirects of a Developer and an End
  User (gating is client-side only until #25); the landing page and Switch Role; the Q&A box
  (Ctrl+Enter, plain Enter, Ask disabled while empty or loading), the citations toggle, markdown,
  the latency and error lines and the End User view with `response_format`; the Retrieval page's
  saved and default texts, alpha slider and Save while loading; and the client's handling of
  nginx's HTML 413, 502, 504 and 500 pages, a non-JSON 200 and a JSON message.
- `ui_pages.js`: Gold Standard generate (selector, sample size, progress, confirm) and review
  (summary bar, approve, reject, regenerate, edit, failed action, export enabled); the Transfer
  export controls (collection selector, fidelity line, include-models payload, progress and
  result), the package list, the empty hint, Refresh, conflict choice and import failure display;
  the Help page error state and its links to and from Transfer; and the Health latency stat row,
  chart and empty state.

The Transfer polling and `/tune` failure labels stay with `transfer_page.js` (#382). Like
`query_config.js`, each module can run on its own against a UI with no backend:

```bash
RAG_UI_BASE=http://127.0.0.1:3000 RAG_CHROMIUM_PATH=/path/to/chromium node scripts/verify/browser/ui_controls.js
RAG_UI_BASE=http://127.0.0.1:3000 RAG_CHROMIUM_PATH=/path/to/chromium node scripts/verify/browser/ui_pages.js
```

Suite 05 also runs `scripts/tests/test_batch_recovery_fixture.py` in a network-isolated container from the built API image, with the repository mounted read-only so its sibling fixture is available.

Suite 05 also runs `scripts/tests/test_batch_implementation.py` on the host (Python and bash).

Suite 01 also runs `scripts/tests/test_error_envelope.py` in the built API image with
`--network none` and the repository mounted read-only (#373). It sends each request through
the app with Weaviate and Ollama mocked, and checks that an unknown route answers 404
`NOT_FOUND`, a wrong method 405 `METHOD_NOT_ALLOWED` with its `Allow` header and an unhandled
error 500 `INTERNAL_ERROR`, all in the JSON envelope; that `GET /collections` answers 503
`SERVICE_UNAVAILABLE` when Weaviate fails, and `POST /query` when an Ollama call (connect
error, timeout or error status) or a Weaviate query fails after the existence check, with no
metrics record; that a missing help template answers 500 `HELP_RENDER_FAILED`; and that
`CREATE_FAILED` and `DELETE_FAILED` send a fixed message. Each cause is logged, not sent.
It also checks (#411) that other HTTP errors keep FastAPI's own answer (a malformed
multipart upload is FastAPI's 400 with `detail`, not the envelope), that the engineer-format
synthesis failing is the Ollama 503, that a Weaviate failure in `hybrid` and `semantic`
modes names Ollama too (Weaviate embeds the question through it there), and that a help
template `RuntimeError` answers `HELP_RENDER_FAILED` with its own message. Suite 01 then
checks the envelope through the proxy on the running stack: an unknown route answers 404
`NOT_FOUND` and `PUT /query` answers `METHOD_NOT_ALLOWED`.

Suite 02 runs `scripts/tests/test_collection_spelling.py` the same way (#372). With Weaviate
and the job workers mocked, it sends each route that takes a collection name a lowercase-first
spelling (`myDocs`) and checks that the registry, the 201 and 202 answers, the ingest and
retrieval settings files, the query metric and the metrics filter, retained originals found by
`GET /tune`, and the export, tune, ingest and generation workers all get the stored name
(`MyDocs`). It fails on the code before #372. Suite 05 then, on the verify stack, creates and
ingests under a lowercase-first name, reads the tuning options and exports under the stored
name, re-chunks under the lowercase name and, unless `RAG_SKIP_SLOW=1`, checks a query's
metrics under both spellings.

Suite 01 also runs `scripts/tests/test_health.py` the same way (#375). It sends `GET /health`
through the app with Ollama's `/api/tags` answered by an httpx mock transport and the Weaviate
client check mocked, and checks the 503 `degraded` body: the LLM or embedding model missing
from Ollama's list (that service `error`, the rest `ok`), Ollama unreachable (both models
`error`), Weaviate not ready or unreachable (Weaviate `error`), Weaviate and Ollama down
together (all three `error`), each with its latency, model names and `resources`; and 200
`ok` when everything is up, also with memory below the recommendation or unreadable
(`resources.memory.status` `below_recommended` or `unknown`; memory is advisory, #415). `browser/ui_criteria.js` (suite 06)
stubs `/api/health` and checks that the Health page shows a degraded 503 body with the failing
service red and the others green, that a proxy 502, a refused connection and a 503 without a
health body each show "Service health could not be read:" with the reason and remove the
earlier cards, that the next good load clears it, and that the latency metrics load on every
refresh whatever health answers.

Suite 01 also runs `scripts/tests/test_collection_registry.py` the same way (#388). It points
`UPLOAD_DIR` at a temporary folder and checks that a save replaces
`collection_registry.json` through a uniquely named, fsynced temporary file instead of
rewriting it in place (a hard link to the old file keeps its bytes), leaving no temporary file;
that a failed fsync (a full volume), a failed replace or a registry that can't be serialized
leaves the old file whole and no temporary file; and that a registry that can't be read (not
JSON, a JSON array, not UTF-8, a folder at its path) is kept unchanged and logged while
`POST /collections` still answers 201, `DELETE /collections/{name}` 200 and `GET /collections`
200 with `created_at` `null`, with updates resuming once the file is readable again. It also
checks creates, deletes of both spellings and 20 concurrent creates on a readable registry, and
the `IMPLEMENTATION.md` copy of `api/routers/collections.py`. It also checks (#445) that a save
fsyncs the registry's folder after the replace and closes it, that a failed folder fsync fails
the save with the new file in place and no temporary file, and that API startup (the lifespan,
in a child process with the backend sweeps mocked) removes `.collection_registry.json.*.tmp`
leftovers while keeping the registry, other files, files in subfolders and what a link points
to, logging a leftover it can't remove.

Suite 01 ends by running `16_api_paths.sh` (#399), for API paths no other check covered. It
runs `scripts/tests/test_api_paths.py` the same way as `test_health.py`, with `UPLOAD_DIR` a
temporary folder: an upload with no supported file answers 400 `NO_SUPPORTED_FILES` with the
exact message listing a direct file and a ZIP member it skipped, and leaves no job, staging
folder or registration; an unknown ingest job answers 404 `JOB_NOT_FOUND`; a `/query` body
with only `question` and `collection` reaches the pipeline with the defaults (`hnsw`, `top_k`
5, `alpha` 0.75, no citations, `end_user`), and through the real pipeline runs a top-5 vector
search with the end-user prompt; the reformulation, end-user and engineer prompts and their
`[i] ...` contexts equal the SPECIFICATIONS.md §6.1-§6.3 blocks, read from the file; a
successful query records one metric; a saved retrieval config missing newer keys is filled
from the defaults, and one that isn't JSON, can't be read, holds JSON that isn't an object
(an array, a number, a string, a boolean) or has a field of the wrong type (also a fractional
or nested `top_k`) gives the defaults with `is_default: true` and a warning, while coercible
and out-of-range values are still served as saved (#439); so do a `NaN` or infinite `alpha`
and a saved `is_default` key, which answered `"alpha": null` and 500 before #477, while a
finite `alpha` of 1.5 is served as saved. `GET /metrics/latency` is
checked for its statistics against hand-worked percentiles, its exact-match `collection` and
`retrieval_mode` filters, and `history_limit` (default 100, 0, 500, 501 refused with 422). A
Weaviate client check that hangs makes `/health` give up on it after 4 s, and an unreadable
`/proc/meminfo` reports memory `unknown` with a note while `/health` stays 200. On the host,
`scripts/tests/test_ollama_entrypoint.py` runs `ollama/entrypoint.sh` with stub `ollama`,
`timeout` and `sleep` commands: models already listed are not pulled, a missing one is pulled
as `timeout 7200 ollama pull`, a failed or timed-out pull is retried after 15 s, and the fifth
failure exits 1 with its error. Then, on the verify stack: the upload and job errors through
the proxy; a new collection's schema has BM25 off for `source_file`, `source_type` and
`chunk_strategy` and refuses an insert with an undeclared property; the running proxy's
loaded configuration (`nginx -T`) reads `/api/` for 300 s and connects for 10 s; the metrics
route's shape, `history_limit=0` and the 422. Unless `RAG_SKIP_SLOW=1`, a query with the
defaults on a text of more than five chunks retrieves 5 chunks without citations and records
one `hnsw` metric with its latencies. With `RAG_ALLOW_RESTART=1`, a saved retrieval config
and that metric read back unchanged after the API restarts, and then a first start: `down -v`
(the model copy is external and kept) and `up -d --wait` must bring every service healthy
within 300 s, reporting the time, and start with no collections. That is SPECIFICATIONS.md's
criterion, a first start from empty data volumes with the models already present; the model
pull from an empty volume is a recorded manual measurement, not timed here (#465). The first
start runs only on `rag-verify` with the overlay in `COMPOSE_FILE` (see "The live stack is
refused"); host tests in `test_verify_stack.py` run this suite with Docker stubbed and check
that it removes no volumes otherwise. It removes the verify project's data, so it runs last in
suite 01.

Suite 03 also runs `scripts/tests/test_metrics.py` in the built API image with
`--network none` and the repository mounted read-only (#374), also in a `RAG_SKIP_SLOW=1` run.
It points `UPLOAD_DIR` at a temporary folder and checks that `POST /query`, sent through the
app with Weaviate and the pipeline mocked, still answers 200 with its answer when the metrics
record can't be written (an unusable file, a full volume, an `UPLOAD_DIR` that can't be
created), with the failure logged naming the metrics file and the record still counted by
`GET /metrics/latency`; that a line left unfinished by a failed write doesn't cost the next
record; that an append bringing `metrics.jsonl` to 1,000 lines compacts it and the newest
records are kept; that a compaction whose temporary write or replace fails leaves the old file
whole and no temporary file, and is tried again; that 1,100 concurrent records all land; and
that a restart restores the last 500 records, loads a file within the bound without rewriting
it, reads a 40 MB legacy file with a peak under 8 MiB and compacts it, loads nothing from a
last 1 MiB with no line break, removes temporary files a crash left, and starts when the file
can't be read or compacted or `UPLOAD_DIR` can't be created. It also checks the
`IMPLEMENTATION.md` copy of `api/services/metrics.py`. With `RAG_ALLOW_RESTART=1`, suite 03
also restarts the API after its LLM queries and checks that `GET /metrics/latency` reports
the same records afterwards (#374); a `RAG_SKIP_SLOW=1` run has no queries to reload and
doesn't reach it.

Suite 03 also checks citation scores (#376), also in a `RAG_SKIP_SLOW=1` run.
`scripts/tests/test_citation_score.py` runs in the built API image with `--network none`, with
the Weaviate client and Ollama mocked: for `hnsw`/`flat` and `semantic` queries the score is
`1 - distance` for cosine, `-distance` for dot and `1 / (1 + distance)` for l2-squared, highest
first; a missing distance, a missing hybrid score and a config with no metric or an unknown
one give `null` (a distance of 0 is a real value); the metric is read again on every query; a
failed metric read answers 503 `SERVICE_UNAVAILABLE` in each distance mode; the pipeline
rounds numbers and passes `null` through, and `/query` returns it. It also checks the
`IMPLEMENTATION.md` copies of the changed files. `citation_score.py` then runs in the verify
project's API container: it creates owned cosine, dot and l2-squared collections, inserts known
vectors, asks `/query` in the `hnsw` and `flat` modes with the models controlled, and checks the
scores against the expected similarities; then it deletes the cosine collection, recreates it
with l2-squared, and checks the next query uses the new metric. With models, the suite checks
that in each of the four modes the citation scores are numbers, highest first.
`browser/query_config.js` (suite 06) checks that the Q&A page shows a score with 3 decimals and
`n/a` for a `null` score.

Retrieval deferred cases also cover superseded success/error notices, notice timer
ownership, both orders of an acknowledged success and newer failure, and a
three-save race that must not republish the same result over new edits.

### Optional telemetry foundation (#282)

Suite 01 runs `scripts/tests/test_telemetry.py` in the built API image with
`--network none`; its real OTLP/protobuf receiver uses container loopback only.
Checks cover SDK traces/logs/metrics, sentinel privacy across schema surfaces,
configuration/no-op behavior, overload and bounded lifecycle failure. It also
checks embedded copies with `scripts/tests/test_telemetry_implementation.py`.
Suite 01 also runs `scripts/tests/test_tracing.py` for #283 in the same isolated
API image. Synthetic real ASGI and service launchers exercise all five background
job families, raw executor/task/thread propagation, correlation after 202,
concurrent isolation, partial/handled errors, generation failure reporting,
regeneration timeout, async cancellation and a surviving thread waiter. Tests
cover actual Ollama client errors, iterator/batch boundaries, finite routes,
untrusted headers, disabled/failing telemetry and sanitized OTLP links. Backend
operations are faked; no collector setup or full deployed end-to-end claim is
made by these offline tests.


### Operational telemetry (#284)

Suite 01 also runs `scripts/tests/test_telemetry_operations.py` in the built API
image with `--network none`. Real SDK aggregation and a container-local OTLP
receiver check exact counts/durations/active values, repeated IDs without new
series, preserved multiple points, fixed correlated logs, cancellation and
sampled-out/disabled tracing. Actual ingest/export/import/tuning/evaluation
launchers use synthetic dependencies to prove handled outcomes preserve durable
status. No live application content, external model or hosted collector is used.
Exporter error and malformed-wire cases check bounded failure isolation;
collector deployment acceptance remains #285.
