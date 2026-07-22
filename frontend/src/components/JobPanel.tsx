// Live view of one job: message line, per-item LED bars, terminal states.
// Pure render over the snapshot the jobs context keeps fresh via SSE.

import { useState, type ReactNode } from 'react'
import { api } from '../api'
import Button from './Button'
import type { Job, JobItemState } from '../types/api'

export function LedBar({ pct, state }: { pct: number; state: JobItemState }) {
  const cls = state === 'done' ? 'done' : state === 'failed' ? 'failed' : ''
  const width = state === 'done' ? 100 : pct
  return (
    <div className="led-track">
      <div className={`led-fill ${cls}`} style={{ width: `${width}%` }} />
    </div>
  )
}

interface JobPanelProps {
  // Renders any tool's job, so the result shape is irrelevant here — this
  // component only ever reads the envelope.
  snapshot: Job<unknown> | null
  children?: ReactNode
}

export default function JobPanel({ snapshot, children }: JobPanelProps) {
  const [cancelError, setCancelError] = useState<string | null>(null)
  if (!snapshot) return null
  const { state, message, items = [], error, id } = snapshot
  const cancel = () =>
    api.cancelJob(id).catch((err: Error) => setCancelError(err.message || 'Cancel failed.'))
  return (
    <div>
      {state === 'running' && (
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div className="job-message">{message || 'Working…'}</div>
          <Button variant="ghost" size="sm" onClick={cancel}>
            Cancel
          </Button>
        </div>
      )}
      {cancelError && <div className="note error">{cancelError}</div>}
      {state !== 'running' && message && <div className="job-message">{message}</div>}
      {items.map((item, i) => (
        <div className="job-item" key={i}>
          <span className={`name ${item.state}`}>{item.name}</span>
          <LedBar pct={item.pct} state={item.state} />
          <span className="pct">{item.state === 'done' ? '100' : item.pct}%</span>
        </div>
      ))}
      {state === 'failed' && <div className="note error">{error}</div>}
      {state === 'cancelled' && (
        <div className="note warn">Run cancelled — showing partial results.</div>
      )}
      {children /* tool-specific result rendering */}
    </div>
  )
}
