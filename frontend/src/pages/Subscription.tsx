import { useCallback, useEffect, useState } from 'react'
import { api, saveBlob } from '../api'
import Button from '../components/Button'
import CodeBox from '../components/CodeBox'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import type { Subscription as SubscriptionData, SubsHistoryItem } from '../types/api'

const PREVIEW_COLS = ['name', 'type', 'server', 'port', 'host', 'sni', 'network', 'tls']

function historyDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

export default function Subscription() {
  const [nodeLinks, setNodeLinks] = useState('')
  const [preferredIps, setPreferredIps] = useState('')
  const [namePrefix, setNamePrefix] = useState('')
  const [keepHost, setKeepHost] = useState(true)

  const [result, setResult] = useState<SubscriptionData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [dlError, setDlError] = useState<string | null>(null)

  const [history, setHistory] = useState<SubsHistoryItem[]>([])
  const [historyError, setHistoryError] = useState<string | null>(null)

  const refreshHistory = useCallback(async () => {
    try {
      setHistory(await api.subsHistory())
      setHistoryError(null)
    } catch (err) {
      setHistoryError((err as Error).message)
    }
  }, [])

  // Inlined rather than refreshHistory() so a response arriving after unmount is dropped.
  useEffect(() => {
    let cancelled = false
    api
      .subsHistory()
      .then((items) => {
        if (cancelled) return
        setHistory(items)
        setHistoryError(null)
      })
      .catch((err: Error) => {
        if (!cancelled) setHistoryError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [])

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
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function download(target: string) {
    if (!result) return
    setDlError(null)
    try {
      const { blob, filename } = await api.subsDownload(result.sub_id, target)
      saveBlob(blob, filename)
    } catch (err) {
      setDlError(`${target}: ${(err as Error).message}`)
    }
  }

  async function loadSub(id: string) {
    setError(null)
    setDlError(null)
    try {
      setResult(await api.subsGet(id))
    } catch (err) {
      setError((err as Error).message)
      refreshHistory()
    }
  }

  async function deleteSub(id: string) {
    setError(null)
    try {
      await api.subsDelete(id)
      setResult((cur) => (cur && cur.sub_id === id ? null : cur))
      refreshHistory()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const count = (v: number | null | undefined): string | number =>
    v === null || v === undefined ? '—' : v
  const ready = nodeLinks.trim() !== '' && preferredIps.trim() !== ''

  return (
    <div>
      <ToolHeader slug="subscription" />
      <p className="page-sub">
        Combine your node links with preferred IPs or domains. Generate links,
        QR codes, and configuration files for Shadowrocket, Clash, or Surge.
      </p>

      <div className="station">
        <div>
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
              <p className="hint">Supports vmess://, vless://, and trojan:// links, or a base64 subscription.</p>
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
              <p className="hint">Each node is paired with each address. Add :port to override the original port.</p>
            </div>

          </div>

          <div className="panel">
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
              disabled={!ready}
            >
              <Icon name="network" size={17} /> Generate subscription
            </Button>
            {!ready && <p className="hint">Add node links and at least one preferred address to continue.</p>}
          </div>
        </div>

        <div className="panel result-panel">
          <div className="step"><span className="n">04</span><span>Subscription &amp; export</span></div>

          {error && <div className="note error">{error}</div>}

          {!result && (
            <EmptyState icon="network" title="Your subscription will appear here">
              Generate a subscription to preview its nodes, copy import links, or download configuration files.
            </EmptyState>
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
                        {result.preview.map((node: Record<string, unknown>, i: number) => (
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
                <summary>Import links</summary>
                <div className="body">
                  <p className="hint">Open these links on a phone connected to the same Wi-Fi.</p>
                  {Object.entries(result.urls).map(([label, url]) => (
                    <div className="field" key={label}>
                      <span className="label">{label}</span>
                      <CodeBox text={url} />
                    </div>
                  ))}
                </div>
              </details>

              <div className="field">
                <span className="label">Download configurations</span>
                <div className="row">
                  <Button onClick={() => download('raw')}><Icon name="download" size={16} /> Raw .txt</Button>
                  <Button onClick={() => download('clash')}><Icon name="download" size={16} /> Clash .yaml</Button>
                  <Button onClick={() => download('surge')}><Icon name="download" size={16} /> Surge .conf</Button>
                </div>
                <p className="hint">Import a downloaded file without keeping this server running.</p>
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
                  <div className="hint" style={{ marginTop: 6 }}>
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

      <div className="panel" style={{ marginTop: 18 }}>
        <div className="step"><span>Subscription history</span></div>

        {historyError && <div className="note error">{historyError}</div>}
        {!historyError && history.length === 0 && (
          <EmptyState icon="clock" title="No saved subscriptions">Generated subscriptions are saved locally so you can load them again later.</EmptyState>
        )}

        {history.map((item) => (
          <div
            className="row"
            key={item.id}
            style={{ padding: '7px 0', borderBottom: '1px solid var(--edge)' }}
          >
            <span
              className="grow"
              style={{ color: 'var(--muted)', overflowWrap: 'anywhere' }}
            >
              <strong>{item.name_prefix || item.id}</strong> · {item.node_count} nodes
              <span className="hint" style={{ display: 'block', marginTop: 3 }}><time dateTime={item.created_at}>{historyDate(item.created_at)}</time>{item.name_prefix && <> · <code>{item.id}</code></>}</span>
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
