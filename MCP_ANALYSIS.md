# RAG Docker — MCP Server Analysis

> **STATUS: PARKED (not wired into the project).**
> The MCP server is fully built and passed all 16 acceptance criteria, but it is
> deliberately not part of the running stack: no compose service, not in the
> offline bundle, nothing depends on it. The source is preserved in `./mcp` and
> these documents remain accurate as of that build.
>
> To resume: restore the compose service recorded in `MCP_IMPLEMENTATION.md` §7,
> add `rag-docker-mcp:latest` to the `IMAGES` array in `package-offline.sh`, and
> `docker compose build mcp`. No code changes are required.

**Status:** analysis complete, pending specification
**Scope:** an MCP server exposing this RAG platform's capabilities to MCP clients
**Companion documents:** `MCP_SPECIFICATIONS.md`, `MCP_IMPLEMENTATION.md`

---

## 1. Executive Summary

The RAG platform currently exposes 16 HTTP endpoints behind the nginx proxy on
host port 8080. Everything a user can do through the React UI is available over
that API. An MCP server would make the same capabilities available to an MCP
client (Claude Code, Claude Desktop, or any other), so a model can query the
corpus, ingest documents, manage collections and generate evaluation data
without a human driving the browser.

The central finding of this analysis is that **"online" and "offline" are not two
different systems**. Both install paths produce byte-identical images running the
same API on the same port. They differ only in how they were installed
(`package.sh` + network, versus `package-offline.sh` + `install-offline.sh`).
The MCP server therefore targets **one interface**, not two, and the only
requirement that flows from the offline mode is that the MCP server must make
**no outbound network calls of its own** and must ship inside the existing
packages.

Four design decisions are settled (Section 5): Python 3.11, stdio transport in a
container, full lifecycle tool surface, and distribution inside both existing
packages.

The hard problems are not the transport or the tool list. They are **file
ingestion across a process boundary** (Section 7.1) and **long-running
operations** (Section 7.2). Those two shape most of the specification.

---

## 2. Goals and Non-Goals

### Goals

- Expose the full RAG lifecycle as MCP tools: query, collection management,
  document ingestion, gold-standard generation, health and metrics.
- Work identically against an online or air-gapped deployment.
- Require nothing on the host but Docker — no Python, Node or compiler, matching
  the guarantee the offline bundle already makes.
- Ship inside `package.sh` and `package-offline.sh` with no new download.
- Fail legibly: an MCP client should get an actionable message when the RAG stack
  is down, a collection is missing, or a model is still loading.

### Non-Goals (Current Phase)

- **Authentication and multi-tenancy.** The RAG API is anonymous by design
  (`AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: 'true'`). The MCP server inherits
  that trust model and adds no auth of its own.
- **Remote/hosted access.** stdio only; no HTTP transport, no TLS, no exposure
  beyond the local machine.
- **A second copy of business logic.** The MCP server is a thin adapter over the
  HTTP API. Chunking, retrieval and generation stay in the api service.
- **Resources and prompts.** MCP tools only in this phase. MCP *resources*
  (browsable corpus) and *prompts* are deferred; see Section 9.
- **Replacing the UI.** The React interface remains the human surface.

---

## 3. What Already Exists

### API surface (measured from the live OpenAPI document)

| Method | Path | Purpose |
|---|---|---|
| GET | `/collections` | List collections with object counts and index config |
| POST | `/collections` | Create a collection |
| DELETE | `/collections/{name}` | Delete a collection and its contents |
| POST | `/query` | Ask a question (the core RAG call) |
| POST | `/ingest/upload` | Upload files; returns `202` + `job_id` |
| GET | `/ingest/job/{job_id}` | Poll ingest progress |
| POST | `/ingest/config` | Save per-collection chunking defaults |
| GET | `/ingest/config/{collection}` | Read those defaults |
| POST | `/goldstandard/generate` | Start gold-standard generation; returns `session_id` |
| GET | `/goldstandard/session/{session_id}` | Poll/read a generation session |
| POST | `/goldstandard/regenerate` | Regenerate one Q/A pair |
| PATCH | `/goldstandard/session/{session_id}/pair/{pair_id}` | Edit or approve a pair |
| POST | `/goldstandard/save` | Persist a session to a file |
| GET | `/goldstandard/download/{filename}` | Download a saved file |
| GET | `/metrics/latency` | p50/p95/p99 latency stats |
| GET | `/health` | Weaviate + LLM + embedding status |

### Error envelope

All handled errors return a consistent shape (`api/utils.py`):

```json
{ "error": { "code": "COLLECTION_NOT_FOUND", "message": "...", "detail": null } }
```

This is a gift for MCP: the `code` field maps cleanly onto actionable tool
errors, and `message` is already written for humans.

