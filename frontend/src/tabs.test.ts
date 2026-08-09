import { describe, it, expect } from 'vitest'
import { nextTabAfterClose, parseToolSlug, restoreTabs, tabOrder } from './tabs'

const KNOWN = new Set(['remux', 'magnet-scraper', 'cache-purge'])
const isKnown = (s: string): s is string => KNOWN.has(s)

describe('parseToolSlug', () => {
  it('extracts the slug from a tool route', () => {
    expect(parseToolSlug('/tools/remux')).toBe('remux')
    expect(parseToolSlug('/tools/magnet-scraper')).toBe('magnet-scraper')
  })

  it('returns null for home, unknown routes, and nested paths', () => {
    expect(parseToolSlug('/')).toBeNull()
    expect(parseToolSlug('/nope')).toBeNull()
    expect(parseToolSlug('/tools/')).toBeNull()
    expect(parseToolSlug('/tools/remux/extra')).toBeNull()
  })
})

describe('tabOrder', () => {
  it('keeps opening order and appends job-only tools', () => {
    expect(tabOrder(['a', 'b'], ['b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('passes open tabs through when every job tool is already open', () => {
    expect(tabOrder(['a', 'b'], ['a'])).toEqual(['a', 'b'])
    expect(tabOrder([], [])).toEqual([])
  })
})

describe('nextTabAfterClose', () => {
  it('prefers the right neighbour, like a browser', () => {
    expect(nextTabAfterClose(['a', 'b', 'c'], 'b')).toBe('c')
    expect(nextTabAfterClose(['a', 'b', 'c'], 'a')).toBe('b')
  })

  it('falls back to the left neighbour at the end of the row', () => {
    expect(nextTabAfterClose(['a', 'b', 'c'], 'c')).toBe('b')
  })

  it('returns null when the row empties or the tab is unknown', () => {
    expect(nextTabAfterClose(['a'], 'a')).toBeNull()
    expect(nextTabAfterClose(['a', 'b'], 'z')).toBeNull()
  })
})

describe('restoreTabs', () => {
  it('rehydrates a persisted list', () => {
    expect(restoreTabs('["remux","cache-purge"]', isKnown)).toEqual(['remux', 'cache-purge'])
  })

  it('drops unknown slugs, non-strings, and duplicates', () => {
    expect(restoreTabs('["remux","gone",7,"remux"]', isKnown)).toEqual(['remux'])
  })

  it('survives junk: null, malformed JSON, and non-array shapes', () => {
    expect(restoreTabs(null, isKnown)).toEqual([])
    expect(restoreTabs('not json', isKnown)).toEqual([])
    expect(restoreTabs('{"a":1}', isKnown)).toEqual([])
  })
})
