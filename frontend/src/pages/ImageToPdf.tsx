import { useState } from 'react'
import { api, saveBlob } from '../api'
import FileDrop from '../components/FileDrop'
import Button from '../components/Button'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import { useToolBusy } from '../toolHost'

export default function ImageToPdf() {
  const [name, setName] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const outputName = /\.pdf$/i.test(name.trim()) ? name.trim() : `${name.trim()}.pdf`

  useToolBusy(busy)

  async function convert() {
    if (!name.trim() || files.length === 0 || busy) return
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      const form = new FormData()
      form.append('name', name)
      files.forEach((f) => form.append('files', f))
      const { blob, filename } = await api.imgToPdf(form)
      saveBlob(blob, filename)
      setSaved(filename)
      setFiles([])
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <ToolHeader slug="image-to-pdf" />
      <p className="page-sub">
        Combine images into a single PDF and download it to your browser.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Name your PDF</span></div>
          <div className="field">
            <label htmlFor="imgpdf-name">Output PDF file name</label>
            <input
              id="imgpdf-name"
              className="control"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. scanned_docs"
              disabled={busy}
            />
          </div>

          <div className="step"><span className="n">02</span><span>Add images</span></div>
          <FileDrop
            accept=".png,.jpg,.jpeg,.heic"
            files={files}
            onChange={setFiles}
            hint="Drop images here or browse files"
          />
          <p className="hint">PNG, JPG, JPEG, and HEIC · Multiple files supported</p>
          <div className="note info">
            Pages follow filename order. Rename your files to set the sequence.
            HEIC photos from iPhone are supported.
          </div>
        </div>

        <div className="panel result-panel">
          <div className="step"><span className="n">03</span><span>Create PDF</span></div>
          <div className="action-summary">
            <strong>{files.length > 0 ? `${files.length} image${files.length === 1 ? '' : 's'} selected` : 'Add images to begin'}</strong>
            <p>{name.trim() ? `Output: ${outputName}` : 'Choose an output name and add your images.'}</p>
          </div>
          <Button
            variant="primary"
            onClick={convert}
            loading={busy}
            disabled={!name.trim() || files.length === 0}
          >
            {busy ? 'Creating PDF…' : 'Create PDF'}
          </Button>
          {error && <div className="note error">{error}</div>}
          <div className="step"><span>Result</span></div>
          {saved ? (
            <div className="note ok">Downloaded <strong>{saved}</strong> to your browser.</div>
          ) : busy ? (
            <div className="note info">Combining your images. The PDF will download when it is ready.</div>
          ) : (
            <EmptyState icon="image" title="Your PDF will appear here">
              Create your PDF to download it automatically. Each image becomes a page.
            </EmptyState>
          )}
        </div>
      </div>
    </div>
  )
}
