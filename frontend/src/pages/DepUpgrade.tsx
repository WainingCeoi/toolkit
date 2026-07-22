// Dependency Upgrader — scan a project (incl. subfolders) for uv (pyproject.toml)
// and npm (package.json) manifests, review the outdated deps per manifest, then
// upgrade + commit each. Scan runs as a tracked job (the syncs are slow +
// cancellable); apply is a quick synchronous write, gated on your review.

import { useState, type CSSProperties } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import Button from '../components/Button'
import type { Bump, DepApplyResult, DepScanResult } from '../types/api'

// Mirrors depsync.COMMIT_SUBJECT — the server falls back to it if this is blank.
const DEFAULT_COMMIT_MESSAGE = 'chore(deps): update dependencies'

const mono: CSSProperties = { font: '11px var(--mono)', color: 'var(--faint)' }
const caption: CSSProperties = { font: '11px var(--mono)', color: 'var(--faint)', margin: '6px 0 0' }
const change: CSSProperties = { font: '12px var(--mono)' }
const badge: CSSProperties = {
  font: '10px var(--mono)',
  textTransform: 'uppercase',
  padding: '1px 6px',
  borderRadius: 4,
  border: '1px solid var(--edge)',
  color: 'var(--muted)',
  marginLeft: 8,
}

