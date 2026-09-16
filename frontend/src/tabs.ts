export function parseToolSlug(pathname: string): string | null {
  const m = /^\/tools\/([^/]+)\/?$/.exec(pathname)
  return m ? m[1] : null
}

export function tabOrder<T extends string>(open: T[], jobSlugs: T[]): T[] {
  return [...new Set([...open, ...jobSlugs])]
}

/** Right neighbour, else left, else null. */
export function nextTabAfterClose<T extends string>(tabs: T[], closing: T): T | null {
  const idx = tabs.indexOf(closing)
  if (idx === -1) return null
  const rest = tabs.filter((s) => s !== closing)
  return rest.length ? rest[Math.min(idx, rest.length - 1)] : null
}

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
