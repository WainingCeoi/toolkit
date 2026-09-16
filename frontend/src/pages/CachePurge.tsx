import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'
import type { PurgeResult, PurgeScanResult } from '../types/api'

type ScanState = PurgeScanResult & { folder: string }

const DEFAULT_PATTERNS = '*.dwl *.dwl2 *.bak *.log *.db *.tmp *.err'
const PREVIEW_LIMIT = 200

// Mirrors the backend's normalize_pattern; keep them in sync.
const GLOB_CHARS = /[*?[\]{}]/
// Whole bracket expression: it matches one arbitrary char, so `*[!/]*` is a catch-all.
const BRACKET = /\[!?\]?[^\]]*\]/g
const EDGES = /^[*?.{},]+|[*?.{},]+$/g

function normalizeToken(raw: string): string | null {
  const token = raw.trim()
  if (!token) return null
  if (!GLOB_CHARS.test(token)) return `*.${token.replace(/^\.+/, '')}`
  return token.replace(BRACKET, '').replace(EDGES, '') ? token : null
}

function livePatterns(raw: string): string[] {
  const patterns = new Set<string>()
  for (const token of raw.replace(/,/g, ' ').split(/\s+/)) {
    if (!token) continue
    const pattern = normalizeToken(token)
    if (pattern) patterns.add(pattern)
  }
  return [...patterns].sort()
}

const basename = (path: string): string => path.slice(path.lastIndexOf('/') + 1)

// rawRoot may still be `~/…` while the paths are absolute, hence the tilde tail search.
function subfolderOf(path: string, rawRoot: string): string {
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

const caption: CSSProperties = { font: '11px var(--mono)', color: 'var(--faint)', margin: '6px 0 0' }

export default function CachePurge() {
  const [folder, setFolder] = useState('~/Desktop')
  const [patternsRaw, setPatternsRaw] = useState(DEFAULT_PATTERNS)
  const [scan, setScan] = useState<ScanState | null>(null)
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)

  const { start, snapshot, running, error, setError } = useToolJob<PurgeResult>('/tools/cache-purge')

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
      setScanError((err as Error).message)
    } finally {
      setScanning(false)
    }
  }

  async function runDelete() {
    if (!scan) return
    setError(null)
    const started = await start(() => api.purgeDelete(scan.scan_id))
    if (started === null) {
      // null means the scan expired server-side, so the preview no longer describes the disk.
      setScan(null)
      setConfirm(false)
    }
  }

  // Any terminal state, cancelled included, has changed the disk, so the preview must go.
  const clearedFor = useRef<string | null>(null)
  useEffect(() => {
    if (!snapshot) return
    const terminal =
      snapshot.state === 'done' || snapshot.state === 'cancelled' || snapshot.state === 'failed'
    if (terminal && clearedFor.current !== snapshot.id) {
      clearedFor.current = snapshot.id
      setScan(null)
      setConfirm(false)
    }
  }, [snapshot])

  // A cancelled run still reports what it deleted.
  const result =
    snapshot && (snapshot.state === 'done' || snapshot.state === 'cancelled')
      ? snapshot.result
      : null
  const preview = current && scan ? scan.files.slice(0, PREVIEW_LIMIT) : []

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

          <Button
            variant="primary"
            onClick={runScan}
            loading={scanning}
            disabled={running}
          >
            🔍 Scan folder
          </Button>

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

              <Button
                variant="danger"
                onClick={runDelete}
                disabled={!confirm || running}
              >
                🗑️ Delete {scan.files.length} file(s)
              </Button>
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
