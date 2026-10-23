# RAG Docker — Export / Import Implementation Plan

**Status:** complete — all eight phases are implemented and verified, and the
offline bundle has been regenerated and installed from scratch.
**Implements:** `RAG_EXPORT_SPECIFICATIONS.md`
**Background:** `RAG_EXPORT_ANALYSIS.md`

---

## 1. Verified Facts

Confirmed by running against the live stack, not from memory. The first two
decide whether the design works at all.

| Fact | Value | Why it matters |
|---|---|---|
| Explicit vectors survive a vectorizer | `col.data.insert(properties=…, vector=[…])` stores the supplied vector **verbatim** even though the collection is configured with `text2vec-ollama` | Import can reuse exported vectors. If the vectorizer had overwritten them, every import would silently re-embed and the embedding-model check would be pointless |
| `iterator` returns a **dict**, not a list | `col.iterator(include_vector=True)` yields objects whose `.vector` is `{'default': [...768 floats]}` | Code written for a bare list breaks at runtime. Read `o.vector['default']` |
| Export API | `col.iterator(include_vector=True, ...)` — supports `after` and `cache_size` | Paging without loading a collection into memory |
| Import API | `col.data.insert(properties, uuid, vector)`; batch context available | Preserves chunk UUIDs across the move |
| Embedding dimensions | `nomic-embed-text` → **768** | Manifest value and the import check |
| Ollama model layout | `models/manifests/registry.ollama.ai/library/<model>/latest` (JSON listing blob digests) + `models/blobs/sha256-*` | §6.3 bundling copies one manifest plus the blobs it names |
| Chunk properties | exactly **8** on every object | `chunks.jsonl` `properties` shape |
| Source deletion | `api/services/ingest_pipeline.py:108` `shutil.rmtree(tmp_dir, …)` in `finally` | The line Phase 1 changes |
| Volumes today | 3 (`weaviate_data`, `ollama_models`, `ingest_uploads`) | Phase 1 adds a 4th |

---

## 2. Sequencing

Eight phases. The two prerequisites come first because export is meaningless
without them (spec §2, §3), and each phase is independently verifiable.

| Phase | Deliverable | Verified by |
|---|---|---|
| 1 | Source retention | E1, E2, E3 |
| 2 | Retrieval config persistence | E4 |
| 3 | Package format + export | E5, E6, E7 |
| 4 | `retrieve.py` generation | E8, E9, E10 |
| 5 | Import | E11–E15 |
| 6 | Model bundling | E21, E22 |
| 7 | Tuning + gold-standard invalidation | E16, E17, E18 |
| 8 | UI, packaging, docs | E19, E20 |

**Do not start Phase 3 before Phases 1 and 2 pass.** An exporter built first
would be specified against data that does not exist, and §5.2 would force it to
omit `retrieve.py` anyway.

---

## 3. File Manifest

```
api/
├── services/
│   ├── sources.py           # retention: store, look up, delete (Phase 1)
│   ├── retrieval_config.py  # per-collection retrieval settings (Phase 2)
│   ├── packager.py          # build and read packages (Phase 3, 5)
│   ├── exporter.py          # export job (Phase 3)
│   ├── importer.py          # import job (Phase 5)
│   └── model_bundle.py      # ollama manifest + blob copy (Phase 6)
├── routers/
│   ├── transfer.py          # /export, /import and their job endpoints
│   └── retrieval_config.py  # GET/POST /retrieval/config
└── templates/
    ├── retrieve.py.tmpl     # generated into every package (Phase 4)
    └── package_readme.md.tmpl
ui/src/pages/
├── TransferPage.tsx         # export / import
└── HelpTransferPage.tsx     # /help/transfer
exports/
└── .gitkeep                 # bind-mount target, contents never packaged
```

`retrieve.py.tmpl` and `package_readme.md.tmpl` are the single source required by
spec §10 — the help page renders from the same templates so the two cannot
drift.

---

## 4. Phase 1 — Source Retention  ✅ implemented

### 4.1 Volume

`docker-compose.yml`: add the `rag_sources` volume and mount it on `api` at
`/app/sources`. Add `SOURCES_DIR` to the api environment, defaulting to that
path, following the existing `UPLOAD_DIR` convention.

### 4.2 `services/sources.py`

```python
def store(collection: str, filename: str, data: bytes) -> str:
    """Content-address one uploaded file; return its sha256."""
    digest = hashlib.sha256(data).hexdigest()
    target = _dir(collection) / digest
    if not target.exists():                    # duplicates stored once (spec §2.2)
        target.write_bytes(data)
    _index_add(collection, digest, filename, len(data))
    return digest
```

`_index_add` updates `index.json` — digest → logical filenames, media type, size,
first seen. A repeat upload appends the filename rather than rewriting the file.

