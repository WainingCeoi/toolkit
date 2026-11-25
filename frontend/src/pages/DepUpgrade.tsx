import { useEffect, useRef, useState, type CSSProperties } from 'react'
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

function BumpTable({ bumps, compact = false }: { bumps: Bump[]; compact?: boolean }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Package or interpreter</th>
            <th scope="col">Change</th>
            {!compact && <th scope="col">Section</th>}
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
                {compact && <small className="dep-apply-section">{b.table}</small>}
              </td>
              <td style={change}>{b.old} → {b.new}</td>
              {!compact && <td style={mono}>{b.table}</td>}
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

function groupProjects(root: string, targets: (ScanTarget & { written?: number })[]): ProjectScan[] {
  const projects = new Map<string, ProjectScan>()
  const fallback = { root, name: root.split('/').filter(Boolean).at(-1) ?? root }
  for (const target of targets) {
    const { root, name } = target.project ?? fallback
    let project = projects.get(root)
    if (!project) {
      project = { root, name, updates: 0, errors: 0, details: [], current: [] }
      projects.set(root, project)
    }
    const updates = target.written ?? target.bumps.length
    project.updates += updates
    if (target.error) project.errors++
    const files = target.error || updates > 0 ? project.details : project.current
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

function ProjectResult({ project, applied = false }: { project: ProjectScan; applied?: boolean }) {
  const files = project.details.length + project.current.length
  const updateLabel = applied ? 'applied' : project.updates === 1 ? 'update' : 'updates'
  return (
    <details className="expander dep-project" open={project.details.length > 0}>
      <summary>
        <strong className="dep-project-name" title={project.root}>{project.name}</strong>
        <span className="dep-project-counts">
          <span>{files} {files === 1 ? 'file' : 'files'}</span>
          {project.updates > 0 && <span>{project.updates} {updateLabel}</span>}
          {project.errors > 0 && <span className="dep-project-errors">{project.errors} {project.errors === 1 ? 'error' : 'errors'}</span>}
          {project.details.length === 0 && <span className="dep-project-current">Up to date</span>}
        </span>
      </summary>
      <div className="body">
        {project.details.map((target) => (
          <div className="dep-project-target" key={target.rel}>
            <strong style={change}>{target.project_rel ?? target.rel}</strong>
            <span style={badge}>{target.kind}</span>
            {target.error && <div className="note error">{target.error}</div>}
            {!target.error && target.bumps.length > 0 && <BumpTable bumps={target.bumps} compact={applied} />}
          </div>
        ))}
        {project.current.length > 0 && (
          <CurrentTargets targets={project.current} showStatus={project.details.length > 0} />
        )}
      </div>
    </details>
  )
}

function ApplyReport({ result, scan }: { result: DepApplyResult; scan: DepScanResult }) {
  const summaryRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    summaryRef.current?.focus()
  }, [])

  const scannedFiles = new Map(scan.targets.map((target) => [target.rel, target]))
  const applied = result.results.flatMap((target) => {
    if (target.written <= 0 || target.error) return []
    const scanned = scannedFiles.get(target.rel)
    return [{ ...target, project: scanned?.project, project_rel: scanned?.project_rel }]
  })
  const attention = result.results.filter((target) => target.error || target.skipped.length > 0)
  const total = result.written_total

  return (
    <>
      <div className="action-summary" role="status" tabIndex={-1} ref={summaryRef}>
        <strong>{total === 0 ? 'No upgrades applied' : `${total} version ${total === 1 ? 'change' : 'changes'} applied`}</strong>
      </div>

      {attention.length > 0 && (
        <details className="expander dep-apply-attention" open={total === 0}>
          <summary>Needs attention · {attention.length} {attention.length === 1 ? 'file' : 'files'}</summary>
          <div className="body">
            <ul className="dep-apply-items" role="list">
              {attention.map((target) => (
                <li key={target.rel}>
                  <strong>{target.rel}</strong>
                  {target.error && <p className="dep-apply-error">{target.error}</p>}
                  {target.skipped.length > 0 && (
                    <ul>
                      {target.skipped.map(({ name, reason }) => (
                        <li key={name}>Skipped <strong>{name}</strong> — {reason}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          </div>
        </details>
      )}

      {groupProjects(scan.root, applied).map((project) => (
        <ProjectResult key={project.root} project={project} applied />
      ))}

      {result.commits.length > 0 && (
        <details className="expander">
          <summary>{result.commits.length} {result.commits.length === 1 ? 'commit' : 'commits'} created</summary>
          <div className="body">
            <ul className="dep-apply-items" role="list">
              {result.commits.map((commit, index) => (
                <li key={`${commit.sha}:${index}`}>
                  {commit.sha && <strong><code>{commit.sha}</code></strong>}
                  <p>{commit.files.join(', ')}</p>
                </li>
              ))}
            </ul>
          </div>
        </details>
      )}
    </>
  )
}

export default function DepUpgrade() {
  const [commitAfter, setCommitAfter] = useState(true)
  const [commitMessage, setCommitMessage] = useState(DEFAULT_COMMIT_MESSAGE)
  const [scannedThisMount, setScannedThisMount] = useState<string | null>(null)
  const [applying, setApplying] = useState(false)
  const [appliedScan, setAppliedScan] = useState<{ result: DepApplyResult; scan: DepScanResult } | null>(null)
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
  const canApply = totalBumps > 0 && !stale && !running && appliedScan == null

  async function runScan() {
    setError(null)
    setApplyError(null)
    // Only once the job starts: a rejected scan must not retarget Apply.
    const id = await start(() => api.depsScan(folder))
    if (id) {
      setAppliedScan(null)
      setScannedThisMount(folder)
    }
  }

  async function runApply() {
    if (completedScan === null || scannedFolder === null || !canApply) return
    setApplying(true)
    setApplyError(null)
    try {
      const response = await api.depsApply(scannedFolder, commitAfter, commitMessage, completedScan.id)
      setAppliedScan({ result: response, scan: completedScan.result })
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
          <div className="step"><span className="n">03</span><span>{appliedScan ? 'Apply result' : 'Review & apply'}</span></div>

          {!snapshot && !appliedScan && (
            <EmptyState icon="package" title="Updates appear here">Scan a project to compare current and proposed package and interpreter versions.</EmptyState>
          )}

          {appliedScan ? (
            <ApplyReport {...appliedScan} />
          ) : (
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

              {completedScan && result && groupProjects(result.root, result.targets).map((project) => (
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
            </JobPanel>
          )}
          {applyError && <div className="note error">{applyError}</div>}
        </div>
      </div>
    </div>
  )
}
