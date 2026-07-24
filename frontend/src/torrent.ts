// Torrent Downloader pure helpers. Kept out of the page file so that module
// exports components only — react-refresh cannot hot-reload a file that mixes
// the two. Same split as jobs.ts / JobsProvider.tsx.

import type { TorrentFileRow, TorrentRow } from './types/api'

// Mirrors toolkit_engine/filetypes.py SIZED_CATEGORIES. Duplicated on purpose:
// this drives the live preview as boxes are ticked, before any round trip. The
// backend re-derives the same answer and stays authoritative.
export const SIZED_CATEGORIES = new Set(['video', 'audio'])

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