### 4.3 Ingest hook

In `ingest_pipeline.py`, the `finally: shutil.rmtree(tmp_dir)` stays. Retention
happens **before** it, as each file is accepted, so a parse failure does not
leave an orphan source. The digest is carried onto every chunk that file
produces, so `chunks.jsonl` can emit `source_sha256` later.

This is the only change to ingest, and it must not alter chunking, embedding or
any response shape.

### 4.4 Deletion

`_delete_collection_sync` gains a call to `sources.delete(collection)`. Spec §2.3
requires it: the volume is surfaced nowhere in the UI, so a leak here is
invisible.

**Phase 1 check:** ingest a file, confirm the bytes appear under
`/app/sources/<collection>/<sha256>` with an `index.json` entry (E1); ingest it
again and confirm one copy with two logical names (E2); delete the collection and
confirm the directory is gone (E3).

---

## 5. Phase 2 — Retrieval Config  ✅ implemented

`services/retrieval_config.py` for storage and defaults,
`routers/retrieval_config.py` for the endpoints — mirroring `ingest_config`
exactly, including the asymmetry the spec calls out: GET takes the collection in
the path, POST takes it in the body.

```python
@router.get("/retrieval/config/{collection}")
async def get_config(collection: str): ...      # returns defaults + is_default: true

@router.post("/retrieval/config")
async def save_config(body: SaveRetrievalConfigBody): ...
```

Then `ui/src/context/QueryConfigContext.tsx` reads and writes through these
instead of `sessionStorage`. Settings become per-collection and durable.

`POST /query` is unchanged: saved settings are defaults, not a constraint.

**Phase 2 check:** E4 — round-trip the endpoints, and confirm the built UI bundle
no longer contains `rag_query_config`.

**Result:** passed. The endpoints round-trip (defaults with `is_default: true`
before a save, persisted values with `is_default: false` after); the five
validation failures return 422 and valid edge values return 201; deleting the
collection removes the stored config. The built bundle contains no
`rag_query_config` (verified with a `rag_role` control proving the grep matches).
A headless-Chromium pass confirms the Retrieval page loads a collection's saved
settings, saves changes through the API, and that a brand-new browser context
with no shared storage sees those values — with zero console errors across all
pages. Breaking the GET path deliberately turned 8 of those checks red, so the
passes are meaningful; it also confirmed the intended fallback, where the page
degrades to defaults and stays usable rather than blanking.

**Deviation from spec:** `retrieval_mode` accepts four values, not three —
`semantic` was added. The Retrieval page has always offered it and
`rag_pipeline.run_query` has always implemented it (via `near_text`), so
rejecting it would have broken a working UI control.
`RAG_EXPORT_SPECIFICATIONS.md` §3 and `MCP_SPECIFICATIONS.md` were corrected to
match. An exported `retrieve.py` (Phase 4) therefore needs a `near_text` branch.

---

## 6. Phase 3 — Package Format and Export  ✅ implemented

Three modules: `services/packager.py` owns the on-disk format (writing and
reading a package, digests, manifest); `services/exporter.py` owns the export job
and its lifecycle; `routers/transfer.py` exposes `/export`, `/import` and their
job endpoints. Keeping format separate from job means Phase 5 reuses `packager`
rather than reimplementing the reader.

### 6.1 Reading chunks

```python
def read_chunks(col, collection: str):
    """Stream one record per chunk; never materialise the collection."""
    for obj in col.iterator(include_vector=True):
        vec = obj.vector["default"]        # dict, not a list — see §1
        # source_sha256 is NOT a stored property: spec §4.5 keeps the eight
        # chunk properties unchanged and makes it a sibling field. It is
        # resolved from the retention index instead.
        digests = sources.digests_for_filename(collection, obj.properties["source_file"])
        yield {"id": str(obj.uuid), "vector": vec,
               "properties": obj.properties,
               "source_sha256": digests[0] if len(digests) == 1 else None}
```

Stream straight to an open `chunks.jsonl`; never accumulate the collection in
memory. A 100k-chunk corpus is ~880 MB of JSON.

`digests_for_filename` returns a list because one logical filename can map to
several digests when the same name was ingested with different content. Exactly
one is the unambiguous case; anything else yields `null` and the exporter records
a warning rather than guessing which document a chunk came from.

### 6.2 Assembly order

Write `chunks.jsonl`, `collection.json`, the configs, `goldstandard/` and
`sources/` first; compute each digest as it is written; then write
`manifest.json` from those digests; then `README.md` and `retrieve.py`, which
read the manifest. Manifest last is what makes `<id8>` (spec §4.1) derivable.

Build under a temporary name in `/app/exports` and rename on success, so a
partial file is never mistaken for a package.

### 6.3 Job model

