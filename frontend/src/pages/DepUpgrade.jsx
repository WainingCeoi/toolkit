// Dependency Upgrader — point at a uv project, run `uv sync -U`, review the
// lagging >= floors it turned up, then rewrite pyproject.toml and commit it
// with uv.lock. Scan runs as a tracked job (uv sync is slow + cancellable);
// apply is a quick synchronous write, gated on your review.

import React, { useEffect, useState } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'

const mono = { font: '11px var(--mono)', color: 'var(--faint)' }
const change = { font: '12px var(--mono)' }

export default function DepUpgrade() {
  const [folder, setFolder] = useState('')
  const [commitAfter, setCommitAfter] = useState(true)
  const [scannedFolder, setScannedFolder] = useState(null)
  const [applying, setApplying] = useState(false)
  const [applyResult, setApplyResult] = useState(null)
  const [applyError, setApplyError] = useState(null)

  const { start, snapshot, running, error, setError } = useToolJob('/tools/dep-upgrade')

  const result = snapshot?.state === 'done' ? snapshot.result : null
  const bumps = result?.bumps ?? []
  const stale = result != null && scannedFolder !== folder
  const applied = applyResult != null && applyResult.written > 0

  // Returning to the page restores the last scan from the jobs context, but the
  // folder fields reset on unmount. Rehydrate them from the scan's own result so
  // the restored bumps aren't wrongly flagged "folder changed" and stay applyable.
  useEffect(() => {
    if (result?.folder && scannedFolder === null) {
      setScannedFolder(result.folder)
      setFolder((current) => current || result.folder)
    }
  }, [result, scannedFolder])

  async function runScan() {
    setError(null)
    setApplyResult(null)
    setApplyError(null)
    // Mark the folder scanned only once the job actually starts — a rejected
    // scan (e.g. no pyproject.toml) must leave the prior scan flagged stale, not
    // silently retarget Apply at an unscanned folder.
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
        Point at a uv project. It runs <code>uv sync -U</code>, shows which
        declared <code>&gt;=</code> floors are now behind, then — on your OK —
        rewrites <code>pyproject.toml</code> and commits it with{' '}
        <code>uv.lock</code>.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Project &amp; scan</span></div>

          <FolderField
            label="uv project folder"
            value={folder}
            onChange={setFolder}
            placeholder="/Users/you/my-project"
            startDir={folder}
          />

          <label className="check" style={{ margin: '12px 0 10px' }}>
            <input
              type="checkbox"
              checked={commitAfter}
              onChange={(e) => setCommitAfter(e.target.checked)}
            />
            Commit pyproject.toml + uv.lock after applying
          </label>

          <div className="note info">
            Scanning runs <code>uv sync -U</code> in this folder, updating its{' '}
            <code>uv.lock</code> and virtualenv. Your review gates only the{' '}
            <code>pyproject.toml</code> rewrite and the commit.
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
            <div className="note info">
              Scan a uv project to see which floors are behind.
            </div>
          )}

          <JobPanel snapshot={snapshot}>
            {result && bumps.length === 0 && (
              <div className="note ok">
                Every declared <code>&gt;=</code> floor already matches the
                resolved version — nothing to bump. 🎉
              </div>
            )}

            {result && bumps.length > 0 && (
              <>
                <div className="note warn">
                  {bumps.length} floor(s) behind the resolved versions.
                  {bumps.some((b) => b.major) &&
                    ' Includes a major-version jump — review before applying.'}
                </div>

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
                              <span
                                style={{ color: 'var(--red-text)', fontWeight: 700, marginLeft: 6 }}
                              >
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

                {stale ? (
                  <div className="note warn" style={{ marginTop: 10 }}>
                    Folder changed since this scan — re-scan before applying.
                  </div>
                ) : (
                  <Button
                    variant="primary"
                    onClick={runApply}
                    loading={applying}
                    disabled={applying || applied}
                    style={{ marginTop: 12 }}
                  >
                    ✍️ Apply {bumps.length} bump(s){commitAfter ? ' & commit' : ''}
                  </Button>
                )}
              </>
            )}

            {applyError && <div className="note error">{applyError}</div>}

            {applyResult && (
              <div className="note ok" style={{ marginTop: 10 }}>
                {applyResult.written === 0
                  ? applyResult.note
                  : applyResult.committed
                    ? `Wrote ${applyResult.written} floor(s) and committed ${applyResult.commit_sha}.`
                    : `Wrote ${applyResult.written} floor(s) to pyproject.toml (not committed).`}
              </div>
            )}

            {applyResult?.commit_message && (
              <details className="expander" style={{ marginTop: 8 }}>
                <summary>Commit message</summary>
                <div className="body"><CodeBox text={applyResult.commit_message} /></div>
              </details>
            )}
          </JobPanel>
        </div>
      </div>
    </div>
  )
}
