// Canvas mask editor: the image with its proposed mask tinted red on top.
//
// The mask lives on an offscreen canvas at native image resolution — the
// visible canvas only composites image + overlay (and the brush cursor), so
// nothing is ever resampled. Pointer strokes paint (or erase, via
// destination-out) round-capped lines onto the overlay; export thresholds the
// overlay back into the black/white PNG the backend expects. When maskUrl
// changes (the sensitivity slider), the fresh proposal REPLACES the overlay,
// with the previous state pushed onto the undo stack so a slider nudge is
// never destructive — except for the very first proposal, which has no
// previous state worth restoring (see loadMask).

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
   * The approved mask as bare base64 PNG (white = remove), or null while the
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
}

const MaskEditor = forwardRef<MaskEditorHandle, MaskEditorProps>(
  function MaskEditor({ imageUrl, maskUrl, width, height, brush, mode, onReady }, ref) {
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
    // Held in a ref so a parent passing an inline callback cannot re-trigger
    // the mask fetch on every render.
    const readyCb = useRef(onReady)
    readyCb.current = onReady
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
      // of the one the user asked for afterwards.
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
        redraw()
      }
      img.onerror = () => {
        if (token !== loadToken.current) return
        readyCb.current?.(loaded.current)
      }
      img.src = maskUrl
      return () => {
        // Abandon this load; a later one (or none) wins.
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
          e.currentTarget.setPointerCapture(e.pointerId)
          if (!loaded.current) {
            // Painting before the proposal lands: the canvas now means what
            // the user drew, and letting the pending proposal land would
            // erase it. Their strokes win; Reset mask fetches it back.
            loadToken.current++
            loaded.current = true
            readyCb.current?.(true)
          }
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
        onPointerUp={() => {
          drawing.current = false
          last.current = null
        }}
        // Without this a cancelled stroke (system gesture, focus loss) would
        // leave drawing=true, and the next buttonless move would paint.
        onPointerCancel={() => {
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
