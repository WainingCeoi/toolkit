// Guarded sessionStorage access.
//
// Storage can throw outright — Safari private mode, hardened browser profiles,
// a full quota — and everything stored here (open tabs, tracked job ids) is a
// convenience that must never take the app down with it. Reads fall back to
// null, writes are best-effort.
//
// sessionStorage, not local: each browser tab is its own workbench, and a
// fresh session starts clean.

export function readSession(key: string): string | null {
  try {
    return sessionStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeSession(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value)
  } catch {
    /* best-effort only */
  }
}

/** Parse a persisted JSON array, or [] for missing/corrupt/non-array data. */
export function readSessionArray(key: string): unknown[] {
  const raw = readSession(key)
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}
