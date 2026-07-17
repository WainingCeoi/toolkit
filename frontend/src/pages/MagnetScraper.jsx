// 🧲 Magnet Scraper — auto/manual magnet scraping and de-duplication.
// Automatic walks the configured site's pagination until CUTOFF_VIDEO is
// found (then advances it); Manual scrapes a pasted URL list; Remove
// duplicated de-dupes a magnet list locally (sync, no job).

import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'

const MODES = [
  { key: 'auto', label: 'Automatic' },
  { key: 'manual', label: 'Manual' },
  { key: 'cleanup', label: 'Remove duplicated' },
]

const BIG_TAP = { minHeight: 44 }

function splitLines(raw) {
  return raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
}

// Shared result block for auto + manual scrapes (snapshot.result).
function ScrapeResult({ result }) {
  if (!result) return null

  // Automatic mode only: pagination never located the cutoff video.
  if (result.cutoff_found === false) {
    return (
      <>
        {result.error && <div className="note error">{result.error}</div>}
        <div className="note warn">{result.warning}</div>
      </>
    )
  }

  if (!result.urls || result.urls.length === 0) {
    return <div className="note info">No new unwatched video found.</div>
  }

  const successful = result.successful || []
  const failed = result.failed || []
  return (
    <>
      <div className="note ok">🧲 {successful.length} magnet(s) found.</div>
      <div className="metrics">
        <div className="metric">
          <div className="v">{result.urls.length}</div>
          <div className="k">Total found</div>
        </div>
        <div className="metric ok">
          <div className="v">{successful.length}</div>
          <div className="k">Successful ✅</div>
        </div>
        <div className={failed.length ? 'metric bad' : 'metric'}>
          <div className="v">{failed.length}</div>
          <div className="k">Failed ❌</div>
        </div>
      </div>
      {successful.length > 0 && (
        <div className="field">
          <span className="label">🚀 Grabbed magnets</span>
          <CodeBox text={successful.map((s) => s.result).join('\n')} />
        </div>
      )}
      {failed.length > 0 && (
        <div className="field">
          <span className="label">⚠️ Failed URLs — with reasons</span>
          <CodeBox text={failed.map((f) => `${f.url} — ${f.reason}`).join('\n')} />
        </div>
      )}
    </>
  )
}

