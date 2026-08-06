// Watermark Remover — auto-detect a mask, review it, inpaint it away. One job
// per batch; each cleaned image is its own artifact plus one zip of them all.
//
// The mask is reviewed, not edited. There used to be a brush and an eraser, and
// dropping them is a deliberate narrowing: the detector masks the copies of a
// mark it actually recovered, or reports that it recovered nothing and the image
// is skipped. Hand-painting the second case masks whatever the person could see
// rather than the watermark, and inpainting that damaged photographs while
// leaving the watermark in place. What is shown is what runs.
//
// Results render from the job SNAPSHOT, not from the batch held in local
// state: the jobs context keeps snapshots alive across navigation, so leaving
// the page mid-run and coming back must still reach the downloads. The
// snapshot carries its own batch_id, which is also how a finished run is kept
// from presenting itself as the outcome of a batch uploaded after it.

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, artifactUrl, watermarkImageUrl, watermarkMaskUrl } from '../api'
import { useToolJob } from '../jobs'
import Button from '../components/Button'
import CodeBox from '../components/CodeBox'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import MaskPreview, { type MaskPreviewHandle } from '../components/MaskPreview'
import type {
  WatermarkBatch,
  WatermarkDetector,
  WatermarkHealth,
  WatermarkResult,
} from '../types/api'

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
  const [noPattern, setNoPattern] = useState<Record<string, boolean>>({})
  const [detector, setDetector] = useState<WatermarkDetector>('pattern')
  const [inpainter, setInpainter] = useState<'lama' | 'cv2'>('lama')
  const previews = useRef<Record<string, MaskPreviewHandle | null>>({})

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

  // An all-black proposal means the detector declined; the run will skip that
  // image rather than inpaint anything, and the review panel should say so.
  const markEmpty = useCallback((id: string, empty: boolean) => {
    setNoPattern((prev) => (prev[id] === empty ? prev : { ...prev, [id]: empty }))
  }, [])

  async function detect() {
    setUploading(true)
    setUploadError(null)
    try {
      const fd = new FormData()
      files.forEach((f) => fd.append('files', f))
      const next = await api.watermarkUpload(fd)
      previews.current = {}
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
      const mask = previews.current[img.id]?.exportMask()
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
    setNoPattern({})
    previews.current = {}
  }

  // Results are shown in every state, because the worker publishes them per
  // image: a run that dies on image 8 must still hand back images 1-7, and a
  // cancelled or still-running one has finished images worth reaching too.
  const result = snapshot?.result ?? null
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
        Auto-detect a repeating watermark, review what will be removed, and
        inpaint it away — LaMa for quality, cv2 for speed. For images you own or
        are licensed to edit.
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
            tiled text) — you see exactly what will be removed before anything
            is changed, and an image with no watermark found is left alone.
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
            <span className="wm-hint">
              red = will be inpainted · sensitivity re-runs detection
            </span>
            <Button size="sm" variant="ghost" onClick={startOver}>
              Start over
            </Button>
          </div>
          <div className="row wm-toolbar">
            <label className="wm-slider" htmlFor="wm-detector">
              detection
            </label>
            <select
              id="wm-detector"
              className="control"
              style={{ width: 'auto' }}
              value={detector}
              onChange={(e) => setDetector(e.target.value as WatermarkDetector)}
            >
              <option value="pattern">Repeating pattern — recovers a tiled mark</option>
              <option value="texture">Standing out locally — any watermark</option>
            </select>
            <span className="wm-hint">
              {detector === 'pattern'
                ? 'Recovers the repeated mark and masks only its copies, so edges and detail are left alone. A mark found on one image is tried on the rest of the batch; any image with no recoverable repeat is skipped, not guessed at.'
                : 'Works on anything, including a single logo — but thin detail like seams and wires read as watermark too, and there is no brush to correct that. Prefer the pattern detector.'}
            </span>
          </div>

          {batch.images.map((img) => (
            <div className="wm-card" key={img.id}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <strong>{img.name}</strong>
                <span className="wm-dims">
                  {ready[img.id] === false && 'detecting… · '}
                  {noPattern[img.id] && 'no watermark found — will be skipped · '}
                  {img.width}×{img.height}
                </span>
              </div>
              <MaskPreview
                ref={(handle) => {
                  previews.current[img.id] = handle
                }}
                imageUrl={watermarkImageUrl(batch.batch_id, img.id)}
                maskUrl={watermarkMaskUrl(
                  batch.batch_id,
                  img.id,
                  applied[img.id] ?? DEFAULT_SENSITIVITY,
                  detector,
                )}
                width={img.width}
                height={img.height}
                onReady={(isReady) => markReady(img.id, isReady)}
                onEmpty={(empty) => markEmpty(img.id, empty)}
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
                  <div className={`note ${snapshot.state === 'failed' ? 'warn' : 'ok'}`}>
                    {snapshot.state === 'running' &&
                      `${result.done.length} done so far — each is ready to download as it lands.`}
                    {snapshot.state === 'done' &&
                      `✅ Cleaned ${result.done.length} image(s).`}
                    {snapshot.state === 'cancelled' &&
                      `Stopped after ${result.done.length} image(s) — these are finished and safe to download.`}
                    {snapshot.state === 'failed' &&
                      `The run stopped early, but these ${result.done.length} image(s) finished and are safe to download.`}
                  </div>
                )}
                {result.artifact_id && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    ⬇ Download all (.zip)
                  </Button>
                )}
                {result.files.map((file) => (
                  <div className="row wm-file" key={file.artifact_id}>
                    <strong>{file.name}</strong>
                    <Button as="a" size="sm" href={artifactUrl(file.artifact_id)}>
                      ⬇ Download
                    </Button>
                  </div>
                ))}
                {result.skipped.length > 0 && (
                  <div className="note warn">
                    Left untouched — no repeating watermark could be recovered,
                    so nothing was inpainted rather than risk damaging the
                    image: {result.skipped.join(', ')}
                  </div>
                )}
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
