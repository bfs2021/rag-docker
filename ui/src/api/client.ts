const BASE = '/api'

// The proxy's request-body limit: `client_max_body_size` in proxy/nginx.conf.
// It covers a whole request, so a multi-file upload counts every file. Kept
// here only so the Import page can warn before sending; nginx enforces it.
// Change both together.
export const MAX_UPLOAD_MB = 512
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

// Errors the proxy answers itself, before the API sees the request. Those
// arrive as nginx's HTML error pages, not the API's JSON error shape.
const PROXY_ERRORS: Record<number, string> = {
  413: `The upload is larger than the ${MAX_UPLOAD_MB} MB limit. Split it into smaller batches.`,
  502: 'The API is not responding. It may still be starting; try again in a minute.',
  504: 'The API took too long to respond.',
}

async function request<T>(method: string, path: string, body?: unknown, isFormData = false): Promise<T> {
  const headers: Record<string, string> = isFormData ? {} : { 'Content-Type': 'application/json' }
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: isFormData ? (body as FormData) : body !== undefined ? JSON.stringify(body) : undefined,
  })
  // Read as text first: calling res.json() on an nginx error page threw a
  // JSON syntax error, which is what the user saw instead of the real problem.
  const text = await res.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { /* not JSON; handled below */ }
  if (!res.ok) {
    throw new Error(data?.error?.message ?? PROXY_ERRORS[res.status] ?? `HTTP ${res.status}`)
  }
  if (data === null && text) throw new Error('The server sent a response the UI could not read.')
  return data as T
}

function isHealthResult(data: any): data is HealthResult {
  const service = (s: any) => !!s && typeof s === 'object' && typeof s.status === 'string'
  return service(data?.services?.weaviate) && service(data?.services?.ollama?.llm) && service(data?.services?.ollama?.embed)
}

// GET /health answers 503 with the same per-service body when a dependency is
// down. request() throws on any non-2xx, so the Health page never saw which
// service failed and could not show it red (#375). This call accepts a 200 or
// a 503 that carries that body; every other error is thrown as request() would,
// and a 200 without it is unreadable (#415).
async function getHealthResult(): Promise<HealthResult> {
  let res: Response
  try {
    res = await fetch(`${BASE}/health`)
  } catch {
    // fetch rejects only when no answer arrived; its own text ("Failed to fetch") says nothing.
    throw new Error('The API could not be reached. Check that the stack is running.')
  }
  const text = await res.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { /* not JSON; handled below */ }
  if ((res.ok || res.status === 503) && isHealthResult(data)) return data
  if (!res.ok) {
    throw new Error(data?.error?.message ?? PROXY_ERRORS[res.status] ?? `HTTP ${res.status}`)
  }
  // Another JSON shape, text or an empty body: the page would throw while
  // rendering the cards, or show nothing at all (#415).
  throw new Error('The server sent a response the UI could not read.')
}

