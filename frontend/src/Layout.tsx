// App frame: left rail (tool nav grouped by category, "/" quick filter),
// keep-alive hosts for every open tool, and the tab dock along the bottom.
//
// Open tools stay MOUNTED — an inactive one is display:none, not unmounted —
// so a half-configured form survives jumping to another tool and back. The
// dock shows one browser-style tab per open tool with its latest job state
// inline; a tab with a running job refuses to close, so running work can
// never silently disappear.

import React, { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Link, useLocation, useNavigate } from 'react-router'
import { api } from './api'
import { useJobs } from './jobs'
import { LedBar } from './components/JobPanel'
import Button from './components/Button'
import ErrorBoundary from './components/ErrorBoundary'
import ThemeToggle from './components/ThemeToggle'
import Home from './Home'
import { PAGES, isToolSlug, type ToolSlug } from './pages'
import { TOOL_EMOJI } from './tools'
import { nextTabAfterClose, parseToolSlug, restoreTabs, tabOrder } from './tabs'
import { ToolActiveContext } from './toolHost'
import type { Category } from './types/api'

// sessionStorage (not local): each browser tab is its own workbench, and a
// fresh session starts clean. Reload restores the tabs, not their form state.
const TABS_KEY = 'toolkit.openTabs'

const toolPath = (slug: string) => `/tools/${slug}`

interface DockProps {
  openTabs: ToolSlug[]
  activeSlug: ToolSlug | null
  titles: Record<string, string>
  onCloseTab: (slug: ToolSlug) => void
}

function Dock({ openTabs, activeSlug, titles, onCloseTab }: DockProps) {
  const { jobs, dismiss } = useJobs()
  const navigate = useNavigate()

  // The dock scrolls horizontally when tabs overflow (narrow screens); keep
  // the tab just activated visible, the way a browser keeps its active tab.
  const dockRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    dockRef.current
      ?.querySelector('.dock-tab.active')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [activeSlug])

  // One tab per open tool. Tools that still have tracked jobs are appended
  // even if their tab is gone (defensive: jobs must never become invisible).
  const jobSlugs = Object.values(jobs)
    .map((j) => parseToolSlug(j.toolPath))
    .filter((s): s is ToolSlug => s !== null && isToolSlug(s))
  const tabs = tabOrder(openTabs, jobSlugs)

  const close = (slug: ToolSlug) => {
    // Closing a tab is also how its finished jobs get dismissed; running ones
    // can't reach here (the × is disabled) and would keep the tab derived.
    for (const [id, j] of Object.entries(jobs)) {
      if (j.toolPath === toolPath(slug) && j.snapshot.state !== 'running') dismiss(id)
    }
    if (slug === activeSlug) {
      const next = nextTabAfterClose(tabs, slug)
      // replace, not push: the closed tool's path must leave the history
      // stack, or one Back press would resurrect the tab as a blank copy.
      navigate(next ? toolPath(next) : '/', { replace: true })
    }
    onCloseTab(slug)
  }

  return (
    <div className="dock" ref={dockRef}>
      {tabs.length === 0 && <span className="dock-empty">NO OPEN TOOLS</span>}
      {tabs.map((slug) => {
        const path = toolPath(slug)
        const title = titles[slug]
        const emoji = title?.split(' ')[0] || TOOL_EMOJI[path] || '⚙️'
        const label = title ? title.split(' ').slice(1).join(' ') : slug.replace(/-/g, ' ')

        // Badge: the tool's running job if any, else its most recent one.
        const toolJobs = Object.values(jobs).filter((j) => j.toolPath === path)
        const runningJob = toolJobs.findLast((j) => j.snapshot.state === 'running')
        const shown = runningJob ?? toolJobs[toolJobs.length - 1]
        let badge: React.ReactNode = null
        if (shown) {
          const { snapshot } = shown
          const items = snapshot.items ?? []
          const total = items.length
          const done = items.filter((i) => i.state === 'done').length
          const pct = total > 0 ? Math.round(items.reduce((s, i) => s + i.pct, 0) / total) : null
          badge =
            snapshot.state === 'running' ? (
              pct === null ? (
                <span>{snapshot.message || 'working…'}</span>
              ) : (
                <>
                  <LedBar pct={pct} state="running" />
                  <span>{total ? `${done}/${total}` : `${pct}%`}</span>
                </>
              )
            ) : (
              // State name spelled out: failed and cancelled share the red ✕,
              // and a bare glyph says nothing to a screen reader.
              <span className={`state-${snapshot.state}`}>
                {snapshot.state === 'done' ? '✓ done' : `✕ ${snapshot.state}`}
              </span>
            )
        }

        return (
          <Link
            key={slug}
            className={`dock-tab ${slug === activeSlug ? 'active' : ''}`}
            to={path}
            aria-current={slug === activeSlug ? 'page' : undefined}
          >
            <span>{emoji}</span>
            <span className="dock-tab-label">{label}</span>
            {badge}
            <Button
              variant="ghost"
              size="sm"
              className="dock-close"
              disabled={runningJob !== undefined}
              title={runningJob ? 'a job is still running' : 'close tool'}
              onClick={(e) => {
                e.preventDefault()
                close(slug)
              }}
              aria-label={`Close ${label}`}
            >
              ×
            </Button>
          </Link>
        )
      })}
    </div>
  )
}

