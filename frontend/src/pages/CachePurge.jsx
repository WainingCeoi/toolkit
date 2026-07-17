// Cache Purge — scan a folder for cache/junk files by pattern, preview the
// exact hit list, then permanently delete it. Scan is synchronous; delete
// runs as a tracked job. Results are keyed to the folder they were scanned
// from: edit the folder field and the stale preview disappears.

import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'

const DEFAULT_PATTERNS = '*.dwl *.dwl2 *.bak *.log *.db *.tmp *.err'
const PREVIEW_LIMIT = 200

// Mirror of the engine's normalize_pattern — used only for the live
// "Matching:" caption; the backend re-parses authoritatively on scan.
function normalizeToken(raw) {
  const token = raw.trim()
  if (!token || ['*', '*.*', '**', '*.', '.*', '?'].includes(token)) return null
  if (token.includes('*') || token.includes('?')) return token
  return `*.${token.replace(/^\.+/, '')}`
}

function livePatterns(raw) {
  const patterns = new Set()
  for (const token of raw.replace(/,/g, ' ').split(/\s+/)) {
    if (!token) continue
    const pattern = normalizeToken(token)
    if (pattern) patterns.add(pattern)
  }
  return [...patterns].sort()
}

const basename = (path) => path.slice(path.lastIndexOf('/') + 1)

// Subfolder of `path`'s parent relative to the scanned root. The root is the
// raw field value at scan time — possibly `~/…`, which the backend expanded
// before building the absolute paths — so for tilde roots we locate the
// post-tilde tail inside the parent path; if that fails, show the full parent.
function subfolderOf(path, rawRoot) {
  const cut = path.lastIndexOf('/')
  const parent = cut > 0 ? path.slice(0, cut) : '/'
  let root = rawRoot.trim().replace(/\/+$/, '')
  if (root.startsWith('~')) {
    const tail = root.slice(1)
    if (!tail) return parent
    const i = parent.indexOf(`${tail}/`)
    if (i >= 0) root = parent.slice(0, i + tail.length)
    else if (parent.endsWith(tail)) root = parent
    else return parent
  }
  if (parent === root) return '.'
  if (parent.startsWith(`${root}/`)) return parent.slice(root.length + 1)
  return parent
}

const caption = { font: '11px var(--mono)', color: 'var(--faint)', margin: '6px 0 0' }

export default function CachePurge() {
  const [folder, setFolder] = useState('~/Desktop')
  const [patternsRaw, setPatternsRaw] = useState(DEFAULT_PATTERNS)
  const [scan, setScan] = useState(null) // {folder, files, errors, total_bytes, rejected_tokens}
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState(null)
  const [confirm, setConfirm] = useState(false)

  const { start, snapshot, running, error, setError } = useToolJob('/tools/cache-purge')

  // Results stay visible only while the folder field still matches the folder
  // they came from — the old page's staleness guard, done with state.
  const current = scan !== null && scan.folder === folder

  const matching = livePatterns(patternsRaw)

  async function runScan() {
    setScanning(true)
    setScanError(null)
    setConfirm(false)
    try {
      const result = await api.purgeScan(folder, patternsRaw)
      setScan({ folder, ...result })
    } catch (err) {
      setScan(null)
      setScanError(err.message)
    } finally {
      setScanning(false)
    }
  }

  function runDelete() {
    setError(null)
    start(() => api.purgeDelete(scan.files))
  }

  // Once a delete job finishes, the previewed list no longer exists on disk —
  // clear the scan so the stale table can't be deleted twice.
  const clearedFor = useRef(null)
  useEffect(() => {
    if (snapshot?.state === 'done' && clearedFor.current !== snapshot.id) {
      clearedFor.current = snapshot.id
      setScan(null)
      setConfirm(false)
    }
  }, [snapshot])

  const result = snapshot?.state === 'done' ? snapshot.result : null
  const preview = current ? scan.files.slice(0, PREVIEW_LIMIT) : []

  return (
    <div>
      <div className="page-head"><h1>🧹 Cache Purge</h1></div>
      <p className="page-sub">
        Recursively find and delete cache / junk files (logs, backups, temp
        files) from a folder. Scan to preview first — deletion is permanent.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Folder &amp; patterns</span></div>

          <FolderField
            label="Folder to clean"
            value={folder}
            onChange={setFolder}
            placeholder="/Users/you/Library/Caches"
            startDir={folder}
          />

          <div className="field">
            <label htmlFor="purge-patterns">Cache extensions / patterns</label>
            <input
              id="purge-patterns"
              className="control"
              value={patternsRaw}
              onChange={(e) => setPatternsRaw(e.target.value)}
              placeholder={DEFAULT_PATTERNS}
              spellCheck={false}
            />
            <p style={caption}>Space- or comma-separated globs, e.g. *.bak *.log tmp</p>
            {matching.length > 0 && <p style={caption}>Matching: {matching.join(', ')}</p>}
          </div>

          <button
            type="button"
            className="btn primary"
            onClick={runScan}
            disabled={scanning || running}
          >
            {scanning ? 'Scanning…' : '🔍 Scan folder'}
          </button>

          {scanError && <div className="note error">{scanError}</div>}

          {current && scan.rejected_tokens.length > 0 && (
            <div className="note warn">
              Ignored catch-all pattern(s) that would match every file:{' '}
              {scan.rejected_tokens.join(', ')}
            </div>
          )}
        </div>

        <div className="panel">
          <div className="step"><span className="n">02</span><span>Delete</span></div>

          {!current && !snapshot && !scanError && (
            <div className="note info">
              Scan a folder to preview what would be deleted.
            </div>
          )}

          {current && scan.files.length === 0 && (
            <div className="note info">No matching cache files found. ✨</div>
          )}

          {current && scan.files.length > 0 && (
            <>
              <div className="note warn">
                Found {scan.files.length} file(s) ·{' '}
                ~{(scan.total_bytes / 1048576).toFixed(1)} MB — deletion cannot
                be undone.
              </div>

              {scan.errors.length > 0 && (
                <details className="expander">
                  <summary>⚠️ Scan warnings ({scan.errors.length})</summary>
                  <div className="body">
                    <CodeBox text={scan.errors.join('\n')} />
                  </div>
                </details>
              )}

              <div style={{ overflowX: 'auto', maxHeight: 320, overflowY: 'auto' }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">File</th>
                      <th scope="col">Subfolder</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.map((f) => (
                      <tr key={f}>
                        <td>{basename(f)}</td>
                        <td>{subfolderOf(f, scan.folder)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {scan.files.length > preview.length && (
                <p style={caption}>
                  Showing first {preview.length} of {scan.files.length}.
                </p>
              )}

              <label className="check" style={{ margin: '12px 0 10px' }}>
                <input
                  type="checkbox"
                  checked={confirm}
                  onChange={(e) => setConfirm(e.target.checked)}
                />
                I understand this permanently deletes the files listed above.
              </label>

              <button
                type="button"
                className="btn danger"
                onClick={runDelete}
                disabled={!confirm || running}
              >
                🗑️ Delete {scan.files.length} file(s)
              </button>
            </>
          )}

          {error && <div className="note error">{error}</div>}

          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                <div className="note ok">Deleted {result.deleted.length} file(s).</div>
                {result.failed.length > 0 && (
                  <details className="expander">
                    <summary>❌ {result.failed.length} failed</summary>
                    <div className="body">
                      <CodeBox
                        text={result.failed.map((f) => `${f.name}: ${f.error}`).join('\n')}
                      />
                    </div>
                  </details>
                )}
              </>
            )}
          </JobPanel>
        </div>
      </div>
    </div>
  )
}
