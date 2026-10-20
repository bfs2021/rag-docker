# RAG Docker — Export / Import Specification

**Status:** specification
**Depends on:** `RAG_EXPORT_ANALYSIS.md` (findings, measurements, rationale)
**Platform contracts:** `SPECIFICATIONS.md`

This document is the contract. Where it conflicts with the analysis, this wins;
where it is silent, the analysis explains the reasoning.

---

## 1. Scope and Settled Decisions

Export one collection as a portable package; import it into another instance and
tune it there.

| Decision | Value | Source |
|---|---|---|
| Package scope | one collection | confirmed |
| Source documents | retained, and exported | confirmed |
| Models in package | optional, off by default | confirmed |
| Tuning after import | retrieval, re-chunk, re-embed, continue ingesting | confirmed |
| Source storage | new `rag_sources` volume | confirmed |
| Transport | mounted `./exports` directory | confirmed |
| Pre-retention collections | declared `chunks-only`, no reconstruction | confirmed |
| Gold-standard sessions | exported; invalidated on re-chunk | confirmed |
| Duplicate documents | content-addressed, stored once per collection | **decided here** (§2.2) |
| Sessions of a replaced collection | retained, marked orphaned, count reported | **decided here** (§8, rule 4) |

### Out of scope

Incremental export, encryption, signing, merging two corpora into one
collection, migration to another vector store, and cross-version vector
portability guarantees beyond those in §7.2.

---

## 2. Prerequisite A — Source Document Retention

Export cannot ship what the system throws away. This MUST land before export.

### 2.1 Storage

- A new Docker volume **`rag_sources`**, mounted at `/app/sources` in the api
  service. It is separate from `ingest_uploads` because it grows with the corpus
  while that volume holds only small config and session files.
- Layout: `/app/sources/<collection>/<sha256>` — the raw bytes, named by digest,
  no extension.
- A per-collection index `/app/sources/<collection>/index.json` maps each digest
  to its logical filenames, media type, size and first-seen timestamp.

### 2.2 Ingest changes

`api/services/ingest_pipeline.py` currently deletes its temp directory in a
`finally` block (line 108). It MUST instead, for each accepted file, compute the
SHA-256 of the raw bytes and copy it into the collection's source directory
before the temp directory is removed.

**Duplicates are content-addressed and stored once.** A file already present by
digest is not written again; its `index.json` entry gains the additional logical
filename. Rationale: re-ingesting the same document is common, the storage is
proportional to the corpus rather than to upload count, and it makes "have we
seen this document?" answerable. The cost is that the original upload filename is
no longer the storage key, which `index.json` exists to solve.

**Same name, new content replaces (#22).** A logical filename belongs to one digest at a
time. When ingest retains a file, its name is first removed from every other digest's
`filenames`; a digest left with no names is removed from `index.json`, and its blob is
deleted after the index is saved. This happens even if writing the new blob fails, so the
name never keeps pointing at the version it replaced (re-chunking would rebuild that).
Ingest replaces the name's chunks the same way (`SPECIFICATIONS.md` §3.1.3), so the
retained originals keep matching the chunks. The same content under two names is still one
blob listing both names. Logical names of ZIP members include their folders inside the
archive (`a/report.pdf`). A name retained under several digests before this rule (a
re-upload) stays so until it is ingested again.

An imported source index MUST name blobs only by lowercase SHA-256 digest.
Import validates the index and its regular content-addressed files before
model installation, collection mutation, or sidecar restoration; a mismatch is
`PACKAGE_CORRUPT`. Export and re-chunking refuse invalid identities or linked
blobs at their read boundaries, so source metadata cannot select an arbitrary
filesystem path. If an indexed blob is missing on disk, export warns and omits
that entry from the shipped index; the package remains importable with its
available sources and chunks. Existing valid indexes keep their original fidelity.
An invalid on-disk index makes tuning options return a typed 409
`SOURCE_INDEX_INVALID`; re-embedding from stored chunks and re-indexing do not
need to read retained sources. A failed retention write after chunk storage does
not misreport the ingested file as failed.

Retained-source import restores only blobs named by the validated index. Unindexed
files are ignored, including sources in chunks-only packages with no index. Older
packages whose indexes name missing blobs remain rejected as `PACKAGE_CORRUPT`;
re-export from the repaired source rather than silently reducing claimed fidelity.
After manifest digest verification, each indexed blob’s manifest digest must equal
`sha256:<filename>`. This preserves content-addressed identity without hashing a
listed blob twice. Indexed blobs not listed in the manifest are hashed separately;
a listed digest that differs from the filename is rejected.
The same indexed-only copy rule applies to replace-mode recovery collections;
unindexed package files cannot survive a failed final cutover.

Retention MUST NOT change chunking, embedding, or any existing response shape.

### 2.3 Deletion

`DELETE /collections/{name}` MUST remove `/app/sources/<collection>/` entirely.
Without this, storage leaks silently and invisibly — the volume is not surfaced
anywhere in the UI.

### 2.4 Pre-existing collections

Collections ingested before this change have no source directory. They are not
reconstructed. Their exports are `chunks-only` (§4.3) and the UI MUST label them
as such wherever export is offered, so the lower fidelity is visible before the
user ships the package, not after.

### 2.5 Disk

Storage becomes proportional to the corpus. `README.md`'s guidance (20 GB
minimum, 32 GB recommended) assumes the current behaviour and MUST be restated as
"plus the size of your document corpus".

---


