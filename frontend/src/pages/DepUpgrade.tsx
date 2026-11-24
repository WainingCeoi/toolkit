import { useState, type CSSProperties } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import Button from '../components/Button'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import type { Bump, DepApplyResult, DepScanResult, ScanTarget } from '../types/api'

// Mirrors depsync.COMMIT_SUBJECT; keep them in sync.
const DEFAULT_COMMIT_MESSAGE = 'chore(deps): update dependencies'

const mono: CSSProperties = { font: '11px var(--mono)', color: 'var(--faint)' }
const caption: CSSProperties = { fontSize: 12, color: 'var(--muted)', margin: '6px 0 0' }
const change: CSSProperties = { font: '12px var(--mono)' }
const badge: CSSProperties = {
  fontSize: 10,
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
            <th scope="col">Package or interpreter</th>
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

interface ProjectScan {
  root: string
  name: string
  updates: number
  errors: number
  details: ScanTarget[]
  current: ScanTarget[]
}

function groupProjects({ root, targets }: DepScanResult): ProjectScan[] {
  const projects = new Map<string, ProjectScan>()
  const fallback = { root, name: root.split('/').filter(Boolean).at(-1) ?? root }
  for (const target of targets) {
    const { root, name } = target.project ?? fallback
    let project = projects.get(root)
    if (!project) {
      project = { root, name, updates: 0, errors: 0, details: [], current: [] }
      projects.set(root, project)
    }
    project.updates += target.bumps.length
    if (target.error) project.errors++
    const files = target.error || target.bumps.length > 0 ? project.details : project.current
    files.push(target)
  }
  return [...projects.values()].sort((a, b) => Number(b.details.length > 0) - Number(a.details.length > 0))
}

function CurrentTargets({ targets, showStatus }: { targets: ScanTarget[]; showStatus: boolean }) {
  const directories = new Map<string, { name: string; kind: string }[]>()
  for (const target of targets) {
    const relative = target.project_rel ?? target.rel
    const slash = relative.lastIndexOf('/')
    const directory = slash < 0 ? '' : relative.slice(0, slash + 1)
    const file = { name: relative.slice(slash + 1), kind: target.kind }
    const files = directories.get(directory)
    if (files) files.push(file)
    else directories.set(directory, [file])
  }

  return (
    <div className="dep-current-targets">
      {showStatus && <strong>Up to date</strong>}
      <ul role="list">
        {[...directories].map(([directory, files]) => (
          <li key={directory}>
            <strong>{directory || 'Root'}</strong>
            {files.map(({ name, kind }) => (
              <span key={name}>{name} <small>{kind}</small></span>
            ))}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ProjectResult({ project }: { project: ProjectScan }) {
  const files = project.details.length + project.current.length
  return (
    <details className="expander dep-project" open={project.details.length > 0}>
      <summary>
        <strong className="dep-project-name" title={project.root}>{project.name}</strong>
        <span className="dep-project-counts">
          <span>{files} {files === 1 ? 'file' : 'files'}</span>
          {project.updates > 0 && <span>{project.updates} {project.updates === 1 ? 'update' : 'updates'}</span>}
          {project.errors > 0 && <span className="dep-project-errors">{project.errors} {project.errors === 1 ? 'error' : 'errors'}</span>}
          {project.details.length === 0 && <span className="dep-project-current">Up to date</span>}
        </span>
      </summary>
      <div className="body">
        {project.details.map((target) => (
          <div className="dep-project-target" key={target.rel}>
            <strong style={change}>{target.project_rel ?? target.rel}</strong>
            <span style={badge}>{target.kind}</span>
            {target.error ? <div className="note error">{target.error}</div> : <BumpTable bumps={target.bumps} />}
          </div>
        ))}
        {project.current.length > 0 && (
          <CurrentTargets targets={project.current} showStatus={project.details.length > 0} />
        )}
      </div>
    </details>
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

  const completedScan = snapshot?.state === 'done' ? snapshot : null
  const result = completedScan?.result ?? null

  // null until the user edits it: an untouched field follows a scan restored after mount.
  const [typedFolder, setTypedFolder] = useState<string | null>(null)
  const folder = typedFolder ?? result?.root ?? ''
  const scannedFolder = scannedThisMount ?? result?.root ?? null

  const targets = result?.targets ?? []
  const totalBumps = result?.total_bumps ?? 0
  const stale = result != null && scannedFolder !== folder
  const canApply = totalBumps > 0 && !stale && !running && applyResult == null

  async function runScan() {
    setError(null)
    setApplyResult(null)
    setApplyError(null)
    // Only once the job starts: a rejected scan must not retarget Apply.
    const id = await start(() => api.depsScan(folder))
    if (id) setScannedThisMount(folder)
  }

  async function runApply() {
    if (completedScan === null || scannedFolder === null || !canApply) return
    setApplying(true)
    setApplyError(null)
    try {
      setApplyResult(await api.depsApply(scannedFolder, commitAfter, commitMessage, completedScan.id))
    } catch (err) {
      setApplyError((err as Error).message)
    } finally {
      setApplying(false)
    }
  }

  return (
    <div>
      <ToolHeader slug="dep-upgrade" />
      <p className="page-sub">
        Review package updates and interpreter patches across your project, then apply them with an optional commit.
      </p>

      <div className="station">
        <div>
          <div className="panel">
            <div className="step"><span className="n">01</span><span>Project &amp; scan</span></div>

            <FolderField
              label="Project folder"
              value={folder}
              onChange={setTypedFolder}
              placeholder="/Users/you/my-monorepo"
              startDir={folder}
            />

            <div className="action-summary">
              <strong>uv lockfiles update on Apply</strong>
              <p>The scan previews uv upgrades, refreshes npm and pnpm lockfiles, and checks pinned interpreters for newer patches.</p>
            </div>

            <Button
              variant="primary"
              onClick={runScan}
              loading={running}
              disabled={!folder.trim() || running || applying}
            >
              <Icon name="search" size={17} /> Scan for updates
            </Button>
            {!folder.trim() && <p className="hint">Choose a folder containing a package manifest or a version file such as .python-version, .node-version, or .nvmrc.</p>}
            {error && <div className="note error">{error}</div>}
          </div>

          <div className="panel">
            <div className="step"><span className="n">02</span><span>Apply options</span></div>

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
                One commit covers the updated files in each repository. Blank
                falls back to “{DEFAULT_COMMIT_MESSAGE}”.
              </p>
            </div>

            <p className="hint">Review major package updates for compatibility. After applying interpreter updates, use your version manager to install the new versions.</p>
          </div>
        </div>

        <div className="panel result-panel">
          <div className="step"><span className="n">03</span><span>Review &amp; apply</span></div>

          {!snapshot && (
            <EmptyState icon="package" title="Updates appear here">Scan a project to compare current and proposed package and interpreter versions.</EmptyState>
          )}

          <JobPanel snapshot={snapshot}>
            {result && <div className="metrics">
              <div className="metric"><span className="v">{targets.length}</span><span className="k">Files scanned</span></div>
              <div className={totalBumps > 0 ? 'metric' : 'metric ok'}><span className="v">{totalBumps}</span><span className="k">Proposed updates</span></div>
            </div>}
            {result && targets.length === 0 && (
              <div className="note info">
                No supported manifests or interpreter pins found under that folder.
              </div>
            )}

            {completedScan && result && groupProjects(result).map((project) => (
              <ProjectResult key={`${completedScan.id}:${project.root}`} project={project} />
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
                  <Icon name="check" size={17} /> Apply {totalBumps} upgrade(s){commitAfter ? ' & commit' : ''}
                </Button>
              ))}

            {applyError && <div className="note error">{applyError}</div>}

            {applyResult && (
              <div style={{ marginTop: 12 }}>
                <div className="action-summary"><strong>{applyResult.written_total} version changes applied</strong><p>{applyResult.commits.length} commit(s) created. Review the results by file below.</p></div>
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
                      <div className="hint" style={{ marginTop: 4 }}>
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
