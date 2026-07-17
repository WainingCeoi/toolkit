// Three-way color-mode switch (Auto / Light / Dark), rendered as a small
// segmented control in the rail. Auto follows the system.

import React from 'react'
import { useTheme, MODES } from '../theme'

const ICON = { auto: '◐', light: '☀', dark: '☾' }
const LABEL = { auto: 'Auto', light: 'Light', dark: 'Dark' }

export default function ThemeToggle() {
  const [mode, setMode] = useTheme()
  return (
    <div className="theme-toggle" role="group" aria-label="Color mode">
      {MODES.map((m) => (
        <button
          key={m}
          type="button"
          className={`theme-opt ${mode === m ? 'active' : ''}`}
          aria-pressed={mode === m}
          title={`${LABEL[m]} mode`}
          onClick={() => setMode(m)}
        >
          <span aria-hidden="true">{ICON[m]}</span>
          <span className="theme-opt-label">{LABEL[m]}</span>
        </button>
      ))}
    </div>
  )
}