## 3. Prerequisite B — Retrieval Settings Persistence

Also required before export, because a package ships a script that claims to
carry the corpus's parameters.

### 3.1 Storage

`{UPLOAD_DIR}/retrieval_configs/<collection>.json`, mirroring the existing
`ingest_configs/` convention.

| Field | Type | Default |
|---|---|---|
| `collection` | string | — |
| `retrieval_mode` | enum `hnsw`\|`flat`\|`hybrid`\|`semantic` | `hnsw` |
| `top_k` | integer 1–50 | `5` |
| `alpha` | number 0–1 | `0.75` |
| `ef` | integer 16–512, optional; legacy and inactive: stored and exported in `retrieval_config.json`, but neither query execution nor the exported `retrieve.py` applies it. An integer outside 16–512, which the API stored before PR #108, is exported and imported as `null`, with an export warning or an import note | — |
| `response_format` | enum `end_user`\|`engineer` | `end_user` |

### 3.2 Endpoints

- `GET /retrieval/config/{collection}` → the saved config, or the defaults above
  with `is_default: true`, matching how `/ingest/config/{collection}` behaves.
- `POST /retrieval/config` → upsert; collection in the **body**, not the path.

The path shape deliberately mirrors ingest config, including the asymmetry that
GET takes the collection in the path and POST in the body. `SPECIFICATIONS.md`
documented that asymmetry incorrectly for ingest once already; it is stated
explicitly here so it is not repeated.

### 3.3 UI change

`ui/src/context/QueryConfigContext.tsx` MUST read and write through these
endpoints instead of `sessionStorage`. Settings become per-collection and durable
rather than per-browser-tab.

`POST /query` keeps accepting `retrieval_mode`, `top_k` and `alpha` per request —
saved settings are defaults, not a constraint.

---

## 4. Package Format

### 4.1 Filename (normative)

```
ragpkg-<collection-slug>-<YYYYMMDDTHHMMSSZ>-<id8>.tar.gz
```

- `<collection-slug>`: stored collection name, lowercased, each run of characters
  outside `[a-z0-9]` replaced by `-`, leading/trailing `-` trimmed.
- `<YYYYMMDDTHHMMSSZ>`: UTC, basic ISO 8601, no separators.
- `<id8>`: first 8 lowercase hex of the manifest digest (§4.4).

The archive MUST expand to a single directory of the same name minus `.tar.gz`.

**The filename is a label, never an input.** The slug is lossy: it lowercases a
name Weaviate capitalised, and two distinct collections can slug identically.
Import MUST read the collection name from `manifest.json` and MUST NOT parse the
filename.

### 4.2 Layout

```
ragpkg-.../
├── README.md               generated; §9
├── manifest.json           §4.4
├── collection.json         schema, stored embedding_model, index type, distance metric, HNSW params
├── chunks.jsonl            one object per line; §4.5
├── ingest_config.json      present if the collection has one
├── retrieval_config.json   §3.1
├── retrieve.py             generated; §5
├── goldstandard/
│   └── <session_id>.json   sessions whose `collection` matches
├── sources/                present only when fidelity is `with-sources`
│   ├── index.json
│   └── <sha256>            raw bytes
└── models/                 present only when models are bundled
```

Exported evaluation sessions are validated, detached snapshots read from the persisted session files. The exporter MUST NOT serialize the live generation/edit cache; later cached pair or counter updates must not change a selected export snapshot. Atomic and serialized session persistence remains separate work.

### 4.3 Fidelity

| Value | Meaning |
|---|---|
| `with-sources` | `sources/` present; every tuning path in §7 available |
| `chunks-only` | no `sources/`; re-chunking unavailable, re-embedding approximate |

Fidelity is recorded in the manifest and MUST be shown by the UI before export
and on import.

### 4.4 `manifest.json`

```json
{
  "package_format": 1,
  "created_at": "2026-09-18T14:30:22Z",
  "produced_by": {"platform": "rag-docker", "weaviate": "1.39.4"},
  "collection": {
    "name": "Policies",
    "chunk_count": 1842,
    "source_document_count": 37
  },
  "embedding": {"model": "nomic-embed-text", "dimensions": 768},
  "llm": {"model": "phi3.5"},
  "chunking": {"strategy": "overlap", "chunk_size": 1000, "chunk_overlap": 200},
  "fidelity": "with-sources",
  "models_bundled": false,
  "files": {"chunks.jsonl": "sha256:...", "collection.json": "sha256:..."}
}
```

`collection.name` is authoritative. `embedding` is the field import checks
hardest (§6.2).

`files` covers every file except `manifest.json`, `README.md` and `retrieve.py`.

**Correction.** An earlier revision of this section excluded only the first two.
That is impossible: `<id8>` (§4.1) is the manifest digest, and both `README.md`
and `retrieve.py` embed it (§5.1.4), so putting their digests in `files` would
make the manifest depend on files that depend on the manifest. The assembly
order in the implementation plan already had it right. The three excluded files
are exactly the ones generated from the manifest, and the package `README.md`
states which they are, so nothing is silently unverified.

Every other file in the package MUST appear in `files`; an unlisted file is a
defect, because import verifies only what is listed.

### 4.5 `chunks.jsonl`

One JSON object per line:

```json
{"id":"<uuid>","vector":[0.92,1.23,"…768 floats"],
 "properties":{"content":"…","source_file":"policy.pdf","source_type":"pdf",
 "chunk_index":14,"chunk_strategy":"overlap","chunk_size":1000,
 "chunk_overlap":200,"created_at":"2026-09-10T14:23:01Z"},
 "source_sha256":"…"}
```

