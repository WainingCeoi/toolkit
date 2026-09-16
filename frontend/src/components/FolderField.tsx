// Browse opens the native chooser on the server's machine, not the browser's.

import { useState } from 'react'
import { api } from '../api'
import Button from './Button'

interface FolderFieldProps {
  label: string
  value: string
  onChange: (path: string) => void
  placeholder?: string
  startDir?: string
  /** Let the chooser select a bundle (*.photoslibrary) as if it were a folder. */
  packages?: boolean
}

export default function FolderField({
  label,
  value,
  onChange,
  placeholder,
  startDir,
  packages = false,
}: FolderFieldProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function browse() {
    setBusy(true)
    setError(null)
    try {
      const { path } = await api.pickFolder(value || startDir, packages)
      if (path) onChange(path)
    } catch (err) {
      setError((err as Error).message || 'Could not open the folder picker.')
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
      {error && (
        <div className="note error" style={{ marginTop: 6 }}>
          {error}
        </div>
      )}
    </div>
  )
}
