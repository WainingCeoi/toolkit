import { describe, it, expect } from 'vitest'
import {
  DEFAULT_SAVE_DIR,
  SIZED_CATEGORIES,
  addTorrent,
  applyFilter,
  formatBytes,
  magnetLink,
  MB,
  parseMagnetLines,
  retryableSend,
  ruleKey,
  selectionFor,
  truncateMiddle,
  updateTorrent,
  windowedRun,
} from './torrent'
import { ApiError } from './api'
import type { TorrentFileRow, TorrentResolve } from './types/api'

const FILES: TorrentFileRow[] = [
  { index: 1, path: 'Movie.mkv', size: 2_000_000_000, category: 'video' },
  { index: 2, path: 'Sample/sample.mkv', size: 40_000_000, category: 'video' },
  { index: 3, path: 'Movie.chi.srt', size: 45_000, category: 'subtitle' },
  { index: 4, path: 'Screens/01.jpg', size: 300_000, category: 'image' },
]

function torrent(infohash: string, files = FILES, over: Partial<TorrentResolve> = {}): TorrentResolve {
  return { infohash, ready: true, name: infohash, files, state: 'awaiting_selection', ...over }
}

describe('DEFAULT_SAVE_DIR', () => {
  it('is ~/Downloads, matching the backend default', () => {
    expect(DEFAULT_SAVE_DIR).toBe('~/Downloads')
  })
})

describe('applyFilter', () => {
  it('keeps only large videos by default', () => {
    expect(applyFilter(FILES, new Set(['video']), 100 * MB)).toEqual([1])
  })

  it('does not apply the size floor to subtitles', () => {
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

describe('formatters', () => {
  it('scales bytes through the units', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(2_000_000_000)).toBe('1.9 GB')
  })
})

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
    expect(ruleKey('a', cats, 100)).not.toBe(ruleKey('b', cats, 100))
    expect(ruleKey('a', cats, 100)).toBe(ruleKey('a', cats, 100))
  })

  it('changes the key when the shared filter changes', () => {
    expect(ruleKey('a', cats, 100)).not.toBe(ruleKey('a', cats, 200))
    expect(ruleKey('a', cats, 100)).not.toBe(ruleKey('a', new Set(['audio']), 100))
  })
})

describe('truncateMiddle', () => {
  it('leaves anything that already fits alone', () => {
    expect(truncateMiddle('Movie.mkv', 44)).toBe('Movie.mkv')
    expect(truncateMiddle('exactly-ten', 11)).toBe('exactly-ten')
  })

  it('keeps the head AND the tail, so the extension survives', () => {
    const long = 'Some.Very.Long.Release.Name.2024.2160p.WEB-DL.DDP5.1.HDR.x265.mkv'
    const cut = truncateMiddle(long, 30)
    expect(cut).toHaveLength(30)
    expect(cut).toContain('…')
    expect(cut.startsWith('Some.Very.Long')).toBe(true)
    expect(cut.endsWith('.mkv')).toBe(true)
  })

  it('never returns more than the budget', () => {
    for (const max of [5, 6, 7, 12, 41, 44]) {
      expect(truncateMiddle('x'.repeat(200), max).length).toBe(max)
    }
  })

  it('gives up rather than emitting an ellipsis with nothing around it', () => {
    expect(truncateMiddle('abcdefgh', 4)).toBe('abcdefgh')
  })

  it('distinguishes two names that differ only in their tail', () => {
    const a = 'The.Show.S01E01.1080p.WEB.h264-ALPHA.mkv'
    const b = 'The.Show.S01E01.1080p.WEB.h264-BRAVO.mp4'
    // Shared 24-char prefix: end-truncation would render these identically.
    expect(a.slice(0, 24)).toBe(b.slice(0, 24))
    expect(truncateMiddle(a, 24)).not.toBe(truncateMiddle(b, 24))
  })
})

