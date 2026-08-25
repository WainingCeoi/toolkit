// Watermark Remover mask pixel math, kept pure so it can be unit-tested
// without a canvas implementation (jsdom has none).
//
// Two pixel formats meet in the editor:
// - the wire mask: an opaque black/white PNG, white = remove (what the
//   backend proposes and what it expects back), and
// - the overlay: what is shown over the image — the theme red at full alpha
//   where masked, fully transparent elsewhere, composited at reduced opacity.

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
