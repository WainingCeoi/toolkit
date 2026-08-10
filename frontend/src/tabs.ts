// Pure logic for the bottom tab dock: which tabs exist, in what order, and
// where focus lands when one closes. Router-free so it tests as plain
// functions.

/** Tool slug from a location pathname, or null when not a tool route. */
export function parseToolSlug(pathname: string): string | null {
  // Trailing slash tolerated: the old exact-route table accepted it, and
  // bookmarks/hand-typed links carry it.
  const m = /^\/tools\/([^/]+)\/?$/.exec(pathname)
  return m ? m[1] : null
}

/** Open tabs in opening order, then tools that only have tracked jobs. */
export function tabOrder<T extends string>(open: T[], jobSlugs: T[]): T[] {
  // Set-dedupe: jobSlugs carries one entry per JOB, so two tracked jobs of
  // the same tool must still collapse to one tab.
  return [...new Set([...open, ...jobSlugs])]
}

/**
 * The tab to activate after closing `closing`: its right neighbour, else the
 * left one, else null (no tabs left — caller goes home). Mirrors browsers.
 */
export function nextTabAfterClose<T extends string>(tabs: T[], closing: T): T | null {
  const idx = tabs.indexOf(closing)
  if (idx === -1) return null
  const rest = tabs.filter((s) => s !== closing)
  return rest.length ? rest[Math.min(idx, rest.length - 1)] : null
}

/** Rehydrate the persisted open-tab list, dropping junk and unknown slugs. */
export function restoreTabs<T extends string>(
  raw: string | null,
  isKnown: (slug: string) => slug is T,
): T[] {
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    const seen = new Set<string>()
    return parsed.filter((s): s is T => {
      if (typeof s !== 'string' || !isKnown(s) || seen.has(s)) return false
      seen.add(s)
      return true
    })
  } catch {
    return []
  }
}
