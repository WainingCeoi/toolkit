// Canvas mask editor: the image with its mask tinted red on top, and a brush
// to change it.
//
// This was briefly a review-only preview. The brush was dropped when the
// pattern detector became the default — it either recovers a repeating mark
// and masks its copies precisely, or reports nothing, and hand-painting marks
// a person could only partly SEE (six faint copies of a tiled overlay) had
// damaged photographs while leaving the watermark in place. What that
// reasoning missed is the watermark that does not repeat at all: a single
// large logo has no copies to fold, so detection is structurally blind to it
// — and unlike a faint tiled mark, a person sees all of it. The brush is back
// for exactly that case; where the detector applies, its proposal is still
// the starting point.
//
// The mask lives on an offscreen canvas at native image resolution — the
// visible canvas only composites image + overlay (and the brush cursor), so
// nothing is ever resampled. Pointer strokes paint (or erase, via
// destination-out) round-capped lines onto the overlay; export thresholds the
// overlay back into the black/white PNG the backend expects — what is
// inpainted is exactly the mask that was shown here. When maskUrl changes
// (the sensitivity slider), the fresh proposal REPLACES the overlay, with the
// previous state pushed onto the undo stack so a slider nudge is never
// destructive — except for the very first proposal, which has no previous
// state worth restoring (see loadMask).

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
} from 'react'
import { canvasPoint, maskToOverlay, overlayToMask, TINT } from '../mask'

const UNDO_LIMIT = 20

export interface MaskEditorHandle {
  /**
   * The shown mask as bare base64 PNG (white = remove), or null while the
   * proposal is still loading — exporting the blank canvas would be a mask
   * that removes nothing, and the run would silently no-op.
   */
  exportMask(): string | null
  undo(): void
  /** Refetch the auto-mask, discarding manual edits (undo-able). */
  reset(): void
}

interface MaskEditorProps {
  imageUrl: string
  maskUrl: string
  width: number
  height: number
  /** Brush radius in image pixels. */
  brush: number
  mode: 'brush' | 'eraser'
  /** Fires false while a proposal is in flight, true once one has landed. */
  onReady?: (ready: boolean) => void
  /**
   * Fires true when the landed proposal marks nothing at all, and false the
   * moment a brush stroke lands — a painted canvas is no longer "nothing to
   * remove", and the caption inviting the paint must stand down.
   */
  onEmpty?: (empty: boolean) => void
}