Reuse the ingest pattern: `POST /export` → `202` + `job_id`; `GET
/export/job/{job_id}` polls. One export per collection at a time, enforced by a
module-level set keyed on collection name — a second request returns
`EXPORT_IN_PROGRESS` with the running id.

**Phase 3 check:** E5 (filename matches §4.1), E6 (digests verify), E7 (fidelity
reflects whether sources exist).

**Result:** passed, verified by a validator that checks a built package against
every clause of §4 rather than by inspection.

- **E5** — `ragpkg-exportcheck-20260918T153127Z-c5c39e72.tar.gz` matches the
  pattern, expands to exactly one directory of the same name, and `<id8>` is
  confirmed to be the first 8 hex of the manifest digest.
- **E6** — every listed digest verifies, and no file in the package is unlisted.
  Negative control: appending one byte to `chunks.jsonl` and adding a stray file
  turned both checks red, so the check is meaningful.
- **E7** — a collection with retained sources exported `with-sources` with
  `sources/` present; a collection whose sources were removed (standing in for a
  pre-retention one) exported `chunks-only` with no `sources/` and a warning that
  one chunk could not be linked to a single document. `source_sha256` was
  recorded as null rather than guessed.

Also verified beyond the stated checks: `EXPORT_IN_PROGRESS` fires for a second
export of the same collection and names the running job, while a different
collection exports concurrently; the slot is released after both success and
failure; a build failed deliberately mid-assembly left no staging directory and
no partial archive; a 150-chunk export kept `/collections` and `/health` at
baseline latency (~8 ms), so §6.1's "MUST NOT block" holds; and `iterator`'s
`.vector` was confirmed first-hand to be a dict keyed `default` with 768 floats.

**Scope note:** §6.2's assembly order places `README.md` and `retrieve.py` inside
the export step, so both templates were written here. Phase 4 keeps E8–E10, which
exercise the script's behaviour. A smoke test already shows it answering a real
question with no third-party packages and exiting 3, 2 and 4 correctly.

**Spec correction:** §4.4's `files` map cannot include `retrieve.py` — that would
be circular, since the script embeds the manifest digest. `RAG_EXPORT_SPECIFICATIONS.md`
§4.4 now says so and explains why.

---

## 7. Phase 4 — `retrieve.py`  ✅ implemented

Rendered from `retrieve.py.tmpl` with collection name, parameters and provenance
substituted. Constraints from spec §5.1:

- **stdlib only** — `urllib.request`, `json`, `argparse`. Verify by running it in
  a bare `python:3.11-slim` container with nothing installed.
- targets `POST /query`, not Weaviate and Ollama directly
- baked defaults, overridable by flag
- exit codes 0/2/3/4/5
- one-sentence failures, never a traceback

```python
try:
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        result = json.load(r)
except urllib.error.URLError as e:
    sys.exit(_fail(3, f"Could not reach the RAG API at {args.api_url}. "
                      f"Is the stack running? ({e.reason})"))
```

If Phase 2 has not landed, the exporter **omits** the script and sets
`"retrieve_script": false` (spec §5.2). Shipping one with API defaults while
claiming tuned parameters is worse than shipping none.

**Phase 4 check:** E8, E9, E10.

**Result:** passed. Every run below was inside a bare `python:3.11-slim`
container with nothing installed, as this section requires.

- **E8** — `retrieve.py "who approves overtime?"` returned a real answer, exit 0.
  The only non-stdlib package present in that image is `packaging`, which the
  script does not import; its imports are `argparse`, `json`, `sys`, `urllib`.
- **E9** — against a package baked with `top_k: 3`, `--top-k 10` moved
  `chunks_retrieved` from 3 to 10, so the override changes behaviour rather than
  merely being accepted.
- **E10** — exit 3 with one sentence, in both shapes of "the stack is down":
  nothing listening on the port, and the API stopped while nginx still answers.

Eleven exit-code cases were checked in total (2, 3, 4, 5 and success), each also
asserting that no traceback reaches the user.

**Two defects this phase found and fixed**, both in error paths that Phase 3's
smoke test did not reach:

1. **A stopped API behind nginx exited 5**, reporting "The API rejected the
   query: Bad Gateway". The reference deployment always has a proxy in front, so
   this is the *likely* shape of a down stack, and E10 requires 3. Gateway
   statuses now map to 3 with a message naming the proxy.
2. **Any 404 was read as a missing collection**, so a mistyped `--api-url`
   produced "The API has no collection named …" and sent the user hunting for an
   import problem. Only an `error.code == "COLLECTION_NOT_FOUND"` envelope now
   means that; a bare 404 exits 5 and says to check the URL.

