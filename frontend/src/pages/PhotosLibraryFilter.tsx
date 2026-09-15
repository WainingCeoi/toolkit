// Photos Library Filter — mirror SRC.photoslibrary → DEST.photoslibrary minus
// its caches, safe while Photos is running. The dry run (plan + verify,
// nothing written) and the real run are both tracked jobs that return the
// same report; the page renders whichever came back last. Mirrors
// backend/src/toolkit_api/routers/photofilter.py.

import { useState, type CSSProperties } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'
import { formatBytes } from '../torrent'
import type { PhotoFilterResult } from '../types/api'

// Mirror of photofilter.DEFAULT_RULES — what the textarea starts with. The
// backend applies exactly the text it is sent, so this copy is the one that
// runs; keep it saying the same thing as the engine's.
const DEFAULT_RULES = `# Photos Library Filter rules (rsync-style)
# Everything not listed here is kept. Do NOT exclude resources/renders/: it holds
# the edit recipes (UUID.plist) and rendered edits that the database expects to
# be present.
.DS_Store
# search index (Spotlight + leo.sqlite): rebuilt by Photos
database/search/
# runtime lock / WAL / SHM: the tool snapshots Photos.sqlite with the WAL folded in
database/*.lock
database/*-wal
database/*-shm
# thumbnails and previews: pure cache, rebuilt by Photos (blank thumbs until then,
# or Repair Library)
resources/derivatives/
resources/caches/
# analysis caches (scene/face/knowledge graph): rebuilt by photoanalysisd
private/**/caches/
`

// A first run into a stale mirror can delete tens of thousands of thumbnails;
// the report keeps the count exact and the list readable.
const LIST_LIMIT = 200

const caption: CSSProperties = { font: '11px var(--mono)', color: 'var(--faint)', margin: '6px 0 0' }
const mono: CSSProperties = { font: '12px var(--mono)', color: 'var(--muted)', overflowWrap: 'anywhere' }
const num: CSSProperties = { textAlign: 'right', whiteSpace: 'nowrap' }