### Deployment modes

| | Online install | Offline install |
|---|---|---|
| Source | `package.sh` (~1.3 MB) | `package-offline.sh` (~4.6 GB) |
| Images | built on target | `docker load` from bundle |
| Models | pulled from ollama registry | restored from bundle volume |
| **Resulting API** | **identical** | **identical** |
| **Host port** | **8080** | **8080** |

---

## 4. Why This Is One Integration, Not Two

It is tempting to design "an online mode and an offline mode". That would be
wrong. The distinction lives entirely in the install step and leaves no trace at
runtime: same images, same compose file, same routes, same port.

What the offline mode *does* impose is a pair of constraints:

1. **No outbound calls at runtime.** The MCP server may not fetch a schema, phone
   a registry, or pull a dependency on first run. Everything it needs must be in
   its image.
2. **No build step on the target.** Its image must be pre-built and loaded from
   the bundle, exactly like the other five.

Both are satisfied by treating the MCP server as a sixth image in the existing
pipeline rather than as an external tool. A single `RAG_API_BASE_URL` setting
covers every deployment, including a non-default host port.

---

## 5. Settled Decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime | **Python 3.11** | Matches the api service; reuses the pinned-lock discipline; no second toolchain or registry to package for offline use |
| Transport | **stdio, launched as a container** | The standard MCP local pattern; needs no Python on the host, preserving the "Docker only" guarantee. Launch mechanism refined to `docker compose run` — see 7.3 |
| Tool surface | **Full lifecycle** | Mirrors the whole API; a read-only subset would omit ingestion, which is half the value |
| Packaging | **Inside both existing packages** | Target machines get it with everything else; no separate versioning story |

### Consequences of "stdio in a container"

The MCP client owns the process lifecycle: it launches the container (see 7.3
for the exact command, which is `docker compose run`, not a bare `docker run`)
and speaks JSON-RPC over stdin/stdout. This has three implications the specification
must address:

- **stdout is the protocol channel.** Any stray `print()` corrupts the stream.
  All logging must go to stderr. This is the single most common way a Python MCP
  server breaks.
- **The container is ephemeral**, so the server must hold no state between runs.
  All state lives in the RAG stack. This is a simplification, not a limitation.
- **It must reach the API from inside a container** — see Section 7.3.

---

## 6. Proposed Tool Surface

Sixteen endpoints do not mean sixteen tools. Some are better merged, and a few
are better hidden.

| Tool | Backed by | Notes |
|---|---|---|
| `rag_query` | `POST /query` | The primary tool. Long-running (LLM generation measured at ~15 s) |
| `rag_list_collections` | `GET /collections` | Read-only |
| `rag_create_collection` | `POST /collections` | Must return the **normalised** name — see 7.5 |
| `rag_delete_collection` | `DELETE /collections/{name}` | **Destructive.** Must carry a destructive annotation |
| `rag_ingest_files` | `POST /ingest/upload` + poll | Merged: upload then wait for the job. See 7.1 and 7.2 |
| `rag_ingest_status` | `GET /ingest/job/{job_id}` | For checking a job started earlier |
| `rag_get_ingest_config` | `GET /ingest/config/{collection}` | Read-only |
| `rag_set_ingest_config` | `POST /ingest/config` | Write |
| `rag_generate_goldstandard` | `POST /goldstandard/generate` + poll | Long-running; LLM-bound per pair |
| `rag_get_goldstandard` | `GET /goldstandard/session/{id}` | Read-only |
| `rag_update_goldstandard_pair` | `PATCH .../pair/{pair_id}` | Write |
| `rag_regenerate_goldstandard_pair` | `POST /goldstandard/regenerate` | Long-running (one LLM call) |
| `rag_save_goldstandard` | `POST /goldstandard/save` | Write |
| `rag_health` | `GET /health` | Read-only; the natural first diagnostic |
| `rag_metrics` | `GET /metrics/latency` | Read-only |

`GET /goldstandard/download/{filename}` is deliberately **not** a tool. It
returns a file for a browser to download; an MCP client that wants the content
already has it via `rag_get_goldstandard`. Exposing it would invite path
traversal questions for no benefit.

That yields **15 tools**. This is a large surface for one server, and Section 9
records the option of splitting it.

---

## 7. The Hard Problems

### 7.1 Getting files into an ingest (the central design problem)

`POST /ingest/upload` is `multipart/form-data` with `files: list[UploadFile]`.
MCP tool arguments are JSON. Bridging that gap has three candidate designs:

**Option A — host paths plus a bind mount.** The tool takes file paths; the
container reads them from a mounted directory.
*Pros:* natural for the caller ("ingest `~/docs/report.pdf`"); no size ceiling;
no encoding cost.
*Cons:* the client's `docker run` command must mount the right directory, and a
path outside it fails confusingly. Couples the MCP config to the user's layout.

