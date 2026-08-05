// Watermark Remover — auto-detect a mask, fix it by hand on a canvas, inpaint.
// The mask that runs is ALWAYS the human-approved one: the backend's proposal
// (dual top-hat, deliberately over-eager) is only a starting point. One job
// per batch; each cleaned image is its own artifact plus one zip of them all.
//
// Results render from the job SNAPSHOT, not from the batch held in local
// state: the jobs context keeps snapshots alive across navigation, so leaving
// the page mid-run and coming back must still reach the downloads. The
// snapshot carries its own batch_id, which is also how a finished run is kept
// from presenting itself as the outcome of a batch uploaded after it.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, artifactUrl, watermarkImageUrl, watermarkMaskUrl } from '../api'
import { useToolJob } from '../jobs'
import BeforeAfter from '../components/BeforeAfter'
import Button from '../components/Button'
import CodeBox from '../components/CodeBox'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import MaskEditor, { type MaskEditorHandle } from '../components/MaskEditor'
import type { WatermarkBatch, WatermarkHealth, WatermarkResult } from '../types/api'

const ACCEPT = '.png,.jpg,.jpeg,.webp'
const DEFAULT_SENSITIVITY = 50
const MAX_IMAGES = 20

export default function WatermarkRemover() {
  const [files, setFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [batch, setBatch] = useState<WatermarkBatch | null>(null)

  // draft follows the sensitivity slider live; applied commits on release and
  // drives the mask refetch, so dragging doesn't flood the detector.
  const [draft, setDraft] = useState<Record<string, number>>({})
  const [applied, setApplied] = useState<Record<string, number>>({})
  const [ready, setReady] = useState<Record<string, boolean>>({})
  const [brush, setBrush] = useState(24)
  const [mode, setMode] = useState<'brush' | 'eraser'>('brush')
  const [inpainter, setInpainter] = useState<'lama' | 'cv2'>('lama')
  const editors = useRef<Record<string, MaskEditorHandle | null>>({})

  // Health lamps load independently of everything else on the page.
  const [health, setHealth] = useState<WatermarkHealth | null>(null)
  useEffect(() => {
    let alive = true
    api
      .watermarkHealth()
      .then((h) => {
        if (!alive) return
        setHealth(h)
        if (!h.lama) setInpainter('cv2')
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  const { start, snapshot, running, error, setError } = useToolJob<WatermarkResult>(
    '/tools/watermark-remover',
  )

  const markReady = useCallback((id: string, isReady: boolean) => {
    setReady((prev) => (prev[id] === isReady ? prev : { ...prev, [id]: isReady }))
  }, [])

  async function detect() {
    setUploading(true)
    setUploadError(null)
    try {
      const fd = new FormData()
      files.forEach((f) => fd.append('files', f))
      const next = await api.watermarkUpload(fd)
      editors.current = {}
      const defaults = Object.fromEntries(
        next.images.map((img) => [img.id, DEFAULT_SENSITIVITY]),
      )
      setDraft(defaults)
      setApplied(defaults)
      setReady({})
      setError(null)
      setBatch(next)
      setFiles([])
    } catch (err) {
      setUploadError((err as Error).message)
    } finally {
      setUploading(false)
    }
  }

  async function run() {
    if (!batch) return
    const masks: Record<string, string> = {}
    const pending: string[] = []
    for (const img of batch.images) {
      const mask = editors.current[img.id]?.exportMask()
      // null means the proposal has not landed yet. Sending nothing for that
      // image would inpaint an empty mask and "succeed" without changing it.
      if (mask) masks[img.id] = mask
      else pending.push(img.name)
    }
    if (pending.length > 0) {
      setError(`Still detecting ${pending.join(', ')} — try again in a moment.`)
      return
    }
    await start(() => api.watermarkRun({ batch_id: batch.batch_id, inpainter, masks }))
  }

  function startOver() {
    setBatch(null)
    setReady({})
    editors.current = {}
  }

  // A cancelled run still carries the images it finished before stopping —
  // the shared JobPanel says "showing partial results", so show them.
  const finished = snapshot?.state === 'done' || snapshot?.state === 'cancelled'
  const result = finished ? snapshot.result : null
  // A finished job from an earlier batch must not be read as this batch's
  // outcome (stale zip, stale filenames) once new images are staged.
  const staleForBatch = batch != null && result != null && result.batch_id !== batch.batch_id
  const showJob = snapshot != null && !staleForBatch

  return (
    <div>
      <div className="page-head">
        <h1>🧽 Watermark Remover</h1>
      </div>
      <p className="page-sub">
        Auto-detect a watermark, correct the mask with a brush, and inpaint it
        away — LaMa for quality, cv2 for speed. For images you own or are
        licensed to edit.
      </p>

      {health && (
        <div className="healthline">
          <span className={`lamp ${health.lama ? '' : 'off'}`}>
            <i />
            LaMa (torch{health.lama ? ` · ${health.device}` : ''})
          </span>
          <span className="lamp">
            <i />
            cv2
          </span>
        </div>
      )}

      <div className="panel">
        <div className="step">
          <span className="n">01</span>
          <span>ADD IMAGES</span>
        </div>
        <FileDrop
          accept={ACCEPT}
          files={files}
          onChange={setFiles}
          hint="Drop up to 20 png / jpg / webp images here — or click to choose"
        />
        {files.length > MAX_IMAGES && (
          <div className="note warn">
            {files.length} images queued — the limit is {MAX_IMAGES} per batch.
          </div>
        )}
        <Button
          variant="primary"
          loading={uploading}
          disabled={files.length === 0}
          onClick={detect}
        >
          Detect watermarks
        </Button>
        {uploadError && <div className="note error">{uploadError}</div>}
        {!batch && !uploadError && (
          <div className="note info">
            Detection proposes a mask per image (tuned for semi-transparent
            tiled text) — you review and fix every mask before anything is
            changed.
          </div>
        )}
      </div>

      {batch && (
        <div className="panel">
          <div className="step">
            <span className="n">02</span>
            <span>REVIEW MASKS ({batch.images.length})</span>
          </div>
          <div className="row wm-toolbar">
            <Button
              size="sm"
              variant={mode === 'brush' ? 'primary' : 'secondary'}
              onClick={() => setMode('brush')}
            >
              🖌 Brush
            </Button>
            <Button
              size="sm"
              variant={mode === 'eraser' ? 'primary' : 'secondary'}
              onClick={() => setMode('eraser')}
            >
              ⌫ Eraser
            </Button>
            <label className="wm-slider">
              size {brush}px
              <input
                type="range"
                min={4}
                max={80}
                value={brush}
                onChange={(e) => setBrush(Number(e.target.value))}
              />
            </label>
            <span className="wm-hint">
              red = will be inpainted · brush adds, eraser restores
            </span>
            <Button size="sm" variant="ghost" onClick={startOver}>
              Start over
            </Button>
          </div>

          {batch.images.map((img) => (
            <div className="wm-card" key={img.id}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <strong>{img.name}</strong>
                <span className="wm-dims">
                  {ready[img.id] === false && 'detecting… · '}
                  {img.width}×{img.height}
                </span>
              </div>
              <MaskEditor
                ref={(handle) => {
                  editors.current[img.id] = handle
                }}
                imageUrl={watermarkImageUrl(batch.batch_id, img.id)}
                maskUrl={watermarkMaskUrl(
                  batch.batch_id,
                  img.id,
                  applied[img.id] ?? DEFAULT_SENSITIVITY,
                )}
                width={img.width}
                height={img.height}
                brush={brush}
                mode={mode}
                onReady={(isReady) => markReady(img.id, isReady)}
              />
              <div className="row">
                <label className="wm-slider">
                  sensitivity {draft[img.id] ?? DEFAULT_SENSITIVITY}
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={draft[img.id] ?? DEFAULT_SENSITIVITY}
                    onChange={(e) =>
                      setDraft({ ...draft, [img.id]: Number(e.target.value) })
                    }
                    onPointerUp={() =>
                      setApplied({
                        ...applied,
                        [img.id]: draft[img.id] ?? DEFAULT_SENSITIVITY,
                      })
                    }
                    onKeyUp={() =>
                      setApplied({
                        ...applied,
                        [img.id]: draft[img.id] ?? DEFAULT_SENSITIVITY,
                      })
                    }
                  />
                </label>
                <Button size="sm" onClick={() => editors.current[img.id]?.undo()}>
                  Undo
                </Button>
                <Button size="sm" onClick={() => editors.current[img.id]?.reset()}>
                  Reset mask
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {batch && (
        <div className="panel">
          <div className="step">
            <span className="n">03</span>
            <span>INPAINT</span>
          </div>
          <div className="field">
            <label htmlFor="wm-inpainter">Inpainting engine</label>
            <select
              id="wm-inpainter"
              className="control"
              value={inpainter}
              onChange={(e) => setInpainter(e.target.value as 'lama' | 'cv2')}
            >
              <option value="lama" disabled={health ? !health.lama : false}>
                LaMa — best quality (ML)
              </option>
              <option value="cv2">cv2 — instant, rougher on large areas</option>
            </select>
          </div>
          {health && !health.lama && (
            <div className="note info">
              LaMa needs the backend&apos;s <code>watermark</code> extra
              (torch): <code>uv sync --extra watermark</code>. Falling back to
              cv2 until then.
            </div>
          )}
          {inpainter === 'lama' && health?.lama && !showJob && (
            <div className="note info">
              First LaMa run downloads a ~200 MB model, so give it a moment.
            </div>
          )}
          <Button variant="primary" loading={running} onClick={run}>
            Remove watermarks
          </Button>
          {error && <div className="note error">{error}</div>}
        </div>
      )}

      {showJob && (
        <div className="panel">
          <div className="step">
            <span className="n">04</span>
            <span>RESULTS</span>
          </div>
          {!batch && (
            <div className="note info">
              Showing a finished run. Its images are no longer staged for
              editing — add images above to start a new one.
            </div>
          )}
          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                {result.done.length > 0 && (
                  <div className="note ok">
                    ✅ Cleaned {result.done.length} image(s). Drag the divider
                    to compare.
                  </div>
                )}
                {result.artifact_id && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    ⬇ Download all (.zip)
                  </Button>
                )}
                {result.files.map((file) => (
                  <div className="wm-card" key={file.artifact_id}>
                    <div className="row" style={{ justifyContent: 'space-between' }}>
                      <strong>{file.name}</strong>
                      <Button as="a" size="sm" href={artifactUrl(file.artifact_id)}>
                        ⬇ Download
                      </Button>
                    </div>
                    <BeforeAfter
                      before={watermarkImageUrl(result.batch_id, file.image_id)}
                      after={artifactUrl(file.artifact_id)}
                      width={file.width}
                      height={file.height}
                    />
                  </div>
                ))}
                {result.failed.length > 0 && (
                  <details className="expander">
                    <summary>❌ {result.failed.length} failed</summary>
                    <div className="body">
                      <CodeBox
                        text={result.failed
                          .map(([name, err]) => `${name}: ${err}`)
                          .join('\n')}
                      />
                    </div>
                  </details>
                )}
              </>
            )}
          </JobPanel>
        </div>
      )}
    </div>
  )
}
