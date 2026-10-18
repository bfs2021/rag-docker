import { JobStatus } from '../api/client'

export default function ProgressPanel({ job }: { job: JobStatus }) {
  const pct = job.files_total > 0 ? Math.round((job.files_completed / job.files_total) * 100) : 0
  return (
    <div className="mt-4 p-4 border rounded bg-gray-50">
      <div className="flex justify-between text-sm mb-1">
        <span>Files: {job.files_completed}/{job.files_total}</span>
        <span>Chunks stored: {job.chunks_stored}</span>
        <span className={job.status === 'completed' ? 'text-green-600' : job.status === 'failed' ? 'text-red-600' : 'text-blue-600'}>
          {job.status}
        </span>
      </div>
      <div className="w-full bg-gray-200 rounded h-2">
        <div className="bg-blue-500 h-2 rounded transition-all" style={{ width: `${pct}%` }} />
      </div>
      {(job.chunks_replaced ?? 0) > 0 && (
        // Re-ingesting a name replaces its chunks (#22), so the total can stay
        // the same while the content changed.
        <p className="mt-2 text-xs text-gray-600">
          Replaced {job.chunks_replaced} chunk{job.chunks_replaced === 1 ? '' : 's'} from a previous version
        </p>
      )}
      {job.errors.length > 0 && (
        <ul className="mt-2 text-xs text-red-600 space-y-1">
          {job.errors.map((e, i) => <li key={i}>{e}</li>)}
        </ul>
      )}
      {(job.skipped?.length ?? 0) > 0 && (
        // Skipped files are never counted in files_total, so without this the
        // count simply reads lower than what the user uploaded. Each entry
        // says why: an unsupported type, or a later file with the same name.
        <div className="mt-2 text-xs text-amber-700">
          <p className="font-medium">
            {job.skipped!.length} file{job.skipped!.length === 1 ? '' : 's'} skipped:
          </p>
          <ul className="space-y-1 mt-1">
            {job.skipped!.map((f, i) => <li key={i}>{f}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}
