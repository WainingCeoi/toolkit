// Torrent Downloader — add magnets or .torrent files, keep only the files
// worth keeping, and manage the queue across restarts.
// Mirrors backend/src/toolkit_api/routers/torrent.py.

import { useEffect, useState } from 'react'
import { api } from '../api'
import Button from '../components/Button'
import FileDrop from '../components/FileDrop'
import FolderField from '../components/FolderField'
import {
  ACTIVE_STATES,
  CATEGORIES,
  DEFAULT_SAVE_DIR,
  MB,
  addTorrent,
  formatBytes,
  formatEta,
  formatSpeed,
  hasActiveWork,
  parseMagnetLines,
  ruleKey,
  selectionFor,
  updateTorrent,
} from '../torrent'
import type { TorrentFileRow, TorrentResolve, TorrentRow, TorrentStatus } from '../types/api'

const NO_OVERRIDES: ReadonlyMap<number, boolean> = new Map()
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const errMsg = (e: unknown, fallback: string) => (e as Error).message || fallback

// A magnet is too long to show whole in an error line; its btih is enough.
function magnetLabel(uri: string): string {
  return uri.match(/btih:([a-z0-9]+)/i)?.[1]?.slice(0, 12) ?? uri.slice(0, 24)
}

function FileTable({
  files,
  selected,
  onToggle,
}: {
  files: TorrentFileRow[]
  selected: Set<number>
  onToggle: (index: number) => void
}) {
  return (
    <div className="table" style={{ maxHeight: 280, overflowY: 'auto' }}>
      {files.map((file) => (
        <label
          key={file.index}
          className="row"
          style={{ padding: '4px 0', cursor: 'pointer', flexWrap: 'nowrap' }}
        >
          <input
            type="checkbox"
            checked={selected.has(file.index)}
            onChange={() => onToggle(file.index)}
            style={{ accentColor: 'var(--amber)' }}
          />
          <span className="grow" style={{ overflowWrap: 'anywhere', fontSize: 13 }}>
            {file.path}
          </span>
          <span style={{ font: '12px var(--mono)', color: 'var(--faint)' }}>{file.category}</span>
          <span
            style={{ font: '12px var(--mono)', color: 'var(--muted)', minWidth: 72, textAlign: 'right' }}
          >
            {formatBytes(file.size)}
          </span>
        </label>
      ))}
    </div>
  )
}

function QuitModal({
  rows,
  onCancel,
  onConfirm,
  busy,
}: {
  rows: TorrentRow[]
  onCancel: () => void
  onConfirm: () => void
  busy: boolean
}) {
  const active = rows.filter((r) => ACTIVE_STATES.has(r.state))
  return (
    <>
      <button className="scrim" onClick={onCancel} aria-label="Cancel" />
      <div
        className="panel"
        role="dialog"
        aria-modal="true"
        style={{
          position: 'fixed',
          zIndex: 31,
          top: '50%',
          left: '50%',
          transform: 'translate(-50%, -50%)',
          maxWidth: 460,
          width: 'calc(100% - 32px)',
        }}
      >
        <div className="step">Stop downloading</div>
        <p style={{ fontSize: 13.5 }}>
          {active.length} torrent{active.length === 1 ? '' : 's'} still downloading. Quitting
          pauses {active.length === 1 ? 'it' : 'them'} and stops aria2 — progress is kept, and
          {active.length === 1 ? ' it resumes' : ' they resume'} next time you open this page.
        </p>
        <ul style={{ fontSize: 13, color: 'var(--muted)', paddingLeft: 18, margin: '0 0 12px' }}>
          {active.map((r) => (
            <li key={r.infohash} style={{ overflowWrap: 'anywhere' }}>
              {r.name ?? r.infohash.slice(0, 12)} — {r.progress.toFixed(0)}%
            </li>
          ))}
        </ul>
        <div className="row">
          <Button onClick={onCancel}>Keep downloading</Button>
          <Button variant="danger" loading={busy} onClick={onConfirm}>
            Pause &amp; quit
          </Button>
        </div>
      </div>
    </>
  )
}