export const api = {
  getCollections: () => request<{ collections: CollectionInfo[] }>('GET', '/collections'),
  // Answers with the name as stored, first letter capitalised (#372).
  createCollection: (body: CreateCollectionBody) => request<{ name: string; status: string }>('POST', '/collections', body),
  deleteCollection: (name: string) => request('DELETE', `/collections/${name}?confirm=true`),

  // Ingest — form field is "strategy"; job status URL is /ingest/job/{id}; config POST is /ingest/config
  uploadFiles: (form: FormData) => request<{ job_id: string; status: string; files_queued: number; collection: string }>('POST', '/ingest/upload', form, true),
  getJobStatus: (jobId: string) => request<JobStatus>('GET', `/ingest/job/${jobId}`),
  getIngestConfig: (collection: string) => request<IngestConfig>('GET', `/ingest/config/${collection}`),
  saveIngestConfig: (body: SaveIngestConfigBody) => request<IngestConfig>('POST', '/ingest/config', body),

  // Retrieval settings are stored per collection by the API, not in the browser.
  getRetrievalConfig: (collection: string) => request<RetrievalConfig>('GET', `/retrieval/config/${collection}`),
  saveRetrievalConfig: (body: SaveRetrievalConfigBody) => request<RetrievalConfig>('POST', '/retrieval/config', body),

  query: (body: QueryBody) => request<QueryResult>('POST', '/query', body),

  generateGoldStandard: (body: GenerateBody) => request<GenerateResult>('POST', '/goldstandard/generate', body),
  getSession: (sessionId: string) => request<Session>('GET', `/goldstandard/session/${encodeURIComponent(sessionId)}`),
  patchPair: (sessionId: string, pairId: string, body: PatchPairBody) => request<GoldPair>('PATCH', `/goldstandard/session/${encodeURIComponent(sessionId)}/pair/${encodeURIComponent(pairId)}`, body),
  regeneratePair: (body: { session_id: string; pair_id: string }) => request<GoldPair>('POST', '/goldstandard/regenerate', body),
  saveSession: (body: { session_id: string; filename?: string; allow_historical?: boolean }) => request<SaveResult>('POST', '/goldstandard/save', body),
  downloadUrl: (filename: string) => `${BASE}/goldstandard/download/${filename}`,

  // Transfer
  startExport: (body: ExportBody) => request<ExportStart>('POST', '/export', body),
  getExportJob: (jobId: string) => request<ExportJob>('GET', `/export/job/${jobId}`),
  startImport: (body: ImportBody) => request<ImportStart>('POST', '/import', body),
  getImportJob: (jobId: string) => request<ImportJob>('GET', `/import/job/${jobId}`),
  getPackages: () => request<{ packages: PackageSummary[] }>('GET', '/packages'),
  getTuneOptions: (collection: string) => request<TuneOptions>('GET', `/tune/${collection}`),
  getTransferHelp: () => request<{ topic: string; markdown: string }>('GET', '/help/transfer'),

  getMetrics: () => request<MetricsResult>('GET', '/metrics/latency'),
  getSessionDiagnostics: () => request<{ issues: { filename: string; code: string; message: string }[] }>('GET', '/goldstandard/diagnostics'),
  getHealth: () => getHealthResult(),
}