function ListExpander({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  const shown = items.slice(0, LIST_LIMIT)
  return (
    <details className="expander">
      <summary>{title}</summary>
      <div className="body">
        <CodeBox text={shown.join('\n')} />
        {items.length > shown.length && (
          <p style={caption}>
            Showing first {shown.length} of {items.length}.
          </p>
        )}
      </div>
    </details>
  )
}

function Report({ result }: { result: PhotoFilterResult }) {
  const { verify } = result
  return (
    <div>
      <p style={{ ...mono, margin: '8px 0 0' }}>
        {result.source} → {result.dest}
        {result.dry_run ? ' (dry run, nothing written)' : ''} · {result.seconds}s
      </p>

      <div className="metrics">
        <div className="metric">
          <span className="v">{result.kept.files}</span>
          <span className="k">kept · {formatBytes(result.kept.bytes)}</span>
        </div>
        <div className="metric">
          <span className="v">{result.excluded.files}</span>
          <span className="k">excluded · {formatBytes(result.excluded.bytes)}</span>
        </div>
        <div className="metric">
          <span className="v">{result.snapshots.length}</span>
          <span className="k">{result.dry_run ? 'databases to snapshot' : 'databases snapshotted'}</span>
        </div>
      </div>

      {!result.dry_run && (
        <div className="metrics">
          <div className="metric ok">
            <span className="v">{result.copied}</span>
            <span className="k">copied</span>
          </div>
          <div className="metric">
            <span className="v">{result.skipped}</span>
            <span className="k">skipped · unchanged</span>
          </div>
          <div className={result.deleted.length > 0 ? 'metric bad' : 'metric'}>
            <span className="v">{result.deleted.length}</span>
            <span className="k">deleted</span>
          </div>
        </div>
      )}

      {result.rules.length > 0 ? (
        <div style={{ overflowX: 'auto' }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Rule</th>
                <th scope="col" style={num}>Files</th>
                <th scope="col" style={num}>Size</th>
              </tr>
            </thead>
            <tbody>
              {result.rules.map((r) => (
                <tr key={r.rule} style={r.files === 0 ? { opacity: 0.55 } : undefined}>
                  <td>{r.rule}</td>
                  <td style={num}>{r.files}</td>
                  <td style={num}>{formatBytes(r.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="note info">No rules — everything is mirrored.</div>
      )}

      {result.snapshots.length > 0 && (
        <p style={caption}>
          Snapshotted with the WAL folded in: {result.snapshots.join(', ')}
        </p>
      )}

      <ListExpander title={`🗑️ Deleted from the destination (${result.deleted.length})`} items={result.deleted} />

      {result.errors.map((e) => (
        <div key={e} className="note error" style={{ overflowWrap: 'anywhere' }}>
          {e}
        </div>
      ))}

      {!verify.ran ? (
        <div className="note warn">Not verified — the run stopped before the check.</div>
      ) : verify.problems.length === 0 ? (
        <div className="note ok">
          Verified {verify.assets} asset(s), {verify.edited} edited — every original and
          edit recipe is {result.dry_run ? 'in the plan' : 'in the mirror'}.
        </div>
      ) : (
        <>
          <div className="note error">
            {verify.problems.length} problem(s) across {verify.assets} asset(s),{' '}
            {verify.edited} edited.
          </div>
          <ListExpander title={`❌ Problems (${verify.problems.length})`} items={verify.problems} />
        </>
      )}
    </div>
  )
}

export default function PhotosLibraryFilter() {
  const [source, setSource] = useState('~/Pictures/Photos Library.photoslibrary')
  const [dest, setDest] = useState('')
  const [rules, setRules] = useState(DEFAULT_RULES)
  const [dryRun, setDryRun] = useState(true)
  const [confirm, setConfirm] = useState(false)

  const { start, snapshot, running, error } = useToolJob<PhotoFilterResult>(
    '/tools/photos-library-filter',
  )

  const ready = source.trim() !== '' && dest.trim() !== ''
  const run = () => {
    const payload = { source, dest, rules }
    return start(() => (dryRun ? api.photofilterDryRun(payload) : api.photofilterRun(payload)))
  }

  const result =
    snapshot && (snapshot.state === 'done' || snapshot.state === 'cancelled')
      ? snapshot.result
      : null

  return (
    <div>
      <div className="page-head"><h1>📸 Photos Library Filter</h1></div>
      <p className="page-sub">
        Mirror a Photos library into a cache-free copy Photos can still open, while
        Photos is running: the live database is snapshotted with its WAL folded in,
        originals are cloned with their metadata, and every asset is verified.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Libraries &amp; rules</span></div>

          <FolderField
            label="Source library (read only)"
            value={source}
            onChange={setSource}
            placeholder="~/Pictures/Photos Library.photoslibrary"
            startDir="~/Pictures"
            packages
          />
          <FolderField
            label="Destination library (the mirror)"
            value={dest}
            onChange={setDest}
            placeholder="~/Pictures/backup_filtered/Photos Library.photoslibrary"
            startDir="~/Pictures"
            packages
          />
          <p style={{ ...caption, margin: '-6px 0 12px' }}>
            Must end in .photoslibrary and lie outside the source. Created if missing;
            anything already in it that is not part of the plan is deleted.
          </p>

          <div className="field">
            <label htmlFor="photofilter-rules">Exclude rules</label>
            <textarea
              id="photofilter-rules"
              className="control"
              rows={14}
              value={rules}
              onChange={(e) => setRules(e.target.value)}
              spellCheck={false}
              // One line per rule: a wrapped comment would read as a rule.
              wrap="off"
            />
            <p style={caption}>
              One rule per line. Trailing / = a directory and everything in it · leading / =
              anchored to the library root · * and ? stay inside one path component · **
              crosses components · # starts a comment.
            </p>
          </div>

          <label className="check" style={{ margin: '4px 0 8px' }}>
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
            Dry run — plan and verify only, write nothing
          </label>

          {!dryRun && (
            <label className="check" style={{ margin: '0 0 10px' }}>
              <input
                type="checkbox"
                checked={confirm}
                onChange={(e) => setConfirm(e.target.checked)}
              />
              I understand the destination becomes a mirror: files in it that are not in the
              plan are deleted.
            </label>
          )}

          {dryRun ? (
            <Button variant="primary" onClick={run} loading={running} disabled={!ready || running}>
              🔍 Dry run
            </Button>
          ) : (
            <Button
              variant="danger"
              onClick={run}
              loading={running}
              disabled={!ready || !confirm || running}
            >
              📸 Mirror library
            </Button>
          )}

          {error && <div className="note error">{error}</div>}
        </div>

        <div className="panel">
          <div className="step"><span className="n">02</span><span>Report</span></div>

          {!snapshot && (
            <div className="note info">
              Dry-run first: it shows what would be kept, what each rule excludes and how much
              it saves, and whether every asset would still be there — without writing a byte.
            </div>
          )}

          <JobPanel snapshot={snapshot}>{result && <Report result={result} />}</JobPanel>
        </div>
      </div>
    </div>
  )
}
