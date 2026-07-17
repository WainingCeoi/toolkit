// App frame: left rail (tool nav grouped by category, "/" quick filter),
// main outlet, and the job dock pinned along the bottom of every route.

import React, { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Link, Outlet } from 'react-router-dom'
import { api } from './api'
import { useJobs } from './jobs'
import { LedBar } from './components/JobPanel'
import { TOOL_EMOJI } from './tools'

function Dock() {
  const { jobs, dismiss } = useJobs()
  const entries = Object.entries(jobs)
  return (
    <div className="dock">
      {entries.length === 0 && <span className="dock-empty">NO ACTIVE JOBS</span>}
      {entries.map(([id, { snapshot, toolPath }]) => {
        const total = snapshot.items.length
        const done = snapshot.items.filter((i) => i.state === 'done').length
        const pct =
          snapshot.state === 'done'
            ? 100
            : total > 0
              ? Math.round(snapshot.items.reduce((s, i) => s + i.pct, 0) / total)
              : null
        return (
          <Link key={id} className="dock-job" to={toolPath}>
            <span>{TOOL_EMOJI[toolPath] || '⚙️'}</span>
            {snapshot.state === 'running' ? (
              pct === null ? (
                <span>{snapshot.message || 'working…'}</span>
              ) : (
                <>
                  <LedBar pct={pct} state="running" />
                  <span>{total ? `${done}/${total}` : `${pct}%`}</span>
                </>
              )
            ) : (
              <span className={`state-${snapshot.state}`}>
                {snapshot.state === 'done' ? '✓ done' : `✕ ${snapshot.state}`}
              </span>
            )}
            {snapshot.state !== 'running' && (
              <button
                type="button"
                className="btn ghost"
                style={{ minHeight: 0, padding: '0 4px' }}
                onClick={(e) => {
                  e.preventDefault()
                  dismiss(id)
                }}
                aria-label="Dismiss job"
              >
                ×
              </button>
            )}
          </Link>
        )
      })}
    </div>
  )
}

export default function Layout() {
  const [categories, setCategories] = useState([])
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState(false)
  const searchRef = useRef(null)

  useEffect(() => {
    api.tools().then(setCategories).catch(() => setCategories([]))
  }, [])

  useEffect(() => {
    function onKey(e) {
      if (e.key === '/' && !/input|textarea|select/i.test(e.target.tagName)) {
        e.preventDefault()
        searchRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const shown = useMemo(() => {
    if (!filter) return categories
    const q = filter.toLowerCase()
    return categories
      .map((c) => ({ ...c, tools: c.tools.filter((t) => t.title.toLowerCase().includes(q)) }))
      .filter((c) => c.tools.length > 0)
  }, [categories, filter])

  const rail = (
    <nav className={`rail ${open ? 'open' : ''}`}>
      <Link to="/" className="brand" onClick={() => setOpen(false)} style={{ textDecoration: 'none' }}>
        <span className="brand-name">🧰 Toolkit</span>
        <span className="brand-sub">media · files</span>
      </Link>
      <div className="rail-search">
        <input
          ref={searchRef}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="find a tool"
          aria-label="Find a tool"
        />
        <kbd>/</kbd>
      </div>
      <NavLink to="/" end className={({ isActive }) => `rail-link ${isActive ? 'active' : ''}`} onClick={() => setOpen(false)}>
        <span className="emoji">🏠</span> Home
      </NavLink>
      {shown.map((cat) => (
        <React.Fragment key={cat.name}>
          <div className="rail-cat">{cat.name}</div>
          {cat.tools.map((tool) => {
            const [emoji, ...rest] = tool.title.split(' ')
            return (
              <NavLink
                key={tool.slug}
                to={`/tools/${tool.slug}`}
                className={({ isActive }) => `rail-link ${isActive ? 'active' : ''}`}
                onClick={() => setOpen(false)}
              >
                <span className="emoji">{emoji}</span> {rest.join(' ')}
              </NavLink>
            )
          })}
        </React.Fragment>
      ))}
    </nav>
  )

  return (
    <div className="frame">
      <div className="topbar">
        <button type="button" onClick={() => setOpen(true)} aria-label="Open navigation">☰</button>
        <span className="brand-name">🧰 Toolkit</span>
      </div>
      {rail}
      {open && <button type="button" className="scrim" onClick={() => setOpen(false)} aria-label="Close navigation" />}
      <main className="main">
        <Outlet />
      </main>
      <Dock />
    </div>
  )
}
