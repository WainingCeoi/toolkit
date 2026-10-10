import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { api, retryingLoad } from './api'
import {
  GROUP_DESCRIPTIONS,
  groupTools,
  TOOL_DETAILS,
  toolTitle,
  type ToolGroup,
} from './tools'
import Icon from './components/Icon'
import Button from './components/Button'
import EmptyState from './components/EmptyState'
import type { Category, Health } from './types/api'

const DEPENDENCIES: {
  key: keyof Omit<Health, 'ok'>
  label: string
  tool: string
  path: string
}[] = [
  { key: 'ffmpeg', label: 'FFmpeg', tool: 'Video processing', path: '/tools/remux' },
  { key: 'soffice', label: 'LibreOffice', tool: 'Word to PDF', path: '/tools/doc-to-pdf' },
  { key: 'mineru', label: 'MinerU', tool: 'Document extraction', path: '/tools/doc-to-markdown' },
]

export default function Home({
  categories,
  toolsError,
  toolsLoaded,
  openTabs,
}: {
  categories: Category[]
  toolsError: string | null
  toolsLoaded: boolean
  openTabs: string[]
}) {
  const [health, setHealth] = useState<Health | null>(null)
  const [query, setQuery] = useState('')
  const [selectedGroup, setSelectedGroup] = useState<ToolGroup | 'All tools'>('All tools')
  const groups = useMemo(() => groupTools(categories), [categories])
  const toolCount = categories.reduce((total, category) => total + category.tools.length, 0)
  const enabled = new Set(
    categories.flatMap((category) => category.tools.map((tool) => tool.slug)),
  )
  const recent = openTabs
    .filter((slug) => enabled.has(slug))
    .slice(-4)
    .reverse()
  const visible = groups
    .filter((group) => selectedGroup === 'All tools' || group.name === selectedGroup)
    .flatMap((group) => group.tools)
    .filter((tool) =>
      `${toolTitle(tool.slug, tool.title)} ${TOOL_DETAILS[tool.slug]?.description} ${tool.description}`
        .toLowerCase()
        .includes(query.trim().toLowerCase()),
    )

  useEffect(() => {
    const loader = retryingLoad(
      () => api.health(),
      setHealth,
      () => setHealth(null),
    )
    window.addEventListener('focus', loader.reload)
    return () => {
      loader.stop()
      window.removeEventListener('focus', loader.reload)
    }
  }, [])

  return (
    <div className="home">
      <div className="bench-head">
        <div>
          <div className="eyebrow">THE EVERYDAY WORKSPACE</div>
          <h1>
            A little less busywork<span>.</span>
          </h1>
          <p>
            Small tools for the things that take too long.<br />
            Process media, tidy files, and make documents work for you.
          </p>
        </div>
        <div className="home-local">
          <Icon name="shield" size={22} />
          <span>
            Runs on your Mac<strong>Your tools, close at hand.</strong>
          </span>
        </div>
      </div>

      {recent.length > 0 && (
        <div className="continue-row">
          <span>
            <Icon name="clock" size={15} /> Continue working
          </span>
          <div>
            {recent.map((slug) => (
              <Link key={slug} to={`/tools/${slug}`}>
                <Icon name={TOOL_DETAILS[slug]?.icon ?? 'toolkit'} size={15} />
                {toolTitle(slug)}
                <Icon name="arrow" size={13} />
              </Link>
            ))}
          </div>
        </div>
      )}

      <section className="library" aria-labelledby="library-title">
        <div className="library-heading">
          <h2 id="library-title">
            Your tools <span>{toolsLoaded ? toolCount : '–'}</span>
          </h2>
          <span>Pick a tool. Make room for more.</span>
        </div>
        <div className="library-toolbar">
          <div className="category-tabs" role="group" aria-label="Filter tools by category">
            {(['All tools', ...groups.map((group) => group.name)] as const).map((group) => (
              <button
                key={group}
                type="button"
                className={selectedGroup === group ? 'active' : ''}
                aria-pressed={selectedGroup === group}
                onClick={() => setSelectedGroup(group)}
              >
                {group}
              </button>
            ))}
          </div>
          <div className="library-search">
            <Icon name="search" size={17} />
            <input
              aria-label="Search tool library"
              placeholder="Search tools…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            {query && (
              <Button
                variant="ghost"
                size="sm"
                aria-label="Clear search"
                onClick={() => setQuery('')}
              >
                <Icon name="close" size={14} />
              </Button>
            )}
          </div>
        </div>
        {toolsError && categories.length === 0 ? (
          <div className="note error" role="alert">
            Unable to connect to Toolkit. Retrying automatically…
          </div>
        ) : !toolsLoaded ? (
          <div className="library-loading" aria-label="Loading tools" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="card-skeleton">
                <span />
                <span />
                <span />
              </div>
            ))}
          </div>
        ) : categories.length === 0 ? (
          <EmptyState icon="toolkit" title="No tools enabled">
            Enable tools in your local Toolkit configuration to add them to this workspace.
          </EmptyState>
        ) : (
          <>
            {selectedGroup !== 'All tools' && (
              <p className="category-description">{GROUP_DESCRIPTIONS[selectedGroup]}</p>
            )}
            <div className="drawer-grid">
              {visible.map((tool) => {
                const detail = TOOL_DETAILS[tool.slug]
                return (
                  <Link
                    key={tool.slug}
                    to={`/tools/${tool.slug}`}
                    className={`drawer tone-${detail?.group.toLowerCase()}`}
                  >
                    <div className="drawer-top">
                      <span className="card-icon">
                        <Icon name={detail?.icon ?? 'toolkit'} size={23} />
                      </span>
                      <Icon name="arrow" size={17} className="drawer-arrow" />
                    </div>
                    <h3>{toolTitle(tool.slug, tool.title)}</h3>
                    <p>{detail?.description ?? tool.description}</p>
                    <div className="drawer-footer">
                      <span className="category-badge">{detail?.group}</span>
                      <span>{detail?.tag}</span>
                    </div>
                  </Link>
                )
              })}
            </div>
            {visible.length === 0 && (
              <EmptyState icon="search" title="No tools found">
                Try a different search or category.<br />
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setQuery('')
                    setSelectedGroup('All tools')
                  }}
                >
                  Reset filters
                </Button>
              </EmptyState>
            )}
            <div className="library-footnote">
              <Icon name="grid" size={15} />
              <span>Switch freely between tools. Your inputs and running tasks stay with you.</span>
              <kbd>/</kbd>
              <span>to find a tool</span>
            </div>
          </>
        )}
      </section>

      {toolCount > 0 && (
        <section className="workspace-status" aria-labelledby="workspace-status-title">
          <div className="status-heading">
            <Icon name="monitor" size={20} />
            <div>
              <h2 id="workspace-status-title">Workspace essentials</h2>
              <p>Apps that power your conversion tools.</p>
            </div>
          </div>
          <div className="dependency-list">
            {DEPENDENCIES
              .filter((dependency) => enabled.has(dependency.path.split('/').pop() ?? ''))
              .map(({ key, label, tool, path }) => (
                <Link key={key} to={path} className="dependency">
                  <span
                    className={`status-dot ${health ? health[key] ? 'ready' : 'missing' : ''}`}
                  />
                  <div>
                    <strong>{label}</strong>
                    <span>{tool}</span>
                  </div>
                  <span className={health && !health[key] ? 'dependency-missing' : ''}>
                    {health
                      ? health[key] ? 'Ready' : 'Setup needed'
                      : 'Checking…'}
                  </span>
                  <Icon name="chevron" size={13} />
                </Link>
              ))}
          </div>
        </section>
      )}
    </div>
  )
}
