// Upload zone: click or drag files in, list them, remove one, clear on run.

import { useRef, useState } from 'react'

function fmtSize(bytes: number): string {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

interface FileDropProps {
  accept?: string
  files: File[]
  onChange: (files: File[]) => void
  hint?: string
}

export default function FileDrop({ accept, files, onChange, hint }: FileDropProps) {
  const input = useRef<HTMLInputElement>(null)
  const [drag, setDrag] = useState(false)

  // FileList is null on an input the user dismissed without choosing; the JS
  // version relied on Array.from(null) never being reached in practice.
  function addFiles(list: FileList | null) {
    if (!list) return
    onChange([...files, ...Array.from(list)])
  }

  return (
    <div className="field">
      <div
        className={`filedrop ${drag ? 'drag' : ''}`}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => e.key === 'Enter' && input.current?.click()}
        role="button"
        tabIndex={0}
        onDragOver={(e) => {
          e.preventDefault()
          setDrag(true)
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDrag(false)
          addFiles(e.dataTransfer.files)
        }}
      >
        {hint || 'Drop files here or click to choose'}
        <input
          ref={input}
          type="file"
          multiple
          accept={accept}
          onChange={(e) => {
            addFiles(e.target.files)
            e.target.value = ''
          }}
        />
      </div>
      {files.length > 0 && (
        <ul className="filelist">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`}>
              <span>
                {f.name} · {fmtSize(f.size)}
              </span>
              <button type="button" onClick={() => onChange(files.filter((_, j) => j !== i))}>
                remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