const MaskEditor = forwardRef<MaskEditorHandle, MaskEditorProps>(
  function MaskEditor(
    { imageUrl, maskUrl, width, height, brush, mode, onReady, onEmpty },
    ref,
  ) {
    const viewRef = useRef<HTMLCanvasElement>(null)
    const image = useRef<HTMLImageElement | null>(null)
    const overlay = useRef<HTMLCanvasElement | null>(null)
    const undoStack = useRef<ImageData[]>([])
    const drawing = useRef(false)
    const last = useRef<{ x: number; y: number } | null>(null)
    const hover = useRef<{ x: number; y: number } | null>(null)
    // A proposal has landed, so the overlay means something. Until then an
    // export would be a blank "remove nothing" mask.
    const loaded = useRef(false)
    const loadToken = useRef(0)
    // Held in refs so a parent passing inline callbacks cannot re-trigger the
    // mask fetch on every render.
    const readyCb = useRef(onReady)
    readyCb.current = onReady
    const emptyCb = useRef(onEmpty)
    emptyCb.current = onEmpty
    // Read by pointer handlers without re-binding them on every prop change.
    const tool = useRef({ brush, mode })
    tool.current = { brush, mode }

    const overlayCtx = () => {
      if (!overlay.current) {
        overlay.current = document.createElement('canvas')
        overlay.current.width = width
        overlay.current.height = height
      }
      return overlay.current.getContext('2d', { willReadFrequently: true })!
    }

    const redraw = useCallback(() => {
      const view = viewRef.current
      const ctx = view?.getContext('2d')
      if (!view || !ctx) return
      ctx.clearRect(0, 0, width, height)
      if (image.current) ctx.drawImage(image.current, 0, 0)
      if (overlay.current) {
        ctx.globalAlpha = 0.45
        ctx.drawImage(overlay.current, 0, 0)
        ctx.globalAlpha = 1
      }
      if (hover.current) {
        // Brush cursor: a ring sized like the stroke it would leave.
        ctx.beginPath()
        ctx.arc(hover.current.x, hover.current.y, tool.current.brush, 0, Math.PI * 2)
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)'
        ctx.lineWidth = Math.max(1, width / 500)
        ctx.stroke()
      }
    }, [width, height])

    const pushUndo = useCallback(() => {
      const ctx = overlayCtx()
      undoStack.current.push(ctx.getImageData(0, 0, width, height))
      if (undoStack.current.length > UNDO_LIMIT) undoStack.current.shift()
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [width, height])

    const loadMask = useCallback(() => {
      // Only the newest load may write: a slow proposal must not land on top
      // of the one the sensitivity slider asked for afterwards.
      const token = ++loadToken.current
      readyCb.current?.(false)
      const img = new Image()
      img.onload = () => {
        if (token !== loadToken.current) return
        const probe = document.createElement('canvas')
        probe.width = width
        probe.height = height
        const probeCtx = probe.getContext('2d', { willReadFrequently: true })!
        probeCtx.drawImage(img, 0, 0)
        const pixels = probeCtx.getImageData(0, 0, width, height)
        maskToOverlay(pixels.data)
        // Undo restores what was on the canvas BEFORE this proposal — but
        // only if there was something. Snapshotting the blank starting
        // canvas would make the very first Undo erase the whole proposal.
        if (loaded.current) pushUndo()
        overlayCtx().putImageData(pixels, 0, 0)
        loaded.current = true
        readyCb.current?.(true)
        emptyCb.current?.(
          !pixels.data.some((_v, i) => i % 4 === 3 && pixels.data[i] > 0),
        )
        redraw()
      }
      img.onerror = () => {
        if (token !== loadToken.current) return
        readyCb.current?.(loaded.current)
      }
      img.src = maskUrl
      return () => {
        // Abandon this load; a later one (or none) wins. Bumping the LIVE
        // counter is the point — a value captured when the effect ran could not
        // invalidate the load that is still in flight, which is the one race
        // this guards.
        loadToken.current++
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [maskUrl, width, height, pushUndo, redraw])

    useEffect(() => {
      let alive = true
      const img = new Image()
      img.onload = () => {
        if (!alive) return
        image.current = img
        redraw()
      }
      img.src = imageUrl
      return () => {
        alive = false
      }
    }, [imageUrl, redraw])

    useEffect(() => loadMask(), [loadMask])

    useImperativeHandle(
      ref,
      () => ({
        exportMask() {
          if (!loaded.current) return null
          const out = document.createElement('canvas')
          out.width = width
          out.height = height
          const ctx = out.getContext('2d', { willReadFrequently: true })!
          if (overlay.current) ctx.drawImage(overlay.current, 0, 0)
          const pixels = ctx.getImageData(0, 0, width, height)
          overlayToMask(pixels.data)
          ctx.putImageData(pixels, 0, 0)
          return out.toDataURL('image/png').split(',')[1]
        },
        undo() {
          const previous = undoStack.current.pop()
          if (!previous) return
          overlayCtx().putImageData(previous, 0, 0)
          // The restored canvas may cross the empty/non-empty line (undoing
          // the first stroke on a blank proposal, or undoing a slider refetch
          // that replaced a mask with nothing) — without this the "paint it
          // by hand" caption sticks to the state before the undo.
          emptyCb.current?.(
            !previous.data.some((_v, i) => i % 4 === 3 && previous.data[i] > 0),
          )
          redraw()
        },
        reset() {
          loadMask()
        },
      }),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [width, height, redraw, loadMask],
    )

    function pointFrom(e: React.PointerEvent<HTMLCanvasElement>) {
      const rect = e.currentTarget.getBoundingClientRect()
      return canvasPoint(rect, width, height, e.clientX, e.clientY)
    }

    function strokeCtx() {
      const ctx = overlayCtx()
      ctx.globalCompositeOperation =
        tool.current.mode === 'eraser' ? 'destination-out' : 'source-over'
      ctx.fillStyle = `rgb(${TINT.r}, ${TINT.g}, ${TINT.b})`
      ctx.strokeStyle = ctx.fillStyle
      ctx.lineWidth = tool.current.brush * 2
      ctx.lineCap = 'round'
      ctx.lineJoin = 'round'
      return ctx
    }

    return (
      <canvas
        ref={viewRef}
        className="wm-canvas"
        width={width}
        height={height}
        onPointerDown={(e) => {
          // Left/first-touch only. A right-click would otherwise paint a blob
          // AND open the context menu, which swallows the pointerup and leaves
          // the stroke armed; a second finger would hijack the first one's
          // stroke state.
          if (e.button !== 0 || !e.isPrimary) return
          e.currentTarget.setPointerCapture(e.pointerId)
          if (!loaded.current) {
            // Painting before the proposal lands: the canvas now means what
            // the user drew, and letting the pending proposal land would
            // erase it. Their strokes win; Reset mask fetches it back.
            loadToken.current++
            loaded.current = true
            readyCb.current?.(true)
          }
          if (tool.current.mode === 'brush') emptyCb.current?.(false)
          pushUndo()
          drawing.current = true
          const pt = pointFrom(e)
          last.current = pt
          const ctx = strokeCtx()
          ctx.beginPath()
          ctx.arc(pt.x, pt.y, tool.current.brush, 0, Math.PI * 2)
          ctx.fill()
          ctx.globalCompositeOperation = 'source-over'
          redraw()
        }}
        onPointerMove={(e) => {
          if (!e.isPrimary) return
          const pt = pointFrom(e)
          hover.current = pt
          if (drawing.current && last.current) {
            const ctx = strokeCtx()
            ctx.beginPath()
            ctx.moveTo(last.current.x, last.current.y)
            ctx.lineTo(pt.x, pt.y)
            ctx.stroke()
            ctx.globalCompositeOperation = 'source-over'
            last.current = pt
          }
          redraw()
        }}
        onPointerUp={(e) => {
          if (!e.isPrimary) return
          drawing.current = false
          last.current = null
        }}
        // Without this a cancelled stroke (system gesture, focus loss) would
        // leave drawing=true, and the next buttonless move would paint.
        onPointerCancel={(e) => {
          if (!e.isPrimary) return
          drawing.current = false
          last.current = null
        }}
        onPointerLeave={() => {
          drawing.current = false
          last.current = null
          hover.current = null
          redraw()
        }}
      />
    )
  },
)

export default MaskEditor
