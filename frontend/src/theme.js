// Color-mode control: light / dark / auto. `auto` follows the system
// (prefers-color-scheme); an explicit choice is stamped as data-theme on
// <html> and persisted. The initial value is applied before React mounts by
// a tiny script in index.html, so there's no flash of the wrong theme.

import { useCallback, useEffect, useState } from 'react'

const KEY = 'toolkit-theme'
export const MODES = ['auto', 'light', 'dark']

export function getStoredMode() {
  const stored = localStorage.getItem(KEY)
  return MODES.includes(stored) ? stored : 'auto'
}

export function applyMode(mode) {
  const root = document.documentElement
  if (mode === 'auto') {
    // Remove the override so the prefers-color-scheme media query governs.
    root.removeAttribute('data-theme')
  } else {
    root.setAttribute('data-theme', mode)
  }
}

// Hook for the toggle: current mode + a setter that persists and applies.
export function useTheme() {
  const [mode, setModeState] = useState(getStoredMode)

  const setMode = useCallback((next) => {
    localStorage.setItem(KEY, next)
    applyMode(next)
    setModeState(next)
  }, [])

  // Keep in sync if another tab changes the preference.
  useEffect(() => {
    const onStorage = (e) => {
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
