// File Gatherer — recursively gather files by type into one target folder.
// Mirrors backend/src/toolkit_api/routers/gather.py (POST /gather/start -> job).

import React, { useMemo, useState } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs.jsx'
import FolderField from '../components/FolderField.jsx'
import JobPanel from '../components/JobPanel.jsx'
import Button from '../components/Button'

// Client-side mirror of the engine presets — used ONLY for the live
// "Matching: …" caption; the backend builds the real pattern list from the
// category names + custom string we send it.
const FILE_TYPE_PRESETS = {
  Video: ['*.mkv', '*.mp4', '*.mov', '*.ts', '*.flv', '*.avi', '*.webm', '*.m4v', '*.wmv', '*.mpg', '*.mpeg'],
  Audio: ['*.mp3', '*.flac', '*.aac', '*.wav', '*.m4a', '*.ogg', '*.opus', '*.wma'],
  Image: ['*.jpg', '*.jpeg', '*.png', '*.gif', '*.heic', '*.webp', '*.bmp', '*.tiff'],
  Subtitle: ['*.srt', '*.ass', '*.ssa', '*.sub', '*.vtt'],
  Document: ['*.pdf', '*.docx', '*.doc', '*.txt', '*.epub', '*.pptx', '*.xlsx'],
  Archive: ['*.zip', '*.rar', '*.7z', '*.tar', '*.gz'],
}
const CATEGORY_NAMES = Object.keys(FILE_TYPE_PRESETS)

// 'srt'/'.srt' -> '*.srt'; tokens with * or ? are kept as real globs.
function normalizePattern(token) {
  const t = token.trim()
  if (!t) return null
  if (t.includes('*') || t.includes('?')) return t
  return `*.${t.replace(/^\.+/, '')}`
}

const captionStyle = {
  font: '12px var(--mono)',
  color: 'var(--muted)',
  overflowWrap: 'anywhere',
  margin: '4px 0 0',
}

function GatherResult({ result }) {
  const { moved, failed, scan_errors: scanErrors, target, warning } = result
  const nothingFound = moved.length === 0 && failed.length === 0
  return (
    <div>
      {scanErrors.length > 0 && (
        <details className="expander">
          <summary>⚠️ {scanErrors.length} scan warning(s)</summary>
          <div className="body">
            {scanErrors.map((err, i) => (
              <div key={i} style={{ ...captionStyle, padding: '2px 0' }}>{err}</div>
            ))}
          </div>
        </details>
      )}

      {nothingFound ? (
        <div className="note info">No matching files found.</div>
      ) : (
        <>
          <div className="metrics">
            <div className="metric ok">
              <span className="v">{moved.length}</span>
              <span className="k">Moved ✅</span>
            </div>
            <div className={failed.length > 0 ? 'metric bad' : 'metric'}>
              <span className="v">{failed.length}</span>
              <span className="k">Failed ❌</span>
            </div>
          </div>

          {failed.length > 0 && (
            <div className="field">
              <span className="label">⚠️ Failures</span>
              {failed.map((f, i) => (
                <div className="note error" key={i} style={{ overflowWrap: 'anywhere' }}>
                  🔴 {f.name}: {f.error}
                </div>
              ))}
            </div>
          )}

          {warning ? (
            <div className="note warn">{warning}</div>
          ) : (
            <div className="note ok">Done! Moved to: {target}</div>
          )}
        </>
      )}
    </div>
  )
}

export default function FileGatherer() {
  const [source, setSource] = useState('~/Desktop')
  const [target, setTarget] = useState('~/Desktop')
  const [selected, setSelected] = useState({ Video: true })
  const [custom, setCustom] = useState('')
  const { start, snapshot, running, error } = useToolJob('/tools/file-gatherer')

  const categories = CATEGORY_NAMES.filter((c) => selected[c])

  // Live pattern preview, assembled exactly the way the backend will.
  const patterns = useMemo(() => {
    const out = []
    for (const c of categories) out.push(...FILE_TYPE_PRESETS[c])
    for (const token of custom.replace(/,/g, ' ').split(/\s+/)) {
      const p = normalizePattern(token)
      if (p) out.push(p)
    }
    return [...new Set(out)].sort()
  }, [custom, selected]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (name) => setSelected((prev) => ({ ...prev, [name]: !prev[name] }))

  const run = () => start(() => api.gatherStart({ source, target, categories, custom }))

  return (
    <div>
      <div className="page-head"><h1>📦 File Gatherer</h1></div>
      <p className="page-sub">
        Recursively gather files by type from a source folder and move them into
        a single target folder — duplicate names are auto-numbered.
      </p>

      <div className="station">
        <div>
          <div className="panel">
            <div className="step"><span className="n">01</span><span>FOLDERS</span></div>
            <FolderField
              label="Source folder"
              value={source}
              onChange={setSource}
              placeholder="e.g. ~/Movies or /Volumes/T7"
              startDir="~/Desktop"
            />
            <FolderField
              label="Target folder"
              value={target}
              onChange={setTarget}
              placeholder="e.g. ~/Movies or /Volumes/T7"
              startDir="~/Desktop"
            />
          </div>

          <div className="panel">
            <div className="step"><span className="n">02</span><span>FILE TYPES</span></div>
            <div className="field">
              <span className="label" id="gather-cats-label">Categories</span>
              <div className="row" role="group" aria-labelledby="gather-cats-label">
                {CATEGORY_NAMES.map((name) => (
                  <Button
                    key={name}
                    variant={selected[name] ? 'primary' : 'secondary'}
                    aria-pressed={!!selected[name]}
                    onClick={() => toggle(name)}
                    style={{ minHeight: 44 }}
                  >
                    {name}
                  </Button>
                ))}
              </div>
            </div>
            <div className="field">
              <label htmlFor="gather-custom">Custom patterns / extensions (optional)</label>
              <input
                id="gather-custom"
                className="control"
                value={custom}
                onChange={(e) => setCustom(e.target.value)}
                placeholder="srt, *.nfo, report*.pdf"
                spellCheck={false}
              />
              <p style={captionStyle}>
                Comma- or space-separated, e.g. srt, *.nfo, report*.pdf — bare
                extensions become *.ext; tokens with * or ? are kept as globs.
              </p>
            </div>
            {patterns.length > 0 ? (
              <p style={captionStyle}>Matching: {patterns.join(', ')}</p>
            ) : (
              <p style={captionStyle}>Nothing selected — toggle a category or add a pattern.</p>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="step"><span className="n">03</span><span>SCAN &amp; MOVE</span></div>
          <p style={{ ...captionStyle, margin: '0 0 12px' }}>
            Scans the source tree for matching files, then moves every match
            into the target folder. You can cancel mid-run.
          </p>
          <Button
            variant="primary"
            onClick={run}
            disabled={running}
            style={{ minHeight: 44 }}
          >
            🚚 Scan &amp; move
          </Button>
          {error && <div className="note error">{error}</div>}
          <JobPanel snapshot={snapshot}>
            {['done', 'cancelled'].includes(snapshot?.state) && snapshot.result && (
              <GatherResult result={snapshot.result} />
            )}
          </JobPanel>
        </div>
      </div>
    </div>
  )
}