- `properties` carries the eight stored properties, unchanged.
- `source_sha256` links the chunk to its file in `sources/`, and is `null` in
  `chunks-only` packages.
- Vectors are JSON numbers. A packed float32 sidecar was measured at only ~12%
  smaller after gzip and is rejected (`RAG_EXPORT_ANALYSIS.md` §6).

---

## 5. The Retrieval Script

`retrieve.py` is generated at export from `manifest.json` and
`retrieval_config.json`. Saved settings MUST satisfy the same typed contract as
`POST /retrieval/config` before export, checked before any chunk is read. Invalid
saved settings fail the export without publishing a package; they are not copied
into executable syntax. The job error names each failing field and says to open
the Retrieval page, save the collection's settings and export again. A legacy
out-of-range integer `ef` (§3.1) is not a failure: it is exported as `null` with a
warning.
All generated Python defaults and package metadata MUST be encoded Python
literals, and substitution MUST NOT interpret tokens inside inserted data.

### 5.1 Requirements

1. **Standard library only.** `urllib.request`, `json`, `argparse`. Anything
   needing `pip install` fails in the air-gapped case this project targets.
2. **Targets the RAG API** (`POST /query`), not Weaviate and Ollama directly.
   Reimplementing the pipeline would drift from `rag_pipeline.py`.
3. **Baked defaults, overridable by flags:**

```
python retrieve.py "who approves overtime?"
python retrieve.py --top-k 10 --mode hybrid "who approves overtime?"
python retrieve.py --api-url http://localhost:9090/api "..."
python retrieve.py --timeout 900 "..."
```

   Defaults: collection and parameters from the package; `--api-url` defaults to
   `http://localhost:8080/api`; `--timeout` defaults to 600 seconds, because
   generation runs on CPU and a single answer was measured at 236 s.
4. **Records its provenance** — the source manifest's `id8` and `created_at` in the
   script's `PACKAGE_METADATA` literal, beside `package_filename`, `embed_model` and
   `embed_dimensions`, so a script separated from its package can still be traced.
5. **Exit codes:** `0` success, `2` bad usage, `3` API unreachable, `4` collection
   absent, `5` API returned an error.

   Three cases are less obvious than they look and are normative:

   - A **gateway status** (502/503/504) is `3`, not `5`. The reference deployment
     puts nginx in front of the API, so a stopped API answers with a gateway
     error rather than refusing the connection. Reporting that as "the API
     rejected your query" sends the user to debug a query when the fix is to
     start the stack.
   - A **timeout** is `3`, with its own message naming the elapsed limit. It is
     not a connection failure and must not be reported as one.
   - A **404 without the error envelope** is `5`, not `4`. Only a body carrying
     `error.code == "COLLECTION_NOT_FOUND"` means the collection is absent; a
     bare 404 means something else answered, usually a mistyped `--api-url`.
6. **Legible failures.** Unreachable API, missing collection and a non-200
   response each produce one sentence naming the cause and the next step — not a
   traceback.

### 5.2 Generation is conditional

If Prerequisite B (§3) is not in place, the exporter MUST omit `retrieve.py` and
record `"retrieve_script": false` in the manifest. A script carrying API defaults
while claiming to carry the corpus's tuned parameters is worse than no script,
because nothing signals the discrepancy.

---

## 6. Export and Import

Both are long-running and both follow the existing job-and-poll pattern used by
`/ingest/upload`.

### 6.1 Export

`POST /export` with `{"collection": "<name>", "include_models": false}` → `202`
and `{"job_id": "..."}`. `GET /export/job/{job_id}` reports progress and, on
completion, the written filename.

- Export of a collection that does not exist fails immediately with
  `COLLECTION_NOT_FOUND`, before any job is created.
