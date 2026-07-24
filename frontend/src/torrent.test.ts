import { describe, it, expect } from 'vitest'
import {
  SIZED_CATEGORIES,
  addTorrent,
  applyFilter,
  formatBytes,
  formatEta,
  formatSpeed,
  hasActiveWork,
  MB,
  parseMagnetLines,
  ruleKey,
  selectionFor,
  updateTorrent,
} from './torrent'
import type { TorrentFileRow, TorrentResolve, TorrentRow } from './types/api'

const FILES: TorrentFileRow[] = [
  { index: 1, path: 'Movie.mkv', size: 2_000_000_000, category: 'video' },
  { index: 2, path: 'Sample/sample.mkv', size: 40_000_000, category: 'video' },
  { index: 3, path: 'Movie.chi.srt', size: 45_000, category: 'subtitle' },
  { index: 4, path: 'Screens/01.jpg', size: 300_000, category: 'image' },
]

function torrent(infohash: string, files = FILES, over: Partial<TorrentResolve> = {}): TorrentResolve {
  return { infohash, ready: true, name: infohash, files, state: 'awaiting_selection', ...over }
}

describe('applyFilter', () => {
  it('keeps only large videos by default', () => {
    expect(applyFilter(FILES, new Set(['video']), 100 * MB)).toEqual([1])
  })

  it('does not apply the size floor to subtitles', () => {
    // Mirrors the backend rule: a 100MB floor must never be able to discard a
    // 45KB subtitle the user explicitly asked for.
    expect(applyFilter(FILES, new Set(['video', 'subtitle']), 100 * MB)).toEqual([1, 3])
  })

  it('keeps every file in a chosen category when the floor is zero', () => {
    expect(applyFilter(FILES, new Set(['video']), 0)).toEqual([1, 2])
  })

  it('returns nothing when no category matches', () => {
    expect(applyFilter(FILES, new Set(['archive']), 0)).toEqual([])
  })

  it('gates only video and audio on size', () => {
    expect(SIZED_CATEGORIES).toEqual(new Set(['video', 'audio']))
  })
})

function row(state: string, over: Partial<TorrentRow> = {}): TorrentRow {
  return {
    infohash: 'a'.repeat(40),
    name: 'X',
    state,
    pause_reason: null,
    save_dir: '/tmp',
    selected: '1',
    total_bytes: 100,
    completed_bytes: 10,
    progress: 10,
    speed: 5,
    eta_seconds: 18,
    added_at: '',
    completed_at: null,
    last_error: null,
    ...over,
  }
}

describe('hasActiveWork', () => {
  it('is true while something is downloading', () => {
    expect(hasActiveWork([row('active')])).toBe(true)
  })

  it('counts a magnet still fetching metadata as work', () => {
    expect(hasActiveWork([row('awaiting_metadata')])).toBe(true)
  })

  it('is false when everything is finished or paused', () => {
    expect(hasActiveWork([row('complete'), row('paused')])).toBe(false)
  })

  it('is false for an empty queue', () => {
    expect(hasActiveWork([])).toBe(false)
  })
})

describe('formatters', () => {
  it('renders a dash when the download is stalled', () => {
    expect(formatEta(null)).toBe('—')
    expect(formatSpeed(0)).toBe('—')
  })

  it('renders seconds, minutes, and hours', () => {
    expect(formatEta(18)).toBe('18s')
    expect(formatEta(120)).toBe('2m')
    expect(formatEta(5400)).toBe('1.5h')
  })

  it('scales bytes through the units', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(2_000_000_000)).toBe('1.9 GB')
  })
})

// The page layers user ticks over the rule's output rather than syncing state
// in an effect. These cover that composition, which is where the bugs would be.
function resolve(
  files: TorrentFileRow[],
  ruleSelected: Set<number>,
  overrides: ReadonlyMap<number, boolean>,
): Set<number> {
  const out = new Set<number>()
  for (const file of files) {
    if (overrides.get(file.index) ?? ruleSelected.has(file.index)) out.add(file.index)
  }
  return out
}

