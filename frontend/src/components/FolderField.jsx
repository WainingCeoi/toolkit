// Editable path field + native Browse dialog (the folder_field successor).
// Typing/pasting works everywhere; Browse opens the macOS chooser through the
// backend (same machine as the server by design).

import React, { useState } from 'react'
import { api } from '../api'
import Button from './Button'

export default function FolderField({ label, value, onChange, placeholder, startDir }) {
  const [busy, setBusy] = useState(false)

  async function browse() {
    setBusy(true)
    try {
      const { path } = await api.pickFolder(value || startDir)
      if (path) onChange(path)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="field">
      <span className="label">{label}</span>
      <div className="row">
        <input
          className="control grow"
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          spellCheck={false}
        />
        <Button onClick={browse} disabled={busy} loading={busy}>
          📂 Browse…
        </Button>
      </div>
    </div>
  )
}
