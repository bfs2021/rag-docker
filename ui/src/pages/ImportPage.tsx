import { useState, useEffect } from 'react'
import { api, CollectionInfo, JobStatus, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from '../api/client'
import { useRole } from '../context/RoleContext'
import StrategyExplainer from '../components/StrategyExplainer'
import ProgressPanel from '../components/ProgressPanel'

const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic']

export default function ImportPage() {
  const { role } = useRole()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [strategy, setStrategy] = useState('overlap')
  const [chunkSize, setChunkSize] = useState(1000)
  const [chunkOverlap, setChunkOverlap] = useState(200)
  const [minChunkSize, setMinChunkSize] = useState(100)
  const [similarityThreshold, setSimilarityThreshold] = useState(0.85)
  const [jobId, setJobId] = useState('')
  const [job, setJob] = useState<JobStatus | null>(null)
  const [error, setError] = useState('')
  // A failed status poll, shown instead of ignored (#290). Polling carries on,
  // and the next successful poll clears it.
  const [pollError, setPollError] = useState('')
  const [showNewColModal, setShowNewColModal] = useState(false)
  const [newColName, setNewColName] = useState('')
  const [newColIndexType, setNewColIndexType] = useState('hnsw')

  const showOverlap = strategy !== 'semantic' && strategy !== 'context_aware'
  const showSimilarity = strategy === 'semantic' && role === 'engineer'

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      if (r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!jobId) return
    const interval = setInterval(async () => {
      try {
        const j = await api.getJobStatus(jobId)
        setJob(j)
        setPollError('')
        if (j.status === 'completed' || j.status === 'failed' || j.status === 'partial') clearInterval(interval)
      } catch (e: unknown) {
        setPollError(`Could not check the import's progress: ${e instanceof Error ? e.message : String(e)}`)
      }
    }, 3000)
    return () => clearInterval(interval)
  }, [jobId])

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setFiles(prev => [...prev, ...Array.from(e.dataTransfer.files)])
  }

  async function createCollection() {
    if (!newColName.trim()) return
    try {
      await api.createCollection({
        name: newColName,
        index_type: newColIndexType,
        distance_metric: 'cosine',
        hnsw_config: { efConstruction: 128, maxConnections: 64, ef: 64 },
      })
      const r = await api.getCollections()
      setCollections(r.collections)
      setCollection(newColName)
      setShowNewColModal(false)
      setNewColName('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function startIngest() {
    if (!files.length || !collection) return
    setError('')
    setPollError('')
    // Refuse before sending: the proxy would reject it anyway, but only after
    // the browser had started pushing hundreds of MB, and a connection the
    // proxy closes mid-upload can surface as a bare network error.
    const total = files.reduce((n, f) => n + f.size, 0)
    if (total > MAX_UPLOAD_BYTES) {
      setError(`These files total ${(total / 1024 / 1024).toFixed(0)} MB; one upload can be at most ${MAX_UPLOAD_MB} MB. Split them into smaller batches.`)
      return
    }
    const form = new FormData()
    files.forEach(f => form.append('files', f))
    form.append('collection', collection)
    // API form field is "strategy" (not "chunking_strategy")
    form.append('strategy', strategy)
    form.append('chunk_size', String(chunkSize))
    form.append('chunk_overlap', String(chunkOverlap))
    form.append('similarity_threshold', String(similarityThreshold))
    form.append('min_chunk_size', String(minChunkSize))
    try {
      const res = await api.uploadFiles(form)
      setJobId(res.job_id)
      setJob(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Import Documents</h1>

      <div
        onDrop={handleDrop}
        onDragOver={e => e.preventDefault()}
        className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center mb-4 cursor-pointer hover:border-blue-400"
        onClick={() => document.getElementById('file-input')?.click()}
      >
        <p className="text-gray-500">Drop files here or click to browse</p>
        <p className="text-xs text-gray-400 mt-1">PDF, DOCX, TXT, MD, CSV, JSON, ZIP · up to {MAX_UPLOAD_MB} MB per upload</p>
        <input id="file-input" type="file" multiple className="hidden" accept=".pdf,.docx,.txt,.md,.csv,.json,.zip"
          onChange={e => setFiles(prev => [...prev, ...Array.from(e.target.files || [])])} />
      </div>

      {files.length > 0 && (
        <ul className="mb-4 space-y-1">
          {files.map((f, i) => (
            <li key={i} className="flex justify-between text-sm bg-gray-50 border rounded px-3 py-1">
              <span>{f.name}</span>
              <button onClick={() => setFiles(files.filter((_, j) => j !== i))} className="text-red-400 hover:text-red-600">✕</button>
            </li>
          ))}
        </ul>
      )}

      <div className="mb-4">
        <label className="block text-sm font-medium mb-1">Collection</label>
        <div className="flex gap-2">
          <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm flex-1">
            {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
          </select>
          <button onClick={() => setShowNewColModal(true)} className="text-sm border rounded px-3 py-2 hover:bg-gray-50">+ New</button>
        </div>
      </div>

      <div className="mb-2">
        <label className="block text-sm font-medium mb-1">Chunking Strategy</label>
        <select value={strategy} onChange={e => setStrategy(e.target.value)} className="border rounded px-3 py-2 text-sm w-full">
          {STRATEGIES.map(s => <option key={s} value={s}>{s.replace('_', ' ')}</option>)}
        </select>
        <StrategyExplainer strategy={strategy} />
      </div>

      <div className="grid grid-cols-2 gap-4 mt-4 mb-4">
        <div>
          <label className="block text-xs text-gray-600 mb-1">Chunk Size: {chunkSize}</label>
          <input type="range" min={50} max={6000} step={50} value={chunkSize} onChange={e => { const size = +e.target.value; setChunkSize(size); setChunkOverlap(o => Math.min(o, Math.max(0, size - 50))) }} className="w-full" />
        </div>
        {showOverlap && (
          <div>
            <label className="block text-xs text-gray-600 mb-1">Overlap: {chunkOverlap}</label>
            <input type="range" min={0} max={Math.max(0, Math.min(2000, chunkSize - 50))} step={50} value={chunkOverlap} onChange={e => setChunkOverlap(+e.target.value)} className="w-full" />
          </div>
        )}
        <div>
          <label className="block text-xs text-gray-600 mb-1">Min Chunk Size: {minChunkSize}</label>
          <input type="range" min={0} max={6000} step={10} value={minChunkSize} onChange={e => setMinChunkSize(+e.target.value)} className="w-full" />
        </div>
        {showSimilarity && (
          <div>
            <label className="block text-xs text-gray-600 mb-1">Similarity Threshold: {similarityThreshold}</label>
            <input type="range" min={0} max={1} step={0.05} value={similarityThreshold} onChange={e => setSimilarityThreshold(+e.target.value)} className="w-full" />
          </div>
        )}
      </div>

      <button onClick={startIngest} disabled={!files.length} className="bg-blue-600 text-white px-6 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700">
        Start Ingest
      </button>
      {error && <p className="text-red-600 text-sm mt-2">{error}</p>}
      {job && <ProgressPanel job={job} />}
      {pollError && <p className="text-red-600 text-sm mt-2">{pollError}</p>}

      {showNewColModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-80 shadow-xl">
            <h2 className="font-semibold mb-4">New Collection</h2>
            <input value={newColName} onChange={e => setNewColName(e.target.value)} placeholder="Collection name" className="border rounded px-3 py-2 text-sm w-full mb-3" />
            <select value={newColIndexType} onChange={e => setNewColIndexType(e.target.value)} className="border rounded px-3 py-2 text-sm w-full mb-4">
              <option value="hnsw">HNSW (Approximate)</option>
              <option value="flat">Flat (Exact KNN)</option>
            </select>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowNewColModal(false)} className="text-sm text-gray-500 hover:text-gray-700">Cancel</button>
              <button onClick={createCollection} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Create</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