A third issue was fixed pre-emptively: the 300 s timeout was close to a measured
236 s generation, and a timeout was being reported as an unreachable API. The
default is now 600 s, `--timeout` is exposed, and a timeout has its own message.
`urllib` wraps a connect timeout in `URLError`, which subclasses `OSError`, so
the `URLError` branch has to unwrap `exc.reason` or it shadows the timeout case.

`RAG_EXPORT_SPECIFICATIONS.md` §5.1 now records `--timeout` and makes the three
non-obvious exit-code mappings normative.

---

## 8. Phase 5 — Import  ✅ implemented

`services/importer.py`, reading the package through `packager.py` so the format
has exactly one implementation.

### 8.1 Validation order

Exactly as spec §6.2, stopping at the first failure: readable archive →
`package_format` understood → every digest matches → **embedding model and
dimensions match** → collision handling. The embedding check is a refusal.

### 8.2 Atomicity

```
import into  <name>__importing_<id8>       # temporary, never user-visible
on success   rename/promote to the target name
on failure   delete the temporary collection; leave the target untouched
```

For `on_conflict=replace`, the existing collection is removed **after** the
temporary one has been built successfully, never before. This is what makes a
failed replace non-destructive.

### 8.3 Insertion

```python
with col.batch.dynamic() as batch:
    for rec in read_jsonl(pkg / "chunks.jsonl"):
        batch.add_object(properties=rec["properties"],
                         uuid=rec["id"],
                         vector=rec["vector"])       # verbatim; see §1
```

Preserving `uuid` keeps gold-standard references valid across the move — which is
the whole reason sessions are exportable.

**Phase 5 check:** E11–E15. E15 (no partial collection after a mid-import
failure) is the one most likely to be skipped and most likely to matter.

**Result:** passed.

- **E11** — a collection was exported, the instance wiped (collection, sources,
  configs), and the package imported. Chunk count reproduced (20). Re-exporting
  the imported collection and diffing against the original package gave
  **20/20 byte-identical vectors, UUIDs, properties and source links**, which
  also confirms §1's claim that an explicit vector survives the vectorizer.
  Retrieval was compared with a *fixed* query vector against the original and a
  second import of the same package: identical files, UUIDs and distances to six
  decimal places.
- **E12** — a package claiming `bge-large-en`/1024 was refused `EMBEDDING_MISMATCH`,
  naming both models and stating the re-embedding remedy.
- **E13** — a truncated package failed `PACKAGE_CORRUPT` naming `chunks.jsonl`
  and carrying both digests.
- **E14** — `abort` failed `COLLECTION_EXISTS`; `rename` imported as
  `Policies_imported_b602671d`; `replace` took a 21-chunk collection to 20 and
  removed the extra source document.
- **E15** — a package that passes every validation but carries one 700-dimension
  vector was imported two ways. To a new name: failed, **no collection left
  behind**. Over an existing collection with `replace`: failed, and the target
  survived intact — 20 chunks, 21 sources, retrieval config and queries all
  working. The post-staging recovery path was exercised separately by forcing
  the second pass to fail: the staging collection was kept with all 20 chunks
  and named in the error.

Also verified: validation runs in spec §6.2 order and stops at the first failure
(missing file, non-archive, absent manifest, future `package_format`, bad digest,
embedding mismatch, collision each produce their own code); `on_conflict` is
rejected as missing or invalid with 422; `IMPORT_IN_PROGRESS` fires for a
concurrent import of the same file (since #391 compared by its name in
`./exports`, and also failing an import job whose target collection another import
is building); and a package containing `../../../../tmp/evil-escaped.txt` was
refused `PACKAGE_UNREADABLE` with nothing written inside or outside the container.

**A defect this phase found and fixed:** a failed `replace` reported "The
imported data is available as 'Policies__importing_d4ce4033'" when the staging
build itself had failed and that collection had already been cleaned up. The
recovery hint now appears only when a staging collection actually survived.

**Two spec corrections**, both forced by the database rather than by preference:

1. §6.4's `rename` target `<name>-imported-<id8>` is rejected by Weaviate with a
   422 — hyphens are not legal in collection names. The separator is now an
   underscore.
2. §8.2's "build into a temporary collection and rename on success" is not
   implementable: **Weaviate has no rename**. `abort` and `rename` now build
   directly into a name that cannot already exist, and only `replace` stages
   first, paying a second insert pass to stay non-destructive. §6.5 records this.

**Deferred to Phase 7 as planned:** E18, the count of gold-standard sessions
orphaned by `replace`. Sessions travelling *with* a package are restored, and
their `collection` field is rewritten when a rename changed the name, because a
session pointing at a collection that does not exist is worse than no session.

---

## 9. Phase 6 — Model Bundling  ✅ implemented

`services/model_bundle.py`, called by both the exporter and the importer.

`include_models=true` copies, for each of the two models named in the manifest:

1. `models/manifests/registry.ollama.ai/library/<model>/latest`
2. every blob that manifest references, from `models/blobs/`