**Option B — inline base64 content.** The tool takes filename + base64 bytes.
*Pros:* self-contained; no mount; works regardless of where the file lives.
*Cons:* base64 inflates payloads ~33%, and the content must pass through the
model's context or the client's tool-call plumbing. A 10 MB PDF is impractical.

**Option C — inline text content.** The tool takes filename + plain text.
*Pros:* simplest; ideal when the model has *generated* the content or already
read the file.
*Cons:* cannot carry PDFs, DOCX or images — precisely the formats
`unstructured[pdf,docx,csv]` exists to handle.

**Assessment.** These are not mutually exclusive and serve different callers.
A defensible design offers **A as the primary path** (it is the only one that
handles real PDFs at real sizes) and **C as a convenience** for model-generated
or already-extracted text. B is rejected: it combines A's configuration burden
with C's size ceiling and adds encoding overhead.

The specification must pin down the mount contract: a documented default
(e.g. `-v "$HOME:/host:ro"`), path validation confined to that root, and a clear
error when a path lies outside it.

### 7.2 Long-running operations

Three operations are slow, and slow means different things to each:

| Operation | Measured / expected | Shape |
|---|---|---|
| `rag_query` | ~15 s (LLM generation, CPU) | Single call, blocking |
| `rag_ingest_files` | seconds to minutes | `202` + poll `job_id` |
| `rag_generate_goldstandard` | minutes (one LLM call per pair, default 20) | `202` + poll `session_id` |

Two viable patterns:

- **Block and poll internally.** The tool returns only when the job finishes.
  Simple for the caller, but risks hitting the MCP client's tool timeout, and the
  caller sees nothing until the end.
- **Return the handle immediately.** The tool returns `job_id`/`session_id`, and
  a second tool polls. Never times out, but forces a multi-turn dance for the
  common case.

**Assessment.** Neither alone is right. The sound approach is **bounded blocking
with a documented escape hatch**: poll internally up to a configurable budget,
and if the job is still running, return the handle and status so the caller can
resume with `rag_ingest_status`. This keeps the common case one call while making
the slow case survivable. The specification must set the default budget and make
it configurable.

Gold-standard generation with a large `sample_size` will routinely exceed any
sane budget, so it should lean toward returning the handle early.

### 7.3 Reaching the API from inside a container

Three routes were considered, and **the obvious one was measured and rejected**.

| Route | Address | Measured result |
|---|---|---|
| Host gateway via the published port | `http://host.docker.internal:8080/api` | **404 — does not reach the stack** |
| Compose network, direct to api | `http://api:8000` | **200 — works** |
| Compose network via `docker compose run` | `http://api:8000`, network resolved by compose | **200 — works, and needs no project name** |

**Why the host-gateway route failed.** On the development machine, port 8080 is
held by *two* listeners: an unrelated Python process on **IPv4** and Docker on
**IPv6**. `curl localhost:8080/api/health` returns `200` only because macOS
resolves `localhost` to `::1` first. Containers resolve `host.docker.internal` to
an **IPv4** address (192.168.65.254), so they reach the other process and get a
`404`. The nginx access log confirms the request never arrived at the proxy.

That specific collision is environmental and may not exist on the target Mac. The
*general* lesson is not: routing through a published host port makes the MCP
server depend on the host's port situation, which the project does not control
and cannot verify at install time. It is also Docker-Desktop-specific — on Linux
`host.docker.internal` requires `--add-host`.

**Assessment.** Reach the api service **directly over the compose network**,
which was measured working and bypasses the host port entirely. The project-name
fragility that made this look unattractive disappears if the MCP server is
launched with `docker compose run` from the project directory: compose resolves
its own network and service names, so nothing needs to know the project name.
`RAG_API_BASE_URL` remains available to override for unusual topologies.

This changes the launch command from a bare `docker run -i` to:

```
docker compose run --rm -T --no-deps mcp
```

which the specification must define as a compose service (kept out of the default
`up` set via a profile, since the MCP client owns its lifecycle).

**Side finding, outside this work:** the port collision above means the running
stack is currently reachable on 8080 over IPv6 only. `README.md` states port 8080
must be free; on this machine it is not. Worth resolving separately.

### 7.4 Destructive operations

`rag_delete_collection` destroys a collection and every chunk in it, with no
undo. MCP tool annotations (`destructiveHint`, `readOnlyHint`, `idempotentHint`)
exist for this and should be set honestly across all 15 tools so clients can
apply their own confirmation policy. Annotations are advisory — they are a signal
to the client, not an enforcement mechanism, and the specification should not
pretend otherwise.