export default function Layout() {
  const location = useLocation()
  const [categories, setCategories] = useState<Category[]>([])
  const [toolsError, setToolsError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)

  const rawSlug = parseToolSlug(location.pathname)
  const activeSlug = rawSlug !== null && isToolSlug(rawSlug) ? rawSlug : null

  const [openTabs, setOpenTabs] = useState<ToolSlug[]>(() =>
    restoreTabs(sessionStorage.getItem(TABS_KEY), isToolSlug),
  )

  // Visiting a tool opens its tab. Adjusted during render (the documented
  // you-might-not-need-an-effect pattern), and keyed to a path TRANSITION,
  // not to the current location alone: closing the active tab removes the
  // slug and navigates away, and a render can land in between with the old
  // location still showing — matching on the transition keeps that
  // intermediate render from re-opening the tab that was just closed.
  const [prevPath, setPrevPath] = useState<string | null>(null)
  if (prevPath !== location.pathname) {
    setPrevPath(location.pathname)
    if (activeSlug && !openTabs.includes(activeSlug)) {
      setOpenTabs([...openTabs, activeSlug])
    }
  }

  useEffect(() => {
    sessionStorage.setItem(TABS_KEY, JSON.stringify(openTabs))
  }, [openTabs])

  // Per-route scroll memory: pages share one scroll container, so switching
  // tabs would otherwise carry one tool's scroll position into the next.
  // Saved as the user scrolls, NOT at switch time: by the time the layout
  // effect runs the outgoing page is already hidden and the browser has
  // clamped scrollTop to the incoming page's height, so reading it there
  // would overwrite a long page's position with 0.
  const mainRef = useRef<HTMLElement>(null)
  const scrollsRef = useRef(new Map<string, number>())
  useLayoutEffect(() => {
    const el = mainRef.current
    if (el) el.scrollTop = scrollsRef.current.get(location.pathname) ?? 0
  }, [location.pathname])

  // Load the tool catalog; on failure keep an error note and retry when the
  // window regains focus, instead of dead-ending on a permanently empty rail.
  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .tools()
        .then((cats) => {
          if (cancelled) return
          setCategories(cats)
          setToolsError(null)
        })
        .catch((err: Error) => {
          if (!cancelled) setToolsError(err.message)
        })
    load()
    window.addEventListener('focus', load)
    return () => {
      cancelled = true
      window.removeEventListener('focus', load)
    }
  }, [])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      // e.target is EventTarget, which has no tagName; the guard exists to skip
      // the shortcut while the user is typing in a field.
      const target = e.target as HTMLElement | null
      if (e.key === '/' && !/input|textarea|select/i.test(target?.tagName ?? '')) {
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

  const titles = useMemo(() => {
    const map: Record<string, string> = {}
    for (const cat of categories) for (const tool of cat.tools) map[tool.slug] = tool.title
    return map
  }, [categories])

  const rail = (
    <nav className={`rail ${open ? 'open' : ''}`}>
      <Link
        to="/"
        className="brand"
        onClick={() => setOpen(false)}
        style={{ textDecoration: 'none' }}
      >
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
      <NavLink
        to="/"
        end
        className={({ isActive }) => `rail-link ${isActive ? 'active' : ''}`}
        onClick={() => setOpen(false)}
      >
        <span className="emoji">🏠</span> Home
      </NavLink>
      {toolsError && categories.length === 0 && (
        <div className="rail-cat" style={{ color: 'var(--red)' }}>
          backend unreachable — retrying…
        </div>
      )}
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
      <ThemeToggle />
    </nav>
  )

  return (
    <div className="frame">
      <div className="topbar">
        <button type="button" onClick={() => setOpen(true)} aria-label="Open navigation">
          ☰
        </button>
        <span className="brand-name">🧰 Toolkit</span>
      </div>
      {rail}
      {open && (
        <button
          type="button"
          className="scrim"
          onClick={() => setOpen(false)}
          aria-label="Close navigation"
        />
      )}
      <main
        className="main"
        ref={mainRef}
        onScroll={(e) => scrollsRef.current.set(location.pathname, e.currentTarget.scrollTop)}
      >
        {/* Home stays mount-on-visit so its health lamps re-check each time. */}
        {location.pathname === '/' && <Home />}
        {location.pathname !== '/' && activeSlug === null && (
          <div className="note info">
            Nothing at <code>{location.pathname}</code> — <Link to="/">back to the bench</Link>.
          </div>
        )}
        {openTabs.map((slug) => {
          const Page = PAGES[slug]
          return (
            // Per-host ErrorBoundary: Suspense doesn't catch a rejected lazy
            // chunk or a render error, and without a boundary here one broken
            // tool would unmount every host — losing all the kept-alive state
            // these tabs exist to preserve.
            <div key={slug} className="tool-host" hidden={slug !== activeSlug}>
              <ToolActiveContext.Provider value={slug === activeSlug}>
                <ErrorBoundary>
                  <Suspense fallback={<div className="note info">Loading…</div>}>
                    <Page />
                  </Suspense>
                </ErrorBoundary>
              </ToolActiveContext.Provider>
            </div>
          )
        })}
      </main>
      <Dock
        openTabs={openTabs}
        activeSlug={activeSlug}
        titles={titles}
        onCloseTab={(slug) => setOpenTabs((prev) => prev.filter((s) => s !== slug))}
      />
    </div>
  )
}
