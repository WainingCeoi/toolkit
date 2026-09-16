// Canvas mask preview; the proposal is exported at native resolution, shown scaled down.

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react'
import { maskToOverlay, overlayToMask } from '../mask'

export interface MaskPreviewHandle {
  /** Bare base64 PNG (white = remove), or null while the proposal is still loading. */
  exportMask(): string | null
}

interface MaskPreviewProps {
  imageUrl: string
  maskUrl: string
  width: number
  height: number
  /** Fires false while a proposal is in flight, true once one has landed. */
  onReady?: (ready: boolean) => void
  /** Fires true when the landed proposal marks nothing at all. */
  onEmpty?: (empty: boolean) => void
  /** Fires true when a proposal could not be loaded; the card still shows the previous one. */
  onError?: (failed: boolean) => void
}

// The canvas is only ever CSS-scaled into a panel, and 20 native-resolution ones exhaust the GPU.
const MAX_VIEW_SIDE = 1600

const MaskPreview = forwardRef<MaskPreviewHandle, MaskPreviewProps>(
  function MaskPreview(
    { imageUrl, maskUrl, width, height, onReady, onEmpty, onError },
    ref,
  ) {
    const viewRef = useRef<HTMLCanvasElement>(null)
    const image = useRef<HTMLImageElement | null>(null)
    const overlay = useRef<HTMLCanvasElement | null>(null)
    const exported = useRef<string | null>(null)
    const loadToken = useRef(0)
    // Refs keep inline parent callbacks out of the effect deps.
    const readyCb = useRef(onReady)
    readyCb.current = onReady
    const emptyCb = useRef(onEmpty)
    emptyCb.current = onEmpty
    const errorCb = useRef(onError)
    errorCb.current = onError

    const scale = Math.min(1, MAX_VIEW_SIDE / Math.max(width, height))
    const viewW = Math.max(1, Math.round(width * scale))
    const viewH = Math.max(1, Math.round(height * scale))

    const overlayCtx = () => {
      if (!overlay.current) {
        overlay.current = document.createElement('canvas')
        overlay.current.width = viewW
        overlay.current.height = viewH
      }
      return overlay.current.getContext('2d')!
    }

    const redraw = useCallback(() => {
      const view = viewRef.current
      const ctx = view?.getContext('2d')
      if (!view || !ctx) return
      ctx.clearRect(0, 0, viewW, viewH)
      if (image.current) ctx.drawImage(image.current, 0, 0, viewW, viewH)
      if (overlay.current) {
        ctx.globalAlpha = 0.45
        ctx.drawImage(overlay.current, 0, 0)
        ctx.globalAlpha = 1
      }
    }, [viewW, viewH])

    useEffect(() => {
      const token = ++loadToken.current
      readyCb.current?.(false)
      errorCb.current?.(false)
      const img = new Image()
      img.src = maskUrl
      // decode() keeps it off the main thread; drawImage would decode inline instead.
      img.decode().then(
        () => {
          if (token !== loadToken.current) return
          const probe = document.createElement('canvas')
          probe.width = width
          probe.height = height
          const probeCtx = probe.getContext('2d', { willReadFrequently: true })!
          probeCtx.drawImage(img, 0, 0)
          const pixels = probeCtx.getImageData(0, 0, width, height)
          const marked = maskToOverlay(pixels.data)
          probeCtx.putImageData(pixels, 0, 0)
          const ctx = overlayCtx()
          ctx.clearRect(0, 0, viewW, viewH)
          ctx.drawImage(probe, 0, 0, viewW, viewH)
          // Encoded here, not in the Remove handler: a full-resolution PNG costs ~0.3 s.
          overlayToMask(pixels.data)
          probeCtx.putImageData(pixels, 0, 0)
          exported.current = probe.toDataURL('image/png').split(',')[1]
          readyCb.current?.(true)
          emptyCb.current?.(!marked)
          redraw()
        },
        () => {
          if (token !== loadToken.current) return
          readyCb.current?.(exported.current !== null)
          errorCb.current?.(true)
        },
      )
      return () => {
        // Bumping the live counter is the point, not a stale-ref bug.
        // eslint-disable-next-line react-hooks/exhaustive-deps
        loadToken.current++
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [maskUrl, width, height, viewW, viewH, redraw])

    useEffect(() => {
      let alive = true
      const img = new Image()
      img.src = imageUrl
      img.decode().then(
        () => {
          if (!alive) return
          image.current = img
          redraw()
        },
        () => {},
      )
      return () => {
        alive = false
      }
    }, [imageUrl, redraw])

    useImperativeHandle(ref, () => ({ exportMask: () => exported.current }), [])

    return (
      <canvas ref={viewRef} className="wm-canvas" width={viewW} height={viewH} />
    )
  },
)

export default MaskPreview
