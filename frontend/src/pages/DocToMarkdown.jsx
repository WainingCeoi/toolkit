// Doc to Markdown — convert PDFs, Office docs, and images with MinerU.
// One job per batch; the backend runs one MinerU subprocess per file and
// bundles every file's output tree into a single downloadable zip.

import React, { useEffect, useState } from 'react'
import { api, artifactUrl } from '../api'
import { useToolJob } from '../jobs'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'

const ACCEPT = '.pdf,.png,.jpg,.jpeg,.docx,.pptx,.xlsx'

const OCR_LANGS = [
  'ch', 'ch_server', 'korean', 'ta', 'te', 'ka', 'th', 'el',
  'arabic', 'east_slavic', 'cyrillic', 'devanagari',
]

// Small muted help line under a control (the old page's `help=` tooltips).
function Hint({ children }) {
  return (
    <p style={{ margin: '4px 0 0', fontSize: '11.5px', color: 'var(--faint)', lineHeight: 1.45 }}>
      {children}
    </p>
  )
}

export default function DocToMarkdown() {
  const [files, setFiles] = useState([])
  const [backend, setBackend] = useState('hybrid-engine')
  const [method, setMethod] = useState('auto')
  const [lang, setLang] = useState('ch')
  const [effort, setEffort] = useState('medium')
  const [formula, setFormula] = useState(true)
  const [table, setTable] = useState(true)

  // Health lamps load independently of everything else on the page.
  const [health, setHealth] = useState(null)
  useEffect(() => {
    let alive = true
    api.docmdHealth().then((h) => alive && setHealth(h)).catch(() => {})
    return () => { alive = false }
  }, [])

  const { start, snapshot, running, error } = useToolJob('/tools/doc-to-markdown')

  async function convert() {
    const fd = new FormData()
    files.forEach((f) => fd.append('files', f))
    fd.append('backend', backend)
    fd.append('method', method)
    fd.append('lang', lang)
    fd.append('effort', effort)
    fd.append('formula', String(formula))
    fd.append('table', String(table))
    const id = await start(() => api.docToMarkdown(fd))
    if (id) setFiles([])
  }

  const result = snapshot?.state === 'done' ? snapshot.result : null

  return (
    <div>
      <div className="page-head"><h1>📝 Doc to Markdown</h1></div>
      <p className="page-sub">
        Convert PDFs, Office documents, and images into clean Markdown with{' '}
        <a href="https://github.com/opendatalab/MinerU" target="_blank" rel="noreferrer">MinerU</a>{' '}
        — text, tables, formulas, and extracted images, bundled as a downloadable zip.
      </p>

      {health && (
        <div className="healthline" style={{ margin: '0 0 8px' }}>
          <span className={`lamp ${health.mineru ? '' : 'off'}`}><i />MinerU CLI</span>
          <span className={`lamp ${health.backend_ready ? '' : 'off'}`}><i />conversion backend</span>
        </div>
      )}
      {health && !health.mineru && (
        <div className="note warn">
          Missing required tool: MinerU (<code>uv add mineru</code>).
        </div>
      )}
      {health && health.mineru && !health.backend_ready && (
        <div className="note warn">
          MinerU is installed but its conversion backend isn&apos;t. Install it with{' '}
          <code>uv add &apos;mineru[core]&apos;</code> (all backends) or{' '}
          <code>uv add &apos;mineru[pipeline]&apos;</code> (pipeline only), then reload.
        </div>
      )}

      <div className="station" style={{ marginTop: 14 }}>
        <div className="panel">
          <div className="step"><span className="n">01</span><span>SELECT FILES</span></div>

          <FileDrop
            accept={ACCEPT}
            files={files}
            onChange={setFiles}
            hint="Drop PDFs, images, or Office files here — or click to choose"
          />

          <details className="expander">
            <summary>⚙️ Advanced options</summary>
            <div className="body">
              <div className="field">
                <label htmlFor="docmd-backend">Backend</label>
                <select
                  id="docmd-backend"
                  className="control"
                  value={backend}
                  onChange={(e) => setBackend(e.target.value)}
                >
                  <option value="pipeline">pipeline</option>
                  <option value="hybrid-engine">hybrid-engine</option>
                  <option value="vlm-engine">vlm-engine</option>
                </select>
                <Hint>
                  pipeline: fast, general, lightest models. hybrid-engine / vlm-engine:
                  higher accuracy on complex layouts, heavier and slower.
                </Hint>
              </div>

              {backend === 'pipeline' && (
                <>
                  <div className="field">
                    <label htmlFor="docmd-method">Parse method</label>
                    <select
                      id="docmd-method"
                      className="control"
                      value={method}
                      onChange={(e) => setMethod(e.target.value)}
                    >
                      <option value="auto">auto</option>
                      <option value="txt">txt</option>
                      <option value="ocr">ocr</option>
                    </select>
                    <Hint>auto picks per file; txt for digital PDFs; ocr for scans.</Hint>
                  </div>
                  <div className="field">
                    <label htmlFor="docmd-lang">OCR language</label>
                    <select
                      id="docmd-lang"
                      className="control"
                      value={lang}
                      onChange={(e) => setLang(e.target.value)}
                    >
                      {OCR_LANGS.map((code) => (
                        <option key={code} value={code}>{code}</option>
                      ))}
                    </select>
                    <Hint><code>ch</code> handles Chinese + English. Only affects OCR accuracy.</Hint>
                  </div>
                  <div className="field">
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={formula}
                        onChange={(e) => setFormula(e.target.checked)}
                      />
                      Parse formulas
                    </label>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={table}
                        onChange={(e) => setTable(e.target.checked)}
                      />
                      Parse tables
                    </label>
                  </div>
                </>
              )}

              {backend === 'hybrid-engine' && (
                <div className="field">
                  <label htmlFor="docmd-effort">Effort</label>
                  <select
                    id="docmd-effort"
                    className="control"
                    value={effort}
                    onChange={(e) => setEffort(e.target.value)}
                  >
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                  </select>
                  <Hint>high enables image/chart analysis (slower, more accurate).</Hint>
                </div>
              )}
            </div>
          </details>
        </div>

        <div className="panel">
          <div className="step"><span className="n">02</span><span>CONVERT</span></div>

          <Button variant="primary" loading={running} onClick={convert}>
            Convert to Markdown
          </Button>

          {error && <div className="note error">{error}</div>}

          {!snapshot && !error && (
            <div className="note info">
              Queue files on the left, then convert. The first run downloads MinerU&apos;s
              models, so give it a moment.
            </div>
          )}

          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                {result.done?.length > 0 && (
                  <div className="note ok">✅ Converted {result.done.length} file(s).</div>
                )}
                {result.artifact_id && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    ⬇ Download Markdown (.zip)
                  </Button>
                )}
                {result.failed?.length > 0 && (
                  <details className="expander">
                    <summary>❌ {result.failed.length} failed</summary>
                    <div className="body">
                      <CodeBox
                        text={result.failed.map(([name, err]) => `${name}: ${err}`).join('\n')}
                      />
                    </div>
                  </details>
                )}
              </>
            )}
          </JobPanel>
        </div>
      </div>
    </div>
  )
}