// Types
export interface CollectionInfo {
  name: string; object_count: number; index_type: string; distance_metric: string; created_at: string | null
  hnsw_config?: { ef: number; efConstruction: number; maxConnections: number } | null
}
export interface CreateCollectionBody {
  name: string; index_type: string; distance_metric: string; hnsw_config: { efConstruction: number; maxConnections: number; ef: number }
}
export interface JobStatus {
  job_id: string; status: string; files_total: number; files_completed: number; files_failed: number; chunks_stored: number; errors: string[]
  // Files that could not be parsed at all, or that a later file with the same
  // name replaced in the upload (#22) — never counted in files_total.
  skipped?: string[]
  // Earlier chunks of the same names this job removed (#22).
  chunks_replaced?: number
  // Chunks sent to Weaviate and those it acknowledged (#302); null on records
  // written before the fields existed. chunks_stored counts the checked ones.
  chunks_submitted?: number | null
  chunks_accepted?: number | null
}
export interface IngestConfig {
  collection: string; chunking_strategy: string; chunk_size: number; chunk_overlap: number; similarity_threshold: number | null; min_chunk_size: number; is_default: boolean
}
export interface SaveIngestConfigBody {
  collection: string; chunking_strategy: string; chunk_size: number; chunk_overlap: number; similarity_threshold: number | null; min_chunk_size: number
}
export interface QueryBody {
  question: string; collection: string; retrieval_mode: string; top_k: number; alpha?: number; include_citations: boolean; response_format: string
}
export interface RetrievalConfig {
  collection: string; retrieval_mode: string; top_k: number; alpha: number; ef: number | null; response_format: string; is_default: boolean
}
export interface SaveRetrievalConfigBody {
  collection: string; retrieval_mode: string; top_k: number; alpha: number; ef: number | null; response_format: string
}
export interface Citation {
  source_file: string; chunk_index: number; score: number | null; excerpt: string
}
export interface QueryResult {
  answer: string; citations: Citation[] | null; retrieval_latency_ms: number; llm_latency_ms: number; chunks_retrieved: number
}
export interface GenerateBody { collection: string; sample_size: number; seed?: number }
export interface GenerateResult { session_id: string; status: string; pairs_total: number; pairs_completed: number }
// pairs_attempted reaches pairs_total even when a pair fails; pairs_completed is how many exist.
export interface GoldPair {
  pair_id: string; question: string; answer: string; contexts: string[]; ground_truth: string; source_file: string; chunk_index: number; status: string
}
export interface SessionValidity {
  stale?: boolean; stale_reason?: string | null; stale_at?: string | null
  orphaned?: boolean; orphaned_reason?: string | null; orphaned_at?: string | null
}
export interface Session extends SessionValidity {
  session_id: string; status: string; pairs_total: number; pairs_attempted?: number
  pairs_completed: number; pairs_failed?: number; pairs: GoldPair[]; collection: string; errors?: string[]
  imported_from?: { session_id: string; collection: string; imported_at: string } | null
}
export interface PatchPairBody { status: string; question?: string; answer?: string; ground_truth?: string }
export interface SaveResult { filename: string; pairs_saved: number; pairs_excluded: number; download_url: string; historical?: boolean; session_validity?: SessionValidity }
export interface LatencyStats { p50: number; p95: number; p99: number }
export interface LatencyRecord {
  timestamp: string
  collection: string
  retrieval_mode: string
  retrieval_ms: number
  llm_ms: number
  total_ms: number
}
export interface MetricsResult {
  total_records: number
  retrieval_latency: LatencyStats
  llm_latency: LatencyStats
  total_latency: LatencyStats
  history: LatencyRecord[]
}
export interface ExportBody { collection: string; include_models: boolean }
export interface ExportStart { job_id: string; status: string; collection: string }
export interface ExportJob {
  job_id: string; status: string; collection: string; chunks_written: number
  filename: string | null; size_bytes: number | null; source_document_count: number | null
  fidelity: string | null; models_bundled: boolean | null; retrieve_script: boolean | null
  warnings: string[]; error: string | null; error_code: string | null; error_detail: Record<string, unknown> | null
}
export interface ImportBody { filename: string; on_conflict: string }
export interface ImportStart { job_id: string; status: string; filename: string }
export interface ImportJob {
  job_id: string; status: string; filename: string; on_conflict: string
  collection: string | null; original_collection: string | null; chunks_written: number
  fidelity: string | null; renamed: boolean; notes: string[]
  restored_sessions?: { source_session_id: string; session_id: string; collection: string }[]
  error: string | null; error_code: string | null; error_detail: Record<string, unknown> | null
}
export interface PackageSummary {
  filename: string; size_bytes: number; collection: string | null
  chunk_count: number | null; fidelity: string | null; created_at: string | null; readable: boolean
}
export interface TuneOptions {
  collection: string; fidelity: string; source_document_count: number
  can_rechunk: boolean; can_reembed: boolean; can_reindex: boolean; note: string
}
export interface HealthResult {
  status: string
  services: {
    weaviate: { status: string; latency_ms: number }
    // The API nests these under `ollama`; they are not flat `ollama_llm` /
    // `ollama_embed` keys. Reading the flat names yielded undefined and threw
    // during render, which unmounted the whole app.
    ollama: {
      llm: { status: string; latency_ms: number; model: string }
      embed: { status: string; latency_ms: number; model: string }
    }
  }
  resources?: {
    memory: {
      status: string
      allocated_gb: number | null
      recommended_minimum_gb: number
      note?: string
    }
  }
}
