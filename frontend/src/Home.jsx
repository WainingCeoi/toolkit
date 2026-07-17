// The bench: tool drawers grouped by category + a dependency health strip
// that loads independently of everything else.

import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from './api'
import { CATEGORY_ACCENT } from './tools'

const LAMPS = [
  ['ffmpeg', 'ffmpeg'],
  ['soffice', 'LibreOffice'],
  ['mineru', 'MinerU'],
]

export default function Home() {
  const [categories, setCategories] = useState([])
  const [health, setHealth] = useState(null)

  useEffect(() => {
    api.tools().then(setCategories).catch(() => setCategories([]))
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [])

  return (
    <div>
      <div className="bench-head">
        <h1>🧰 Toolkit</h1>
        <p>A local collection of small media &amp; file utilities. Pick a tool — or press / to find one.</p>
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
                style={{ '--accent': CATEGORY_ACCENT[cat.name] }}
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
