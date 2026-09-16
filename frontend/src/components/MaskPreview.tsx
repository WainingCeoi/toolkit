// Canvas mask preview; the mask is kept at native resolution offscreen and exported as-shown.

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
}

const MaskPreview = forwardRef<MaskPreviewHandle, MaskPreviewProps>(
  function MaskPreview({ imageUrl, maskUrl, width, height, onReady, onEmpty }, ref) {
    const viewRef = useRef<HTMLCanvasElement>(null)
    const image = useRef<HTMLImageElement | null>(null)
    const overlay = useRef<HTMLCanvasElement | null>(null)
    const loaded = useRef(false)
    const loadToken = useRef(0)
    // Refs keep inline parent callbacks out of the effect deps.
    const readyCb = useRef(onReady)
    readyCb.current = onReady
    const emptyCb = useRef(onEmpty)
    emptyCb.current = onEmpty

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
    }, [width, height])

    useEffect(() => {
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
        // Bumping the live counter is the point, not a stale-ref bug.
        // eslint-disable-next-line react-hooks/exhaustive-deps
        loadToken.current++
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [maskUrl, width, height, redraw])

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
      }),
      [width, height],
    )

    return (
      <canvas ref={viewRef} className="wm-canvas" width={width} height={height} />
    )
  },
)

export default MaskPreview
