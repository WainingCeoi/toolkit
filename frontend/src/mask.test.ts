import { describe, it, expect } from 'vitest'
import { maskToOverlay, overlayToMask, TINT } from './mask'

const px = (r: number, g: number, b: number, a: number) =>
  new Uint8ClampedArray([r, g, b, a])

describe('mask pixel conversions', () => {
  it('turns wire-mask white into opaque tint and black into transparent', () => {
    const white = px(255, 255, 255, 255)
    maskToOverlay(white)
    expect([...white]).toEqual([TINT.r, TINT.g, TINT.b, 255])

    const black = px(0, 0, 0, 255)
    maskToOverlay(black)
    expect(black[3]).toBe(0)
  })

  it('thresholds mid grays at 127 like the backend does', () => {
    const light = px(128, 128, 128, 255)
    maskToOverlay(light)
    expect(light[3]).toBe(255)

    const dark = px(127, 127, 127, 255)
    maskToOverlay(dark)
    expect(dark[3]).toBe(0)
  })

  it('reports whether the proposal marks anything at all', () => {
    expect(maskToOverlay(px(0, 0, 0, 255))).toBe(false)
    expect(maskToOverlay(px(127, 127, 127, 255))).toBe(false)
    expect(maskToOverlay(px(255, 255, 255, 255))).toBe(true)

    const mixed = new Uint8ClampedArray([
      ...px(0, 0, 0, 255),
      ...px(255, 255, 255, 255),
    ])
    expect(maskToOverlay(mixed)).toBe(true)
  })

  it('exports painted overlay pixels as opaque white-on-black', () => {
    const painted = overlayToMask(px(TINT.r, TINT.g, TINT.b, 255))
    expect([...painted]).toEqual([255, 255, 255, 255])

    const empty = overlayToMask(px(0, 0, 0, 0))
    expect([...empty]).toEqual([0, 0, 0, 255])
  })

  it('drops half-erased pixels below the alpha threshold', () => {
    expect(overlayToMask(px(TINT.r, TINT.g, TINT.b, 127))[0]).toBe(0)
    expect(overlayToMask(px(TINT.r, TINT.g, TINT.b, 128))[0]).toBe(255)
  })

  it('roundtrips: propose -> overlay -> export is identity for binary masks', () => {
    const wire = new Uint8ClampedArray([
      ...px(255, 255, 255, 255),
      ...px(0, 0, 0, 255),
    ])
    const buffer = new Uint8ClampedArray(wire)
    maskToOverlay(buffer)
    expect([...overlayToMask(buffer)]).toEqual([...wire])
  })
})
