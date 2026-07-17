// Upload zone: click or drag files in, list them, remove one, clear on run.

import React, { useRef, useState } from 'react'

function fmtSize(bytes) {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

export default function FileDrop({ accept, files, onChange, hint }) {
  const input = useRef(null)
  const [drag, setDrag] = useState(false)

  function addFiles(list) {
    onChange([...files, ...Array.from(list)])
  }

  return (
    <div className="field">
      <div
        className={`filedrop ${drag ? 'drag' : ''}`}
        onClick={() => input.current.click()}
        onKeyDown={(e) => e.key === 'Enter' && input.current.click()}
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
              <span>{f.name} · {fmtSize(f.size)}</span>
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
