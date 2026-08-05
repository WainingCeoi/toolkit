// Watermark Remover mask pixel math, kept pure so it can be unit-tested
// without a canvas implementation (jsdom has none).
//
// Two pixel formats meet in the editor:
// - the wire mask: an opaque black/white PNG, white = remove (what the
//   backend proposes and what it expects back), and
// - the overlay: what the person actually paints — the theme red at full
//   alpha where masked, fully transparent elsewhere, composited over the
//   image at reduced opacity.

/** The overlay tint — the theme's --red, kept in sync by eye, not by import. */
export const TINT = { r: 255, g: 107, b: 94 }

/** Wire-mask RGBA pixels (opaque white-on-black) -> red overlay pixels, in place. */
export function maskToOverlay(pixels: Uint8ClampedArray): Uint8ClampedArray {
  for (let i = 0; i < pixels.length; i += 4) {
    const masked = pixels[i] > 127 // grayscale, so any channel serves
    pixels[i] = TINT.r
    pixels[i + 1] = TINT.g
    pixels[i + 2] = TINT.b
    pixels[i + 3] = masked ? 255 : 0
  }
  return pixels
}

/** Overlay RGBA pixels -> opaque black/white wire-mask pixels, in place. */
export function overlayToMask(pixels: Uint8ClampedArray): Uint8ClampedArray {
  for (let i = 0; i < pixels.length; i += 4) {
    const value = pixels[i + 3] > 127 ? 255 : 0
    pixels[i] = value
    pixels[i + 1] = value
    pixels[i + 2] = value
    pixels[i + 3] = 255
  }
  return pixels
}

/**
 * Client (CSS) coordinates -> canvas pixel coordinates. The canvas renders at
 * the image's native resolution but is CSS-scaled to fit its panel, so pointer
 * events need mapping back into image pixels before they touch the mask.
 */
export function canvasPoint(
  rect: { left: number; top: number; width: number; height: number },
  width: number,
  height: number,
  clientX: number,
  clientY: number,
): { x: number; y: number } {
  return {
    x: (clientX - rect.left) * (width / rect.width),
    y: (clientY - rect.top) * (height / rect.height),
  }
}