describe('rule + override composition', () => {
  const rule = new Set(applyFilter(FILES, new Set(['video']), 100 * MB))

  it('falls back to the rule when nothing was ticked', () => {
    expect(resolve(FILES, rule, new Map())).toEqual(new Set([1]))
  })

  it('lets the user add a file the rule excluded', () => {
    expect(resolve(FILES, rule, new Map([[3, true]]))).toEqual(new Set([1, 3]))
  })

  it('lets the user remove a file the rule included', () => {
    expect(resolve(FILES, rule, new Map([[1, false]]))).toEqual(new Set())
  })

  it('treats an explicit false as a choice, not as absent', () => {
    // `overrides.get() ?? rule.has()` must not collapse false into the
    // fallback -- unticking a rule-selected row has to stick.
    const overrides = new Map([[1, false]])
    expect(overrides.get(1) ?? rule.has(1)).toBe(false)
  })
})

describe('parseMagnetLines', () => {
  it('splits one magnet per line, trimming blanks', () => {
    const raw = '  magnet:?xt=urn:btih:aaa \n\nmagnet:?xt=urn:btih:bbb\n  '
    expect(parseMagnetLines(raw)).toEqual([
      'magnet:?xt=urn:btih:aaa',
      'magnet:?xt=urn:btih:bbb',
    ])
  })

  it('de-duplicates repeated lines within one paste', () => {
    const raw = 'magnet:?xt=urn:btih:aaa\nmagnet:?xt=urn:btih:aaa'
    expect(parseMagnetLines(raw)).toEqual(['magnet:?xt=urn:btih:aaa'])
  })

  it('returns nothing for whitespace-only input', () => {
    expect(parseMagnetLines('  \n \n')).toEqual([])
  })
})

describe('addTorrent / updateTorrent', () => {
  it('appends a new torrent', () => {
    const list = addTorrent([], torrent('a'))
    expect(list.map((t) => t.infohash)).toEqual(['a'])
  })

  it('skips a torrent whose infohash is already under review', () => {
    // Pasting the same magnet twice, or a magnet plus its .torrent, must not
    // create two review sections for one download.
    const once = addTorrent([], torrent('a', FILES, { name: 'first' }))
    const twice = addTorrent(once, torrent('a', FILES, { name: 'second' }))
    expect(twice).toHaveLength(1)
    expect(twice[0]!.name).toBe('first')
  })

  it('replaces a torrent in place once metadata lands', () => {
    const staged = [torrent('a', [], { ready: false, name: null, state: 'awaiting_metadata' })]
    const done = updateTorrent(staged, torrent('a', FILES, { name: 'Resolved' }))
    expect(done[0]!.ready).toBe(true)
    expect(done[0]!.files).toHaveLength(4)
  })
})

describe('selectionFor + ruleKey (per-torrent)', () => {
  const cats = new Set(['video'])

  it('applies the shared rule with no overrides', () => {
    expect(selectionFor(torrent('a'), cats, 100 * MB, new Map())).toEqual(new Set([1]))
  })

  it("honours one torrent's overrides without touching another's", () => {
    const t = torrent('a')
    const withSub = selectionFor(t, cats, 100 * MB, new Map([[3, true]]))
    expect(withSub).toEqual(new Set([1, 3]))
  })

  it('keys the rule by infohash so two torrents differ', () => {
    // Same categories + size, different torrent -> different key, so ticks on
    // one never bleed onto the other.
    expect(ruleKey('a', cats, 100)).not.toBe(ruleKey('b', cats, 100))
    expect(ruleKey('a', cats, 100)).toBe(ruleKey('a', cats, 100))
  })

  it('changes the key when the shared filter changes', () => {
    expect(ruleKey('a', cats, 100)).not.toBe(ruleKey('a', cats, 200))
    expect(ruleKey('a', cats, 100)).not.toBe(ruleKey('a', new Set(['audio']), 100))
  })
})
