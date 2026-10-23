import { Citation } from '../api/client'

export default function CitationsPanel({ citations }: { citations: Citation[] }) {
  return (
    <div className="mt-4 border-t pt-4">
      <h3 className="text-sm font-semibold text-gray-600 mb-2">Sources</h3>
      <div className="space-y-2">
        {citations.map((c, i) => (
          <div key={i} className="text-xs bg-gray-50 border rounded p-2">
            <div className="flex justify-between mb-1">
              <span className="font-medium">{c.source_file}</span>
              <span className="text-gray-500">chunk {c.chunk_index} · score {c.score === null ? 'n/a' : c.score.toFixed(3)}</span>
            </div>
            <p className="text-gray-700 italic">{c.excerpt}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
