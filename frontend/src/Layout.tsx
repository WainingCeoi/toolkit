// App frame: rail, keep-alive hosts for every open tool, and the tab dock.

import React, {
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { NavLink, Link, useLocation, useNavigate } from 'react-router'
import { api, retryingLoad } from './api'
import { useJobs } from './jobs'
import { LedBar } from './components/JobPanel'
import Button from './components/Button'
import ErrorBoundary from './components/ErrorBoundary'
import ThemeToggle from './components/ThemeToggle'
import Home from './Home'
import { PAGES, isToolSlug, type ToolSlug } from './pages'
import { TOOL_EMOJI } from './tools'
import { readSession, writeSession } from './sessionStore'
import { nextTabAfterClose, parseToolSlug, restoreTabs, tabOrder } from './tabs'
import { ToolActiveContext, ToolBusyContext, ToolSlugContext } from './toolHost'
import type { Category } from './types/api'

const TABS_KEY = 'toolkit.openTabs'

const toolPath = (slug: string) => `/tools/${slug}`

interface DockProps {
  openTabs: ToolSlug[]
  activeSlug: ToolSlug | null
  titles: Record<string, string>
  busySlugs: Set<string>
  onCloseTab: (slug: ToolSlug) => void
}

function Dock({ openTabs, activeSlug, titles, busySlugs, onCloseTab }: DockProps) {
  const { jobs, dismiss } = useJobs()
  const navigate = useNavigate()

  const dockRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    dockRef.current
      ?.querySelector('.dock-tab.active')
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }, [activeSlug])

  const jobSlugs = Object.values(jobs)
    .map((j) => parseToolSlug(j.toolPath))
    .filter((s): s is ToolSlug => s !== null && isToolSlug(s))
  const tabs = tabOrder(openTabs, jobSlugs)

  const close = (slug: ToolSlug) => {
    for (const [id, j] of Object.entries(jobs)) {
      if (j.toolPath === toolPath(slug) && j.snapshot.state !== 'running') dismiss(id)
    }
    if (slug === activeSlug) {
      const next = nextTabAfterClose(tabs, slug)
      // replace, not push: otherwise Back would reopen the closed tab.
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

        const toolJobs = Object.entries(jobs).filter(([, j]) => j.toolPath === path)
        const hasRunning =
          busySlugs.has(slug) || toolJobs.some(([, j]) => j.snapshot.state === 'running')

        return (
          // A div, not a Link: buttons cannot nest inside an anchor.
          <div key={slug} className={`dock-tab ${slug === activeSlug ? 'active' : ''}`}>
            <Link
              className="dock-tab-link"
              to={path}
              aria-current={slug === activeSlug ? 'page' : undefined}
            >
              <span>{emoji}</span>
              <span className="dock-tab-label">{label}</span>
            </Link>
            {toolJobs.map(([id, { snapshot }]) => {
              const items = snapshot.items ?? []
              const total = items.length
              const done = items.filter((i) => i.state === 'done').length
              const pct =
                total > 0 ? Math.round(items.reduce((s, i) => s + i.pct, 0) / total) : null
              return (
                <span key={id} className="dock-job">
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
                    // Spelled out: failed and cancelled share a glyph; screen readers need text.
                    <span className={`state-${snapshot.state}`}>
                      {snapshot.state === 'done' ? '✓ done' : `✕ ${snapshot.state}`}
                    </span>
                  )}
                  {snapshot.state !== 'running' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="dock-dismiss"
                      title="dismiss this job"
                      onClick={() => dismiss(id)}
                      aria-label={`Dismiss ${snapshot.state} ${label} job`}
                    >
                      ×
                    </Button>
                  )}
                </span>
              )
            })}
            <Button
              variant="ghost"
              size="sm"
              className="dock-close"
              disabled={hasRunning}
              title={hasRunning ? 'this tool is still working' : 'close tool'}
              onClick={() => close(slug)}
              aria-label={`Close ${label}`}
            >
              ×
            </Button>
          </div>
        )
      })}
    </div>
  )
}

interface ToolHostProps {
  slug: ToolSlug
  active: boolean
  setToolBusy: (slug: string, busy: boolean) => void
}

// memo: a Layout render (rail search, focus refetch, busy toggle) must not re-render every page.
const ToolHost = React.memo(function ToolHost({ slug, active, setToolBusy }: ToolHostProps) {
  const Page = PAGES[slug]
  return (
    // Per-host boundary: one broken tool must not unmount every kept-alive host.
    <div className="tool-host" hidden={!active}>
      <ToolActiveContext.Provider value={active}>
        <ToolSlugContext.Provider value={slug}>
          <ToolBusyContext.Provider value={setToolBusy}>
            <ErrorBoundary>
              <Suspense fallback={<div className="note info">Loading…</div>}>
                <Page />
              </Suspense>
            </ErrorBoundary>
          </ToolBusyContext.Provider>
        </ToolSlugContext.Provider>
      </ToolActiveContext.Provider>
    </div>
  )
})

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
    restoreTabs(readSession(TABS_KEY), isToolSlug),
  )

  // useCallback is load-bearing: useToolBusy's effect depends on this setter's identity.
  const [busySlugs, setBusySlugs] = useState<Set<string>>(() => new Set())
  const setToolBusy = useCallback((slug: string, busy: boolean) => {
    setBusySlugs((prev) => {
      if (prev.has(slug) === busy) return prev
      const next = new Set(prev)
      if (busy) next.add(slug)
      else next.delete(slug)
      return next
    })
  }, [])

  // Keyed to the path transition: keying on location alone re-opens a tab just closed.
  const [prevPath, setPrevPath] = useState<string | null>(null)
  if (prevPath !== location.pathname) {
    setPrevPath(location.pathname)
    if (activeSlug && !openTabs.includes(activeSlug)) {
      setOpenTabs([...openTabs, activeSlug])
    }
  }

  useEffect(() => {
    writeSession(TABS_KEY, JSON.stringify(openTabs))
  }, [openTabs])

  // Scroll memory is saved on scroll, not at switch time: by then the outgoing page is
  // hidden and scrollTop has been clamped to the incoming page's height.
  const mainRef = useRef<HTMLElement>(null)
  const scrollsRef = useRef(new Map<string, number>())
  useLayoutEffect(() => {
    const el = mainRef.current
    if (el) el.scrollTop = scrollsRef.current.get(location.pathname) ?? 0
  }, [location.pathname])

  useEffect(() => {
    const loader = retryingLoad(
      () => api.tools(),
      (cats: Category[]) => {
        setCategories(cats)
        setToolsError(null)
      },
      (err) => setToolsError(err.message),
    )
    window.addEventListener('focus', loader.reload)
    return () => {
      loader.stop()
      window.removeEventListener('focus', loader.reload)
    }
  }, [])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
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
        {openTabs.map((slug) => (
          <ToolHost
            key={slug}
            slug={slug}
            active={slug === activeSlug}
            setToolBusy={setToolBusy}
          />
        ))}
      </main>
      <Dock
        openTabs={openTabs}
        activeSlug={activeSlug}
        titles={titles}
        busySlugs={busySlugs}
        onCloseTab={(slug) => setOpenTabs((prev) => prev.filter((s) => s !== slug))}
      />
    </div>
  )
}
