// Optimized-IP Subscription Generator — paste nodes + optimized IPs, get
// Shadowrocket / Clash / Surge subscription links, files, and a QR code.
// The subs API is synchronous (no job), so this page manages its own
// request state instead of useToolJob.

import React, { useCallback, useEffect, useState } from 'react'
import { api, saveBlob } from '../api'
import Button from '../components/Button'
import CodeBox from '../components/CodeBox'

const PREVIEW_COLS = ['name', 'type', 'server', 'port', 'host', 'sni', 'network', 'tls']

export default function Subscription() {
  // inputs
  const [nodeLinks, setNodeLinks] = useState('')
  const [preferredIps, setPreferredIps] = useState('')
  const [namePrefix, setNamePrefix] = useState('')
  const [keepHost, setKeepHost] = useState(true)

  // result panel
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  // per-target download failure (e.g. Surge can't express vless nodes) — the
  // old page showed the render reason on a disabled button; here it appears
  // inline under the download row.
  const [dlError, setDlError] = useState(null)

  // history (loads independently of any generate)
  const [history, setHistory] = useState([])
  const [historyError, setHistoryError] = useState(null)

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await api.subsHistory())
      setHistoryError(null)
    } catch (err) {
      setHistoryError(err.message)
    }
  }, [])

  useEffect(() => {
    refreshHistory()
  }, [refreshHistory])

  async function generate() {
    setBusy(true)
    setError(null)
    try {
      const res = await api.subsGenerate({
        node_links: nodeLinks,
        preferred_ips: preferredIps,
        name_prefix: namePrefix,
        keep_original_host: keepHost,
      })
      setResult(res)
      setDlError(null)
      refreshHistory()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function download(target) {
    setDlError(null)
    try {
      const { blob, filename } = await api.subsDownload(result.sub_id, target)
      saveBlob(blob, filename)
    } catch (err) {
      setDlError(`${target}: ${err.message}`)
    }
  }

  async function loadSub(id) {
    setError(null)
    setDlError(null)
    try {
      setResult(await api.subsGet(id))
    } catch (err) {
      setError(err.message) // "That subscription no longer exists."
      refreshHistory()
    }
  }

  async function deleteSub(id) {
    setError(null)
    try {
      await api.subsDelete(id)
      setResult((cur) => (cur && cur.sub_id === id ? null : cur))
      refreshHistory()
    } catch (err) {
      setError(err.message)
    }
  }

  const count = (v) => (v === null || v === undefined ? '—' : v)

  return (
    <div>
      <div className="page-head">
        <h1>🛰️ Optimized-IP Subscription Generator</h1>
      </div>
      <p className="page-sub">
        Paste your self-built nodes plus optimized IPs to generate Shadowrocket /
        Clash / Surge subscription links and files. Everything is stored in a
        local SQLite database.
      </p>

      <div className="station">
        {/* -------------------------------------------------- inputs (left) */}
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Original node links</span></div>
          <div className="field">
            <label htmlFor="sub-nodes">Node links, one per line</label>
            <textarea
              id="sub-nodes"
              className="control"
              rows={6}
              placeholder="Paste original node links here"
              value={nodeLinks}
              onChange={(e) => setNodeLinks(e.target.value)}
            />
          </div>

          <div className="step"><span className="n">02</span><span>Optimized IPs / domains</span></div>
          <div className="field">
            <label htmlFor="sub-ips">Optimized addresses, one per line</label>
            <textarea
              id="sub-ips"
              className="control"
              rows={6}
              placeholder="Paste optimized IPs / domains here, one per line"
              value={preferredIps}
              onChange={(e) => setPreferredIps(e.target.value)}
            />
          </div>

          <div className="step"><span className="n">03</span><span>Options</span></div>
          <div className="field">
            <label htmlFor="sub-prefix">Node name prefix (optional)</label>
            <input
              id="sub-prefix"
              className="control"
              placeholder="Node name prefix (optional)"
              value={namePrefix}
              onChange={(e) => setNamePrefix(e.target.value)}
            />
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={keepHost}
              onChange={(e) => setKeepHost(e.target.checked)}
            />
            Keep original Host / SNI (recommended)
          </label>

          <Button
            variant="primary"
            style={{ width: '100%', marginTop: 14, minHeight: 44 }}
            onClick={generate}
            loading={busy}
          >
            Generate subscription
          </Button>
        </div>

        {/* ------------------------------------------------- result (right) */}
        <div className="panel">
          <div className="step"><span>Result</span></div>

          {error && <div className="note error">{error}</div>}

          {!result && (
            <div className="note info">
              After generating, your subscription links, QR code, node preview,
              and download buttons appear here.
            </div>
          )}

          {result && (
            <div>
              {result.loaded ? (
                <div className="note ok">Loaded <code>{result.sub_id}</code> from history.</div>
              ) : result.dedup ? (
                <div className="note ok">
                  Identical input already exists; reusing short link <code>{result.sub_id}</code>.
                </div>
              ) : (
                <div className="note ok">Generated short link <code>{result.sub_id}</code>.</div>
              )}

              <div className="metrics">
                <div className="metric">
                  <div className="v">{count(result.counts.input_nodes)}</div>
                  <div className="k">Input nodes</div>
                </div>
                <div className="metric">
                  <div className="v">{count(result.counts.endpoints)}</div>
                  <div className="k">Optimized addresses</div>
                </div>
                <div className="metric ok">
                  <div className="v">{result.counts.output_nodes}</div>
                  <div className="k">Output nodes</div>
                </div>
              </div>

              <details className="expander">
                <summary>Node preview</summary>
                <div className="body" style={{ overflowX: 'auto' }}>
                  {result.preview.length === 0 ? (
                    <div className="note info">No nodes to preview.</div>
                  ) : (
                    <table className="table">
                      <thead>
                        <tr>
                          {PREVIEW_COLS.map((col) => <th key={col}>{col}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {result.preview.map((node, i) => (
                          <tr key={i}>
                            {PREVIEW_COLS.map((col) => (
                              <td key={col}>
                                {col === 'tls' ? (node.tls ? 'yes' : 'no') : String(node[col] ?? '')}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </details>

              <details className="expander" open>
                <summary>Subscription links (import directly from a phone on the same Wi-Fi)</summary>
                <div className="body">
                  {Object.entries(result.urls).map(([label, url]) => (
                    <div className="field" key={label}>
                      <span className="label">{label}</span>
                      <CodeBox text={url} />
                    </div>
                  ))}
                </div>
              </details>

              <div className="field">
                <span className="label">Download subscription files (import without a server)</span>
                <div className="row">
                  <Button onClick={() => download('raw')}>⬇ raw .txt</Button>
                  <Button onClick={() => download('clash')}>⬇ clash .yaml</Button>
                  <Button onClick={() => download('surge')}>⬇ surge .conf</Button>
                </div>
                {dlError && <div className="note warn">Unavailable — {dlError}</div>}
              </div>

              <details className="expander">
                <summary>Subscription QR code (raw / Shadowrocket)</summary>
                <div className="body">
                  <img
                    className="qr"
                    src={api.subsQrUrl(result.sub_id)}
                    width={220}
                    alt={`QR code for subscription ${result.sub_id}`}
                  />
                  <div style={{ font: '11px var(--mono)', color: 'var(--muted)', marginTop: 6 }}>
                    Scan to import
                  </div>
                </div>
              </details>

              {result.warnings.length > 0 && (
                <div className="note warn" style={{ whiteSpace: 'pre-line' }}>
                  {result.warnings.join('\n')}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ---------------------------------------------------------- history */}
      <div className="panel" style={{ marginTop: 18 }}>
        <div className="step"><span>Subscription history</span></div>

        {historyError && <div className="note error">{historyError}</div>}
        {!historyError && history.length === 0 && (
          <div className="note info">No subscriptions generated yet.</div>
        )}

        {history.map((item) => (
          <div
            className="row"
            key={item.id}
            style={{ padding: '7px 0', borderBottom: '1px solid var(--edge)' }}
          >
            <span
              className="grow"
              style={{ font: '12px var(--mono)', color: 'var(--muted)', overflowWrap: 'anywhere' }}
            >
              <code>{item.id}</code> · {item.node_count} nodes · {item.created_at.slice(0, 19)}
            </span>
            <Button
              onClick={() => loadSub(item.id)}
              aria-label={`Load subscription ${item.id}`}
            >
              Load
            </Button>
            <Button
              variant="danger"
              onClick={() => deleteSub(item.id)}
              aria-label={`Delete subscription ${item.id}`}
            >
              Delete
            </Button>
          </div>
        ))}
      </div>
    </div>
  )
}
