// Torrent Downloader — add a magnet or .torrent, keep only the files worth
// keeping, and manage the queue across restarts.
// Mirrors backend/src/toolkit_api/routers/torrent.py.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import Button from '../components/Button'
import FolderField from '../components/FolderField'
import {
  ACTIVE_STATES,
  CATEGORIES,
  MB,
  applyFilter,
  formatBytes,
  formatEta,
  formatSpeed,
  hasActiveWork,
} from '../torrent'
import type { TorrentFileRow, TorrentResolve, TorrentRow, TorrentStatus } from '../types/api'

const NO_FILES: TorrentFileRow[] = []
const NO_OVERRIDES: ReadonlyMap<number, boolean> = new Map()

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
    <div className="table" style={{ maxHeight: 320, overflowY: 'auto' }}>
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
  const [magnet, setMagnet] = useState('')
  const [resolved, setResolved] = useState<TorrentResolve | null>(null)
  const [resolving, setResolving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [categories, setCategories] = useState<Set<string>>(new Set(['video']))
  const [minMb, setMinMb] = useState(100)
  // Only the user's explicit ticks, tagged with the rule they were made
  // against. The checked set itself is derived below, so editing the rule
  // cannot leave the table showing a stale selection.
  const [overrides, setOverrides] = useState<{ key: string; map: Map<number, boolean> }>({
    key: '',
    map: new Map(),
  })
  const [saveDir, setSaveDir] = useState('')

  const [rows, setRows] = useState<TorrentRow[]>([])
  const [quitting, setQuitting] = useState(false)
  const [showQuit, setShowQuit] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let cancelled = false
    api
      .torrentStatus()
      .then((s) => {
        if (!cancelled) setStatus(s)
      })
      .catch(() => {
        if (!cancelled) setStatus({ running: false, owned: false, version: null, detail: null })
      })
    return () => {
      cancelled = true
    }
  }, [])

  // The dashboard stream is also the presence signal the backend uses to decide
  // when the last tab is gone, so it stays open for the page's whole lifetime.
  useEffect(() => {
    const source = new EventSource('/api/torrent/events')
    const onFrame = (event: Event) => {
      setRows(JSON.parse((event as MessageEvent).data) as TorrentRow[])
    }
    source.addEventListener('torrents', onFrame)
    return () => source.close()
  }, [])

  // Browsers ignore custom text here and show their own generic dialog;
  // preventDefault is the entire API. The informative confirmation is the
  // Quit modal, which we control.
  const working = hasActiveWork(rows)
  useEffect(() => {
    if (!working) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [working])

  const files = resolved?.files ?? NO_FILES

  // Everything below is derived during render rather than synced in an effect:
  // the rule produces a base selection, and the user's ticks layer over it.
  const ruleKey = useMemo(
    () => JSON.stringify([resolved?.infohash ?? null, [...categories].sort(), minMb]),
    [resolved, categories, minMb],
  )
  // Ticks made against a previous rule are dropped, because the rule is the
  // thing the user just changed.
  const activeOverrides = overrides.key === ruleKey ? overrides.map : NO_OVERRIDES

  const ruleSelected = useMemo(
    () => new Set(applyFilter(files, categories, minMb * MB)),
    [files, categories, minMb],
  )

  const selected = useMemo(() => {
    const out = new Set<number>()
    for (const file of files) {
      if (activeOverrides.get(file.index) ?? ruleSelected.has(file.index)) out.add(file.index)
    }
    return out
  }, [files, ruleSelected, activeOverrides])

  const selectedBytes = useMemo(
    () => files.filter((f) => selected.has(f.index)).reduce((sum, f) => sum + f.size, 0),
    [files, selected],
  )

  const pollUntilReady = useCallback(async (infohash: string) => {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500))
      const next = await api.torrentPollResolve(infohash)
      if (next.state === 'error') {
        setResolving(false)
        setError('Could not fetch metadata — the magnet may be dead or have no seeders.')
        return
      }
      if (next.ready) {
        setResolved(next)
        setResolving(false)
        return
      }
    }
  }, [])

  async function resolveMagnet() {
    setError(null)
    setResolved(null)
    setResolving(true)
    try {
      const out = await api.torrentResolveMagnet(magnet.trim())
      if (out.ready) {
        setResolved(out)
        setResolving(false)
      } else {
        setResolved(out)
        await pollUntilReady(out.infohash)
      }
    } catch (err) {
      setResolving(false)
      setError((err as Error).message || 'Could not read that magnet link.')
    }
  }

  async function resolveFile(file: File) {
    setError(null)
    setResolved(null)
    setResolving(true)
    try {
      setResolved(await api.torrentResolveFile(file))
    } catch (err) {
      setError((err as Error).message || 'Could not read that .torrent file.')
    } finally {
      setResolving(false)
    }
  }

  async function addToQueue() {
    if (!resolved) return
    setError(null)
    try {
      await api.torrentCommit({
        infohash: resolved.infohash,
        selected: [...selected].sort((a, b) => a - b),
        save_dir: saveDir.trim(),
      })
      setResolved(null)
      setMagnet('')
      if (fileInput.current) fileInput.current.value = ''
    } catch (err) {
      setError((err as Error).message || 'Could not start that download.')
    }
  }

  async function confirmQuit() {
    setQuitting(true)
    try {
      await api.torrentShutdown()
      setShowQuit(false)
    } catch (err) {
      setError((err as Error).message || 'Could not stop aria2.')
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

  function toggleFile(index: number) {
    const next = new Map(activeOverrides)
    next.set(index, !selected.has(index))
    setOverrides({ key: ruleKey, map: next })
  }

  const engineDown = status !== null && !status.running

  return (
    <>
      <div className="page-head">
        <h1>🌊 Torrent Downloader</h1>
      </div>
      <p className="page-sub">
        Paste a magnet or pick a .torrent, review what is inside it, and download only the files
        worth keeping.
      </p>

      {engineDown && (
        <div className="note error">
          {status?.detail ?? 'aria2 is not running. Install it with `brew install aria2`.'}
        </div>
      )}

      <div className="station">
        <div className="panel">
          <div className="step">1 · Add a torrent</div>

          <div className="field">
            <label htmlFor="magnet">Magnet link</label>
            <div className="row">
              <input
                id="magnet"
                className="grow"
                value={magnet}
                placeholder="magnet:?xt=urn:btih:…"
                onChange={(e) => setMagnet(e.target.value)}
              />
              <Button
                variant="primary"
                loading={resolving}
                disabled={!magnet.trim() || engineDown}
                onClick={resolveMagnet}
              >
                Resolve
              </Button>
            </div>
          </div>

          <div className="field">
            <label htmlFor="torrentfile">…or a .torrent file</label>
            <input
              id="torrentfile"
              ref={fileInput}
              type="file"
              accept=".torrent,application/x-bittorrent"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) void resolveFile(file)
              }}
            />
          </div>

          {resolving && !resolved?.ready && (
            <div className="note info">
              Fetching metadata from peers… A magnet carries no file list, so this can take a
              minute on a cold swarm.
            </div>
          )}
          {error && <div className="note error">{error}</div>}
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

      {resolved?.ready && (
        <div className="panel">
          <div className="step">3 · Review {resolved.name ?? 'the files'}</div>
          <FileTable files={files} selected={selected} onToggle={toggleFile} />
          <div className="row" style={{ marginTop: 12 }}>
            <span className="grow" style={{ font: '12px var(--mono)', color: 'var(--muted)' }}>
              {selected.size} of {files.length} files · {formatBytes(selectedBytes)}
            </span>
            <Button
              variant="primary"
              disabled={selected.size === 0 || !saveDir.trim()}
              onClick={addToQueue}
            >
              Add to queue
            </Button>
          </div>
          {selected.size === 0 && (
            <div className="note warn">
              Select at least one file — a torrent with everything deselected finishes instantly
              having downloaded nothing.
            </div>
          )}
          {!saveDir.trim() && selected.size > 0 && (
            <div className="note info">Choose a destination folder above.</div>
          )}
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
                <span style={{ font: '12px var(--mono)', color: 'var(--faint)' }}>
                  {row.state}
                </span>
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
