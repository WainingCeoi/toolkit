// Color-mode control: light / dark / auto. `auto` follows the system
// (prefers-color-scheme); an explicit choice is stamped as data-theme on
// <html> and persisted. The initial value is applied before React mounts by
// a tiny script in index.html, so there's no flash of the wrong theme.

import { useCallback, useEffect, useState } from 'react'

const KEY = 'toolkit-theme'
export const MODES = ['auto', 'light', 'dark'] as const

export type ThemeMode = (typeof MODES)[number]

// Narrows the arbitrary string localStorage hands back to a known mode; a
// hand-edited or stale value falls through to 'auto' rather than being stamped
// onto <html> as-is.
function isMode(value: string | null): value is ThemeMode {
  return value !== null && (MODES as readonly string[]).includes(value)
}

export function getStoredMode(): ThemeMode {
  // localStorage can throw when storage is blocked (private mode, strict
  // cookie settings) — fall back to 'auto' rather than white-screening, the
  // same defensive pattern index.html already uses for this key.
  try {
    const stored = localStorage.getItem(KEY)
    return isMode(stored) ? stored : 'auto'
  } catch {
    return 'auto'
  }
}

export function applyMode(mode: ThemeMode): void {
  const root = document.documentElement
  if (mode === 'auto') {
    // Remove the override so the prefers-color-scheme media query governs.
    root.removeAttribute('data-theme')
  } else {
    root.setAttribute('data-theme', mode)
  }
}

// Hook for the toggle: current mode + a setter that persists and applies.
// The tuple return is explicit — inference would widen it to an array of the
// union, and `const [mode, setMode] = useTheme()` would lose both types.
export function useTheme(): [ThemeMode, (next: ThemeMode) => void] {
  const [mode, setModeState] = useState<ThemeMode>(getStoredMode)

  const setMode = useCallback((next: ThemeMode) => {
    try {
      localStorage.setItem(KEY, next)
    } catch {
      /* storage blocked — apply for this session without persisting */
    }
    applyMode(next)
    setModeState(next)
  }, [])

  // Keep in sync if another tab changes the preference.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === KEY) {
        const next = getStoredMode()
        applyMode(next)
        setModeState(next)
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  return [mode, setMode]
}
