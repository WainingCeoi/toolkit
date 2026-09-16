// sessionStorage can throw (private mode, blocked storage, quota), so every access is guarded.

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
