// Dependency Upgrader — scan a project (incl. subfolders) for uv (pyproject.toml)
// and npm (package.json) manifests, review the outdated deps per manifest, then
// upgrade + commit each. Scan runs as a tracked job (the syncs are slow +
// cancellable); apply is a quick synchronous write, gated on your review.

import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import Button from '../components/Button'

const mono = { font: '11px var(--mono)', color: 'var(--faint)' }
const change = { font: '12px var(--mono)' }
const badge = {
  font: '10px var(--mono)',
  textTransform: 'uppercase',
  padding: '1px 6px',
  borderRadius: 4,
  border: '1px solid var(--edge)',
  color: 'var(--muted)',
  marginLeft: 8,
}

function BumpTable({ bumps }) {
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
  const [folder, setFolder] = useState('')
  const [commitAfter, setCommitAfter] = useState(true)
  const [scannedFolder, setScannedFolder] = useState(null)
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState(null)
  const [applyError, setApplyError] = useState(null)

  const { start, snapshot, running, error, setError } = useToolJob('/tools/dep-upgrade')

  const result = snapshot?.state === 'done' ? snapshot.result : null
  const targets = result?.targets ?? []
  const totalBumps = result?.total_bumps ?? 0
  const stale = result != null && scannedFolder !== folder
  const canApply = totalBumps > 0 && !stale && applyResult == null

  // Returning to the page restores the last scan from the jobs context, but the
  // folder fields reset on unmount. Rehydrate them from the scanned root so the
  // restored review isn't wrongly flagged "folder changed" and stays applyable.
  useEffect(() => {
    if (result?.root && scannedFolder === null) {
      setScannedFolder(result.root)
      setFolder((current) => current || result.root)
    }
  }, [result, scannedFolder])

  async function runScan() {
    setError(null)
    setApplyResult(null)
    setApplyError(null)
    // Mark the folder scanned only once the job actually starts — a rejected
    // scan must leave any prior scan flagged stale, not retarget Apply.
    const id = await start(() => api.depsScan(folder))
    if (id) setScannedFolder(folder)
  }

  async function runApply() {
    setApplying(true)
    setApplyError(null)
    try {
      setApplyResult(await api.depsApply(scannedFolder, commitAfter))
    } catch (err) {
      setApplyError(err.message)
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

          <label className="check" style={{ margin: '12px 0 10px' }}>
            <input
              type="checkbox"
              checked={commitAfter}
              onChange={(e) => setCommitAfter(e.target.checked)}
            />
            Commit each manifest + its lockfile after applying
          </label>

          <div className="note info">
            Scanning runs <code>uv sync -U</code> / <code>npm install</code> in each
            manifest's folder, updating its lockfile. Your review gates the manifest
            rewrite and the commit.
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
                        ? 'nothing to upgrade'
                        : r.committed
                          ? `upgraded ${r.written} and committed ${r.commit_sha}`
                          : `upgraded ${r.written} (not committed)`}
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
