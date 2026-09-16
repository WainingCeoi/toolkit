// Pure mask pixel math (jsdom has no canvas). Wire mask is opaque black/white, white = remove.

/** The theme's --red, duplicated by hand. */
export const TINT = { r: 255, g: 107, b: 94 }

/** Wire mask -> overlay tint, in place; returns true if anything is masked at all. */
export function maskToOverlay(pixels: Uint8ClampedArray): boolean {
  let marked = false
  for (let i = 0; i < pixels.length; i += 4) {
    const masked = pixels[i] > 127 // grayscale, so any channel serves
    pixels[i] = TINT.r
    pixels[i + 1] = TINT.g
    pixels[i + 2] = TINT.b
    pixels[i + 3] = masked ? 255 : 0
    if (masked) marked = true
  }
  return marked
}

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
