// Live view of one job: message line, per-item LED bars, terminal states.
// Pure render over the snapshot the jobs context keeps fresh via SSE.

import React from 'react'
import { api } from '../api'
import Button from './Button'

export function LedBar({ pct, state }) {
  const cls = state === 'done' ? 'done' : state === 'failed' ? 'failed' : ''
  const width = state === 'done' ? 100 : pct
  return (
    <div className="led-track">
      <div className={`led-fill ${cls}`} style={{ width: `${width}%` }} />
    </div>
  )
}

export default function JobPanel({ snapshot, children }) {
  if (!snapshot) return null
  const { state, message, items, error, id } = snapshot
  return (
    <div>
      {state === 'running' && (
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div className="job-message">{message || 'Working…'}</div>
          <Button variant="ghost" size="sm" onClick={() => api.cancelJob(id)}>
            Cancel
          </Button>
        </div>
      )}
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
