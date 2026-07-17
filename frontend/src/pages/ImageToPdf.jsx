// Image to PDF: combine uploaded images (PNG/JPG/HEIC) into a single PDF,
// returned as a direct download — no job, one synchronous request.

import React, { useState } from 'react'
import { api, saveBlob } from '../api'
import FileDrop from '../components/FileDrop'

export default function ImageToPdf() {
  const [name, setName] = useState('')
  const [files, setFiles] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(null)

  async function convert() {
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
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="page-head"><h1>🖼️ Image to PDF</h1></div>
      <p className="page-sub">
        Combine selected images into a single PDF, downloaded straight to your
        browser.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>NAME THE PDF</span></div>
          <div className="field">
            <label htmlFor="imgpdf-name">Output PDF file name</label>
            <input
              id="imgpdf-name"
              className="control"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. scanned_docs"
            />
          </div>

          <div className="step"><span className="n">02</span><span>SELECT IMAGES</span></div>
          <FileDrop
            accept=".png,.jpg,.jpeg,.heic"
            files={files}
            onChange={setFiles}
            hint="Drop images here or click to choose (.png, .jpg, .jpeg, .heic)"
          />
          <div className="note info">
            HEIC photos from iPhone are supported. Pages are sorted by filename,
            so rename files if you need a specific page order.
          </div>

          <div className="step"><span className="n">03</span><span>CONVERT</span></div>
          <button
            type="button"
            className="btn primary"
            onClick={convert}
            disabled={busy}
          >
            {busy ? 'Converting…' : 'Convert to PDF'}
          </button>
          {error && <div className="note error">{error}</div>}
          {saved && <div className="note ok">✅ Saved {saved}</div>}
        </div>
      </div>
    </div>
  )
}