describe('magnetLink', () => {
  it('builds a link a client can actually take', () => {
    expect(magnetLink('c9e15763f722f23e98a29decdfae341b98d53056')).toBe(
      'magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056',
    )
  })

  it('adds the display name when there is one', () => {
    expect(magnetLink('abc', 'Example Release')).toBe(
      'magnet:?xt=urn:btih:abc&dn=Example%20Release',
    )
  })

  it('escapes a name that would otherwise break the query', () => {
    expect(magnetLink('abc', 'A&B=C')).toBe('magnet:?xt=urn:btih:abc&dn=A%26B%3DC')
  })

  it('omits dn entirely rather than emitting an empty one', () => {
    expect(magnetLink('abc', null)).toBe('magnet:?xt=urn:btih:abc')
    expect(magnetLink('abc', '')).toBe('magnet:?xt=urn:btih:abc')
  })
})

describe('retryableSend', () => {
  it('retries a 503 — BitComet busy or unreachable clears up on its own', () => {
    expect(retryableSend(new ApiError('BitComet is not reachable: read timeout', 503))).toBe(true)
  })

  it('retries a network-level failure that never got an answer', () => {
    expect(retryableSend(new TypeError('Failed to fetch'))).toBe(true)
  })

  it('does not retry a request BitComet answered and rejected', () => {
    expect(retryableSend(new ApiError('this torrent has no file 7', 400))).toBe(false)
    expect(retryableSend(new ApiError('BitComet no longer has this torrent.', 404))).toBe(false)
  })
})

describe('windowedRun', () => {
  const tick = () => new Promise<void>((r) => setTimeout(r, 0))

  function harness(count: number) {
    const started: number[] = []
    const finish = new Map<number, () => void>()
    const items = Array.from({ length: count }, (_, i) => i)
    const run = (i: number) =>
      new Promise<void>((resolve) => {
        started.push(i)
        finish.set(i, resolve)
      })
    return { items, run, started, finish }
  }

  // Top-ups mint new resolvers mid-flight, so one sweep is never enough.
  async function drain(done: Promise<void>, finish: Map<number, () => void>) {
    let settled = false
    void done.then(() => {
      settled = true
    })
    for (let i = 0; i < 20 && !settled; i++) {
      for (const f of finish.values()) f()
      await tick()
    }
    await done
  }

  it('fires the first window together, not one at a time', async () => {
    const { items, run, started, finish } = harness(25)
    const done = windowedRun(items, run, 10)
    expect(started).toHaveLength(10)
    await drain(done, finish)
  })

  it('tops up one-for-one, keeping the window pinned full', async () => {
    const { items, run, started, finish } = harness(25)
    const done = windowedRun(items, run, 10)
    expect(started).toHaveLength(10)

    finish.get(0)!()
    await tick()
    expect(started).toHaveLength(11)

    finish.get(1)!()
    finish.get(2)!()
    await tick()
    expect(started).toHaveLength(13)

    await drain(done, finish)
    expect(started).toHaveLength(25)
  })

  it('never exceeds the window', async () => {
    const { items, run, started, finish } = harness(25)
    const done = windowedRun(items, run, 10)
    // launched minus released is the live in-flight count.
    let released = 0
    let settledFlag = false
    void done.then(() => {
      settledFlag = true
    })
    while (!settledFlag) {
      expect(started.length - released).toBeLessThanOrEqual(10)
      const next = finish.get(released)
      if (next) {
        next()
        released += 1
      }
      await tick()
    }
    await done
    expect(started).toHaveLength(25)
  })

  it('resolves only when every item has settled', async () => {
    const { items, run, finish } = harness(3)
    let settled = false
    const done = windowedRun(items, run, 10).then(() => {
      settled = true
    })
    finish.get(0)!()
    finish.get(1)!()
    await tick()
    expect(settled).toBe(false)
    finish.get(2)!()
    await done
    expect(settled).toBe(true)
  })

  it('keeps pumping when a task rejects — failures are the callback business', async () => {
    const seen: number[] = []
    await windowedRun(
      [1, 2, 3],
      (i) => {
        seen.push(i)
        return i === 2 ? Promise.reject(new Error('boom')) : Promise.resolve()
      },
      2,
    )
    expect(seen).toEqual([1, 2, 3])
  })

  it('resolves immediately for an empty list', async () => {
    await windowedRun([], () => Promise.resolve(), 10)
  })
})