export default function MagnetScraper() {
  const [mode, setMode] = useState('auto')

  // Automatic mode
  const [startPage, setStartPage] = useState('1')
  const [config, setConfig] = useState(null) // {website_url_set, cutoff_set}
  const [configError, setConfigError] = useState(null)

  // Manual mode
  const [manualRaw, setManualRaw] = useState('')

  // Remove duplicated (sync call — result persists while switching modes)
  const [dedupeRaw, setDedupeRaw] = useState('')
  const [dedupeResult, setDedupeResult] = useState(null) // {unique, count}
  const [dedupeError, setDedupeError] = useState(null)
  const [dedupeBusy, setDedupeBusy] = useState(false)

  const { start, snapshot, running, error, setError } = useToolJob('/tools/magnet-scraper')

  // Config lamps load independently of any job.
  useEffect(() => {
    api
      .magnetConfig()
      .then(setConfig)
      .catch((e) => setConfigError(e.message))
  }, [])

  function startAuto() {
    const page = Number(startPage)
    if (!Number.isInteger(page) || page < 1) {
      setError('Start page must be a whole number of 1 or more.')
      return
    }
    start(() => api.magnetAuto(page))
  }

  function startManual() {
    // An empty list is allowed through: the backend answers with its exact
    // "Please enter at least one URL" message.
    start(() => api.magnetManual(splitLines(manualRaw)))
  }

  async function runDedupe() {
    setDedupeBusy(true)
    setDedupeError(null)
    try {
      setDedupeResult(await api.magnetDedupe(splitLines(dedupeRaw)))
    } catch (e) {
      setDedupeResult(null)
      setDedupeError(e.message)
    } finally {
      setDedupeBusy(false)
    }
  }

  const missingConfig = config && (!config.website_url_set || !config.cutoff_set)

  return (
    <div>
      <div className="page-head">
        <h1>🧲 Magnet Scraper</h1>
      </div>
      <p className="page-sub">
        Scrape your unwatched video links automatically or manually — or paste a magnet
        list and strip the duplicates.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step">
            <span>Mode &amp; input</span>
          </div>

          <div className="field">
            <span className="label" id="magnet-mode-label">
              Mode
            </span>
            <div className="row" role="group" aria-labelledby="magnet-mode-label">
              {MODES.map((m) => (
                <button
                  key={m.key}
                  type="button"
                  className={mode === m.key ? 'btn primary' : 'btn'}
                  aria-pressed={mode === m.key}
                  style={{ ...BIG_TAP, flex: '1 1 auto' }}
                  onClick={() => setMode(m.key)}
                >
                  {m.label}
                </button>
              ))}
            </div>
          </div>

          {mode === 'auto' && (
            <>
              <div className="note info">
                Walks the site's pages from the start page until CUTOFF_VIDEO turns up
                (100-page cap), advances the cutoff in backend/.env, then grabs every
                newer magnet.
              </div>
              <div className="field">
                <label htmlFor="magnet-start-page">Start page</label>
                <input
                  id="magnet-start-page"
                  type="number"
                  min={1}
                  step={1}
                  className="control"
                  value={startPage}
                  onChange={(e) => setStartPage(e.target.value)}
                />
              </div>
              <div className="field">
                <span className="label">Config — backend/.env</span>
                {configError ? (
                  <div className="note warn" style={{ margin: 0 }}>
                    Could not read config: {configError}
                  </div>
                ) : config ? (
                  <div className="healthline" style={{ margin: 0 }}>
                    <span className={config.website_url_set ? 'lamp' : 'lamp off'}>
                      <i />
                      WEBSITE_URL {config.website_url_set ? 'set' : 'missing'}
                    </span>
                    <span className={config.cutoff_set ? 'lamp' : 'lamp off'}>
                      <i />
                      CUTOFF_VIDEO {config.cutoff_set ? 'set' : 'missing'}
                    </span>
                  </div>
                ) : (
                  <div className="healthline" style={{ margin: 0 }}>
                    checking…
                  </div>
                )}
              </div>
              {missingConfig && (
                <div className="note warn">
                  Automatic mode needs both keys — set the missing one(s) in
                  backend/.env before scraping.
                </div>
              )}
              <button
                type="button"
                className="btn primary"
                style={BIG_TAP}
                disabled={running}
                onClick={startAuto}
              >
                🚀 Start automatic scrape
              </button>
            </>
          )}

          {mode === 'manual' && (
            <>
              <div className="field">
                <label htmlFor="magnet-manual-urls">
                  Paste your non-fetched video URLs here (one per line)
                </label>
                <textarea
                  id="magnet-manual-urls"
                  className="control"
                  rows={8}
                  value={manualRaw}
                  onChange={(e) => setManualRaw(e.target.value)}
                  placeholder={'https://…\nhttps://…'}
                />
              </div>
              <button
                type="button"
                className="btn primary"
                style={BIG_TAP}
                disabled={running}
                onClick={startManual}
              >
                Process manual links
              </button>
            </>
          )}

          {mode === 'cleanup' && (
            <>
              <div className="field">
                <label htmlFor="magnet-dedupe-links">
                  Paste all your magnet links here (one per line)
                </label>
                <textarea
                  id="magnet-dedupe-links"
                  className="control"
                  rows={8}
                  value={dedupeRaw}
                  onChange={(e) => setDedupeRaw(e.target.value)}
                  placeholder={'magnet:?xt=…\nmagnet:?xt=…'}
                />
              </div>
              <button
                type="button"
                className="btn primary"
                style={BIG_TAP}
                disabled={dedupeBusy}
                onClick={runDedupe}
              >
                {dedupeBusy ? 'Removing…' : 'Remove duplicated'}
              </button>
            </>
          )}
        </div>

        <div className="panel">
          <div className="step">
            <span>Results</span>
          </div>

          {mode === 'cleanup' ? (
            <>
              {dedupeError && <div className="note error">{dedupeError}</div>}
              {dedupeResult ? (
                <>
                  <div className="note ok">Found {dedupeResult.count} unique links</div>
                  <CodeBox text={dedupeResult.unique.join('\n')} />
                </>
              ) : (
                !dedupeError && (
                  <div className="note info">
                    Unique links land here — duplicates dropped, first-seen order kept.
                  </div>
                )
              )}
            </>
          ) : (
            <>
              {error && <div className="note error">{error}</div>}
              {snapshot ? (
                <JobPanel snapshot={snapshot}>
                  {snapshot.state === 'done' && <ScrapeResult result={snapshot.result} />}
                </JobPanel>
              ) : (
                !error && <div className="note info">No scrape yet — magnets land here.</div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
