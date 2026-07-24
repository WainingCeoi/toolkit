// Torrent Downloader pure helpers. Kept out of the page file so that module
// exports components only — react-refresh cannot hot-reload a file that mixes
// the two. Same split as jobs.ts / JobsProvider.tsx.

import type { TorrentFileRow, TorrentResolve, TorrentRow } from './types/api'

// Mirrors toolkit_engine/filetypes.py SIZED_CATEGORIES. Duplicated on purpose:
// this drives the live preview as boxes are ticked, before any round trip. The
// backend re-derives the same answer and stays authoritative.
export const SIZED_CATEGORIES = new Set(['video', 'audio'])

// Mirrors DEFAULT_SAVE_DIR in backend/src/toolkit_api/torrents.py — the default
// destination, shown in this tidy tilde form; the backend expands it.
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

export const ACTIVE_STATES = new Set(['active', 'queued', 'awaiting_metadata'])

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
      // The floor gates video and audio only, so a 100MB minimum can never
      // discard the 40KB subtitle sitting next to the film.
      if (SIZED_CATEGORIES.has(f.category) && f.size < minBytes) return false
      return true
    })
    .map((f) => f.index)
}

export function hasActiveWork(rows: TorrentRow[]): boolean {
  return rows.some((r) => ACTIVE_STATES.has(r.state))
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

export function formatEta(seconds: number | null): string {
  if (seconds === null) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

export function formatSpeed(bytesPerSecond: number): string {
  return bytesPerSecond > 0 ? `${formatBytes(bytesPerSecond)}/s` : '—'
}

// One magnet per line: trimmed, blanks dropped, de-duplicated within the paste.
// Kept pure so the "paste ten magnets" parsing is unit-tested, not eyeballed.
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

// Append a freshly resolved torrent, skipping one whose infohash is already in
// the review list -- pasting the same magnet twice, or a magnet plus its
// .torrent, must not create two review sections for one download.
export function addTorrent(list: TorrentResolve[], t: TorrentResolve): TorrentResolve[] {
  return list.some((x) => x.infohash === t.infohash) ? list : [...list, t]
}

// Replace a torrent in place once its metadata has landed (magnet poll result).
export function updateTorrent(
  list: TorrentResolve[],
  t: TorrentResolve,
): TorrentResolve[] {
  return list.map((x) => (x.infohash === t.infohash ? t : x))
}

// The selection for one torrent: the filter rule, with the user's per-file
// ticks layered over it. Ticks are tagged with the rule they were made against
// (infohash + categories + size), so changing the shared filter drops stale
// ticks for every torrent at once. Mirrors the single-torrent logic that was
// inlined in the page before multi-resolve.
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