export default function TorrentDownloader() {
  const [status, setStatus] = useState<TorrentStatus | null>(null)

  // --- inputs (step 1) ---
  const [magnets, setMagnets] = useState('')
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [staging, setStaging] = useState(false)

  // --- shared filter + destination (step 2) ---
  const [categories, setCategories] = useState<Set<string>>(new Set(['video']))
  const [minMb, setMinMb] = useState(100)
  // Mirrors DEFAULT_SAVE_DIR in backend/src/toolkit_api/torrents.py. Prefilled
  // so downloads land in ~/Downloads with no extra click; the backend expands
  // the tilde. Browsing swaps in an absolute path.
  const [saveDir, setSaveDir] = useState(DEFAULT_SAVE_DIR)

  // --- resolved torrents under review (step 3) ---
  const [resolved, setResolved] = useState<TorrentResolve[]>([])
  const [resolvingHashes, setResolvingHashes] = useState<Set<string>>(new Set())
  const [errors, setErrors] = useState<{ id: string; msg: string }[]>([])
  // Per-torrent file ticks, keyed by infohash so two torrents' index-1 files
  // never collide, then by the rule they were made against so a filter change
  // discards them.
  const [overrides, setOverrides] = useState<
    Map<string, { key: string; map: Map<number, boolean> }>
  >(new Map())

  // --- queue (dashboard) ---
  const [rows, setRows] = useState<TorrentRow[]>([])
  const [quitting, setQuitting] = useState(false)
  const [showQuit, setShowQuit] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .torrentStatus()
      .then((s) => !cancelled && setStatus(s))
      .catch(() => !cancelled && setStatus({ running: false, owned: false, version: null, detail: null }))
    return () => {
      cancelled = true
    }
  }, [])

  // The dashboard stream doubles as the presence signal the backend uses to
  // decide when the last tab is gone, so it stays open for the page's lifetime.
  useEffect(() => {
    const source = new EventSource('/api/torrent/events')
    source.addEventListener('torrents', (event) => {
      setRows(JSON.parse((event as MessageEvent).data) as TorrentRow[])
    })
    return () => source.close()
  }, [])

  // Browsers ignore custom text here and show their own generic dialog;
  // preventDefault is the entire API. The informative confirmation is the Quit
  // modal, which we control.
  const working = hasActiveWork(rows)
  useEffect(() => {
    if (!working) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [working])

  function pushError(id: string, msg: string) {
    setErrors((prev) => [...prev, { id, msg }])
  }

  function clearResolving(infohash: string) {
    setResolvingHashes((prev) => {
      const next = new Set(prev)
      next.delete(infohash)
      return next
    })
  }

  // Selection for one torrent: shared rule + that torrent's own live ticks.
  function selectedFor(t: TorrentResolve): Set<number> {
    const entry = overrides.get(t.infohash)
    const active =
      entry && entry.key === ruleKey(t.infohash, categories, minMb) ? entry.map : NO_OVERRIDES
    return selectionFor(t, categories, minMb * MB, active)
  }

  async function pollUntilReady(infohash: string) {
    for (;;) {
      await sleep(1500)
      let next: TorrentResolve
      try {
        next = await api.torrentPollResolve(infohash)
      } catch (e) {
        clearResolving(infohash)
        pushError(infohash.slice(0, 12), errMsg(e, 'Could not reach the daemon.'))
        return
      }
      if (next.state === 'error') {
        clearResolving(infohash)
        pushError(next.name ?? infohash.slice(0, 12), 'Metadata fetch failed — dead magnet or no seeders.')
        return
      }
      if (next.ready) {
        setResolved((prev) => updateTorrent(prev, next))
        clearResolving(infohash)
        return
      }
    }
  }

  async function stageMagnet(uri: string) {
    try {
      const out = await api.torrentResolveMagnet(uri)
      setResolved((prev) => addTorrent(prev, out))
      if (!out.ready) {
        setResolvingHashes((prev) => new Set(prev).add(out.infohash))
        void pollUntilReady(out.infohash) // background; don't block the others
      }
    } catch (e) {
      pushError(magnetLabel(uri), errMsg(e, 'Could not read that magnet link.'))
    }
  }

  async function stageFile(file: File) {
    try {
      const out = await api.torrentResolveFile(file)
      setResolved((prev) => addTorrent(prev, out))
    } catch (e) {
      pushError(file.name, errMsg(e, 'Could not read that .torrent file.'))
    }
  }

  async function resolveAll() {
    const lines = parseMagnetLines(magnets)
    const files = pendingFiles
    if (!lines.length && !files.length) return
    setErrors([])
    setStaging(true)
    setMagnets('')
    setPendingFiles([])
    // Every line and file resolves on its own; one bad magnet does not sink the
    // rest (allSettled, never all).
    await Promise.allSettled([...lines.map(stageMagnet), ...files.map(stageFile)])
    setStaging(false)
  }

  async function addOne(t: TorrentResolve) {
    const selected = selectedFor(t)
    if (selected.size === 0 || !saveDir.trim()) return
    try {
      await api.torrentCommit({
        infohash: t.infohash,
        selected: [...selected].sort((a, b) => a - b),
        save_dir: saveDir.trim(),
      })
      setResolved((prev) => prev.filter((x) => x.infohash !== t.infohash))
      setOverrides((prev) => {
        const next = new Map(prev)
        next.delete(t.infohash)
        return next
      })
    } catch (e) {
      pushError(t.name ?? t.infohash.slice(0, 12), errMsg(e, 'Could not start that download.'))
    }
  }

  async function addAll() {
    for (const t of resolved) {
      if (t.ready && selectedFor(t).size > 0) await addOne(t)
    }
  }

  async function confirmQuit() {
    setQuitting(true)
    try {
      await api.torrentShutdown()
      setShowQuit(false)
    } catch (e) {
      pushError('shutdown', errMsg(e, 'Could not stop aria2.'))
    } finally {
      setQuitting(false)
    }
  }

  function toggleCategory(key: string) {
    setCategories((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function toggleFile(t: TorrentResolve, index: number) {
    const key = ruleKey(t.infohash, categories, minMb)
    const current = selectedFor(t)
    setOverrides((prev) => {
      const entry = prev.get(t.infohash)
      const map = new Map(entry && entry.key === key ? entry.map : [])
      map.set(index, !current.has(index))
      const next = new Map(prev)
      next.set(t.infohash, { key, map })
      return next
    })
  }

  const engineDown = status !== null && !status.running
  const nothingToResolve = parseMagnetLines(magnets).length === 0 && pendingFiles.length === 0
  const readyCount = resolved.filter((t) => t.ready && selectedFor(t).size > 0).length

  return (
    <>
      <div className="page-head">
        <h1>🌊 Torrent Downloader</h1>
      </div>
      <p className="page-sub">
        Paste magnets or pick .torrent files, review what is inside them, and download only the
        files worth keeping.
      </p>

      {engineDown && (
        <div className="note error">
          {status?.detail ?? 'aria2 is not running. Install it with `brew install aria2`.'}
        </div>
      )}

      <div className="station">
        <div className="panel">
          <div className="step">1 · Add torrents</div>

          <div className="field">
            <label htmlFor="magnets">Magnet links</label>
            <textarea
              id="magnets"
              className="control"
              rows={4}
              value={magnets}
              placeholder={'magnet:?xt=urn:btih:…\none per line'}
              onChange={(e) => setMagnets(e.target.value)}
            />
          </div>

          <div className="field">
            <label>…or .torrent files</label>
            <FileDrop
              accept=".torrent,application/x-bittorrent"
              files={pendingFiles}
              onChange={setPendingFiles}
              hint="Drop .torrent files here or click to choose"
            />
          </div>

          <div className="row">
            <Button
              variant="primary"
              loading={staging}
              disabled={nothingToResolve || engineDown}
              onClick={resolveAll}
            >
              Resolve
            </Button>
            {resolvingHashes.size > 0 && (
              <span className="label" style={{ margin: 0 }}>
                fetching metadata for {resolvingHashes.size} magnet
                {resolvingHashes.size === 1 ? '' : 's'}…
              </span>
            )}
          </div>

          {errors.length > 0 && (
            <div style={{ marginTop: 10 }}>
              {errors.map((e, i) => (
                <div key={`${e.id}-${i}`} className="note error">
                  {e.id}: {e.msg}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="step">2 · Choose what to download</div>

          <div className="field">
            <label>File types</label>
            <div className="row">
              {CATEGORIES.map((category) => (
                <label key={category.key} className="check">
                  <input
                    type="checkbox"
                    checked={categories.has(category.key)}
                    onChange={() => toggleCategory(category.key)}
                  />
                  {category.label}
                </label>
              ))}
            </div>
          </div>

          <div className="field">
            <label htmlFor="minmb">Minimum size</label>
            <div className="row">
              <input
                id="minmb"
                type="number"
                min={0}
                className="control"
                value={minMb}
                onChange={(e) => setMinMb(Math.max(0, Number(e.target.value) || 0))}
                style={{ width: 110 }}
              />
              <span style={{ color: 'var(--muted)', fontSize: 13 }}>MB</span>
            </div>
            <p style={{ font: '12px var(--mono)', color: 'var(--faint)', margin: '4px 0 0' }}>
              Applies to video and audio only, so subtitles and small extras are never filtered
              out by size.
            </p>
          </div>

          <FolderField label="Save to" value={saveDir} onChange={setSaveDir} />
        </div>
      </div>

      {resolved.length > 0 && (
        <div className="panel">
          <div className="row" style={{ marginBottom: 4 }}>
            <div className="step grow" style={{ margin: 0 }}>
              3 · Review ({resolved.length})
            </div>
            <Button
              variant="primary"
              size="sm"
              disabled={readyCount === 0 || !saveDir.trim()}
              onClick={addAll}
            >
              Add all to queue
            </Button>
          </div>

          {resolved.map((t, i) => {
            const selected = selectedFor(t)
            const bytes = t.files
              .filter((f) => selected.has(f.index))
              .reduce((sum, f) => sum + f.size, 0)
            const fetching = resolvingHashes.has(t.infohash)
            return (
              <div
                key={t.infohash}
                style={{
                  padding: '12px 0',
                  borderTop: i === 0 ? undefined : '1px solid var(--edge)',
                }}
              >
                <div className="row">
                  <strong className="grow" style={{ overflowWrap: 'anywhere', fontSize: 13.5 }}>
                    {t.name ?? t.infohash.slice(0, 16)}
                  </strong>
                  {fetching ? (
                    <span className="label" style={{ margin: 0 }}>
                      fetching metadata…
                    </span>
                  ) : (
                    <>
                      <span style={{ font: '12px var(--mono)', color: 'var(--muted)' }}>
                        {selected.size} of {t.files.length} · {formatBytes(bytes)}
                      </span>
                      <Button
                        variant="primary"
                        size="sm"
                        disabled={selected.size === 0 || !saveDir.trim()}
                        onClick={() => void addOne(t)}
                      >
                        Add
                      </Button>
                    </>
                  )}
                </div>
                {t.ready && (
                  <div style={{ marginTop: 8 }}>
                    <FileTable
                      files={t.files}
                      selected={selected}
                      onToggle={(index) => toggleFile(t, index)}
                    />
                    {selected.size === 0 && (
                      <div className="note warn">
                        Select at least one file — a torrent with everything deselected finishes
                        instantly having downloaded nothing.
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}

          {!saveDir.trim() && <div className="note info">Choose a destination folder above.</div>}
        </div>
      )}

      <div className="panel">
        <div className="row">
          <div className="step grow">Queue</div>
          {working && (
            <Button size="sm" variant="danger" onClick={() => setShowQuit(true)}>
              Pause &amp; quit
            </Button>
          )}
        </div>

        {rows.length === 0 ? (
          <div className="note info">Nothing queued yet.</div>
        ) : (
          rows.map((row) => (
            <div key={row.infohash} style={{ padding: '10px 0' }}>
              <div className="row">
                <span className="grow" style={{ overflowWrap: 'anywhere', fontSize: 13.5 }}>
                  {row.name ?? row.infohash.slice(0, 16)}
                </span>
                <span style={{ font: '12px var(--mono)', color: 'var(--faint)' }}>{row.state}</span>
                {row.state === 'active' ? (
                  <Button size="sm" onClick={() => void api.torrentPause(row.infohash)}>
                    Pause
                  </Button>
                ) : (
                  row.state === 'paused' && (
                    <Button size="sm" onClick={() => void api.torrentResume(row.infohash)}>
                      Resume
                    </Button>
                  )
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    const alsoFiles = window.confirm(
                      'Delete the downloaded files too?\n\nOK removes the files, Cancel keeps them.',
                    )
                    void api.torrentRemove(row.infohash, alsoFiles)
                  }}
                >
                  Remove
                </Button>
              </div>
              <div className="led-track" style={{ margin: '6px 0 4px' }}>
                <div className="led-fill" style={{ width: `${Math.min(100, row.progress)}%` }} />
              </div>
              <div style={{ font: '12px var(--mono)', color: 'var(--muted)' }}>
                {formatBytes(row.completed_bytes)} / {formatBytes(row.total_bytes)} ·{' '}
                {formatSpeed(row.speed)} · ETA {formatEta(row.eta_seconds)}
              </div>
              {row.last_error && <div className="note error">{row.last_error}</div>}
            </div>
          ))
        )}
      </div>

      {showQuit && (
        <QuitModal
          rows={rows}
          busy={quitting}
          onCancel={() => setShowQuit(false)}
          onConfirm={confirmQuit}
        />
      )}
    </>
  )
}
