import { useRef, useState } from 'react'
import Icon from './Icon'

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

  function addFiles(list: FileList | null) {
    if (!list) return
    onChange([...files, ...Array.from(list)])
  }

  return (
    <div className="field">
      <div
        className={`filedrop ${drag ? 'drag' : ''}`}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.current?.click() }
        }}
        role="button"
        aria-label={hint || 'Choose files to upload'}
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
        <span className="upload-icon"><Icon name="upload" size={23} /></span>
        <span>{hint || 'Drop files here or click to choose'}</span>
        <span className="filedrop-caption">Choose files from your device</span>
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
              <button type="button" aria-label={`Remove ${f.name}`} onClick={() => onChange(files.filter((_, j) => j !== i))}>
                <Icon name="close" size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
