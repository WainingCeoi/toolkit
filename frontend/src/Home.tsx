import { useEffect, useState, type CSSProperties } from 'react'
import { Link } from 'react-router'
import { api, retryingLoad } from './api'
import { CATEGORY_ACCENT } from './tools'
import type { Category, Health } from './types/api'

const LAMPS: [key: keyof Omit<Health, 'ok'>, label: string][] = [
  ['ffmpeg', 'ffmpeg'],
  ['soffice', 'LibreOffice'],
  ['mineru', 'MinerU'],
]

export default function Home() {
  const [categories, setCategories] = useState<Category[]>([])
  const [health, setHealth] = useState<Health | null>(null)
  const [toolsError, setToolsError] = useState<string | null>(null)

  useEffect(() => {
    const tools = retryingLoad(
      () => api.tools(),
      (cats: Category[]) => {
        setCategories(cats)
        setToolsError(null)
      },
      (err) => setToolsError(err.message),
    )
    const lamps = retryingLoad(
      () => api.health(),
      setHealth,
      () => setHealth(null),
    )
    return () => {
      tools.stop()
      lamps.stop()
    }
  }, [])

  return (
    <div>
      <div className="bench-head">
        <h1>🧰 Toolkit</h1>
        <p>
          A local collection of small media &amp; file utilities. Pick a tool — or press / to find
          one.
        </p>
        {health && (
          <div className="healthline">
            {LAMPS.map(([key, label]) => (
              <span key={key} className={`lamp ${health[key] ? '' : 'off'}`}>
                <i /> {label} {health[key] ? 'ready' : 'not found'}
              </span>
            ))}
          </div>
        )}
      </div>

      {toolsError && categories.length === 0 && (
        <div className="note error">backend unreachable — retrying…</div>
      )}

      {categories.map((cat) => (
        <section key={cat.name} className="drawer-cat">
          <div className="step">
            <span>{cat.name}</span>
          </div>
          <div className="drawer-grid">
            {cat.tools.map((tool) => (
              <Link
                key={tool.slug}
                to={`/tools/${tool.slug}`}
                className="drawer"
                // CSSProperties has no index signature for custom properties, hence the cast.
                style={{ '--accent': CATEGORY_ACCENT[cat.name] } as CSSProperties}
              >
                <div className="t">{tool.title}</div>
                <div className="d">{tool.description}</div>
                <div className="pull" />
              </Link>
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
