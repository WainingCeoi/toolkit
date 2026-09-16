import { useCallback, useEffect, useRef, useState } from 'react'
import { api, artifactUrl, watermarkImageUrl, watermarkMaskUrl } from '../api'
import { useToolJob } from '../jobs'
import Button from '../components/Button'
import CodeBox from '../components/CodeBox'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import MaskPreview, { type MaskPreviewHandle } from '../components/MaskPreview'
import type { WatermarkBatch, WatermarkHealth, WatermarkResult } from '../types/api'

const ACCEPT = '.png,.jpg,.jpeg,.webp'
const DEFAULT_SENSITIVITY = 50
const MAX_IMAGES = 20

export default function WatermarkRemover() {
  const [files, setFiles] = useState<File[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [batch, setBatch] = useState<WatermarkBatch | null>(null)

  // draft tracks the slider live; applied commits on release so dragging does not refetch.
  const [draft, setDraft] = useState<Record<string, number>>({})
  const [applied, setApplied] = useState<Record<string, number>>({})
  const [ready, setReady] = useState<Record<string, boolean>>({})
  const [noPattern, setNoPattern] = useState<Record<string, boolean>>({})
  const [failed, setFailed] = useState<Record<string, boolean>>({})
  const [inpainter, setInpainter] = useState<'lama' | 'cv2'>('lama')
  const previews = useRef<Record<string, MaskPreviewHandle | null>>({})

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

  // An all-black proposal means the detector declined; the run skips that image.
  const markEmpty = useCallback((id: string, empty: boolean) => {
    setNoPattern((prev) => (prev[id] === empty ? prev : { ...prev, [id]: empty }))
  }, [])

  const markFailed = useCallback((id: string, isFailed: boolean) => {
    setFailed((prev) => (prev[id] === isFailed ? prev : { ...prev, [id]: isFailed }))
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
      setFailed({})
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
    const broken: string[] = []
    for (const img of batch.images) {
      // A failed proposal leaves the previous one on the canvas; never inpaint that.
      if (failed[img.id]) {
        broken.push(img.name)
        continue
      }
      // false while a refetch is in flight: the canvas would still export the previous mask.
      if (ready[img.id] === false) {
        pending.push(img.name)
        continue
      }
      const mask = previews.current[img.id]?.exportMask()
      // null means no proposal ever landed; sending nothing would "succeed" on an empty mask.
      if (mask) masks[img.id] = mask
      else pending.push(img.name)
    }
    if (broken.length > 0) {
      setError(
        `Could not load the mask for ${broken.join(', ')} — nudge the sensitivity slider to retry.`,
      )
      return
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
    setFailed({})
    previews.current = {}
  }

  // Shown in every state: results are published per image, so a failed run still has some.
  const result = snapshot?.result ?? null
  const staleForBatch = batch != null && result != null && result.batch_id !== batch.batch_id
  const showJob = snapshot != null && !staleForBatch

  return (
    <div>
      <div className="page-head">
        <h1>🧽 Watermark Remover</h1>
      </div>
      <p className="page-sub">
        Auto-detect a watermark — repeating, or stamped once per photo across
        the batch — review what will be removed, and inpaint it away: LaMa for
        quality, cv2 for speed. For images you own or are licensed to edit.
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
            Detection proposes a mask per image automatically — you see
            exactly what will be removed before anything is changed, and an
            image with no mask is left alone. A mark stamped once per photo
            needs company: upload several photos from the same supplier and
            the batch recovers it together.
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

          {batch.images.map((img) => (
            <div className="wm-card" key={img.id}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <strong>{img.name}</strong>
                <span className="wm-dims">
                  {failed[img.id]
                    ? 'detection failed — nudge the sensitivity slider to retry · '
                    : ready[img.id] === false && 'detecting… · '}
                  {/* Not "no watermark found": an empty mask may mean it cannot be isolated. */}
                  {noPattern[img.id] && 'nothing to remove — will be left alone · '}
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
                )}
                width={img.width}
                height={img.height}
                onReady={(isReady) => markReady(img.id, isReady)}
                onEmpty={(empty) => markEmpty(img.id, empty)}
                onError={(isFailed) => markFailed(img.id, isFailed)}
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
          {health && !health.lama && (
            <div className="note info">
              Best quality needs the backend&apos;s <code>watermark</code>{' '}
              extra (torch): <code>uv sync --extra watermark</code>. Using the
              instant cv2 inpainter until then.
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
                      `${result.done.length} done so far — the download appears once the run finishes.`}
                    {snapshot.state === 'done' &&
                      `✅ Cleaned ${result.done.length} image(s).`}
                    {snapshot.state === 'cancelled' &&
                      `Stopped after ${result.done.length} image(s) — these are finished and safe to download.`}
                    {snapshot.state === 'failed' &&
                      `The run stopped early, but these ${result.done.length} image(s) finished and are safe to download.`}
                  </div>
                )}
                {/* Hidden mid-run: the zip exists but would be a partial batch. */}
                {result.artifact_id && snapshot.state !== 'running' && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    ⬇ Download cleaned images (.zip)
                  </Button>
                )}
                {result.skipped.length > 0 && (
                  <div className="note warn">
                    Left untouched — nothing was masked for them, so nothing
                    was inpainted. A mark stamped once per photo needs more
                    photos from the same supplier in one batch:{' '}
                    {result.skipped.join(', ')}
                  </div>
                )}
                {(result.protected ?? []).length > 0 && (
                  <div className="note warn">
                    Left untouched on purpose — removing the watermark would
                    have destroyed the picture under it (text or line art the
                    mark sits on): {result.protected.join(', ')}
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
