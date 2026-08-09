// Torrent Downloader pure helpers. Kept out of the page file so that module
// exports components only — react-refresh cannot hot-reload a file that mixes
// the two. Same split as jobs.ts / JobsProvider.tsx.

import { ApiError } from './api'
import type { TorrentFileRow, TorrentResolve } from './types/api'

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

// Release names are long, uniform and front-loaded with the same words, so a
// column of them wraps to three lines each and still reads as identical. This
// keeps the head — which is what the user recognises — and the TAIL, because
// the extension and the episode/quality tag live there and are exactly what
// distinguishes one row from the next. Plain end-truncation would throw both
// away. Callers pair it with a `title` holding the untruncated string.
export function truncateMiddle(text: string, max = 44): string {
  // Ellipsis + at least one character each side; below that there is nothing
  // meaningful left to show and the full string is shorter anyway.
  if (max < 5 || text.length <= max) return text
  const head = Math.ceil((max - 1) / 2)
  const tail = max - 1 - head
  return `${text.slice(0, head)}…${text.slice(text.length - tail)}`
}

// A magnet link for a torrent this app already knows the infohash of.
//
// Used when handing a FAILED torrent back to the user. The original pasted
// magnet is preferred where it survives (it carries trackers, which a bare
// infohash does not), but a torrent that came from a .torrent file never had
// one — and after a failure the infohash is the only durable handle on it, so
// reconstructing the minimal form beats offering nothing to copy.
export function magnetLink(infohash: string, name?: string | null): string {
  const dn = name ? `&dn=${encodeURIComponent(name)}` : ''
  return `magnet:?xt=urn:btih:${infohash}${dn}`
}

// Run `run` over `items` with WINDOWED concurrency: fire `window` of them
// together, then top up with the next `window` only once fewer than `lowWater`
// are still in flight.
//
// This is the middle ground between the two obvious shapes, both of which were
// measured failing against a real BitComet. Strictly sequential sending spends
// the whole batch waiting on round-trip latency even when the client is
// healthy; firing everything at once buries a client that does real work per
// task (allocate, hash-check, reach the swarm) and turns the tail of the batch
// into timeouts. The window self-regulates: a busy client answers slowly, so
// in-flight stays above the low-water mark and no new work is added until most
// of the current batch has been answered.
//
// In-flight can briefly exceed `window` (a top-up fires while up to
// `lowWater - 1` stragglers are still out), which is deliberate — the
// stragglers are exactly the sends a grinding client is sitting on, and
// holding the whole next batch hostage to them is the sequential mistake in
// miniature.
//
// `run` owns its failures: a rejection is swallowed here (it only marks the
// slot free), so report-or-retry decisions stay with the caller's callback.
export async function windowedRun<T>(
  items: readonly T[],
  run: (item: T) => Promise<void>,
  window: number,
  lowWater: number,
): Promise<void> {
  const pending = [...items]
  const total = pending.length
  if (total === 0) return
  let inFlight = 0
  let settled = 0
  await new Promise<void>((resolve) => {
    const launch = () => {
      for (const item of pending.splice(0, window)) {
        inFlight += 1
        void run(item)
          .catch(() => undefined)
          .finally(() => {
            inFlight -= 1
            settled += 1
            if (pending.length > 0 && inFlight < lowWater) launch()
            if (settled === total) resolve()
          })
      }
    }
    launch()
  })
}

// Whether a failed send is worth trying again without changing anything.
//
// A 503 is "BitComet did not answer" — unreachable, or timing out while it
// grinds through the batch of tasks it was just handed — which is exactly the
// failure that clears up on its own; measured live, it is what most of a batch
// send's failures are. Anything that is not an ApiError never got an answer at
// all (the network failed mid-request), same treatment. A 400 or 404 is a fact
// about the request — bad selection, torrent gone — and repeats identically no
// matter how many times it is retried.
//
// Retrying a send is SAFE because the operation is idempotent end to end:
// set_priority twice is a no-op, and `start` on an already-running task
// answers "skipped", which the backend counts as success. So a send that
// timed out client-side but actually landed simply succeeds on the retry.
export function retryableSend(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 503
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