- Only one export per collection may run at a time; a second request while one is
  in flight fails with `EXPORT_IN_PROGRESS`, naming the running `job_id`. Two
  concurrent exports would race on the temporary file described below. A request
  that arrives while that export is still storing its first record waits for it,
  at most 30 s, so the named `job_id` can always be read; if that start is
  cancelled instead, or its record write fails, the request starts its own
  export (#326, #347). If it is still storing that record after 30 s, the
  request fails with `EXPORT_STARTING`, with no `job_id`, and can be retried
  shortly (#338).
- The package is written to `/app/exports` (host `./exports`), never streamed.
- Chunks are read with `include_vector=True`, paged, so memory does not scale
  with collection size.
- Export MUST refuse `EMBEDDING_MISMATCH` before reading chunks or publishing a
  package when the collection’s stored embedding model differs from this instance
  or its vectorizer configuration is unsupported. Re-embed into a supported
  collection with this instance’s model before exporting. `collection.json` and
  the manifest bind vectors to that stored model.
- Failed export jobs expose typed package refusal fields `error_code` and
  `error_detail`, alongside the human-readable `error`.
- Export MUST NOT block ingest or query.
- Writing is atomic: build under a temporary name in the same directory and
  rename on success, so a partial file is never mistaken for a package.

### 6.2 Import validation order

`POST /import` with `{"filename": "<name in ./exports>", "on_conflict": "..."}`
→ `202` + `job_id`.

Checks run in this order and stop at the first failure:

| # | Check | On failure |
|---|---|---|
| 1 | File exists and is a readable `.tar.gz` | `PACKAGE_UNREADABLE` |
| 2 | `manifest.json` present, `package_format` understood | `PACKAGE_FORMAT_UNSUPPORTED` |
| 3 | Every `files` digest matches | `PACKAGE_CORRUPT`, naming the file |
| 3a | Retained-source index names only valid digest identities and matching regular blobs | `PACKAGE_CORRUPT`, naming `sources/index.json` |
| 4 | **Embedding model and dimensions match this instance** | `EMBEDDING_MISMATCH` — refuse |
| 4a | Every evaluation sidecar has a valid session schema, generated session ID, matching collection and unique identity within the package; the local session storage directory is a regular directory inside the upload directory | `PACKAGE_CORRUPT`, naming the sidecar |
| 4b | Optional retrieval settings are a JSON object satisfying the API save schema, normalized with its defaults and numeric conversion | `PACKAGE_CORRUPT`, naming `retrieval_config.json` |
| 5 | Collection name collision | resolved per `on_conflict` |

Check 3a runs before embedding-model checks and all live mutation. Check 4b runs before model installation, collection creation/replacement, recovery
ownership, or sidecar publication. Keep its normalized snapshot for restoration;
do not reread unvalidated settings after building the collection. Rebind the
collection to the actual import target. Missing fields retain API defaults,
valid historical `ef` values are preserved, and unknown fields are ignored as
on API saves. A legacy integer `ef` outside 16–512 (§3.1) is restored as `null`,
and the completed job notes it. A missing settings file remains supported.
Invalid JSON, non-object settings, invalid enum values, booleans in numeric fields
(including `ef`), other out-of-range values, and non-finite alpha are refusals in
every conflict mode.

Check 4a runs before bundled-model installation, collection creation/deletion,
or restoring any sidecar. All sessions MUST be preflighted together, including
later files, and the validated snapshots used for restoration. Invalid JSON or
metadata is a refusal, not a skipped session. Session IDs use the locally
generated `gs_[0-9a-f]{8}` grammar; malformed IDs are refused, never rewritten.
A well-formed ID that is already used locally is kept as import provenance and
the restored session gets a new local ID (`SPECIFICATIONS.md` §3.1.5). The
persistence boundary also enforces resolved-path containment and refuses
redirected storage directories and non-regular destinations. Archive extraction
accepts only regular files and directories, so special members cannot block a
later metadata read. Existing review work remains unchanged on validation
failure, including `replace`. Optional legacy progress fields retain their
existing defaults, and historical validity metadata is preserved. A restored
session's `persistence_error` is removed: a write failure on the source system
says nothing about this instance's storage.

Startup loading, collection flagging and export use the same session-record
validation. Invalid legacy files (including filename/identity mismatch) remain
untouched on disk with diagnostics and are excluded from the active cache and
exports. They MUST NOT abort flagging after a collection has been deleted.

Check 4 is a refusal, not a warning. Vectors from a different model are
meaningless rather than merely different, and a collection built from them
answers every query confidently and wrongly. The error MUST name both models and
both dimension counts, and state that re-embedding is available when the package
is `with-sources`.

### 6.3 Bundled models

`include_models` defaults to `false`. When `true`, `models/` contains the
**embedding model and the LLM named in the manifest**, as the Ollama manifest and
blob files required to serve them, under `models/<model-name>/`:

```
models/
├── nomic-embed-text/
│   ├── manifest.json          the Ollama manifest, verbatim
│   └── blobs/
│       └── sha256-<hex>       config and layer blobs it names
└── phi3.5/
    ├── manifest.json
    └── blobs/
        └── sha256-<hex>
```

Blobs are resolved **through each model's manifest**, never by copying the blob
store: the store is shared by every model on the machine, so a naive copy would
bundle unrelated weights. Ollama writes blob filenames with a hyphen
(`sha256-<hex>`) while manifests reference them with a colon (`sha256:<hex>`).

Every model file appears in the package manifest's `files` map and is digest
verified on import like any other file.

**The API service must mount the Ollama model volume** (`ollama_models:/ollama`)
for any of this to work. Installing a model means writing its exact manifest and
blobs: offline there is no registry to pull from, and rebuilding the model
through Ollama's HTTP API would reconstruct its template, params and license
from the config blob — a reproduction, not the model that produced the vectors.
Where the volume is not mounted, export records a warning and bundles nothing,
and import reports that models were not checked rather than assuming.

The embedding model is the one that matters: vectors are meaningless without the
model that produced them. The LLM is included for completeness so a package can
stand alone, and is the larger share of the ~2.5 GB.

On import:

| Target state | Behaviour |
|---|---|
| Model present and its files match their checksums | skip; do not overwrite. A target's existing model is assumed deliberate |
| Model present but a file is missing or doesn't match its checksum | embedding model: fail with `MODEL_INTEGRITY_FAILED`, naming the model and saying to restore or re-pull it. LLM: note it on the import and continue. Never overwrite it: blobs are shared, and replacing one could affect other models |
| Model absent, package bundles it | install into the `ollama_models` volume, then verify it appears in `ollama list` before proceeding |
| Model absent, package does not bundle it | fail with `EMBEDDING_MODEL_MISSING`, naming the model and stating that it must be pulled or a `with-models` package used |
| Namespaced model name (`user/model`) | it has no path in the model store, so it can't be checked or installed from a package. If Ollama reports it, note that its files weren't checked and continue. If not: the embedding model fails `EMBEDDING_MODEL_MISSING`, saying to pull it; the LLM gets a note. If Ollama can't be reached, the embedding model fails `IMPORT_FAILED`, saying so rather than calling the model missing; the LLM gets a note |

`EMBEDDING_MODEL_MISSING` is distinct from `EMBEDDING_MISMATCH` (§6.2): one means
the target has nothing to embed with, the other means it has the wrong thing. The
remedies differ, so the errors must too. `MODEL_INTEGRITY_FAILED` is a third case:
the model is there but damaged, so pulling a model the user already has is not the
fix; restoring or re-pulling it is.

Blobs are written before the manifest. The manifest is what makes Ollama
consider a model present, so writing it last means an interrupted install leaves
unreferenced blobs rather than a model that cannot be served. Each referenced
address must be `sha256:` plus 64 lowercase hex digits. Import stream-hashes
bundled bytes and existing shared bytes against that address before publishing
any manifest. A matching filename alone is not evidence of matching bytes.
Healthy existing blobs are reused without replacement. A mismatched existing
blob is refused with an explicit integrity error; restoring that shared content
is an owner action, because automatic replacement could affect other models.
Model names and tags must be simple path components, and package/store paths
must remain under their roots without symlink components. A copied blob is
hashed again while writing a unique temporary file, then published atomically
without replacing a concurrently published blob. The captured, validated
manifest is written atomically last, after confirming every destination blob.
The installed-model check also verifies referenced byte hashes. Hashes prove
content consistency, not trusted model provenance or safe model parsing.

Because models are content-addressed, a model that travels in a package and is
installed on the target is **byte-identical** to the one that produced the
vectors, and Ollama reports the same model ID.

Bundling models does not change fidelity (§4.3), which describes source documents
only.

### 6.4 Conflict handling

`on_conflict` is required and has no default:

| Value | Behaviour |
|---|---|
| `abort` | fail with `COLLECTION_EXISTS` |
| `rename` | import as `<name>_imported_<id8>`; the new name is reported |
| `replace` | delete the existing collection and its sources, then import |

**Correction — the separator is an underscore, not a hyphen.** An earlier
revision specified `<name>-imported-<id8>`. Weaviate rejects that name with a
422: collection names must match `[A-Za-z][A-Za-z0-9_]*`. Verified against the
live server, along with two related facts that import relies on:

- Weaviate **capitalises the first character** of a collection name and stores
  it that way, so `policies` and `Policies` are the same collection. Collision
  checks MUST compare the canonical form or `abort` will miss a collision and
  the import will fail later, less clearly.
- Only the first character is normalised: `CASETEST` is a different collection
  from `Casetest`.

If the renamed target is also taken — which happens when the same package is
imported twice — a numeric suffix is appended (`..._2`, `..._3`).

### 6.5 Atomicity

A failed new-target build MUST remove its partial collection. `replace` MUST
retain a verified recovery copy before deleting the original. There is no atomic
swap: a final create or write failure can leave the original name unavailable,
and the error MUST identify the retained incoming data.

All writers MUST check the supported completed-batch failure list after context
exit, then confirm the persisted UUID set, properties and vectors. Duplicate IDs,
partial acceptance and manifest count mismatches MUST fail. Supplied vectors are
compared by their float32 encoding; generated vectors must be finite and nonempty.
`chunks_written` and ingest stored counts represent confirmed objects, not enqueue
attempts; ingest's `chunks_submitted` and `chunks_accepted` are the unconfirmed
counts, reported beside them (#302). Verification MUST stream records with disk-backed UUID/property/vector
fingerprints rather than retain a full decoded corpus in memory. Failed ingestion
MUST remove only UUIDs generated by that attempt; cleanup failure MUST preserve
the original error and explicitly report that accepted chunks may remain.

**Correction — "promote" is not available.** An earlier revision required
building into a temporary collection and renaming it on success. **Weaviate has
no rename**: `client.collections` exposes create, delete, exists, get, list_all
and export_config, and `config.update` cannot change a name (verified against
weaviate-client 4.23.1). The guarantee is therefore met differently depending on
whether anything is at risk:

| `on_conflict` | Strategy |
|---|---|
| `abort` | Fails before any build if the name is taken, so the build target is always new. Built directly; deleted on failure. |
| `rename` | The target name is new by construction. Built directly; deleted on failure. |
| `replace` | Built into `<name>__importing_<operation-id>` first. Only once that succeeds is the existing collection deleted and the real one built. |

`replace` therefore performs **two insert passes**. That is the cost of a
non-destructive replace in a database that cannot rename, and it is paid only
when there is an existing collection to protect.

If the second pass fails, the staging collection MUST be kept and named in the
error, so the imported data is recoverable rather than lost. It MUST NOT be
named when the staging build itself failed — there is nothing there to recover,
and pointing the user at a collection that does not exist is worse than silence.

### 6.5.1 Surviving a hard kill

The guarantees above rely on cleanup code running. A `SIGKILL`, an OOM kill or
`docker compose kill` skips it entirely, so the API MUST also repair the damage
at startup. Nothing can legitimately be mid-import before the app serves its
first request, which is what makes this safe.

| Left behind by | Detected at startup by | Action |
|---|---|---|
| Owned import/tuning scratch | a valid durable `collection_operations/<operation-id>.json` record with state `scratch` | delete the recorded scratch collection and its copied sidecars |
| Verified recovery | a durable record with state `recovery`, written before deleting the target | preserve the collection and all sidecars; log its identity and metadata snapshot directory |
| Successful/intentional cleanup interrupted by I/O failure | durable `cleanup` phase, written before deletion | retry only that authorized backend/sidecar/metadata cleanup until complete |
| Unowned marker-like name or unreadable ownership | insufficient ownership evidence | preserve; a substring or age is never deletion authority |
| New-target partial import | a small version-4 marker bound by SHA-256 to a compact SQLite expectation snapshot, and by a random instance token to the schema description (`rag-import:<token>`) the import gives the collection it creates | only for the collection carrying the marker's token: verify the full stored records and delete a proven mismatch. A collection without the token was created later under that name: keep it and retire the marker. Preserve on unreadable/legacy metadata or backend read failure. A version-3 marker carries no token and never deletes: in state `building` it is kept and logged; in state `cleanup` its cleanup is finished |
| Orphaned expectation snapshot | a `<32 hex>.sqlite3` file in the markers folder that no marker names | delete it; if any marker cannot be parsed, delete none |
| Extraction workspace (`import-*`, `rechunk-*` under `UPLOAD_DIR`) | the directory name prefix | delete the abandoned workspace; recovery sidecars are stored outside it |
| Job record left `queued` or `running` | its file under `UPLOAD_DIR/jobs/<kind>/` | mark it interrupted: export, import and tune `failed` with `INTERRUPTED`, ingest `partial` (a file was stored) or `failed`; keep the newest 200 records per kind, at startup and while the API runs (only finished records are removed, #309). A job record never authorizes or blocks any cleanup above |

Recovery ownership is atomic and flushed before the destructive step. Cleanup
intent is also durable, so a failure after backend deletion does not leave a
phantom recovery record. Import markers are bounded metadata; expected identities
and SHA-256 fingerprints live in a separate integrity-bound SQLite snapshot, and
startup comparisons stream through a temporary index with a 1 MiB cache. Corrupt,
legacy or oversized metadata is preserved without authorizing backend deletion. Original
source bytes and ingest/retrieval configs are copied under the recovery collection
name; evaluation JSON is copied to the operation's metadata snapshot directory
without overwriting live session identity. An imported recovery also retains its
manifest and collection config. The error detail includes `recovered_as` and
`sidecar_snapshots`, the snapshot directory relative to `UPLOAD_DIR`
(`collection_operations/<operation-id>`); the absolute path appears only in the
server log. A recovery record remains preserved even if its backend
collection is later missing, since its sidecars may still be useful.

The owner can export the named recovery collection, inspect its metadata snapshots,
and intentionally recover or discard it. Automatic startup cleanup never discards
recovery data. For an intentional full discard in the API container, load the
identified operation record and call `collection_recovery.discard(record,
weaviate_client.get_client())`; this removes that recorded collection, its copied
sidecars and ownership metadata. Do not discard a record until its data is no
longer needed. Import never modifies or deletes the original package in `./exports`.

---

## 7. Tuning After Import

### 7.1 Available operations

| Goal | Requires | Mechanism |
|---|---|---|
| Change mode / `top_k` / `alpha` | nothing | per-request on `POST /query`; persist via §3.2 |
| Change index type or distance metric | nothing | rebuild collection, re-insert the same vectors |
| Change chunk size / overlap / strategy | `with-sources` | re-ingest from `sources/` |
| Change embedding model | `with-sources` preferred | re-embed; dimensions may change, so the collection is rebuilt |
| Add documents | matching embedding model | normal ingest |

### 7.2 Re-embedding a `chunks-only` package

Permitted, with a stated limitation: vectors are regenerated from stored chunk
text, so **chunk boundaries cannot change**. The API MUST reject a request that
combines re-embedding with new chunking parameters on a `chunks-only` collection
rather than silently ignoring one of them.

### 7.3 Gold-standard invalidation

Any operation that changes chunk identity — re-chunking, or re-embedding that
rebuilds the collection — MUST mark every gold-standard session for that
collection `stale`, recording why and when. Sessions are not deleted and are not
remapped: a wrong remap corrupts an evaluation baseline silently, which is worse
than an honest stale flag. Invalidation MUST cover both supported first-character
collection aliases without changing stored session provenance.

For identity-changing rebuilds, persist the flag after preparation succeeds but before deleting the live collection. A failure after cutover begins also marks history stale, including a failed reindex whose original collection may be missing or partial. A successful identity-preserving reindex keeps its existing validity semantics. This request-time validity barrier is separate from session persistence concurrency and durable collection recovery.

Re-ingesting a file whose name already has chunks replaces them (#22), which also
changes chunk identity: ingest marks the collection's sessions `stale` once per job, before
storing the first replacing file's chunks, and that file fails if the marking can't be
saved (`SPECIFICATIONS.md` §3.1.3). Ingesting new names marks nothing.

Changing only the index type or distance metric does **not** change chunk
identity, and MUST NOT mark sessions stale.

A successfully verified index/distance change preserves exact UUIDs, properties
and vectors, and MUST NOT mark sessions stale. Single-process application writers
share the collection guard through snapshot, replacement and final verification;
external backend writers remain outside it and an observed change is refused.
A stored vectorizer mismatch with the complete recreation configuration is refused before
staging, including unknown module options and property name/type/vectorization flags.
Complete replace-import workers hold the same target guard through conflict check,
cutover and sidecar restoration; their staging ownership is persisted before creation.
Staging-creation failures immediately discard only positively owned scratch. Verified staging and pre-cutover sidecars must survive an uncertain
cutover under durable recovery ownership. A failed delete that leaves exact

**Sessions are cached in memory as well as on disk.** Anything that writes a
session file directly, without going through the gold-standard service, will be
silently undone: the cache still holds the previous version and the next
flagging pass writes it back over the file. Import hit exactly this — a restored
session reverted to the orphaned state of the collection it replaced. Every
writer MUST go through the service.

### 7.4 Failed rebuilds

Before deleting the original, tuning MUST verify the staged rebuild and durably
retain its source/config/evaluation sidecars. Final-create, batch and verification
failures MUST report the recovery collection and preserve it across restart,
including chunks-only data. Cleanup may delete scratch while the original remains
safe, or delete recovery only after final persisted-record verification succeeds
or when a step fails before the original is deleted. If marking gold-standard
sessions stale fails at that point, the job fails `TUNE_FAILED`, says so and that
the original collection is unchanged, and the retained copy is discarded.

---

## 8. Lifecycle Rules

1. Deleting a collection deletes its sources, its ingest and retrieval configs,
   and leaves its gold-standard sessions orphaned per rule 4 below.
2. Exports in `./exports` are never deleted automatically.
3. Import never modifies the package file.
4. **Replaced collections:** when `on_conflict=replace` removes an existing
   collection, its gold-standard sessions are **retained and marked orphaned**,
   and the import result reports how many. Silent deletion destroys evaluation
   work; refusing the import would block a legitimate operation over data the
   user may not care about. Reporting lets them decide afterwards.

---

## 9. Generated `README.md`

Every package contains one, generated from the manifest so it cannot drift:

- what this package is, its collection name, chunk and document counts
- the fidelity and what that permits and forbids
- the filename convention, and that the manifest is authoritative
- exact import steps for this package
- how to run `retrieve.py`, or why it is absent
- the embedding-model requirement, stated as a prerequisite
- a plain statement that the package is **not encrypted** and may contain
  sensitive documents

---

## 10. Help Page

A new route `/help/transfer`, available to AI Engineer and Developer roles.

It MUST cover: what a package contains and omits; the naming convention;
both fidelity levels and why older collections are `chunks-only`; the
embedding-model rule; the tuning table from §7.1; how to run `retrieve.py`; and
where packages live (`./exports`).

The page and the generated `README.md` MUST be produced from one shared source.
This project has already found five places where documentation and implementation
drifted apart, three of them affecting the running UI.

**How this is done.** The shared blocks live in `api/templates/partials/` —
`encryption.md`, `fidelity_table.md`, `embedding_rule.md`, `naming.md`,
`contents.md` and `retrieve_usage.md`. Both `package_readme.md.tmpl` and
`help_transfer.md.tmpl` pull them in with `@@INCLUDE:<name>@@`, and the API
renders the help page on request rather than the UI holding its own copy. Editing
a partial changes the in-app page and the next package's README together.

`retrieve_usage` appears in a package README only when that package ships
`retrieve.py`; the help page always carries it.

---

## 11. Error Codes

All use the existing envelope, `{"error": {"code", "message", "detail"}}`.

| Code | Meaning |
|---|---|
| `PACKAGE_UNREADABLE` | missing or not a readable archive |
| `PACKAGE_FORMAT_UNSUPPORTED` | `package_format` newer than this instance |
| `PACKAGE_CORRUPT` | digest mismatch, invalid retained sources (check 3a), or invalid evaluation-session metadata (check 4a); names the file |
| `EMBEDDING_MISMATCH` | model or dimensions differ; names both |
| `EMBEDDING_MODEL_MISSING` | target lacks the embedding model and the package does not bundle it |
| `MODEL_INTEGRITY_FAILED` | the embedding model is installed but a file is missing or doesn't match its checksum; names the model |
| `COLLECTION_EXISTS` | collision with `on_conflict=abort` |
| `COLLECTION_NOT_FOUND` | export requested for a collection that does not exist |
| `SOURCES_REQUIRED` | tuning needs retained originals covering every stored source file unambiguously; missing or ambiguous coverage is refused before staging; a collection none of whose retained names has stored chunks is refused before any job is created (HTTP 409) (#328) |
| `EXPORT_IN_PROGRESS` | concurrent export of the same collection |
| `EXPORT_STARTING` | HTTP 409: a concurrent export of the same collection is still storing its first record after 30 s; no `job_id`; retry shortly (#338) |
| `IMPORT_IN_PROGRESS` | concurrent import of the same package file |
| `TUNE_IN_PROGRESS` | concurrent tuning of the same collection |
| `COLLECTION_DELETING` | HTTP 409 for a new export or tune, and an import job's failure: a delete of the collection is in progress (SPECIFICATIONS.md §3.1.2; #302) |
| `TUNE_FAILED` | an unexpected error during a rebuild; the message carries the cause |
| `SOURCE_INDEX_INVALID` | HTTP 409: the retained source index is invalid; repair it before source-dependent tuning. Reindex and re-embed from stored chunks remain available. |
| `IMPORT_FAILED` | an unexpected error during import; the message carries the cause |
| `INTERRUPTED` | the API restarted while the export, import or tune job was queued or running; start it again (#23) |

---

## 12. Packaging and Offline Implications

- `docker-compose.yml` gains the `rag_sources` volume and an `./exports` bind
  mount on the api service.
- `package.sh` and `package-offline.sh` MUST include `exports/` as an empty
  directory and MUST exclude its contents — the same trap already hit with
  `ingest-inbox/`, where excluding the contents also dropped the directory.
- The offline bundle does **not** carry `rag_sources` or `weaviate_data`: it
  ships the system, packages ship the content. That separation is the point.

---

## 13. Acceptance Criteria

| # | Criterion |
|---|---|
| E1 | Ingesting a document leaves its bytes in `rag_sources/<collection>/<sha256>` with an `index.json` entry |
| E2 | Ingesting the same file twice stores one copy and records both logical names |
| E3 | Deleting a collection removes its source directory entirely |
| E4 | `GET/POST /retrieval/config` round-trips; the UI no longer writes `rag_query_config` to `sessionStorage` |
| E5 | Export produces a file matching the §4.1 pattern in `./exports` |
| E6 | Manifest digests match every file listed |
| E7 | A collection with sources exports `fidelity: with-sources`; a pre-retention one exports `chunks-only` |
| E8 | `python retrieve.py "question"` returns an answer with no third-party packages installed |
| E9 | `retrieve.py --top-k 10` overrides the baked default |
| E10 | `retrieve.py` exits 3 with one sentence when the stack is down |
| E11 | Import into a clean instance reproduces chunk count and answers equivalently |
| E12 | Import refuses `EMBEDDING_MISMATCH` when the target uses a different embedding model |
| E13 | A truncated package fails `PACKAGE_CORRUPT` naming the file |
| E14 | `on_conflict=abort` fails; `rename` imports under a new name; `replace` succeeds |
| E15 | Failed new-target builds remove partial collections; failed destructive replacement retains and names verified recovery data and sidecars |
| E16 | Re-chunking and failed destructive cutover mark sessions under both supported first-character collection aliases `stale`; the reported count includes both spellings, and stored provenance is unchanged |
| E17 | Re-chunking a `chunks-only`, partially retained or ambiguously retained collection (a name retained under more than one digest, or a name with more than one chunk set) fails `SOURCES_REQUIRED` before staging; re-embed with new chunking parameters uses the same coverage guard. A second name for the same retained content is covered: re-chunking emits one chunk set per (digest, name) pair, under the full logical name (#22). Only pairs whose name has stored chunks are rebuilt: a name the index lists without stored chunks is skipped, and the index keeps it (#311). When no listed name has stored chunks there is nothing to rebuild: the tuning options don't offer re-chunking and say why, and the request is refused up front, HTTP 409 `SOURCES_REQUIRED`, with no job (#328); a Weaviate error during that check answers HTTP 503 `SERVICE_UNAVAILABLE`, with no job (#340) |
| E18 | `replace` reports the number of orphaned sessions |
| E19 | Package `README.md` states the collection name, fidelity and encryption warning |
| E20 | `docker compose up -d` still starts five services, with `./exports` mounted |
| E21 | Importing a `with-models` package into an instance lacking the embedding model installs it and it appears in `ollama list` |
| E22 | Importing a package without bundled models into such an instance fails `EMBEDDING_MODEL_MISSING` |
| E23 | A digest-valid package with malformed evaluation metadata fails `PACKAGE_CORRUPT` before model installation, collection mutation or sidecar restoration; existing review work remains unchanged |
| E24 | Export, edit original, rename-import twice: all three session identities retain independent review/export state and reported import provenance |
| E25 | With the embedding endpoint unavailable, reindex changes the physical index while preserving exact UUIDs/properties/vectors; completed jobs leave retained evaluation sessions unchanged; same-process ingestion is serialized, incompatible vectorizers are refused, and uncertain cutover retains durable recovery |
| E26 | Importing when the installed embedding model's files don't match their checksums fails `MODEL_INTEGRITY_FAILED`, leaves the model's files untouched and says to restore or re-pull it |
| E27 | With a namespaced `LLM_MODEL` (`user/model`), an import of a package without bundled models succeeds and notes the model |
| E28 | Digest-valid malformed retrieval settings are refused before live mutation in all conflict modes; valid historical settings round-trip, a legacy out-of-range integer `ef` imports as `null` with a note, and generated script defaults/metadata remain encoded typed literals. On export, a saved legacy out-of-range integer `ef` is written to the package as `null` with a warning and the saved settings are left alone; other invalid saved settings fail the export before any chunk is read, with an error naming the failing fields and the Retrieval page, and no package is published |
| E29 | An imported source index with a non-digest identity or linked/mismatched blob fails `PACKAGE_CORRUPT` before live mutation; export and re-chunking refuse unsafe retained-source paths; import and replace-mode recovery ignore unindexed source blobs; invalid-index tune options return `SOURCE_INDEX_INVALID`, while record-only tuning still works; missing-blob exports pass import preflight |
| E30 | Export refuses `EMBEDDING_MISMATCH` before streaming/bundling when the stored collection model is unknown, named-vector, or differs from the configured model; vectors are never relabeled from current environment settings |
| E31 | Export, import and tune job records survive an API restart: a completed export reads back unchanged, and an export interrupted by the restart answers `200` with `status: failed`, `error_code: INTERRUPTED` and its `chunks_written` kept; an import interrupted by the restart answers the same (#309); tune likewise, by `test_jobstore.py` |
| E32 | Re-ingesting a changed file replaces its chunks and its retained source: one digest names it and the replaced blob is gone; re-chunking afterwards brings back only the new version, under each of its names (#22) |

---

## 14. Deferred

Incremental export, encryption and signing, corpus merging, cross-vector-store
migration, gold-standard remapping after re-chunk, and HTTP transport as an
alternative to the mounted directory.