function BumpTable({ bumps }: { bumps: Bump[] }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Package</th>
            <th scope="col">Change</th>
            <th scope="col">Section</th>
          </tr>
        </thead>
        <tbody>
          {bumps.map((b) => (
            <tr key={`${b.table}:${b.name}`}>
              <td>
                {b.name}
                {b.major && (
                  <span style={{ color: 'var(--red-text)', fontWeight: 700, marginLeft: 6 }}>
                    major
                  </span>
                )}
              </td>
              <td style={change}>{b.old} → {b.new}</td>
              <td style={mono}>{b.table}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function DepUpgrade() {
  const [commitAfter, setCommitAfter] = useState(true)
  const [commitMessage, setCommitMessage] = useState(DEFAULT_COMMIT_MESSAGE)
  const [scannedThisMount, setScannedThisMount] = useState<string | null>(null)
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState<DepApplyResult | null>(null)
  const [applyError, setApplyError] = useState<string | null>(null)

  const { start, snapshot, running, error, setError } = useToolJob<DepScanResult>('/tools/dep-upgrade')

  const result = snapshot?.state === 'done' ? snapshot.result : null

  // Returning to the page restores the last scan from the jobs context, but the
  // folder fields reset on unmount. Both are recovered from the restored root so
  // the review isn't wrongly flagged "folder changed" and stays applyable —
  // derived here rather than written back by an effect, which would have to
  // render once with the wrong values before correcting them.
  const [folder, setFolder] = useState(() => result?.root ?? '')
  const scannedFolder = scannedThisMount ?? result?.root ?? null

  const targets = result?.targets ?? []
  const totalBumps = result?.total_bumps ?? 0
  const stale = result != null && scannedFolder !== folder
  const canApply = totalBumps > 0 && !stale && applyResult == null

  async function runScan() {
    setError(null)
    setApplyResult(null)
    setApplyError(null)
    // Mark the folder scanned only once the job actually starts — a rejected
    // scan must leave any prior scan flagged stale, not retarget Apply.
    const id = await start(() => api.depsScan(folder))
    if (id) setScannedThisMount(folder)
  }

  async function runApply() {
    // Unreachable while the Apply button is gated on canApply, which
    // requires a completed scan; the guard is what lets the call site stay
    // honest about scannedFolder being nullable rather than asserting.
    if (scannedFolder === null) return
    setApplying(true)
    setApplyError(null)
    try {
      setApplyResult(await api.depsApply(scannedFolder, commitAfter, commitMessage))
    } catch (err) {
      setApplyError((err as Error).message)
    } finally {
      setApplying(false)
    }
  }

  return (
    <div>
      <div className="page-head"><h1>📦 Dependency Upgrader</h1></div>
      <p className="page-sub">
        Scan a project for uv (<code>pyproject.toml</code>) and npm
        (<code>package.json</code>) manifests — including subfolders — review the
        outdated dependencies, then upgrade and commit each one.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Project &amp; scan</span></div>

          <FolderField
            label="Project folder"
            value={folder}
            onChange={setFolder}
            placeholder="/Users/you/my-monorepo"
            startDir={folder}
          />

          <label className="check" style={{ margin: '12px 0 8px' }}>
            <input
              type="checkbox"
              checked={commitAfter}
              onChange={(e) => setCommitAfter(e.target.checked)}
            />
            Commit after applying
          </label>

          <div className="field">
            <label htmlFor="dep-commit-message">Commit message</label>
            <input
              id="dep-commit-message"
              className="control"
              value={commitMessage}
              onChange={(e) => setCommitMessage(e.target.value)}
              placeholder={DEFAULT_COMMIT_MESSAGE}
              disabled={!commitAfter}
              spellCheck={false}
            />
            <p style={caption}>
              One commit covers every upgraded manifest and its lockfile. Blank
              falls back to “{DEFAULT_COMMIT_MESSAGE}”.
            </p>
          </div>

          <Button
            variant="primary"
            onClick={runScan}
            loading={running}
            disabled={!folder.trim() || running}
            style={{ marginTop: 10 }}
          >
            🔍 Scan &amp; upgrade
          </Button>

          {error && <div className="note error">{error}</div>}
        </div>

        <div className="panel">
          <div className="step"><span className="n">02</span><span>Review &amp; apply</span></div>

          {!snapshot && (
            <div className="note info">Scan a project to see what's outdated.</div>
          )}

          <JobPanel snapshot={snapshot}>
            {result && targets.length === 0 && (
              <div className="note info">
                No uv or npm manifests found under that folder.
              </div>
            )}

            {result &&
              targets.map((t) => (
                <div key={t.rel} style={{ marginTop: 14 }}>
                  <div style={{ display: 'flex', alignItems: 'center', marginBottom: 6 }}>
                    <strong style={{ font: '12px var(--mono)' }}>{t.rel}</strong>
                    <span style={badge}>{t.kind}</span>
                  </div>
                  {t.error ? (
                    <div className="note error">{t.error}</div>
                  ) : t.bumps.length === 0 ? (
                    <div className="note ok">Up to date.</div>
                  ) : (
                    <BumpTable bumps={t.bumps} />
                  )}
                </div>
              ))}

            {result &&
              totalBumps > 0 &&
              (stale ? (
                <div className="note warn" style={{ marginTop: 12 }}>
                  Folder changed since this scan — re-scan before applying.
                </div>
              ) : (
                <Button
                  variant="primary"
                  onClick={runApply}
                  loading={applying}
                  disabled={!canApply}
                  style={{ marginTop: 14 }}
                >
                  ✍️ Apply {totalBumps} upgrade(s){commitAfter ? ' & commit' : ''}
                </Button>
              ))}

            {applyError && <div className="note error">{applyError}</div>}

            {applyResult && (
              <div style={{ marginTop: 12 }}>
                {applyResult.results.map((r) => (
                  <div
                    key={r.rel}
                    className={`note ${r.error ? 'error' : 'ok'}`}
                    style={{ marginTop: 6 }}
                  >
                    <strong>{r.rel}</strong>
                    {' — '}
                    {r.error
                      ? r.error
                      : r.written === 0
                        ? 'nothing upgraded'
                        : `upgraded ${r.written}`}
                    {r.skipped.length > 0 && (
                      <div style={{ marginTop: 4, font: '11px var(--mono)' }}>
                        skipped {r.skipped.map((s) => s.name).join(', ')} —{' '}
                        {r.skipped[0].reason}
                      </div>
                    )}
                  </div>
                ))}
                {applyResult.commits.map((c) => (
                  <div key={c.sha} className="note ok" style={{ marginTop: 6 }}>
                    Committed <strong>{c.sha}</strong> — {c.files.join(', ')}
                  </div>
                ))}
              </div>
            )}
          </JobPanel>
        </div>
      </div>
    </div>
  )
}