Resolving blobs from the manifest rather than copying the whole blob store is the
point: the store holds 2.3 GB across 9 blobs shared by both models, and a naive
copy would bundle unrelated weights.

On import, per spec §6.3: present by name → skip; absent and bundled → install
and verify it appears in `ollama list`; absent and not bundled →
`EMBEDDING_MODEL_MISSING`.

**Phase 6 check:** E21, E22.

**Result:** passed, against a genuinely stripped instance — `ollama rm
nomic-embed-text` was run first, with the model's manifest and four blobs backed
up to the host so the stack was recoverable without internet if the test failed.

- **E22** — importing a package built with `include_models: false` into that
  instance failed `EMBEDDING_MODEL_MISSING`, naming the model, the package's
  model and the pull command. No collection was created.
- **E21** — importing the `include_models: true` package (2.33 GB, built in
  2.5 min) installed the model and it appeared in `ollama list` **with the same
  ID, `0a109f422b47`**, that it had before removal. `phi3.5`, already present,
  was left untouched as §6.3 requires.

Verified beyond the stated checks: after the install, `/health` reports the
embed service ok, a query against the imported collection returns citations, and
a fresh ingest succeeds — which exercises Weaviate's `text2vec-ollama` path, a
different route into Ollama than the API's own embed call. All five services
returned to healthy.

**Fidelity was proven by content address, not by inference.** Every installed
blob's real SHA-256 equals both the pre-removal backup and the digest its
manifest claims, and the manifest file itself is byte-identical. The restored
model is bit-for-bit the one that produced the vectors.

