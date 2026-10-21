import { useState, useEffect, useRef } from 'react'
import { api, HealthResult, MetricsResult } from '../api/client'
import LatencyCharts from '../components/LatencyCharts'

export default function HealthPage() {
  const [health, setHealth] = useState<HealthResult | null>(null)
  const [metrics, setMetrics] = useState<MetricsResult | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)

  const [sessionIssues, setSessionIssues] = useState<{ filename: string; code: string; message: string }[]>([])
  const [diagnosticError, setDiagnosticError] = useState(false)

  const [diagnosticPending, setDiagnosticPending] = useState(true)
  const diagnosticTicket = useRef(0)

  function load() {
    const ticket = ++diagnosticTicket.current
    setDiagnosticPending(true)
    api.getSessionDiagnostics().then(result => {
      if (ticket !== diagnosticTicket.current) return
      setSessionIssues(result.issues); setDiagnosticError(false)
    }).catch(() => {
      if (ticket !== diagnosticTicket.current) return
      setSessionIssues([]); setDiagnosticError(true)
    }).finally(() => {
      if (ticket === diagnosticTicket.current) setDiagnosticPending(false)
    })
    // Health and metrics load separately: a degraded or unreachable API must not
    // stop the latency charts, and no stale green cards stay up (#375).
    api.getHealth().then(h => {
      setHealth(h); setHealthError(null)
    }).catch(e => {
      setHealth(null); setHealthError(e instanceof Error ? e.message : String(e))
    })
    api.getMetrics().then(setMetrics).catch(() => { /* keep the last metrics shown */ })
  }

  useEffect(() => {
    load()
    const interval = setInterval(load, 30000)
    return () => { ++diagnosticTicket.current; clearInterval(interval) }
  }, [])

  function StatusBadge({ status }: { status: string }) {
    return (
      <span className={`inline-block w-2 h-2 rounded-full mr-2 ${status === 'ok' ? 'bg-green-500' : 'bg-red-500'}`} />
    )
  }

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="text-2xl font-bold mb-6">Health Dashboard</h1>
      {diagnosticPending && <p role="status" className="text-gray-500 mb-4">Refreshing session recovery diagnostics…</p>}
      {diagnosticError && <p role="alert" className="text-amber-700 mb-4">Session recovery diagnostics could not be refreshed.</p>}
      {sessionIssues.length > 0 && <div role="alert" className="border border-amber-300 bg-amber-50 rounded p-4 mb-6">
        <h2 className="font-semibold">{diagnosticPending ? "Previous evaluation session recovery results — refresh pending" : "Evaluation session recovery needs attention"}</h2>
        {sessionIssues.map(issue => <p key={issue.filename} className="text-sm mt-2">{issue.filename}: {issue.code} — {issue.message}</p>)}
      </div>}
      {healthError && <p role="alert" className="text-red-700 mb-4">Service health could not be read: {healthError}</p>}
      {health && (
        <div className="grid grid-cols-3 gap-4 mb-8">
          <div className="bg-white border rounded p-4">
            <div className="flex items-center text-sm font-medium mb-1">
              <StatusBadge status={health.services.weaviate.status} />
              Weaviate
            </div>
            <div className="text-xs text-gray-500">{health.services.weaviate.latency_ms}ms</div>
          </div>
          <div className="bg-white border rounded p-4">
            <div className="flex items-center text-sm font-medium mb-1">
              <StatusBadge status={health.services.ollama.llm.status} />
              LLM ({health.services.ollama.llm.model})
            </div>
            <div className="text-xs text-gray-500">{health.services.ollama.llm.latency_ms}ms</div>
          </div>
          <div className="bg-white border rounded p-4">
            <div className="flex items-center text-sm font-medium mb-1">
              <StatusBadge status={health.services.ollama.embed.status} />
              Embed ({health.services.ollama.embed.model})
            </div>
            <div className="text-xs text-gray-500">{health.services.ollama.embed.latency_ms}ms</div>
          </div>
        </div>
      )}
      {metrics && metrics.total_records > 0 && (
        <div className="bg-white border rounded p-4">
          <h2 className="font-semibold mb-4">Latency Trends ({metrics.total_records} queries in ring buffer)</h2>
          <LatencyCharts data={metrics} />
        </div>
      )}
      {metrics && metrics.total_records === 0 && (
        <p className="text-gray-400 text-sm">No query data yet. Run some Q&A queries to populate charts.</p>
      )}
    </div>
  )
}
