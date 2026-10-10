import { useEffect, useState, type ReactNode } from 'react'
import { api, artifactUrl } from '../api'
import { useToolJob } from '../jobs'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import type { DocConvertResult, MarkdownHealth } from '../types/api'

const ACCEPT = '.pdf,.png,.jpg,.jpeg,.docx,.pptx,.xlsx'

const OCR_LANGS = [
  'ch', 'ch_server', 'korean', 'ta', 'te', 'ka', 'th', 'el',
  'arabic', 'east_slavic', 'cyrillic', 'devanagari',
]

function Hint({ children }: { children: ReactNode }) {
  return (
    <p className="hint">
      {children}
    </p>
  )
}

export default function DocToMarkdown() {
  const [files, setFiles] = useState<File[]>([])
  const [backend, setBackend] = useState('hybrid-engine')
  const [method, setMethod] = useState('auto')
  const [lang, setLang] = useState('ch')
  const [effort, setEffort] = useState('medium')
  const [formula, setFormula] = useState(true)
  const [table, setTable] = useState(true)

  const [health, setHealth] = useState<MarkdownHealth | null>(null)
  useEffect(() => {
    let alive = true
    api.docmdHealth().then((h) => alive && setHealth(h)).catch(() => {})
    return () => { alive = false }
  }, [])

  const { start, snapshot, running, error } = useToolJob<DocConvertResult>('/tools/doc-to-markdown')

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

  const result =
    snapshot?.state === 'done' || snapshot?.state === 'cancelled' ? snapshot.result : null

  return (
    <div>
      <ToolHeader slug="doc-to-markdown" />
      <p className="page-sub">
        Turn documents and images into editable Markdown with{' '}
        <a href="https://github.com/opendatalab/MinerU" target="_blank" rel="noreferrer">MinerU</a>{' '}
        including text, tables, formulas, and extracted images in a downloadable ZIP.
      </p>

      {health && (
        <div className="healthline" style={{ margin: '0 0 8px' }}>
          <span className={`lamp ${health.mineru ? '' : 'off'}`}><i />MinerU CLI</span>
          <span className={`lamp ${health.backend_ready ? '' : 'off'}`}><i />conversion backend</span>
        </div>
      )}
      {health && !health.mineru && (
        <div className="note warn">
          Missing required tool: MinerU. Install it with{' '}
          <code>uv sync --extra docmd</code> in <code>backend/</code>, then reload.
        </div>
      )}
      {health && health.mineru && !health.backend_ready && (
        <div className="note warn">
          MinerU is installed but its conversion backend isn&apos;t. Run{' '}
          <code>uv sync --extra docmd</code> in <code>backend/</code>, then reload.
        </div>
      )}

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Add source files</span></div>

          <FileDrop
            accept={ACCEPT}
            files={files}
            onChange={setFiles}
            hint="Drop documents or images here or browse files"
          />
          <p className="hint">PDF, PNG, JPG, JPEG, DOCX, PPTX, and XLSX</p>

          <details className="expander">
            <summary>Conversion options</summary>
            <div className="body">
              <div className="field">
                <label htmlFor="docmd-backend">Backend</label>
                <select
                  id="docmd-backend"
                  className="control"
                  value={backend}
                  onChange={(e) => setBackend(e.target.value)}
                >
                  <option value="pipeline">Pipeline · Fastest</option>
                  <option value="hybrid-engine">Hybrid · Balanced</option>
                  <option value="vlm-engine">Vision language · Complex layouts</option>
                </select>
                <Hint>
                  Pipeline uses lighter models. Hybrid and vision language backends
                  improve accuracy on complex layouts and take more time.
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
                      <option value="auto">Automatic</option>
                      <option value="txt">Text · Digital PDFs</option>
                      <option value="ocr">OCR · Scanned documents</option>
                    </select>
                    <Hint>Automatic chooses a method for each file.</Hint>
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
                  <label htmlFor="docmd-effort">Analysis detail</label>
                  <select
                    id="docmd-effort"
                    className="control"
                    value={effort}
                    onChange={(e) => setEffort(e.target.value)}
                  >
                    <option value="medium">Standard</option>
                    <option value="high">High · Include image and chart analysis</option>
                  </select>
                  <Hint>High adds image and chart analysis for greater accuracy and takes more time.</Hint>
                </div>
              )}
            </div>
          </details>
        </div>

        <div className="panel result-panel">
          <div className="step"><span className="n">02</span><span>Convert &amp; download</span></div>
          <div className="action-summary">
            <strong>{files.length > 0 ? `${files.length} file${files.length === 1 ? '' : 's'} selected` : 'Add source files to begin'}</strong>
            <p>Markdown, extracted images, and supporting files in one ZIP.</p>
          </div>

          <Button
            variant="primary"
            loading={running}
            onClick={convert}
            disabled={files.length === 0 || Boolean(health && (!health.mineru || !health.backend_ready))}
          >
            Convert to Markdown
          </Button>

          {error && <div className="note error">{error}</div>}

          <div className="step"><span>Results</span></div>
          {!snapshot && !error && (
            <EmptyState icon="markdown" title="Your Markdown will appear here">
              Start a conversion to track progress and download your files.
              The first run may take longer while MinerU downloads its models.
            </EmptyState>
          )}

          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                {result.done?.length > 0 && (
                  <div className="note ok">Converted {result.done.length} file{result.done.length === 1 ? '' : 's'}.</div>
                )}
                {result.artifact_id && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    <Icon name="download" size={17} /> Download Markdown (.zip)
                  </Button>
                )}
                {result.failed?.length > 0 && (
                  <details className="expander">
                    <summary>{result.failed.length} file{result.failed.length === 1 ? '' : 's'} could not be converted</summary>
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
