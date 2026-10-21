import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import { api, CollectionInfo, Citation } from '../api/client'
import { useRole } from '../context/RoleContext'
import { useQueryConfig } from '../context/QueryConfigContext'
import CitationsPanel from '../components/CitationsPanel'

export default function QAPage() {
  const { role } = useRole()
  // The selected collection lives in the context because the retrieval
  // settings are stored per collection and must follow the selection.
  const { collection, setCollection, config } = useQueryConfig()
  const [collections, setCollections] = useState<CollectionInfo[]>([])
  const [question, setQuestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [answer, setAnswer] = useState('')
  const [citations, setCitations] = useState<Citation[] | null>(null)
  const [showCitations, setShowCitations] = useState(false)
  const [latency, setLatency] = useState<{ ret: number; llm: number } | null>(null)
  const [error, setError] = useState('')
  // A failed collection list is shown, not swallowed (#379).
  const [listError, setListError] = useState('')

  useEffect(() => {
    api.getCollections().then(r => {
      setCollections(r.collections)
      setListError('')
      if (!collection && r.collections.length > 0) setCollection(r.collections[0].name)
    }).catch((e: unknown) => setListError(`Could not load the collections: ${e instanceof Error ? e.message : String(e)}`))
    // Runs once; a collection already chosen on the Retrieval page is kept.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function submit() {
    if (!question.trim() || !collection) return
    setLoading(true)
    setError('')
    setAnswer('')
    setCitations(null)
    setLatency(null)
    try {
      const result = await api.query({
        question,
        collection,
        retrieval_mode: config.retrieval_mode,
        top_k: config.top_k,
        alpha: config.alpha,
        include_citations: showCitations,
        response_format: role === 'end_user' ? 'end_user' : 'engineer',
      })
      setAnswer(result.answer)
      setCitations(result.citations)
      setLatency({ ret: result.retrieval_latency_ms, llm: result.llm_latency_ms })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && e.ctrlKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="max-w-3xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Ask a Question</h1>
      <div className="flex gap-3 mb-3">
        <select value={collection} onChange={e => setCollection(e.target.value)} className="border rounded px-3 py-2 text-sm flex-1">
          {collections.map(c => <option key={c.name} value={c.name}>{c.name} ({c.object_count} chunks)</option>)}
        </select>
        {role !== 'end_user' && (
          <select value={config.retrieval_mode} disabled className="border rounded px-3 py-2 text-sm bg-gray-50 text-gray-500">
            <option value={config.retrieval_mode}>{["hnsw", "flat"].includes(config.retrieval_mode) ? "Vector — existing index" : config.retrieval_mode}</option>
          </select>
        )}
      </div>
      {listError && <p role="alert" className="text-red-600 text-sm mb-3">{listError}</p>}
      <textarea
        value={question}
        onChange={e => setQuestion(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Type your question… (Ctrl+Enter to submit)"
        rows={3}
        className="w-full border rounded px-3 py-2 text-sm mb-3 resize-none focus:outline-none focus:ring-2 focus:ring-blue-300"
      />
      <div className="flex items-center gap-4 mb-4">
        <button
          onClick={submit}
          disabled={loading || !question.trim()}
          className="bg-blue-600 text-white px-5 py-2 rounded text-sm disabled:opacity-50 hover:bg-blue-700"
        >
          {loading ? 'Thinking…' : 'Ask'}
        </button>
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input type="checkbox" checked={showCitations} onChange={e => setShowCitations(e.target.checked)} />
          Show source citations
        </label>
      </div>
      {error && <p className="text-red-600 text-sm mb-3">{error}</p>}
      {answer && (
        <div className="bg-white border rounded p-4">
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown>{answer}</ReactMarkdown>
          </div>
          {latency && (
            <p className="text-xs text-gray-400 mt-3">
              Retrieved in {latency.ret}ms · Generated in {latency.llm}ms
            </p>
          )}
          {showCitations && citations && <CitationsPanel citations={citations} />}
        </div>
      )}
    </div>
  )
}
