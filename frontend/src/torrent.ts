// Torrent Downloader pure helpers, kept out of the page module for Fast Refresh.

import { ApiError } from './api'
import type { TorrentFileRow, TorrentResolve } from './types/api'

// Mirrors SIZED_CATEGORIES in toolkit_engine/filetypes.py; update both.
export const SIZED_CATEGORIES = new Set(['video', 'audio'])

// Mirrors DEFAULT_SAVE_DIR in the backend; update both.
export const DEFAULT_SAVE_DIR = '~/Downloads'

export const CATEGORIES: { key: string; label: string }[] = [
  { key: 'video', label: 'Video' },
  { key: 'audio', label: 'Audio' },
  { key: 'image', label: 'Images' },
  { key: 'subtitle', label: 'Subtitles' },
  { key: 'document', label: 'Documents' },
  { key: 'archive', label: 'Archives' },
  { key: 'other', label: 'Other' },
]

export const MB = 1024 * 1024

/** 1-based indices to download: in a ticked category, and big enough. */
export function applyFilter(
  files: TorrentFileRow[],
  categories: Set<string>,
  minBytes: number,
): number[] {
  return files
    .filter((f) => {
      if (!categories.has(f.category)) return false
      if (SIZED_CATEGORIES.has(f.category) && f.size < minBytes) return false
      return true
    })
    .map((f) => f.index)
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = n / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`
}

// Keeps head and tail: the extension and episode tag at the end are what distinguish rows.
export function truncateMiddle(text: string, max = 44): string {
  // Below 5 there is nothing meaningful around the ellipsis.
  if (max < 5 || text.length <= max) return text
  const head = Math.ceil((max - 1) / 2)
  const tail = max - 1 - head
  return `${text.slice(0, head)}…${text.slice(text.length - tail)}`
}

// Minimal magnet (no trackers) for a torrent known only by infohash.
export function magnetLink(infohash: string, name?: string | null): string {
  const dn = name ? `&dn=${encodeURIComponent(name)}` : ''
  return `magnet:?xt=urn:btih:${infohash}${dn}`
}

// Keeps `window` items in flight; rejections are swallowed, the caller's callback reports them.
export async function windowedRun<T>(
  items: readonly T[],
  run: (item: T) => Promise<void>,
  window: number,
): Promise<void> {
  const pending = [...items]
  const total = pending.length
  if (total === 0) return
  let inFlight = 0
  let settled = 0
  await new Promise<void>((resolve) => {
    const launch = () => {
      while (inFlight < window && pending.length > 0) {
        const item = pending.shift()!
        inFlight += 1
        void run(item)
          .catch(() => undefined)
          .finally(() => {
            inFlight -= 1
            settled += 1
            if (pending.length > 0) launch()
            if (settled === total) resolve()
          })
      }
    }
    launch()
  })
}

// 503 (BitComet did not answer) and network failures are transient; a resend is idempotent.
export function retryableSend(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 503
}

export function parseMagnetLines(raw: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (trimmed && !seen.has(trimmed)) {
      seen.add(trimmed)
      out.push(trimmed)
    }
  }
  return out
}

export function addTorrent(list: TorrentResolve[], t: TorrentResolve): TorrentResolve[] {
  return list.some((x) => x.infohash === t.infohash) ? list : [...list, t]
}

export function updateTorrent(
  list: TorrentResolve[],
  t: TorrentResolve,
): TorrentResolve[] {
  return list.map((x) => (x.infohash === t.infohash ? t : x))
}

export function selectionFor(
  t: TorrentResolve,
  categories: Set<string>,
  minBytes: number,
  overrides: ReadonlyMap<number, boolean>,
): Set<number> {
  const rule = new Set(applyFilter(t.files, categories, minBytes))
  const out = new Set<number>()
  for (const file of t.files) {
    if (overrides.get(file.index) ?? rule.has(file.index)) out.add(file.index)
  }
  return out
}

export function ruleKey(infohash: string, categories: Set<string>, minMb: number): string {
  return JSON.stringify([infohash, [...categories].sort(), minMb])
}
