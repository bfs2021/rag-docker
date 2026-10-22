import { useState, useEffect, useRef } from 'react'
import { api, CollectionInfo, Session, GoldPair } from '../api/client'

export default function GoldStandardPage() {
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [collection, setCollection] = useState('')
  const [sampleSize, setSampleSize] = useState(20)
  const [session, setSession] = useState<Session | null>(null)
  const [sessionId, setSessionId] = useState('')
  const [sessionLookup, setSessionLookup] = useState('')
  const [allowHistorical, setAllowHistorical] = useState(false)
  const [loading, setLoading] = useState(false)
  const [exportPending, setExportPending] = useState(false)
  const exportPendingRef = useRef(false)
  const activeSessionRef = useRef('')
  const [error, setError] = useState('')
  // A failed collection list is shown, not swallowed (#379).
  const [listError, setListError] = useState('')
  const [filename, setFilename] = useState('')
  const [saveResult, setSaveResult] = useState('')
  const [editingPair, setEditingPair] = useState<GoldPair | null>(null)
  const [editQuestion, setEditQuestion] = useState('')
  const [editAnswer, setEditAnswer] = useState('')
  const [editGroundTruth, setEditGroundTruth] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      setListError('')
      if (r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch((e: unknown) => setListError(`Could not load the collections: ${e instanceof Error ? e.message : String(e)}`))
  }, [])

  useEffect(() => {
    if (!sessionId) return
    let active = true
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.getSession(sessionId)
        if (!active || activeSessionRef.current !== sessionId) return
        setSession(s)
        if (s.status !== 'generating') {
          clearInterval(pollRef.current!)
        }
      } catch { /* ignore */ }
    }, 2000)
    return () => { active = false; if (pollRef.current) clearInterval(pollRef.current) }
  }, [sessionId])

  useEffect(() => {
    setAllowHistorical(false)
  }, [sessionId, session?.stale, session?.orphaned, session?.stale_at, session?.orphaned_at])

  async function loadSession() {
    const id = sessionLookup.trim()
    if (!id || exportPendingRef.current) return
    activeSessionRef.current = ''
    setSessionId(''); setSession(null); setAllowHistorical(false); setFilename(''); setEditingPair(null)
    setError(''); setSaveResult(''); setLoading(true)
    try {
      const loaded = await api.getSession(id)
      activeSessionRef.current = loaded.session_id
      setSessionId(loaded.session_id); setSession(loaded); setAllowHistorical(false)
      setFilename('')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setLoading(false) }
  }

  async function generate() {
    if (session && !confirm('Start a new session? You can inspect this retained session again using its session ID.')) return
    setError('')
    setLoading(true)
    setSaveResult('')
    try {
      const res = await api.generateGoldStandard({ collection, sample_size: sampleSize })
      activeSessionRef.current = res.session_id
      setSessionId(res.session_id)
      setSessionLookup(res.session_id)
      setSession(null)
      const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 15)
      setFilename(`${collection}_${ts}.json`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  async function patchPair(pairId: string, status: string, updates?: { question?: string; answer?: string; ground_truth?: string }) {
    if (!sessionId) return
    const body = { status, ...updates }
    try {
      const updated = await api.patchPair(sessionId, pairId, body)
      setSession(prev => prev ? { ...prev, pairs: prev.pairs.map(p => p.pair_id === pairId ? updated : p) } : prev)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function regenerate(pairId: string) {
    if (!sessionId) return
    try {
      const updated = await api.regeneratePair({ session_id: sessionId, pair_id: pairId })
      setSession(prev => prev ? { ...prev, pairs: prev.pairs.map(p => p.pair_id === pairId ? updated : p) } : prev)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function saveExport() {
    if (!sessionId || exportPendingRef.current || loading) return
    exportPendingRef.current = true; setExportPending(true); setError(''); setSaveResult('')
    try {
      const res = await api.saveSession({ session_id: sessionId, filename: filename || undefined, allow_historical: allowHistorical })
      setSaveResult(`${res.pairs_saved} pairs exported, ${res.pairs_excluded} excluded.${res.historical ? " Historical data; not a current collection baseline." : ""}`)
      window.open(api.downloadUrl(res.filename), '_blank')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { exportPendingRef.current = false; setExportPending(false) }
  }

  function openEdit(pair: GoldPair) {
    setEditingPair(pair)
    setEditQuestion(pair.question)
    setEditAnswer(pair.answer)
    setEditGroundTruth(pair.ground_truth)
  }

  async function submitEdit() {
    if (!editingPair) return
    await patchPair(editingPair.pair_id, 'edited', {
      question: editQuestion,
      answer: editAnswer,
      ground_truth: editGroundTruth,
    })
    setEditingPair(null)
  }

  const approved = session?.pairs.filter(p => p.status === 'approved').length ?? 0
  const edited = session?.pairs.filter(p => p.status === 'edited').length ?? 0
  const rejected = session?.pairs.filter(p => p.status === 'rejected').length ?? 0
  const pending = session?.pairs.filter(p => p.status === 'pending').length ?? 0
  const historical = Boolean(session?.stale || session?.orphaned)
  const canExport = approved + edited > 0 && (!historical || allowHistorical)

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Gold Standard Generator</h1>

      <div className="bg-white border rounded p-4 mb-6">
        <h2 className="font-semibold mb-3">Phase 1 — Generate</h2>
        <div className="flex gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-600 mb-1">Collection</label>
            <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm">
              {collections.map(c => <option key={c.name} value={c.name}>{c.name}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">Sample Size (1–100)</label>
            <input type="number" min={1} max={100} value={sampleSize} onChange={e => setSampleSize(+e.target.value)} className="border rounded px-3 py-2 text-sm w-24" />
          </div>
          <button onClick={generate} disabled={loading || exportPending} className="bg-blue-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700">
            Generate Pairs
          </button>
        </div>
        {listError && <p role="alert" className="text-red-600 text-sm mt-3">{listError}</p>}

        {session && session.status === 'generating' && (
          <div className="mt-3">
            <p className="text-sm text-blue-600">Generating… {session.pairs_attempted ?? session.pairs_completed}/{session.pairs_total} pairs</p>
            <div className="w-full bg-gray-200 rounded h-2 mt-1">
              <div className="bg-blue-500 h-2 rounded transition-all" style={{ width: `${session.pairs_total > 0 ? ((session.pairs_attempted ?? session.pairs_completed) / session.pairs_total) * 100 : 0}%` }} />
            </div>
          </div>
        )}
      </div>

      <div className="bg-white border rounded p-4 mb-6">
        <label htmlFor="retained-session" className="block text-sm font-semibold mb-2">Inspect a retained session</label>
        <div className="flex gap-3">
          <input id="retained-session" disabled={exportPending} value={sessionLookup} onChange={e => setSessionLookup(e.target.value)} placeholder="Session ID" className="border rounded px-3 py-2 text-sm flex-1" />
          <button onClick={loadSession} disabled={loading || exportPending || !sessionLookup.trim()} className="border rounded px-3 py-2 text-sm disabled:opacity-50">Load / refresh session</button>
        </div>
        {session && <p className="text-xs text-gray-500 mt-2">Session {session.session_id} · Collection {session.collection}</p>}
      </div>

      {error && <p className="text-red-600 text-sm mb-4">{error}</p>}
      {session && historical && (
        <div role="alert" className="border border-amber-300 bg-amber-50 rounded p-4 mb-6">
          <h2 className="font-semibold">Historical evaluation data</h2>
          <p className="text-sm">These retained pairs are not a current collection baseline. Review them as historical data.</p>
          {session.stale && <p className="text-sm mt-2">Stale: {session.stale_reason || 'No reason was recorded.'} {session.stale_at && <span>Recorded {session.stale_at}</span>}</p>}
          {session.orphaned && <p className="text-sm mt-2">Orphaned: {session.orphaned_reason || 'No reason was recorded.'} {session.orphaned_at && <span>Recorded {session.orphaned_at}</span>}</p>}
        </div>
      )}


      {session && session.pairs.length > 0 && (
        <div className="bg-white border rounded p-4 mb-6">
          <h2 className="font-semibold mb-2">Phase 2 — Review</h2>
          <p className="text-xs text-gray-500 mb-3">
            {approved} approved · {edited} edited · {rejected} rejected · {pending} pending
          </p>
          <div className="space-y-3">
            {session.pairs.map(pair => (
              <details key={pair.pair_id} className={`border rounded ${pair.status === 'approved' ? 'border-green-300 bg-green-50' : pair.status === 'rejected' ? 'border-red-200 bg-red-50' : pair.status === 'edited' ? 'border-yellow-300 bg-yellow-50' : ''}`}>
                <summary className="px-3 py-2 cursor-pointer flex items-center gap-2 text-sm">
                  <span className="flex-1 font-medium">{pair.question}</span>
                  <span className="text-xs text-gray-400">{pair.source_file}</span>
                  <span className={`text-xs px-2 py-0.5 rounded ${
                    pair.status === 'approved' ? 'bg-green-200 text-green-800' :
                    pair.status === 'rejected' ? 'bg-red-200 text-red-800' :
                    pair.status === 'edited' ? 'bg-yellow-200 text-yellow-800' :
                    'bg-gray-200 text-gray-600'
                  }`}>{pair.status}</span>
                  <button onClick={e => { e.preventDefault(); patchPair(pair.pair_id, 'approved') }} className="text-green-600 hover:text-green-800 text-lg leading-none" title="Approve">✓</button>
                  <button onClick={e => { e.preventDefault(); openEdit(pair) }} className="text-blue-500 hover:text-blue-700 text-sm" title="Edit">✎</button>
                  <button onClick={e => { e.preventDefault(); patchPair(pair.pair_id, 'rejected') }} className="text-red-500 hover:text-red-700 text-sm" title="Reject">✕</button>
                  <button onClick={e => { e.preventDefault(); regenerate(pair.pair_id) }} className="text-gray-400 hover:text-gray-600 text-sm" title="Regenerate">↺</button>
                </summary>
                <div className="px-3 pb-3 space-y-2 text-xs text-gray-600">
                  <div><strong>Answer:</strong> {pair.answer}</div>
                  <div><strong>Ground Truth:</strong> {pair.ground_truth}</div>
                  <div><strong>Context:</strong> <span className="text-gray-500">{pair.contexts[0]?.slice(0, 300)}…</span></div>
                </div>
              </details>
            ))}
          </div>
        </div>
      )}

      {session && session.pairs.length > 0 && (
        <div className="bg-white border rounded p-4">
          <h2 className="font-semibold mb-3">Phase 3 — Export</h2>
          {historical && <label className="flex gap-2 items-start text-sm mb-3">
            <input type="checkbox" checked={allowHistorical} onChange={e => setAllowHistorical(e.target.checked)} />
            <span>I want to export historical pairs. The RAGAS file does not carry these validity warnings and must not be treated as a current baseline.</span>
          </label>}

          <div className="flex gap-3 items-center">
            <input value={filename} onChange={e => setFilename(e.target.value)} placeholder="filename.json" className="border rounded px-3 py-2 text-sm flex-1" />
            <button onClick={saveExport} disabled={!canExport || exportPending || loading} className="bg-green-600 text-white px-4 py-2 rounded text-sm disabled:opacity-50 hover:bg-green-700">
              {exportPending ? "Exporting…" : historical ? "Export Historical Approved" : "Export Approved"}
            </button>
          </div>
          {saveResult && <p className="text-sm text-green-600 mt-2">{saveResult}</p>}
        </div>
      )}

      {editingPair && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl p-6 w-full max-w-lg shadow-xl">
            <h2 className="font-semibold mb-4">Edit Pair</h2>
            <label className="block text-xs text-gray-600 mb-1">Question</label>
            <textarea value={editQuestion} onChange={e => setEditQuestion(e.target.value)} rows={2} className="w-full border rounded px-2 py-1 text-sm mb-3" />
            <label className="block text-xs text-gray-600 mb-1">Answer</label>
            <textarea value={editAnswer} onChange={e => setEditAnswer(e.target.value)} rows={2} className="w-full border rounded px-2 py-1 text-sm mb-3" />
            <label className="block text-xs text-gray-600 mb-1">Ground Truth</label>
            <textarea value={editGroundTruth} onChange={e => setEditGroundTruth(e.target.value)} rows={2} className="w-full border rounded px-2 py-1 text-sm mb-4" />
            <div className="flex justify-end gap-2">
              <button onClick={() => setEditingPair(null)} className="text-sm text-gray-500">Cancel</button>
              <button onClick={submitEdit} className="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
