# RAG Platform

A self-contained, Dockerized Retrieval-Augmented Generation (RAG) platform. Upload documents, ask questions, and generate evaluation datasets — all running locally with no external API keys required.

## What's included

| Component | Technology | Purpose |
|---|---|---|
| Vector store | Weaviate 1.39.6 | Stores and retrieves document chunks |
| LLM + embeddings | Ollama 0.3.14 | Runs phi3.5 (chat) and nomic-embed-text (embeddings) locally |
| API | FastAPI + Python 3.11 | RAG pipeline, ingest, gold standard generation |
| UI | React 18 + Vite + Tailwind | Three-role web interface |
| Proxy | nginx 1.29 | Routes traffic; listens on port 80 in-container, published on host port 8080 |

## Prerequisites

- **Docker Desktop with Engine 28.0.0 or newer** (or Docker Engine 28.0.0+ with Compose v2) — [install](https://docs.docker.com/get-docker/)
- **12 GB RAM and 2 GB swap allocated to Docker** (Docker Desktop → Settings → Resources → Memory and Swap). phi3.5 alone is ~6 GB resident; at 10 GB, full verification runs still hit Ollama timeouts under memory pressure, and below that the model is repeatedly evicted and reloaded and queries time out. Swap turns a short spike into a slowdown instead of a failure. Leave macOS at least 4 GB: on a 16 GB Mac, 12 GB is the practical ceiling. `/health` reports what Docker actually has against this recommendation
- **20 GB disk minimum allocated to Docker, 32 GB recommended** — see [Storage requirements](#storage-requirements) below. Check your current limit before building — a small virtual disk (8 GB or so) cannot hold this stack, and the build fails partway through with a confusing error.
- Port **8080** free on the host — `docker-compose.yml` publishes the proxy as `127.0.0.1:8080:80`, bound to host loopback on supported engines

Check the daemon version with `docker version --format '{{.Server.Version}}'`. Docker documents that engines older than 28.0.0 allow same-network hosts to reach localhost-published ports; upgrade the engine before using this unauthenticated workbench. See [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/#publishing-ports). This default assumes Docker's standard bridge/NAT configuration; custom direct-routing settings are outside this local profile.

No API keys, no cloud accounts, no Python or Node installs required on your machine.

### Storage requirements

All of this lives inside Docker's virtual disk, not your host filesystem. Measured on a clean build (Apple Silicon, linux/arm64):

| What | Size | When |
|---|---|---|
| `ollama/ollama` image | 3.2 GB | first pull |
| `rag-docker-api` image | 2.8 GB | build |
| `rag-docker-ui` image | 149 MB | build |
| `weaviate` image | 200 MB | first pull |
| `nginx` image | 50 MB | first pull |
| **Images after a full build** | **~6.5 GB** | |
| Model weights (phi3.5 2.2 GB, nomic-embed-text 274 MB) | ~2.5 GB | first run, into the `ollama_models` volume |
| **Total steady state** | **~9.0 GB** | |

Build cache adds several GB on top, though much of it shares layers with the images rather than duplicating them. A rebuild also holds the old and new image layers at once until you prune.

**Allocate 20 GB minimum.** That covers the steady-state ~9.0 GB plus build cache and rebuild headroom. **32 GB is recommended** if you plan to ingest a meaningful document corpus, since the Weaviate index, the uploaded files and the retained originals all grow on top of everything above.

Three things scale with your corpus rather than with the stack:

- the **Weaviate index** and its vectors,
- **retained source documents** (`rag_sources`), which hold one copy of every ingested file so a collection can be exported at `with-sources` fidelity and re-chunked later,
- **export packages** in `./exports`, which live on the *host* filesystem rather than Docker's virtual disk. A package is roughly the size of the corpus it came from, and about 2.3 GB larger if exported with `include_models: true`.

So budget the figures above **plus the size of your document corpus** — counted twice if you keep exports of it alongside the live collections.

> The api image is 2.8 GB rather than 7.6 GB because `api/Dockerfile` installs CPU-only torch from the PyTorch CPU index before resolving `requirements.txt`. The default PyPI torch wheel drags in ~4.1 GB of NVIDIA CUDA and Triton packages that cannot execute without an NVIDIA GPU. See the comment in `api/Dockerfile` before changing those pins.

To change the disk allocation: **Docker Desktop → Settings → Resources → Virtual disk limit**, then apply and restart. On Docker Engine (Linux) there is no virtual disk — the limit is simply free space on `/var/lib/docker`.

Reclaim space at any time with `docker builder prune` (build cache only) or `docker system prune -a` (also removes unused images, forcing a full rebuild).

## Quick start

```bash
# 1. Clone and enter the project
git clone <repo-url> rag-docker
cd rag-docker

# 2. Start the stack
docker compose up -d

# 3. Watch Ollama download the models (first run only — ~5 minutes)
docker compose logs -f ollama

# 4. Open the UI once all services are healthy
open http://localhost:8080       # macOS
# or visit http://localhost:8080 in your browser
```

The stack is ready when `docker compose ps` shows **weaviate**, **ollama**, and **api** as healthy. The ui and proxy services have no healthcheck and show as running — wait an additional 10–15 seconds after api becomes healthy before opening the browser.

## First run timeline

Build times below are measured on Apple Silicon; download times are estimated
from transfer size at roughly 90 Mbps. Both downloads and pulls are
bandwidth-bound, so a slower link stretches them proportionally.

| Time | What happens |
|---|---|
| 0s | Docker starts pulling base images (~3.8 GB on disk: ollama 3.2 GB, weaviate 200 MB, python 158 MB, node 136 MB, nginx 50 MB) and building api and ui in parallel |
| ~1min | Weaviate starts — its image is only 120 MB, so it is ready well before the others |
| ~4min | api image finishes building. That build measures **~3 min from scratch**: apt 32s, pip install 97s, sentence-transformers pre-cache 19s. The ui image finishes sooner |
| ~5min | The 3.2 GB ollama image finishes pulling; the container starts and immediately begins downloading models |
| ~10min | phi3.5 (2.2 GB) and nomic-embed-text (274 MB) finish; ollama passes its healthcheck and api, ui and nginx start |

**Total first run: roughly 10–20 minutes**, dominated by downloads rather than CPU.
Note that nothing can pull models until the ollama image itself has downloaded,
so the model wait begins several minutes in — the stack is not stuck.

On subsequent starts everything is immediate: the images are built and the model weights are cached in the `ollama_models` Docker volume.

## Roles

The landing page asks you to choose a role. Role is stored in `sessionStorage` and persists across page refreshes within the same browser tab.

| Role | Access |
|---|---|
| **End User** | Q&A only |
| **Developer** | Q&A, Import, Chunking, Retrieval, Gold Standard |
| **AI Engineer** | All of the above + Collections management + Health dashboard |

## Workflow

### 1. Create a collection

**AI Engineers** can manage collections from the dedicated **Collections** page. **Developers** can create a collection inline from the Import page using the **+ New** button next to the collection selector.

A collection is a named vector index that holds chunks from one or more documents. Choose HNSW (approximate, fast) or Flat (exact, slower at scale).

### 2. Ingest documents

Navigate to **Import**, drop in your files (PDF, DOCX, TXT, MD, CSV, JSON, JSON Lines (`.jsonl`, `.jsonl.gz`), or ZIP), choose a collection, and click **Start Ingest**. A ZIP may also contain gzipped JSON (`.json.gz`) files. The page polls for progress automatically.

Uploading a file again under the same name replaces its earlier chunks (even if the content is unchanged); files inside a ZIP are named by their path in the archive, so `a/report.pdf` and `b/report.pdf` stay separate documents. macOS metadata in a ZIP (the `__MACOSX/` folder and `._*` files the Finder adds) is not ingested; the job lists it under skipped files.

Supported chunking strategies:

| Strategy | Best for |
|---|---|
| `overlap` | General-purpose — recommended default |
| `language` | Narrative text where sentence boundaries matter |
| `context_aware` | Structured documents with headings and tables |
| `semantic` | Dense documents; groups sentences by topic similarity |
| `fixed` | Structured data (CSV, JSON) |

### 3. Configure retrieval

Navigate to **Retrieval** to choose a retrieval mode and top-K value for a collection. Settings are saved **per collection on the server**, so they survive a restart, are shared by every browser, and travel with a RAG export. The Q&A page loads the settings for whichever collection you have selected.

| Mode | Description |
|---|---|
| Vector — existing index | Similarity search using the collection's physical index |
| Hybrid | BM25 keyword + vector; tune the alpha slider |
| Semantic | Pure meaning-based via Weaviate's text2vec-ollama |

The API accepts `hnsw` and `flat` as aliases for Vector; selecting either does not change the collection's physical index. Exact KNN requires a collection created with a Flat index.

### 4. Ask questions

Navigate to **Q&A**, select a collection, type your question, and press **Ask** (or Ctrl+Enter). Toggle **Show source citations** to see which chunks were used.

### 5. Generate gold standard data

Navigate to **Gold Standard**, choose a collection and sample size, and click **Generate Pairs**. The platform samples chunks and uses the LLM to generate question/answer pairs. The page shows how the generation ended, including pairs that failed and why. Review each pair (approve, edit, reject, or regenerate), then export to a RAGAS-compatible JSON file.

## Health check

Navigate to **Health** (AI Engineer role) to see live status and latency for Weaviate, the LLM, and the embedding model. Latency charts populate after the first few Q&A queries.

You can also hit the API directly:

```bash
curl http://localhost:8080/api/health
```

The response includes a `resources` block reporting the memory Docker actually
provides against the recommended minimum:

```json
{
  "resources": {
    "memory": {
      "status": "ok",
      "allocated_gb": 11.67,
      "recommended_minimum_gb": 12.0
    }
  }
}
```

(shown on its own; it sits alongside the existing `status` and `services` keys)

`allocated_gb` reads `MemTotal` inside the container, which on Docker Desktop is
the VM's total memory. It reads a little **below** the figure configured in
Docker Desktop — 12288 MiB configured shows as 11.7 GB, about 5% lost to VM
overhead — so the comparison allows a 5% margin rather than flagging a correctly
sized allocation as too small.

When memory is short, `status` becomes `below_recommended` and a `note` explains
what to change. This never fails the endpoint: low memory slows the stack but
does not break it, and returning 503 would mark the api container unhealthy and
take the whole stack down over a tuning warning.

## Useful commands

```bash
# Start in the background
docker compose up -d

# Stop
docker compose down

# Stop and wipe all data (volumes)
docker compose down -v

# View logs for a specific service
docker compose logs -f api
docker compose logs -f ollama

# Rebuild after code changes
docker compose build api ui
docker compose up -d

# Check service health
docker compose ps
```

## GPU acceleration (optional)

To use a CUDA GPU for Ollama, uncomment the `deploy` block in `docker-compose.yml`:

```yaml
ollama:
  # ...
  deploy:
    resources:
      reservations:
        devices:
          - capabilities: [gpu]
```

Requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed on the host.

## Configuration

All API settings are environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `LLM_MODEL` | `phi3.5` | Ollama model name for chat |
| `EMBED_MODEL` | `nomic-embed-text` | Ollama model name for embeddings |
| `WEAVIATE_HOST` | `weaviate` | Weaviate hostname (internal) |
| `OLLAMA_HOST` | `ollama` | Ollama hostname (internal) |
| `UPLOAD_DIR` | `/app/uploads` | Container path for uploads and session data |
| `RECOMMENDED_MEMORY_GB` | `12` | Memory recommendation reported by `/health`. Raise it if you allocate more to Docker; no rebuild needed |
| `MAX_ZIP_EXPANDED_BYTES` | `2147483648` (2 GiB) | The most one upload's ZIPs may expand to in all, gzipped members' output included. Over it the upload is refused with `ARCHIVE_TOO_LARGE` |

To swap the LLM (e.g. to `llama3.2`), update `LLM_MODEL` in `docker-compose.yml` and add the model name to `ollama/entrypoint.sh`.

**Upload size limit.** One upload (all files in it together, or one ZIP) can be at most **512 MB**. The proxy enforces it with `client_max_body_size` in `proxy/nginx.conf`. To change it, edit that value and `MAX_UPLOAD_MB` in `ui/src/api/client.ts` (the Import page uses it to warn before sending), then rebuild the UI and recreate the proxy:

```bash
docker compose build ui
docker compose up -d --force-recreate ui proxy
```

`--force-recreate` matters for the proxy. `nginx.conf` is mounted as a single file, and most editors save by replacing the file, so a running container keeps reading the old copy until it is recreated.

## Data persistence

Four named Docker volumes persist data across restarts:

| Volume | Contents |
|---|---|
| `weaviate_data` | Vector index and all ingested chunks |
| `ollama_models` | Downloaded model weights |
| `ingest_uploads` | Ingest configs, gold standard sessions, exported gold-standard JSON files (`goldstandard_exports/`), metrics ring buffer (`metrics.jsonl`, bounded to 1,000 lines) |
| `rag_sources` | Original uploaded documents, content-addressed per collection. Grows with your corpus |

Run `docker compose down -v` to delete all volumes and start fresh.

## Project structure

```
rag-docker/
├── package.sh            # Builds a source-only zip (~1.3 MB; target needs internet)
├── package-offline.sh    # Builds a self-contained offline bundle (~4.6 GB)
├── install-offline.sh    # Installs from that bundle with no network access
├── api/                  # FastAPI backend
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── requirements.in   # Direct dependencies — edit this one
│   ├── requirements.txt  # Generated lock — all packages pinned; do not hand-edit
│   ├── main.py
│   ├── config.py
│   ├── utils.py
│   ├── models/           # Pydantic schemas
│   ├── routers/          # API route handlers
│   ├── templates/        # Rendered into every export package
│   └── services/         # Business logic (RAG, ingest, chunker, packaging, etc.)
├── ui/                   # React frontend
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── package.json
│   ├── package-lock.json # Required — `npm ci` fails without it
│   ├── src/
│   │   ├── api/          # API client
│   │   ├── components/   # Shared UI components
│   │   ├── context/      # Role and query config state
│   │   └── pages/        # One file per page
├── ollama/
│   └── entrypoint.sh     # Pulls models on first start
├── proxy/
│   └── nginx.conf        # Routes /api/ to API, / to UI
├── exports/              # Export packages land here (bind mount)
└── docker-compose.yml
```

## Exporting and importing a RAG

> In the web UI this is the **Transfer** page, with a help page at
> **/help/transfer** that explains the whole process. That page is generated from
> the same templates as the `README.md` inside every package, so it cannot drift
> from what actually ships.


A collection can be exported as a single portable `.tar.gz` — its chunks and
vectors, the settings it was tuned with, a standalone query script, and (when
the originals were retained) the source documents themselves.

```bash
# start an export
curl -X POST http://localhost:8080/api/export \
  -H 'Content-Type: application/json' \
  -d '{"collection": "Policies"}'

# poll it
curl http://localhost:8080/api/export/job/<job_id>

# list what is in ./exports
curl http://localhost:8080/api/packages
```

The package lands in `./exports/` on the host, named
`ragpkg-<collection>-<timestamp>-<id8>.tar.gz`. Every package carries its own
`README.md` explaining what it contains, how to import it and how to run its
`retrieve.py`.

Two things worth knowing before you move one:

- **Packages are not encrypted.** A `with-sources` package contains the original
  documents byte for byte. Treat the file as you would the corpus.
- **The embedding model must match.** Vectors made by a different embedding
  model are meaningless in this vector space, not merely different, so import
  refuses the mismatch rather than warning about it.

`./exports/` is a bind mount so you can pick packages up directly. Its contents
are never included when you package the project with `package.sh` or
`package-offline.sh` — the directory ships, the corpus does not.

To import one, drop the `.tar.gz` into `./exports/` on the target machine and:

```bash
curl -X POST http://localhost:8080/api/import \
  -H 'Content-Type: application/json' \
  -d '{"filename": "ragpkg-policies-...tar.gz", "on_conflict": "abort"}'

curl http://localhost:8080/api/import/job/<job_id>
```

`on_conflict` is required and has no default, because the wrong choice can
delete a collection:

| Value | Behaviour |
|---|---|
| `abort` | fail if a collection of that name already exists |
| `rename` | import alongside it as `<name>_imported_<id8>` |
| `replace` | overwrite it — but only after the incoming package has been proven to import cleanly. A failure before the existing collection is deleted leaves it as it was; a failure while the final copy is being built can leave the name missing or partial, with the incoming data kept in a recovery collection named in the error (`recovered_as`) |

The package is validated before anything touches the database: the archive must
be readable, the format understood, every digest must match, and the embedding
model must be the one this instance runs. Chunk UUIDs are preserved, so
gold-standard sessions keep pointing at the right chunks after the move.
Retained-source indexes and blobs are validated before the embedding check;
invalid source metadata fails with `PACKAGE_CORRUPT` before any live mutation.

Evaluation-session metadata is also validated before importing models or
changing a collection. Invalid session JSON, identities or schemas fail the
import with `PACKAGE_CORRUPT`, naming the sidecar; they are not silently skipped.

### Carrying the models too

By default a package assumes the target machine already runs the same embedding
model — and import refuses if it does not, because vectors from another model are
meaningless rather than merely different. For a machine that has never pulled
anything, export with the models included:

```bash
curl -X POST http://localhost:8080/api/export \
  -H 'Content-Type: application/json' \
  -d '{"collection": "Policies", "include_models": true}'
```

That takes the package from roughly 80 KB to ~2.3 GB, because it carries the
embedding model and the LLM as Ollama's own manifest and blob files. On import,
a model already present is left alone, provided its files match their checksums;
a missing one is installed from the package and verified before the collection
is built. If the embedding model is present but damaged, the import fails
`MODEL_INTEGRITY_FAILED`: restore or re-pull it, then import again. The models are
content-addressed, so what lands on the target is byte-identical to what produced
the vectors.

### Tuning an imported collection

Once a collection is in, it can be rebuilt in place:

```bash
# re-split the original documents differently (needs with-sources)
curl -X POST http://localhost:8080/api/tune/rechunk \
  -H 'Content-Type: application/json' \
  -d '{"collection": "Policies", "chunking_strategy": "fixed", "chunk_size": 600}'

# regenerate vectors, leaving chunk boundaries alone
curl -X POST http://localhost:8080/api/tune/reembed -d '{"collection": "Policies"}' \
  -H 'Content-Type: application/json'

# switch index type or distance metric, reusing the existing vectors
curl -X POST http://localhost:8080/api/tune/reindex \
  -H 'Content-Type: application/json' \
  -d '{"collection": "Policies", "index_type": "flat", "distance_metric": "cosine"}'
```

`GET /api/tune/<collection>` says which of these the collection can do — a
`chunks-only` collection cannot be re-chunked, because the originals are not
there to re-split. Nor can a collection where a stored file has no single
retained original (a file uploaded more than once under the same name, or the
same content uploaded under a second name), or where a retained original is
missing from disk.

Each rebuild verifies a staging copy before cutover. Failures before cutover leave the original intact; a failure during final replacement can leave its name missing or partial, with a retained recovery copy and historical sessions. Retained sessions can be loaded by ID. Historical export returns `409 HISTORICAL_SESSION` until the user explicitly opts in.

**Gold-standard sessions are flagged, never deleted.** Re-chunking or re-embedding
changes which chunks exist, so any evaluation pairs built against the old ones no
longer describe what is stored; those sessions are marked `stale` with a reason
and a timestamp. They are not remapped onto the new chunks — a wrong remap
corrupts a baseline silently, which is worse than an honest flag. Deleting or
replacing a collection marks its sessions `orphaned` and reports how many.

## Verifying a change

```bash
bash scripts/verify/stack.sh run                  # everything, ~20 min plus start-up
RAG_SKIP_SLOW=1 bash scripts/verify/stack.sh run  # skip LLM work, ~8 min, start-up included
```

Runs the acceptance criteria in this project's specifications — ingest,
retrieval, gold standard, export/import, tuning, and the UI in a real headless
browser — on a disposable verify project: the checkout is built as compose
project `rag-verify` on port 8081, with its own empty volumes, and removed after
the run. `stack.sh` doesn't build, start or stop your own stack, and only
reads its model volume, to copy the models. That guards against accidents,
not hostile code: a branch's scripts run on your host with full access to
Docker. Exits non-zero if any check fails, so it can gate a commit.

These are integration tests on purpose. Every defect this project has produced
was invisible to a unit test of the same function: a parser dependency missing
from the image, an endpoint returning 200 where it should have refused, vectors
surviving a vectorizer. See `scripts/verify/README.md`.

## Dependency management

Both services pin their dependencies, so a rebuild six months from now installs the same versions as today.

**Python (`api/`)** — `requirements.in` holds the direct dependencies; `requirements.txt` is the generated lock with all packages pinned — direct dependencies plus their transitive dependencies — and is what the Dockerfile installs. To change a dependency:

Follow the resolve-against-current-image procedure at the top of
`api/requirements.in`: resolve changes with `pip install --dry-run --report`,
update the sorted lock with the resolved pins, rebuild, then compare the image's
`pip freeze` (excluding torch/torchvision) against the lock. Editing the input
and rebuilding alone does not resolve changes because Docker installs the lock.

`LC_ALL=C` keeps the ordering stable across machines, so re-locking produces a clean diff instead of a reshuffle.

`torch` and `torchvision` are deliberately excluded from the lock — they are installed from the PyTorch CPU index in `api/Dockerfile`, and their `+cpu` local versions are not published on PyPI. Do not add them to `requirements.txt`.

**Node (`ui/`)** — `package-lock.json` is committed and `npm ci` installs from it exactly. Regenerate with `npm install --package-lock-only` after editing `package.json`.

## MCP server (parked)

An MCP server exposing the RAG lifecycle as tools is **built and tested but not
wired in**. It has no compose service, is not in the offline bundle, and nothing
in the stack depends on it.

The source is preserved in `./mcp` (17 files, 15 tools, all 16 acceptance
criteria passed) and the design in `MCP_ANALYSIS.md`, `MCP_SPECIFICATIONS.md` and
`MCP_IMPLEMENTATION.md`.

To bring it back: restore the compose service recorded in
`MCP_IMPLEMENTATION.md` §7, add `rag-docker-mcp:latest` to the `IMAGES` array in
`package-offline.sh`, then `docker compose build mcp`. No code changes needed.

`./ingest-inbox/` is kept for the same reason — it is the read-only mount the MCP
server uses for file ingestion, and is otherwise unused.


## Moving to another Mac

There are two ways to move this project, depending on whether the target Mac has
internet access.

| | `package.sh` | `package-offline.sh` |
|---|---|---|
| Archive size | ~1.3 MB | **~4.6 GB** |
| Contains | source only | source + all 5 images + model weights |
| Target needs internet | **yes** (~8–10 GB downloaded) | **no** |
| First-run time on target | 10–20 min (downloads + build) | a few min (decompress + load only) |
| Docker disk on target | 20 GB | ~12 GB (no build cache needed) |

Use the offline bundle for an air-gapped machine, a slow link, or to guarantee the
target gets byte-identical images rather than rebuilding them.

### Option A — source package (target has internet)

The project ships as source. Docker images, the Weaviate index and the Ollama
model weights are **not** in the archive — they are rebuilt or re-downloaded on
the target machine.

#### 1. Package it

```bash
bash package.sh              # writes rag-docker.zip (~1.3 MB)
```

The zip holds exactly the files git tracks in the current commit (`HEAD`), built
with `git archive`. Untracked and ignored files never go in (`.env` files,
`node_modules/`, offline tars, previous archives, `.claude/`, `dev-docs/`, the
contents of `ingest-inbox/` and `exports/`), and neither do uncommitted changes,
so commit first. Run it in a git checkout of the project, with `bash package.sh`,
not `./package.sh`, so it does not depend on its own executable bit.

#### 2. What the target Mac needs

| Requirement | Value |
|---|---|
| Hardware | Apple Silicon (arm64) |
| Docker Desktop | installed and running, Engine 28.0.0+ |
| Docker disk | 20 GB minimum, 32 GB recommended |
| Docker memory | 12 GB, plus 2 GB swap (phi3.5 is ~6 GB resident) |
| Free host port | 8080 |
| Network | ~8–10 GB downloaded on first run |

No Python, Node, compiler or model files are needed on the target machine. Everything is built inside Docker.

#### 3. Start it

```bash
unzip rag-docker.zip -d rag-docker
cd rag-docker
docker compose up -d
```

First run pulls the base images, builds the api and ui images, then downloads the
two models — roughly 10–20 minutes on a fast connection, and almost entirely
network-bound. The model pull is the long pole and resumes if interrupted. Watch
it with `docker compose logs -f ollama`, then open <http://localhost:8080>. See
[First run timeline](#first-run-timeline) for the breakdown.

#### Why this reproduces cleanly

- **Both dependency sets are locked** — `ui/package-lock.json` and the pinned `api/requirements.txt`, so the new machine resolves the same versions rather than whatever is current.
- **No image is pinned to an architecture**, so arm64 variants resolve natively on Apple Silicon.
- **torch comes from the PyTorch CPU index**, which publishes linux/aarch64 CPU wheels — no CUDA packages and no GPU assumption.
- **Every bind mount in `docker-compose.yml` is relative** to the project directory, so the archive works from any path.
- **The Ollama entrypoint runs via `bash`**, so it does not depend on the executable bit surviving the zip.
- **Weaviate's cluster identity is pinned** with `CLUSTER_HOSTNAME`, so it survives container recreation instead of failing on the second `docker compose up`.
- **The api and ui services carry explicit `image:` tags**, so image names do not depend on the extracted folder name.

### Option B — offline bundle (target has NO internet)

Build the bundle on a machine where the stack already works and the models have
been downloaded:

```bash
bash package-offline.sh              # writes rag-docker-offline.tar (~4.6 GB)
```

Its source tree is the same as `package.sh`'s: the files git tracks in the
current commit, so run it in a git checkout too. It runs `docker save` on all
five images (~2.5 GB compressed) and exports the
Ollama model weights from the `ollama_models` volume (~2.2 GB). Expect a few
minutes; saving the image layers is the slow part.

Move the file to the target Mac, then:

```bash
tar xf rag-docker-offline.tar
cd rag-docker
bash install-offline.sh
```

`install-offline.sh` loads the images, restores the model weights into the
project's volume, and runs `docker compose up -d --no-build`. Nothing is pulled
and nothing is compiled. The target needs only Docker Desktop, running with Engine 28.0.0 or newer.

**Requirements on the target:** Apple Silicon, Docker Desktop with Engine 28.0.0+, ~12 GB of Docker
disk, ~10 GB of free space on the host filesystem for the archive plus its
extraction, and port 8080 free. No Python, Node, compiler or network needed.

The bundle deliberately omits the Weaviate index and uploaded files — those are
created empty on first start. Only the model weights, which are expensive to
fetch, are carried across.

#### Verifying an offline install

```bash
docker compose exec ollama ollama list          # both models, no download
docker compose logs ollama | grep 'already present'
curl http://localhost:8080/api/health
```

The Ollama log should read `phi3.5 already present, skipping pull` — that
confirms the weights came from the bundle rather than the network.

## Troubleshooting

**UI shows "no collections" after ingest** — The collection must exist before ingesting. AI Engineers can create one on the Collections page; Developers can use the **+ New** button on the Import page.

**`docker compose up -d` hangs on "Pulling" with no progress** — Docker's image pull has no built-in timeout; a dropped or stalled TCP connection sits silently with only a timer incrementing and no download bars. This is a Docker-level pull that happens before any application code runs, so the retry logic in `entrypoint.sh` cannot help here.

The fix is to pull each base image individually first, which shows real layer-by-layer download progress and can be safely interrupted and resumed:

```bash
docker compose down
docker pull semitechnologies/weaviate:1.39.6
docker pull nginx:1.29-alpine
docker pull ollama/ollama:0.3.14
```

Pull one at a time. Each shows per-layer progress bars (`Downloading 45MB/312MB`). If a pull stalls, Ctrl+C and re-run the same command — Docker resumes from where it stopped. Once all three images are cached locally, `docker compose up -d` skips the pulls entirely and proceeds to build the `api` and `ui` images.

If `docker pull` itself hangs with no output at all (not even layer names), Docker cannot reach Docker Hub. Work through these steps in order:

**1. Confirm internet connectivity from the terminal:**
```bash
curl -I https://registry-1.docker.io
```
If this hangs, the network issue is upstream of Docker — check your connection before proceeding.

**2. Restart the Docker daemon:**
```bash
osascript -e 'quit app "Docker"'
sleep 5
open -a Docker
```
Wait for Docker Desktop to fully restart (the whale icon in the menu bar stops animating), then retry the pull.

**3. Test Docker's internal DNS:**
```bash
docker run --rm alpine ping -c 1 registry-1.docker.io
```
If this fails after Docker restarts, Docker's internal networking is broken — quit and reopen Docker Desktop again.

**4. Disconnect any active VPN** — VPNs frequently block Docker's connection to Docker Hub. Disconnect, retry the pull, then reconnect once all images are cached.

**Ollama health check is stuck after images are pulled** — The model download phase (`ollama pull phi3.5` etc.) is in progress. Run `docker compose logs -f ollama` to watch. If the internet drops mid-download, the entrypoint script detects the stall via a 2-hour per-attempt timeout, kills the hung pull, and retries automatically up to 5 times. Ollama resumes partial downloads so retries pick up where they left off.

**LLM answers are garbage or time out** — Answers come back in mixed scripts or as fragments of unrelated instructions, run to thousands of characters, or time out, while `/api/health` still reports the LLM as ok. Ollama's model runner has degraded; it does not recover on its own. Restart it with `docker compose restart ollama`. `scripts/verify/all.sh` checks for this before its LLM suites and stops with the same advice.

**Port 8080 already in use** — The proxy publishes on host loopback port 8080 (`docker-compose.yml`, the `proxy` service: `"127.0.0.1:8080:80"`). Change the middle number to any free port, for example `"127.0.0.1:9090:80"`, then access the UI at `http://localhost:9090`. Keep the `127.0.0.1` host address and container port 80.

**Sharing with your office** — The default binding is now local to the Docker host. An existing installation accessed from another computer will stop accepting those connections after its proxy is recreated. This workbench currently has no API authentication; authenticated LAN access with TLS is tracked in issue #26 and is not yet provided. Keep the loopback binding for the current local setup.
**Upload fails with "larger than the 512 MB limit" or HTTP 413** — The upload is over the proxy's limit (see [Configuration](#configuration)). The limit covers the whole request, so split a large batch into several uploads, or raise the limit. Before issue #21 the proxy used nginx's 1 MB default, so a 413 on an ordinary PDF means the proxy is running an old `nginx.conf`: run `docker compose up -d --force-recreate proxy`.

**Build fails with `ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device`** — Docker's virtual disk is full, not your host disk — the two are reported separately, and `df -h` on the host will look fine. A virtual disk of 8 GB or so cannot hold ~6.5 GB of images plus build cache, so the build runs out of room partway through `pip install`. Check the real numbers with:

```bash
docker system df                  # what Docker is holding
docker run --rm alpine df -h /    # free space inside Docker's VM
```

If the host has plenty of free space but the VM does not, raise **Docker Desktop → Settings → Resources → Virtual disk limit** to at least 20 GB (32 GB recommended) and rebuild. See [Storage requirements](#storage-requirements). Running `docker system prune -a` first is a stopgap that buys a few GB, but the build will fail again on the next clean rebuild if the limit stays that small.

**`npm ci` fails during the UI build with `EUSAGE ... can only install with an existing package-lock.json`** — `ui/package-lock.json` is missing. `npm ci` requires a lockfile by design and will not fall back to resolving from `package.json`. Regenerate it without installing anything locally:

```bash
cd ui && npm install --package-lock-only
```

Commit the resulting lockfile — it is what makes UI builds reproducible.

**Weaviate exits immediately with `could not open cloud meta store: bootstrap: context deadline exceeded`** — Weaviate 1.25 stores Raft cluster state in the `weaviate_data` volume keyed by node identity. If that identity changes between runs, bootstrap fails and the container exits 1, which in turn aborts `docker compose up` with "dependency failed to start". `docker-compose.yml` pins `CLUSTER_HOSTNAME: 'node1'` to prevent this. If you have a volume created *before* that setting existed, its recorded identity no longer matches and you must reset it:

```bash
docker compose down
docker volume rm rag-docker_weaviate_data   # deletes the vector index
docker compose up -d
```

This discards ingested documents; re-ingest after it comes back up. The `ollama_models` volume is untouched, so models are not re-downloaded.

**`docker compose up -d` fails with `dependency failed to start: container rag-docker-weaviate-1 is unhealthy`** — Weaviate's start-up grows with the collection deletes in its Raft log since its last snapshot: each one costs a short wait before `/v1/.well-known/ready` answers. `docker-compose.yml` has Weaviate snapshot that log often (`RAFT_SNAPSHOT_THRESHOLD`, `RAFT_SNAPSHOT_INTERVAL`), so starts are usually quick. The slow ones are the first start after upgrading a volume that has no snapshot yet, which can take over two and a half minutes, and a start right after many collections were deleted. The health check allows 180 seconds for that (`start_period` in `docker-compose.yml`) before failures count, so this should be rare. If `up` still gives up, the api, ui and proxy are left stopped, but Weaviate keeps starting. Wait until `docker compose ps` shows weaviate as healthy, then run `docker compose up -d` again; it starts the rest.

**A healthcheck never passes and the service sits in `health: starting` forever** — Check that the probe binary exists in that image. The `ollama` image ships only the `ollama` binary and the `weaviate` image has busybox `wget` but no `curl`, so `curl`-based healthchecks can never succeed there. Only the `api` image installs `curl`. Verify with:

```bash
docker compose exec <service> sh -c 'command -v curl wget'
```

**Queries time out, or `httpx.ReadTimeout` appears in the api log** — Almost always memory. phi3.5 is ~6 GB resident, so on a Docker allocation below the recommended 12 GB the model can be evicted and reloaded between calls and generation never completes. Note the Ollama healthcheck cannot detect this: it runs `ollama list`, which succeeds while generation is wedged.

Check what Docker actually has, and what is loaded:

```bash
curl -s http://localhost:8080/api/health      # resources.memory
docker compose exec ollama ollama ps          # "Stopping..." means thrashing
```

Raise it in **Docker Desktop → Settings → Resources → Memory**, then restart Docker. If the daemon does not come back, quit Docker fully (check no `com.docker.backend` process survives) before relaunching — a stale backend can leave the VM unreachable.

**API returns 404 for a collection** — Weaviate **capitalises the first letter** of a collection name and leaves the rest untouched, so the name you create is not always the name you read back. Everything after the first character is case-sensitive. Measured against Weaviate 1.39.4:

| You send | Result |
|---|---|
| create `myDocs` | stored and listed as **`MyDocs`**; the API answers `MyDocs` |
| reference `myDocs` | accepted — the first letter is normalised for you |
| reference `MyDocs` | accepted — the stored name |
| reference `mydocs` | **404 `COLLECTION_NOT_FOUND`** — interior case must match |
| reference `MYDOCS` | **404 `COLLECTION_NOT_FOUND`** |

So a lowercase first letter is fine, but `mydocs` will not find `MyDocs`. The API applies the same rule to every collection name it receives, so the collection's settings, retained originals, gold-standard sessions and query metrics are all kept under `MyDocs` whichever of the two spellings you use (#372). State saved under a lowercase-first spelling before that change is not migrated: re-ingest the documents and save the settings again to put them under the stored name. If you get a 404 for a collection you are sure exists, run `curl http://localhost:8080/api/collections` and copy the name exactly as listed.

To check the behaviour yourself without waiting on the LLM, use the ingest endpoint — it validates the collection name and returns immediately:

```bash
curl -s -X POST http://localhost:8080/api/collections \
  -H 'Content-Type: application/json' -d '{"name":"myDocs"}'
curl -s http://localhost:8080/api/collections      # -> "MyDocs"
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8080/api/ingest/upload \
  -F 'collection=mydocs' -F 'files=@/dev/null;type=text/plain'   # -> 404
```

Overlap uses exact character windows, including internal whitespace-only windows, so boundaries can split words. Per-file limits are 10,000 chunks and 10 million parsed characters; overlap also caps duplicated output at 10 million characters. At default 1000/200 sizing, more than 8,000,200 characters exceeds the window limit. Split large documents or select suitable chunk settings. These limits bound ingestion work independently of the upload byte limit.

## Optional backend telemetry

The API includes a private OpenTelemetry SDK lifecycle (#282), disabled by
default. This foundation does not yet instrument requests/jobs, bridge Python
logging, or install a collector (#283–#285). Existing application logs retain
their existing content and behavior; the policy below applies only to OTLP.

To enable it, inject these variables into the **API container** using your own
Compose override or deployment environment. Compose does not automatically pass
host variables through. No destination or secret is supplied by this repository.

| Variable | Default / accepted values |
|---|---|
| `RAG_OTEL_ENABLED` | `false`; exact `true` or `false` |
| `RAG_OTEL_ENDPOINT` | Required when enabled; HTTP(S) origin, e.g. `http://collector:4318`; no userinfo, path, query or fragment |
| `RAG_OTEL_PROTOCOL` | `http/protobuf` only; gRPC is rejected |
| `RAG_OTEL_HEADERS_FILE` | Optional mounted UTF-8 JSON regular file containing `Authorization` and/or `X-Api-Key`; max 8 KiB, max 2 KiB per value |
| `RAG_OTEL_CA_FILE` | Optional mounted PEM CA bundle (a regular file) for an HTTPS endpoint; max 1 MiB; replaces the bundled roots |
| `RAG_OTEL_SERVICE_NAME` | `rag-api` |
| `RAG_OTEL_SERVICE_VERSION` | `1.1.0` |
| `RAG_OTEL_ENVIRONMENT` | `development` |
| `RAG_OTEL_TRACES`, `RAG_OTEL_LOGS`, `RAG_OTEL_METRICS` | Each `true`; exact booleans |
| `RAG_OTEL_SAMPLE_RATIO` | `1.0`; finite 0–1, root-independent ratio sampling |
| `RAG_OTEL_QUEUE_SIZE` | `256`; integer 1–4096 per trace/log queue |
| `RAG_OTEL_BATCH_SIZE` | `64`; integer 1–512, no larger than queue |
| `RAG_OTEL_TIMEOUT_MS` | `1000`; integer 100–10000 per transport attempt |
| `RAG_OTEL_INTERVAL_MS` | `5000`; integer 1000–60000 for batching/metric export |
| `RAG_OTEL_SHUTDOWN_MS` | `3000`; integer 100–30000 per lifecycle call |

Service metadata is explicitly operator-selected public telemetry data: use
non-sensitive identifiers (1–64 ASCII letters, digits, dots, underscores or
hyphens, starting with a letter/digit). Do not put customer names or secrets in
these fields. Headers belong in an uncommitted mounted secret file, never in
service metadata. Credential headers require HTTPS, including loopback destinations;
HTTP is accepted only without credentials. Header names are case-insensitive and
duplicate names (including repeated JSON keys) are rejected. Exporter transport ignores ambient proxies/netrc and uses TLS
verification; redirects are refused. TLS verification trusts the bundled roots,
or, when `RAG_OTEL_CA_FILE` is set, only the certificates in that file, for a
collector behind a private CA; to trust both, put both in the file. The file is
checked once at startup (it must exist, be a regular file, be readable, be at
most 1 MiB and load as a CA bundle) and requests re-reads it on new connections. It requires an
HTTPS endpoint. An enabled runtime rejects a process-level
`OTEL_SDK_DISABLED` value that the SDK recognizes as true (case-insensitive,
ignoring surrounding whitespace), before reading secrets or creating exporters.
Remove that setting or set it to false to enable RAG telemetry. Explicit bootstrap
configuration mappings cannot override this process-level conflict. Disabled RAG
telemetry remains a no-op regardless of that setting. The runtime never mutates
the process environment. Other `OTEL_*` variables are not a supported
configuration interface for this private runtime.

Disabled mode creates no providers, workers or exporters and does not read the
secret or CA file. Invalid enabled configuration fails API startup with field-only
errors. No global OTel provider or root logging handler is installed. Manual
request, worker and dependency instrumentation uses `app.state.telemetry.tracer`,
`.logger` and `.meter`, with `force_flush()` and `shutdown()` for lifecycle.
Raw SDK providers are internal implementation details and are not part of the
supported runtime API. This encapsulation is not a security boundary against
Python private-state access.

Before trace/log queueing and again at the final protobuf transport boundary,
the runtime drops arbitrary text. Queue records use runtime-owned resources and
fixed scopes; log wrappers also use fixed limits and discard exception objects
and context references. The runtime logger copies supplied plain records and both
layers of supplied wrappers before SDK normalization or exception expansion,
so caller-owned records remain unchanged. For supplied records and keyword
emission, only schema-approved attribute strings are snapshotted before SDK
delegation; forbidden mutable values are discarded, not recursively copied.
Callers must not mutate their mappings while that snapshot is being constructed.
Non-mapping attributes consistently become empty. Every emission form receives
runtime-owned limits before SDK processing (16 attributes, 128 characters), so
ambient `OTEL_LOGRECORD_ATTRIBUTE_*` and `OTEL_ATTRIBUTE_*` limits cannot truncate
approved log attributes or cause emission errors.
Wire resources contain only the three explicit
service fields; scope is `rag.telemetry`. Request names use the fixed registered
HTTP method and route template, with `rag.request` for unmatched routes. Dynamic
path values and query strings are never recorded. Other names are finite
`rag.*` operation/stage names and `ollama.*` / `weaviate.*` dependency names
listed in `api/services/telemetry.py`; unknown names become `rag.operation`.
Attributes are finite operation, outcome (ok/error/cancelled/partial), error type
(timeout/connection/validation/internal), registered method-and-route,
HTTP status class (1xx–5xx) and tuning operation values. Raw exception messages,
job/session identifiers, model names, customer names and content are excluded.
All other attributes, span events, trace state, status descriptions, log severity
text and arbitrary log bodies are removed; log body becomes `rag.operation`.
At most one link survives, containing only fixed-size nonzero trace/span IDs and
a sampled flag. Trace/span IDs remain correlation fields, never authentication.
The foundation metric allowlist is `rag.telemetry.check`; SDK views remove all
metric dimensions before aggregation. An explicit always-off exemplar filter
prevents original measurement attributes from entering SDK exemplar reservoirs,
even with ambient `OTEL_METRICS_EXEMPLAR_FILTER=always_on`. Export removes
descriptions, units and exemplars. Export admits exactly one finite data point per metric; empty,
malformed and multi-point metrics are rejected before consuming batch capacity.
Multi-point input cannot be merged safely after removing dimensions. Histograms
require at most 31 finite, strictly increasing boundaries, consistent bucket
totals and finite optional sum/min/max with min no greater than max. Invalid
histograms are dropped. Later stories extend this schema deliberately.
Do not pass content into instrumentation even though the exporter excludes it:
active SDK spans may retain inputs until completion.

The pinned SDK and OTLP HTTP exporter are version 1.44.0. Bounded batch queues
drop oldest pending records under load. Each export makes one HTTP attempt;
HTTP failures, redirects and network exceptions become fixed diagnostics with
no endpoint, headers or response body. No retries occur. Application work never
waits for export. Flush/shutdown wait at most the configured lifecycle deadline
and return whether processing completed, **not** proof of collector delivery.
A single daemon lifecycle worker per runtime continues cleanup after a timeout;
repeated calls reuse it. Requests timeouts cannot cancel OS DNS resolution or
force-stop a stalled system call, so a timed-out export may finish later. There
are no additional workers per record or per repeated lifecycle call. API cleanup
runs on both startup failure and normal shutdown. Collector provisioning and
deployed end-to-end acceptance remain separate work.


### Request and worker tracing (#283)

Each request starts a locally sampled trace. One strictly valid version-00 W3C
`traceparent` can supply a correlation link; duplicated, malformed, oversized,
zero-ID and unsupported-version headers are ignored. Caller sampled flags do
not control local sampling. `tracestate` and `baggage` are ignored. Headers never
change application identity, permissions, service metadata or response headers.

Query spans cover reformulation, retrieval and synthesis. Ingest covers parsing,
chunking, storage and source retention. Export/import, tuning and gold-standard
generation/regeneration have operation spans and narrower stages. Explicit
Ollama and Weaviate spans surround application-owned calls, iterator consumption
and batch flushes. Weaviate-managed embedding remains part of its dependency
latency; the API cannot report an internal model span for that work.

At each job admission, the runtime and local parent span context are captured.
The worker starts its child span during actual execution, including raw executor
threads and async task/thread handoffs. This child may outlive its already-ended
request parent; it retains the same trace ID after the 202 response. Concurrent
jobs keep separate contexts, and thread context is restored after execution.
Application job/session IDs are not exported. Caught and partial failures mark
safe outcomes without changing existing responses or durable job accounting.

Tracing preserves existing shutdown and cancellation behavior. Cancelling a
waiter does not stop a running thread; that thread's span ends when its actual
work exits. Async cancellation and existing timeouts close their spans. The API
does not gain a worker drain, new retry or new deadline. Forced process death or
workers surviving telemetry shutdown can lose their final spans; traces are not
a durable execution ledger. Operational metrics/log correlation and a collector
profile are subsequent stories.
