// Web Images to PDF — drive one live Chrome session, capture into a PDF.
// The flow IS the page: 01 open the URL, 02 scroll the real Chrome window,
// 03 capture. All endpoints are synchronous (no job stream); a 3s status
// poll keeps the page honest about the single browser session, even if the
// page was reloaded or Chrome was closed by hand.

import React, { useEffect, useRef, useState } from 'react'
import { api, artifactUrl } from '../api'

export default function WebImagesToPdf() {
  const [url, setUrl] = useState('')
  const [open, setOpen] = useState(false)
  const [opening, setOpening] = useState(false)
  const [capturing, setCapturing] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const busy = useRef(false) // pause the poll while a request is in flight

  // Independent status region: poll every 3s while mounted so a session
  // opened before a reload (or closed out-of-band) is reflected here.
  useEffect(() => {
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
  }, [])

  const openBrowser = async () => {
    setError(null)
    setResult(null)
    setOpening(true)
    busy.current = true
    try {
      await api.webpdfOpen(url.trim())
      setOpen(true)
    } catch (err) {
      setError(err.message) // 409 already open / 502 could not open
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
      // 400 "no images" leaves the browser open for a retry; 409/502 too.
      setError(err.message)
    } finally {
      setCapturing(false)
      busy.current = false
    }
  }

  const closeBrowser = async () => {
    setError(null)
    busy.current = true
    try {
      await api.webpdfClose()
      setOpen(false)
    } catch (err) {
      setError(err.message)
    } finally {
      busy.current = false
    }
  }

  return (
    <div>
      <div className="page-head"><h1>🌐 Web Images to PDF</h1></div>
      <p className="page-sub">
        Open a web page in a real Chrome window, scroll until every image has
        loaded, then capture them into a single PDF — with bookmarks when the
        page provides them. Requires Google Chrome. One browser session at a
        time.
      </p>

      <div className="station">
        <div>
          <div className="panel">
            <div className="step"><span className="n">01</span><span>Open</span></div>
            <div className="field">
              <label htmlFor="wipdf-url">Page URL</label>
              <input
                id="wipdf-url"
                className="control"
                type="url"
                placeholder="https://…"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                disabled={open || opening}
              />
            </div>
            <div className="row">
              <button
                type="button"
                className="btn"
                onClick={openBrowser}
                disabled={open || opening || !url.trim()}
              >
                {opening ? 'Launching Chrome…' : '🌐 Open in browser'}
              </button>
            </div>
          </div>

          <div className="panel">
            <div className="step"><span className="n">02</span><span>Scroll in Chrome</span></div>
            {open ? (
              <div className="note info">
                Chrome is open. <strong>Scroll down in that window until every
                page and image has loaded</strong>, then capture in step 03.
              </div>
            ) : (
              <div className="note info">
                No browser session — open a page in step 01 first. This step
                happens in the real Chrome window, not here.
              </div>
            )}
          </div>
        </div>

        <div>
          <div className="panel">
            <div className="step"><span className="n">03</span><span>Capture</span></div>
            <div className="row">
              <button
                type="button"
                className="btn primary"
                onClick={captureAndBuild}
                disabled={!open || capturing}
              >
                {capturing ? 'Capturing…' : '📸 Capture & build PDF'}
              </button>
              <button
                type="button"
                className="btn ghost"
                onClick={closeBrowser}
                disabled={!open || capturing}
              >
                ✖ Close browser
              </button>
            </div>

            {capturing && (
              <div className="note info">
                Capturing page &amp; downloading images… this can take a while
                on long pages.
              </div>
            )}

            {error && <div className="note error">{error}</div>}

            {result && (
              <>
                <div className="note ok">
                  Saved <strong>{result.pages}</strong>-page PDF →{' '}
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
                <a className="btn" href={artifactUrl(result.artifact_id)}>
                  ⬇ Download PDF
                </a>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
