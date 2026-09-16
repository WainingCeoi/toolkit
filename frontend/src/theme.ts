// Color mode. index.html applies the stored value before first paint; keep the key in sync.

import { useCallback, useEffect, useState } from 'react'

const KEY = 'toolkit-theme'
export const MODES = ['auto', 'light', 'dark'] as const

export type ThemeMode = (typeof MODES)[number]

function isMode(value: string | null): value is ThemeMode {
  return value !== null && (MODES as readonly string[]).includes(value)
}

export function getStoredMode(): ThemeMode {
  // localStorage can throw when storage is blocked.
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
    root.removeAttribute('data-theme')
  } else {
    root.setAttribute('data-theme', mode)
  }
}

// Explicit tuple return: inference would widen it to an array of the union.
export function useTheme(): [ThemeMode, (next: ThemeMode) => void] {
  const [mode, setModeState] = useState<ThemeMode>(getStoredMode)

  const setMode = useCallback((next: ThemeMode) => {
    try {
      localStorage.setItem(KEY, next)
    } catch {
      /* storage blocked */
    }
    applyMode(next)
    setModeState(next)
  }, [])

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