A first attempt to prove this behaviourally was **my error**: re-embedding a
chunk's `content` through `ollama_client.embed` gave cosine 0.926 against the
stored vector, which looked alarming. The comparison was invalid — Weaviate's
`text2vec-ollama` vectorises all TEXT properties concatenated, not `content`
alone, and normalises the result to unit length (the stored vector's norm is
exactly 1.0; the raw Ollama vector's is 19.3). Content addressing answers the
question directly and without that confound.

Only the blobs each model's manifest names are bundled: 4 for
`nomic-embed-text`, 5 for `phi3.5`, 11 files in total including both manifests,
each digested into the package manifest's `files` map.

---

## 10. Phase 7 — Tuning and Invalidation  ✅ implemented

- Re-chunk and re-embed operate from `sources/`; both rebuild the collection.
- A `chunks-only` collection rejects re-chunking with `SOURCES_REQUIRED`, and
  rejects re-embed **combined with** new chunking parameters rather than silently
  ignoring one (spec §7.2).
- Any operation changing chunk identity marks every gold-standard session for
  that collection `stale`, with reason and timestamp. Sessions are never deleted
  and never remapped — a wrong remap corrupts an evaluation baseline silently.

**Phase 7 check:** E16, E17, E18.

**Result:** passed.

- **E16** — re-chunking `Tunecheck` from `overlap/400` to `fixed/150` took it
  from 10 chunks to 39 and marked its gold-standard session `stale` with a
  reason and a fresh timestamp. The session's pairs were retained, not deleted.
- **E17** — a collection whose sources were removed refused `/tune/rechunk` with
  `SOURCES_REQUIRED`, and refused `/tune/reembed` carrying chunking fields with a
  message saying to send one or the other (§7.2). The collection was intact after
  both refusals.
- **E18** — `on_conflict=replace` reported "2 gold-standard session(s) from the
  replaced collection were kept and marked orphaned". The session that travelled
  in the package was restored and unflagged (it is valid again); the one that
  existed only on the target stayed orphaned, with its pairs intact.

Also verified: `/tune/reembed` on a `chunks-only` collection is permitted and
leaves chunk count unchanged (39 → 39); `/tune/reindex` moved the collection from
`hnsw/cosine` to `flat/l2-squared` and correctly did **not** mark sessions stale,
because chunk identity did not change; `TUNE_IN_PROGRESS` fires on a concurrent
tune; unknown collections give 404 and bad strategy/index/distance give 422; and
deleting a collection orphans its sessions rather than deleting them (spec §8
rule 1).

**A defect this phase found and fixed.** Gold-standard sessions are cached in
memory as well as on disk, and import wrote restored session files *directly*.
The cache kept the pre-import version, so the next flagging pass wrote it back
over the restored file — a session restored from a package silently reverted to
the orphaned state of the collection it replaced. Import now writes through
`goldstandard.store_session()`, which updates both. Verified by re-running E18
and then triggering a re-embed: the restored session keeps `orphaned=False`,
where before it flipped back to `True`.

**Two things found that are not Phase 7's to fix**, reported rather than changed:

1. `goldstandard._run_generation` increments `pairs_completed` in a `finally`, so
   it counts *attempts*, not successes. A session whose LLM output failed to
   parse reports `2/2 complete` while holding one pair. Fixing it properly is a
   UI question — a progress bar that counts only successes would appear stuck —
   so it needs a decision rather than a one-line change.
2. `chunk_overlap` uses LangChain's `CharacterTextSplitter`, which splits on
   `\n\n` and then *merges* up to `chunk_size`; it never splits a run of text
   containing no separator. Reducing `chunk_size` on single-paragraph documents
   therefore changes the recorded parameters but not the boundaries. This is
   pre-existing chunker behaviour, not a tuning bug, but it makes `overlap` a
   poor choice for demonstrating a re-chunk.

---

## 11. Phase 8 — UI, Packaging, Docs  ✅ implemented

| Item | Change |
|---|---|
| `TransferPage.tsx` | export (collection, include-models, fidelity shown before export) and import (file from `./exports`, `on_conflict` required) |
| `HelpTransferPage.tsx` | `/help/transfer`, rendered from the same templates as the package `README.md` |
| `NavBar.tsx` | link for AI Engineer and Developer roles |
| `docker-compose.yml` | `rag_sources` volume; `./exports:/app/exports` on api |
| `package.sh` / `package-offline.sh` | include `exports/` as an **empty** directory, exclude its contents |
| `README.md` | disk guidance becomes "plus the size of your document corpus" |

**The `exports/` packaging trap is known.** Excluding a directory's contents also
drops the directory in both `zip` and `tar` — exactly what happened with
`ingest-inbox/`. `package.sh` re-adds `.gitkeep` after the main archive;
`package-offline.sh` recreates the directory in its staging area.

**Phase 8 check:** E19, E20.

**Result:** passed.

- **E19** — the package `README.md`, regenerated from the rewritten templates,
  states the collection name, the fidelity and the "not encrypted" warning, with
  no unsubstituted placeholders.
- **E20** — `docker compose down && docker compose up -d` brings up all five
  services, `./exports` is mounted into the api container, and a file created on
  the host appears inside it. `package.sh` still ships `exports/.gitkeep` while
  excluding a planted `ragpkg-secret.tar.gz`.

**§10's shared-source requirement is met and demonstrated.** The six partials in
`api/templates/partials/` were each checked to appear in both a generated package
README and the rendered help page. `retrieve_usage` appears in a package README
only when that package ships `retrieve.py`, which was confirmed by exporting a
collection with saved retrieval settings.

Browser verification (headless Chromium, zero console errors throughout):

- The **Transfer** nav link is present for AI Engineer and Developer and hidden
  for End User.
- The help page renders 7.3 KB of content, three markdown tables, and headings
  larger than body text; all eight sections §10 requires are present.
- A full round trip **through the UI**: export → the package appears in the
  import list → import with `rename` → "Imported as Uicheck_imported_e139acae
  (renamed from Uicheck)".
- SPA navigation across all nine pages with no errors.

**Two UI gaps this phase found**, both pre-existing rather than introduced:

1. `@tailwindcss/typography` was never installed, so every `prose` class compiled
   to nothing — the built CSS contained zero prose rules. Tailwind's preflight
   has already stripped heading and list styling, so the Q&A answer pane has been
   rendering markdown as one undifferentiated block, and the help page would have
   too. Installed and verified in the browser: `h1` is 24px against 14px body.
2. `remark-gfm` was missing, so `react-markdown` could not render tables. §10
   requires the §7.1 tuning table, so it is now a dependency.

A CSS build warning (`-: T;`) was checked and is **not** from either addition —
it appears with the typography plugin removed as well, so it predates this phase.

**A harness bug, not a product bug.** The first browser run reported the export
failing. The export had in fact succeeded: the test regex
`/ragpkg-[a-z0-9-]+\.tar\.gz/` could not match the uppercase `T` and `Z` in the
timestamp segment. Traced with a request-level trace before changing anything.

---

## 11a. Offline bundle — regenerated and tested

`rag-docker-offline.tar` was rebuilt after Phase 8 and verified by installing it
from nothing, rather than by inspecting it.

**Build:** 4.6 GB in 5m18s — 2.5 GB of images, 2.2 GB of model weights. The
script's own header still claimed "roughly 5-7 GB" and was corrected to the
measured figure.

**Test, in this order so that nothing could come from cache:**

1. The bundle was moved out of the project directory.
2. The stack was taken down and **every Docker image on the machine was deleted**
   (`docker images -q` returned nothing afterwards).
3. The bundle was extracted to `airgapped-rag-v2` — deliberately *not* the
   original directory name, because compose derives the project name from the
   directory and the explicit `image:` tags exist to survive that.
4. `install-offline.sh` brought all five services up in 1m58s with `--no-build`.

**Results.** Both models were restored with the **same IDs** they had on the
source machine (`nomic-embed-text` 0a109f422b47, `phi3.5` 61819fb370a3), so the
weights came from the bundle rather than a download. `/health` reported ok across
Weaviate and both Ollama models.

Every phase was then exercised on the fresh install: ingest with retention
(5 chunks, 6 retained files), retrieval config, export (package validated against
every §4 clause), `retrieve.py` answering a real question, import with `rename`,
the model store visible to the API, re-chunking 5 → 19 chunks, and the help page
rendering 7431 characters with no unsubstituted placeholders. The browser suite
passed in full against the offline install, including a complete export → import
round trip through the UI.

The rename in that round trip produced `Airgap_imported_83e30991_2`: the base
name was already taken by the earlier API-driven import, so Phase 5's numeric
suffix path fired unplanned and worked.

**Two notes on the test itself.** `python:3.11-slim` had been deleted along with
everything else, so the first `retrieve.py` run pulled it — that is the test
harness reaching for an image, not the product needing one, and the run was
repeated using the bundled api image to keep the test genuinely offline. Separately,
a first pass at grepping the served UI bundle reported every feature missing; the
`rag_role` control also failed, which showed the fault was shell capture of a
750 KB file rather than the bundle. Re-checked from a file, all Phase 8 features
were present and `rag_query_config` correctly absent.

---

## 11c. Bundle regenerated after the acceptance-criteria work

`rag-docker-offline.tar` was rebuilt on 2026-09-21, after the ten fixes made
while verifying the base platform's 32 acceptance criteria, and re-verified by
installing it from nothing.

**Build:** 4.6 GB in 5m06s.

**Verification, in this order:**

1. All fifteen changed source files were confirmed present inside the archive.
2. The saved image IDs were compared against the local ones — `rag-docker-api`
   and `rag-docker-ui` both matched exactly, so the bundle carries the rebuilt
   images and not a stale save. Shipping corrected source next to a stale image
   would be the worst of both.
3. Every image on the machine was deleted, the bundle extracted to
   `verify-install-v3` (again not the original directory name), and
   `install-offline.sh` run: five services healthy in 2m26s, models restored
   with their original IDs.

**On the fresh install**, the session's fixes were exercised rather than assumed:
`.md` files ingested, an unsupported file was reported in `skipped`, a
gold-standard session generated 2/2 with consistent counters, the PATCH guard
returned 422 for a content-only edit and 200 with `status="edited"`, and an
export/import round trip completed leaving no extraction directory behind.

The new startup sweeps matter here: they run on every boot, so a fault in them
would stop a fresh install dead. On a clean instance they produced no output and
no traceback, which is correct — there is nothing to clean.

**Not covered by this run:** the 409-while-generating guard. Generation finished
before a regenerate could overlap it, so that check rests on the dev-stack
verification rather than this one.

---

## 11b. Closing out E12 and E15

Two criteria had been verified by a route other than the one §12 specifies. Both
were re-run as written, and one of them found a real defect.

**E12 — "set `EMBED_MODEL` to another model".** Previously tested by editing a
package's manifest to claim a different model, which exercises the same
comparison from the other side. Re-run properly with a temporary compose
override setting `EMBED_MODEL: bge-large-en`: the import failed
`EMBEDDING_MISMATCH` naming both models and the remedy, and created nothing. The
override was removed and the instance restored. Worth noting: with the configured
embedding model absent, `/health` correctly returns **503**, which is why a
`curl -sf` health gate hangs in that state.

**E15 — "kill the api mid-import".** Previously tested with a package that fails
during insertion, which exercises the handled-failure path. A `SIGKILL` is a
different failure mode: it skips the `finally` blocks that all of that cleanup
lives in.

Run as specified, it **failed**. Killing the api during a `replace` left
`Killtest__importing_460a88a5` in Weaviate permanently, across restarts. The
target collection survived intact at every kill point, so the non-destructive
guarantee held — but an abandoned staging collection is not merely untidy: it is
returned by `GET /collections` and appears in the UI's collection pickers as
though it were real.

Historical fixes (superseded by the ownership rules in the note below):

1. `weaviate_client.sweep_staging()` removes any collection carrying the
   `__importing_` or `__tuning_` marker.
2. `importer.sweep_interrupted_imports()` handles the case the markers cannot
   reach — `abort` and `rename` build straight into the target, so a kill during
   their insert would leave a half-filled collection that looks ordinary. A
   marker file written before the build records the expected chunk count; at
   startup a collection is deleted only when its actual count differs.

> **Note, 2026-10-03 (#126):** both fixes above have since been replaced, and the
> rest of this section is history. Since #61, names never authorize deletion:
> `sweep_staging()` removes only a staging collection named by a valid ownership
> record in `UPLOAD_DIR/collection_operations/` in state `scratch` or `cleanup`,
> and keeps retained recovery. The import marker is bound to a hash-checked SQLite
> snapshot of the expected records, and startup compares records, not counts.
> Since #126 the marker (version 4) also carries an instance token that the import
> writes into the collection's schema description; startup deletes only the
> collection carrying that token, and removes expectation snapshots no marker
> names. The current rules are in `RAG_EXPORT_SPECIFICATIONS.md` §6.5.1.

The count comparison was verified in both directions, because the failure mode
matters more than the success: a marker left over from a *successful* import
(expected 720, actual 720) left the collection untouched, while a marker claiming
5000 against 720 removed it and logged
`Removed 1 collection(s) left half-built by an interrupted import: Killtest
(720 of 5000 chunks). Re-import the package to try again.`

Re-running the kill sweep afterwards: at every point across the import, the
target ends intact and no staging collection survives the restart.

**Timing note for anyone repeating this.** A clean import of 720 chunks takes
1.8 s, and the insert occupies roughly t+1.2 s to t+1.8 s of it. Kills outside
that window prove nothing, and the first three attempts here landed after the
job had already finished. The job's `chunks_written` counter is not a usable
trigger either: it advances every 500 chunks, so it reads 0 through most of a
small import.

Nothing found here is unrecoverable in any case: import never modifies or
deletes the package it read, so re-importing always restores the collection.

---

## 12. Acceptance Mapping

All 22 criteria are defined in spec §13.

| # | How to run it |
|---|---|
| E1 | Ingest a file; `docker run --rm -v rag-docker_rag_sources:/s alpine ls /s/<collection>` |
| E2 | Ingest the same file twice; one blob, two names in `index.json` |
| E3 | `DELETE /collections/{name}`; source directory gone |
| E4 | `GET`/`POST /retrieval/config` round-trip; `rag_query_config` absent from the built bundle |
| E5 | `POST /export`; filename matches the §4.1 regex |
| E6 | Recompute every digest in `manifest.files` |
| E7 | Export a retained collection and a pre-retention one; compare `fidelity` |
| E8 | `docker run --rm -v <pkg>:/p python:3.11-slim python /p/retrieve.py "q"` — no installs |
| E9 | `--top-k 10` changes `chunks_retrieved` |
| E10 | `docker compose stop api`; script exits 3 with one sentence |
| E11 | Import into a clean instance; chunk count matches; a query returns an equivalent answer |
| E12 | Set `EMBED_MODEL` to another model; import fails `EMBEDDING_MISMATCH` |
| E13 | Truncate `chunks.jsonl`; import fails `PACKAGE_CORRUPT` naming it |
| E14 | Import three times with `abort`, `rename`, `replace` |
| E15 | Kill the api mid-import; assert no `__importing_` collection and no partial target |
| E16 | Re-chunk; sessions become `stale` |
| E17 | Re-chunk a `chunks-only` collection; `SOURCES_REQUIRED` |
| E18 | `replace` a collection with sessions; orphan count reported |
| E19 | Package `README.md` names the collection, fidelity and the encryption warning |
| E20 | `docker compose up -d` starts five services with `./exports` mounted |
| E21 | Remove the embedding model; import a `with-models` package; `ollama list` shows it |
| E22 | Same, with a package lacking models; `EMBEDDING_MODEL_MISSING` |

---

## 13. Risks During Implementation

| Risk | Symptom | Response |
|---|---|---|
| Treating `obj.vector` as a list | `TypeError` on export | Verified fact §1; read `["default"]` |
| Loading a collection into memory | OOM on large corpora | Stream via `iterator`, write as you go |
| Manifest written before file digests | `<id8>` cannot be derived | Assembly order §6.2 |
| `replace` deletes before import succeeds | Data loss on a failed import | Delete only after promotion (§8.2) |
| Dropping `uuid` on insert | Gold-standard references break silently | Pass `uuid` explicitly |
| `exports/` directory vanishes from packages | Bind mount source missing; Docker creates it root-owned | Re-add `.gitkeep` / recreate in staging (§11) |
| Retention applied after parse | Failed parses leave orphan sources | Store on acceptance, before the `finally` |
| Copying the whole blob store | 2.3 GB bundled regardless of models | Resolve blobs from the model's manifest (§9) |
| Shipping `retrieve.py` before Phase 2 | Script silently carries API defaults | Omit it and record `retrieve_script: false` |

---

## 14. Definition of Done

- All 22 acceptance criteria pass.
- `docker compose up -d` still starts exactly five services.
- A package exported on one instance imports into a clean one and answers
  equivalently, with no network access.
- The package `README.md` and `/help/transfer` are rendered from the same
  templates.
- `README.md` disk guidance reflects corpus-proportional storage.
