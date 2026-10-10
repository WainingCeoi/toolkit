import { useEffect, useRef, useState } from 'react'
import { api, artifactUrl } from '../api'
import Button from '../components/Button'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import { useToolActive, useToolBusy } from '../toolHost'
import type { WebPdfCapture } from '../types/api'

export default function WebImagesToPdf() {
  const [url, setUrl] = useState('')
  const [open, setOpen] = useState(false)
  const [opening, setOpening] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [closing, setClosing] = useState(false)
  const [result, setResult] = useState<WebPdfCapture | null>(null)
  const [error, setError] = useState<string | null>(null)
  const busy = useRef(false)
  const active = useToolActive()

  // Capture has no job registry entry: closing the tab mid-run would drop the artifact id.
  useToolBusy(opening || capturing || closing)

  // Polls only while the tab is visible: keep-alive keeps hidden pages mounted.
  useEffect(() => {
    if (!active) return
    let alive = true
    const tick = async () => {
      if (busy.current) return
      try {
        const { open: isOpen } = await api.webpdfStatus()
        if (alive) setOpen(isOpen)
      } catch {
        /* backend unreachable — keep the last known state */
      }
    }
    tick()
    const timer = setInterval(tick, 3000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [active])

  const openBrowser = async () => {
    setError(null)
    setResult(null)
    setOpening(true)
    busy.current = true
    try {
      await api.webpdfOpen(url.trim())
      setOpen(true)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setOpening(false)
      busy.current = false
    }
  }

  const captureAndBuild = async () => {
    setError(null)
    setCapturing(true)
    busy.current = true
    try {
      const res = await api.webpdfCapture()
      setResult(res)
      setOpen(false) // a successful capture also closes the browser
    } catch (err) {
      // Errors leave the browser open for a retry.
      setError((err as Error).message)
    } finally {
      setCapturing(false)
      busy.current = false
    }
  }

  const closeBrowser = async () => {
    setError(null)
    setClosing(true)
    busy.current = true
    try {
      await api.webpdfClose()
      setOpen(false)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setClosing(false)
      busy.current = false
    }
  }

  return (
    <div>
      <ToolHeader slug="web-images-to-pdf" />
      <p className="page-sub">
        Open a page in Chrome, load its images, and capture a single PDF.
        Bookmarks are included when the page provides them.
      </p>

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Open your page</span></div>
          <div className="field">
            <label htmlFor="wipdf-url">Page URL</label>
            <input
              id="wipdf-url"
              className="control"
              type="url"
              placeholder="https://…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={open || opening || capturing || closing}
            />
          </div>
          <div className="row">
            <Button
              onClick={openBrowser}
              loading={opening}
              disabled={open || capturing || closing || !url.trim()}
            >
              <Icon name="globe" size={17} /> Open in Chrome
            </Button>
          </div>
          <p className="hint">Requires Google Chrome · One browser session at a time</p>

          <div className="step"><span className="n">02</span><span>Load all images</span></div>
          {open ? (
            <div className="note info">
              Chrome is open. <strong>Scroll down in that window until every
              page and image has loaded</strong>, then capture your PDF.
            </div>
          ) : (
            <div className="note info">
              After opening your page, scroll through it in the real Chrome
              window so every image has time to load.
            </div>
          )}
        </div>

        <div className="panel result-panel">
          <div className="step"><span className="n">03</span><span>Capture &amp; download</span></div>
          <div className="action-summary">
            <strong>{open ? 'Chrome session is ready' : 'Open a page to begin'}</strong>
            <p>{open ? 'Capture after every image has loaded in Chrome.' : 'Your capture will combine the page images into one PDF.'}</p>
          </div>
          <div className="row">
            <Button
              variant="primary"
              onClick={captureAndBuild}
              loading={capturing}
              disabled={!open || opening || closing}
            >
              <Icon name="image" size={17} /> Capture &amp; create PDF
            </Button>
            <Button
              variant="ghost"
              onClick={closeBrowser}
              loading={closing}
              disabled={!open || opening || capturing}
            >
              Close Chrome
            </Button>
          </div>

          {capturing && (
            <div className="note info">
              Capturing page &amp; downloading images… this can take a while
              on long pages.
            </div>
          )}

          {error && <div className="note error">{error}</div>}

          <div className="step"><span>Result</span></div>
          {result && (
            <>
              <div className="note ok">
                Created a <strong>{result.pages}</strong>-page PDF:{' '}
                <code>{result.name}</code>
              </div>
              {result.skipped > 0 && (
                <div className="note warn">
                  {result.skipped} image(s) couldn&rsquo;t be downloaded and
                  were skipped.
                </div>
              )}
              {result.warn && (
                <div className="note warn">Bookmarks skipped: {result.warn}</div>
              )}
              <Button as="a" href={artifactUrl(result.artifact_id)}>
                <Icon name="download" size={17} /> Download PDF
              </Button>
            </>
          )}
          {!result && !capturing && (
            <EmptyState icon="globe" title="Your captured PDF will appear here">
              Open a page, load its images in Chrome, then capture to download the PDF.
            </EmptyState>
          )}
        </div>
      </div>
    </div>
  )
}
