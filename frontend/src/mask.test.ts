import { describe, it, expect } from 'vitest'
import { canvasPoint, maskToOverlay, overlayToMask, TINT } from './mask'

// One RGBA pixel per call site keeps the fixtures readable.
const px = (r: number, g: number, b: number, a: number) =>
  new Uint8ClampedArray([r, g, b, a])

describe('mask pixel conversions', () => {
  it('turns wire-mask white into opaque tint and black into transparent', () => {
    const white = maskToOverlay(px(255, 255, 255, 255))
    expect([...white]).toEqual([TINT.r, TINT.g, TINT.b, 255])

    const black = maskToOverlay(px(0, 0, 0, 255))
    expect(black[3]).toBe(0)
  })

  it('thresholds mid grays at 127 like the backend does', () => {
    expect(maskToOverlay(px(128, 128, 128, 255))[3]).toBe(255)
    expect(maskToOverlay(px(127, 127, 127, 255))[3]).toBe(0)
  })

  it('exports painted overlay pixels as opaque white-on-black', () => {
    const painted = overlayToMask(px(TINT.r, TINT.g, TINT.b, 255))
    expect([...painted]).toEqual([255, 255, 255, 255])

    const empty = overlayToMask(px(0, 0, 0, 0))
    expect([...empty]).toEqual([0, 0, 0, 255])
  })

  it('drops half-erased pixels below the alpha threshold', () => {
    // destination-out erasing leaves fractional alpha behind; anything at or
    // below 127 must export as "keep", not "remove".
    expect(overlayToMask(px(TINT.r, TINT.g, TINT.b, 127))[0]).toBe(0)
    expect(overlayToMask(px(TINT.r, TINT.g, TINT.b, 128))[0]).toBe(255)
  })

  it('roundtrips: propose -> overlay -> export is identity for binary masks', () => {
    const wire = new Uint8ClampedArray([
      ...px(255, 255, 255, 255),
      ...px(0, 0, 0, 255),
    ])
    const roundtripped = overlayToMask(maskToOverlay(new Uint8ClampedArray(wire)))
    expect([...roundtripped]).toEqual([...wire])
  })
})

describe('canvasPoint', () => {
  it('maps CSS coordinates back to image pixels on a scaled canvas', () => {
    // A 1000×500 canvas displayed at 250×125 (4x downscale).
    const rect = { left: 10, top: 20, width: 250, height: 125 }
    expect(canvasPoint(rect, 1000, 500, 10, 20)).toEqual({ x: 0, y: 0 })
    expect(canvasPoint(rect, 1000, 500, 260, 145)).toEqual({ x: 1000, y: 500 })
    expect(canvasPoint(rect, 1000, 500, 135, 82.5)).toEqual({ x: 500, y: 250 })
  })
})
