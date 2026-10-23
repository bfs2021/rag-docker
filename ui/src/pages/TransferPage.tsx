import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  api, CollectionInfo, ExportJob, ImportJob, PackageSummary, TuneOptions,
} from '../api/client'

function sizeLabel(bytes: number | null): string {
  if (bytes === null) return ''
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(0)} KB`
  return `${bytes} B`
}

const CONFLICT = [
  { id: 'abort', label: 'Abort', description: 'Fail if a collection of that name already exists. Nothing is changed.' },
  { id: 'rename', label: 'Rename', description: 'Import alongside the existing one, under a new name the API reports back.' },
  { id: 'replace', label: 'Replace', description: 'Overwrite the existing collection — but only after this package is proven to import cleanly. A failure while the final copy is built can leave the name missing; the data is then kept in a recovery collection the error names.' },
]

// Both halves poll their job every 2 s. A failed poll is shown and retried;
// after POLL_ATTEMPTS failures in a row (about a minute, long enough for an API
// restart) the page stops watching that job and enables its button again, so
// one transient 502 can no longer leave it stuck until a reload (#382).
const POLL_MS = 2000
const POLL_ATTEMPTS = 30

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export default function TransferPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [tune, setTune] = useState<TuneOptions | null>(null)
  const [tuneError, setTuneError] = useState('')
  const [includeModels, setIncludeModels] = useState(false)
  const [exportJob, setExportJob] = useState<ExportJob | null>(null)
  const [exportError, setExportError] = useState('')
  const [exportWatching, setExportWatching] = useState(false)
  const [exportPollError, setExportPollError] = useState('')

  const [packages, setPackages] = useState<PackageSummary[]>([])
  const [packagesError, setPackagesError] = useState('')
  const [filename, setFilename] = useState('')
  const [onConflict, setOnConflict] = useState('abort')
  const [importJob, setImportJob] = useState<ImportJob | null>(null)
  const [importError, setImportError] = useState('')
  const [importWatching, setImportWatching] = useState(false)
  const [importPollError, setImportPollError] = useState('')

  // Polling handles are kept so a job that finishes, or a page that unmounts,
  // does not leave a timer running against a job nobody is watching. The job
  // id being polled is kept too: a poll that answers after the page unmounted,
  // or after it stopped watching that job, schedules nothing and changes nothing.
  const alive = useRef(true)
  const exportTimer = useRef<number | null>(null)
  const importTimer = useRef<number | null>(null)
  const exportPoll = useRef<string | null>(null)
  const importPoll = useRef<string | null>(null)

  const loadPackages = useCallback(() => {
    api.getPackages().then(r => {
      setPackages(r.packages)
      setPackagesError('')
    }, (e: unknown) => setPackagesError(`Could not list the packages in ./exports: ${message(e)}`))
  }, [])

  useEffect(() => {
    alive.current = true
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(c => c || r.collections[0].name)
    }).catch(() => {})
    loadPackages()
    return () => {
      alive.current = false
      exportPoll.current = null
      importPoll.current = null
      if (exportTimer.current) window.clearTimeout(exportTimer.current)
      if (importTimer.current) window.clearTimeout(importTimer.current)
    }
  }, [loadPackages])

  // Fidelity is shown before the export runs, so the choice is informed rather
  // than discovered afterwards in the manifest. A lookup that fails says so
  // instead of hiding the line, and an answer for a collection that is no
  // longer selected is ignored.
  useEffect(() => {
    setTune(null); setTuneError('')
    if (!collection) return
    let current = true
    api.getTuneOptions(collection).then(t => {
      if (current) setTune(t)
    }, (e: unknown) => {
      if (current) setTuneError(message(e))
    })
    return () => { current = false }
  }, [collection])

  function pollExport(jobId: string, failures = 0) {
    api.getExportJob(jobId).then(job => {
      if (exportPoll.current !== jobId) return
      setExportJob(job)
      setExportPollError('')
      if (job.status === 'completed' || job.status === 'failed') {
        exportPoll.current = null
        setExportWatching(false)
        loadPackages()
      } else {
        exportTimer.current = window.setTimeout(() => pollExport(jobId), POLL_MS)
      }
    }, (e: unknown) => {
      if (exportPoll.current !== jobId) return
      if (failures + 1 >= POLL_ATTEMPTS) {
        exportPoll.current = null
        setExportWatching(false)
        setExportPollError(`Stopped checking the export's progress after ${POLL_ATTEMPTS} failed attempts; the export may still be running. Last error: ${message(e)}`)
      } else {
        setExportPollError(`Could not check the export's progress, retrying: ${message(e)}`)
        exportTimer.current = window.setTimeout(() => pollExport(jobId, failures + 1), POLL_MS)
      }
    })
  }

  function pollImport(jobId: string, failures = 0) {
    api.getImportJob(jobId).then(job => {
      if (importPoll.current !== jobId) return
      setImportJob(job)
      setImportPollError('')
      if (job.status !== 'completed' && job.status !== 'failed') {
        importTimer.current = window.setTimeout(() => pollImport(jobId), POLL_MS)
        return
      }
      importPoll.current = null
      setImportWatching(false)
      if (job.status === 'completed') {
        api.getCollections().then(r => setCollections(r.collections)).catch(() => {})
      }
    }, (e: unknown) => {
      if (importPoll.current !== jobId) return
      if (failures + 1 >= POLL_ATTEMPTS) {
        importPoll.current = null
        setImportWatching(false)
        setImportPollError(`Stopped checking the import's progress after ${POLL_ATTEMPTS} failed attempts; the import may still be running. Last error: ${message(e)}`)
      } else {
        setImportPollError(`Could not check the import's progress, retrying: ${message(e)}`)
        importTimer.current = window.setTimeout(() => pollImport(jobId, failures + 1), POLL_MS)
      }
    })
  }

  async function startExport() {
    setExportError(''); setExportPollError(''); setExportJob(null)
    try {
      const r = await api.startExport({ collection, include_models: includeModels })
      if (!alive.current) return
      exportPoll.current = r.job_id
      setExportWatching(true)
      pollExport(r.job_id)
    } catch (e: unknown) {
      setExportError(message(e))
    }
  }

  async function startImport() {
    setImportError(''); setImportPollError(''); setImportJob(null)
    try {
      const r = await api.startImport({ filename, on_conflict: onConflict })
      if (!alive.current) return
      importPoll.current = r.job_id
      setImportWatching(true)
      pollImport(r.job_id)
    } catch (e: unknown) {
      setImportError(message(e))
    }
  }

  const exportBusy = exportWatching
  const importBusy = importWatching

  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-baseline justify-between mb-2">
        <h1 className="text-2xl font-bold">Transfer</h1>
        <Link to="/help/transfer" className="text-sm text-blue-600 hover:underline">
          How export and import work →
        </Link>
      </div>
      <p className="text-sm text-gray-500 mb-6">
        Packages are written to and read from <code>./exports</code> in the project directory.
      </p>

      {/* ── Export ─────────────────────────────────────────────────────── */}
      <section className="bg-white border rounded p-4 mb-6">
        <h2 className="font-medium mb-3">Export a collection</h2>

        <label className="block text-sm font-medium mb-1">Collection</label>
        <select
          value={collection}
          onChange={e => setCollection(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm mb-2"
        >
          {collections.length === 0 && <option value="">No collections yet</option>}
          {collections.map(c => (
            <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>
          ))}
        </select>

        {tune ? (
          <p className="text-xs mb-3">
            <span className={tune.fidelity === 'with-sources' ? 'text-green-700' : 'text-amber-700'}>
              Fidelity: <strong>{tune.fidelity}</strong>
            </span>
            {' — '}
            {tune.fidelity === 'with-sources'
              ? `${tune.source_document_count} original document(s) will travel with the package.`
              : 'No original documents were retained, so the package cannot be re-chunked after import.'}
          </p>
        ) : tuneError ? (
          <p className="text-xs mb-3 text-red-600">
            Fidelity: <strong>unknown</strong> — it could not be read: {tuneError}
          </p>
        ) : collection ? (
          <p className="text-xs mb-3 text-gray-500">Fidelity: reading…</p>
        ) : null}

        <label className="flex items-start gap-2 text-sm mb-3">
          <input type="checkbox" checked={includeModels} onChange={e => setIncludeModels(e.target.checked)} className="mt-1" />
          <span>
            Include the models
            <span className="block text-xs text-gray-500">
              Bundles the embedding model and the LLM, taking the package to roughly 2.3 GB.
              Needed only when the target machine has never pulled them.
            </span>
          </span>
        </label>

        <button
          onClick={startExport}
          disabled={!collection || exportBusy}
          className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {exportBusy ? 'Exporting…' : 'Export'}
        </button>

        {exportError && <p className="text-red-600 text-sm mt-3">{exportError}</p>}
        {exportPollError && <p className="text-red-600 text-sm mt-3">{exportPollError}</p>}
        {exportJob && (
          <div className="mt-3 text-sm">
            <p className="text-gray-600">
              {exportJob.status === 'completed' ? 'Done.' :
               exportJob.status === 'failed' ? 'Failed.' :
               `Writing… ${exportJob.chunks_written} chunks so far.`}
            </p>
            {exportJob.status === 'completed' && exportJob.filename && (
              <p className="mt-1">
                <code className="text-xs break-all">{exportJob.filename}</code>
                <span className="text-gray-500 text-xs">
                  {' '}({sizeLabel(exportJob.size_bytes)}, {exportJob.fidelity}
                  {exportJob.models_bundled ? ', models included' : ''}
                  {exportJob.retrieve_script ? ', with retrieve.py' : ', no retrieve.py'})
                </span>
              </p>
            )}
            {exportJob.status === 'failed' && (
              <p className="text-red-600">
                {exportJob.error_code && <code className="text-xs mr-2">{exportJob.error_code}</code>}
                {exportJob.error}
              </p>
            )}
            {exportJob.warnings.map((w, i) => (
              <p key={i} className="text-amber-700 text-xs mt-1">{w}</p>
            ))}
          </div>
        )}
      </section>

      {/* ── Import ─────────────────────────────────────────────────────── */}
      <section className="bg-white border rounded p-4">
        <h2 className="font-medium mb-3">Import a package</h2>

        <div className="flex items-center justify-between mb-1">
          <label className="block text-sm font-medium">Package in ./exports</label>
          <button onClick={loadPackages} className="text-xs text-blue-600 hover:underline">Refresh</button>
        </div>
        <select
          value={filename}
          onChange={e => setFilename(e.target.value)}
          className="w-full border rounded px-3 py-2 text-sm mb-3"
        >
          <option value="">Select a package…</option>
          {packages.map(p => (
            <option key={p.filename} value={p.filename}>
              {p.filename}
              {p.readable
                ? ` — ${p.collection}, ${p.chunk_count} chunks, ${p.fidelity}, ${sizeLabel(p.size_bytes)}`
                : ' — unreadable'}
            </option>
          ))}
        </select>
        {packagesError && <p className="text-red-600 text-xs mb-3">{packagesError}</p>}
        {packages.length === 0 && !packagesError && (
          <p className="text-xs text-gray-500 mb-3">
            Nothing in <code>./exports</code> yet. Copy a package there and press Refresh.
          </p>
        )}

        <label className="block text-sm font-medium mb-2">If the collection already exists</label>
        <div className="space-y-2 mb-3">
          {CONFLICT.map(c => (
            <label key={c.id} className={`flex gap-3 p-3 border rounded cursor-pointer ${onConflict === c.id ? 'border-blue-500 bg-blue-50' : 'hover:bg-gray-50'}`}>
              <input type="radio" name="conflict" value={c.id} checked={onConflict === c.id}
                     onChange={() => setOnConflict(c.id)} className="mt-1" />
              <div>
                <div className="text-sm font-medium">{c.label}</div>
                <div className="text-xs text-gray-500">{c.description}</div>
              </div>
            </label>
          ))}
        </div>

        <button
          onClick={startImport}
          disabled={!filename || importBusy}
          className="bg-blue-600 text-white px-5 py-2 rounded text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {importBusy ? 'Importing…' : 'Import'}
        </button>

        {importError && <p className="text-red-600 text-sm mt-3">{importError}</p>}
        {importPollError && <p className="text-red-600 text-sm mt-3">{importPollError}</p>}
        {importJob && (
          <div className="mt-3 text-sm">
            <p className="text-gray-600">
              {importJob.status === 'completed' ? 'Done.' :
               importJob.status === 'failed' ? 'Failed.' :
               `Importing… ${importJob.chunks_written} chunks so far.`}
            </p>
            {importJob.status === 'completed' && (
              <p className="mt-1">
                Imported as <strong>{importJob.collection}</strong>
                {importJob.renamed && <span className="text-gray-500"> (renamed from {importJob.original_collection})</span>}
                <span className="text-gray-500"> — {importJob.chunks_written} chunks, {importJob.fidelity}</span>
              </p>
            )}
            {importJob.status === 'failed' && (
              <p className="text-red-600 mt-1">
                {importJob.error_code && <code className="text-xs mr-2">{importJob.error_code}</code>}
                {importJob.error}
              </p>
            )}
            {importJob.notes.map((n, i) => (
              <p key={i} className="text-gray-500 text-xs mt-1">{n}</p>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