### 7.5 Collection-name normalisation (verified, and counter-intuitive)

Weaviate capitalises the first letter of a collection name on storage. Measured
against the running stack:

| Action | Sent | Result |
|---|---|---|
| `POST /collections` | `casetest` | `201` |
| `GET /collections` | — | returns **`Casetest`** |
| `POST /ingest/upload` | `casetest` | `202` — accepted |
| `POST /query` | `casetest` | `200` — accepted |
| `DELETE /collections/{name}` | `casetest` | `200` — accepted |

Only the **first character** is normalised. Interior case is preserved and
significant:

| Created as `myDocs`, stored as `MyDocs` | Result |
|---|---|
| reference `myDocs` | accepted — first letter normalised |
| reference `MyDocs` | accepted — exact stored name |
| reference `mydocs` | **`404 COLLECTION_NOT_FOUND`** |
| reference `MYDOCS` | **`404 COLLECTION_NOT_FOUND`** |

(Measured against `POST /ingest/upload`, which validates the collection name and
returns immediately — unlike `/query`, it needs no LLM call, so the check is
fast and unaffected by model load.)

So the lowercase-initial form is accepted, but it is *not* case-insensitive
matching. **The name you create is not the name you read back.**

This is a trap for a model specifically: it creates `casetest`, lists
collections, sees `Casetest`, and may conclude the creation failed — or create a
second collection to "fix" it. Two obligations follow for the specification:

1. `rag_create_collection` must return the **stored** name, not echo the input.
2. Every tool description that takes a collection name must state that only the
   first letter is normalised, and that the remainder is case-sensitive — so a
   name differing beyond the first character will 404.

`README.md` previously claimed names "must start with an uppercase letter" and
that "the API passes names through as-is" — both false against Weaviate 1.39.4.
That entry has since been corrected to match the behaviour measured here.

### 7.6 Error translation

The API's `{"error": {"code", "message", "detail"}}` envelope should surface to
the caller with the code preserved, because codes like `COLLECTION_NOT_FOUND`
tell a model exactly what to do next. Three failure classes need distinct
handling:

- **Stack down / connection refused** — the likeliest failure. The message must
  say so plainly and suggest `docker compose ps`, not surface a raw `ConnectError`.
- **Stack up but degraded** (`/health` returns 503) — e.g. Ollama still loading.
  Distinguishable and worth a distinct message.
- **Request rejected** (4xx with the envelope) — pass the code and message through.

---

## 8. Constraints and Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | `print()` to stdout corrupts the JSON-RPC stream | Server appears to hang or client errors cryptically | Log to stderr only; forbid bare prints in review |
| 2 | Host-port routing is unreliable (measured: IPv4/IPv6 port collision) | MCP cannot reach the API despite the stack being healthy | Use the compose network, not the published port (7.3) |
| 3 | Tool timeout on slow LLM calls | Query or ingest appears to fail | Bounded blocking + resumable handles (7.2) |
| 4 | Bind-mount path confusion | "File not found" for a file the user can see | Single documented mount root; validate and explain |
| 5 | 15 tools is a large surface | Tool-selection noise in the client | Clear names and descriptions; consider splitting (Section 9) |
| 6 | Image adds to the offline bundle | Bundle grows beyond 4.6 GB | Build on `python:3.11-slim`, already in the bundle as the api base — the marginal cost is only the MCP layer |
| 7 | Dependency drift | The exact failure already seen with `weaviate-client` | Pin with a lock file, same discipline as `api/requirements.txt` |
| 8 | API changes silently break tools | Tools fail at runtime, not build time | Contract tests against the live OpenAPI document |

---

## 9. Open Questions for the Specification

1. **Ingest mount root.** Default to `$HOME`, the current directory, or require
   an explicit mount? Affects the documented `docker run` line.
2. **Blocking budget.** What default wait before returning a handle — 60 s? 120 s?
   Should it differ between ingest and gold-standard generation?
3. **Tool splitting.** Keep one server with 15 tools, or split into a core
   (query, collections, ingest) and an evaluation server (gold standard)?
4. **Text ingestion format.** Should `rag_ingest_text` write a `.txt` and reuse
   the normal pipeline, or does that distort `source_file` provenance?
5. **Linux support.** In scope now, or Mac-only to match the rest of the project?

---

## 10. Conclusion

An MCP server for this platform is a thin, well-bounded adapter: no new business
logic, one HTTP dependency, and a deployment story that reuses machinery already
built and tested. The work is genuinely small. The risk concentrates in three
places — stdout discipline, file ingestion across the container boundary, and
long-running operations — and each has a known, unexciting solution.

The decision to treat online and offline as one target removes what would
otherwise have been the largest source of accidental complexity.
