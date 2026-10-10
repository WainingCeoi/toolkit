import { useEffect, useState } from 'react'
import { api, artifactUrl } from '../api'
import { useToolJob } from '../jobs'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import type { DocConvertResult } from '../types/api'

export default function DocToPdf() {
  const [files, setFiles] = useState<File[]>([])
  const [soffice, setSoffice] = useState<boolean | null>(null)
  const { start, snapshot, running, error } = useToolJob<DocConvertResult>('/tools/doc-to-pdf')

  useEffect(() => {
    api
      .health()
      .then((h) => setSoffice(h.soffice))
      .catch(() => setSoffice(null))
  }, [])

  async function convert() {
    const form = new FormData()
    files.forEach((f) => form.append('files', f))
    const id = await start(() => api.docToPdf(form))
    if (id) setFiles([])
  }

  const result =
    snapshot?.state === 'done' || snapshot?.state === 'cancelled' ? snapshot.result : null
  const failed = result?.failed ?? []

  return (
    <div>
      <ToolHeader slug="doc-to-pdf" />
      <p className="page-sub">
        Export clean PDFs from Word documents. Tracked changes are accepted and
        comments removed; your PDFs download together as a ZIP.
      </p>

      {soffice === false && (
        <div className="note warn">
          Missing required tool: LibreOffice (<code>brew install --cask libreoffice</code>).
        </div>
      )}

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>Add Word documents</span></div>
          <FileDrop
            accept=".docx"
            files={files}
            onChange={setFiles}
            hint="Drop Word documents here or browse files"
          />
          <p className="hint">DOCX · Multiple files supported</p>
          <div className="note info">
            LibreOffice accepts every tracked change and removes every comment
            before rendering each PDF. The output contains no revision marks.
          </div>
        </div>

        <div className="panel result-panel">
          <div className="step"><span className="n">02</span><span>Convert &amp; download</span></div>
          <div className="action-summary">
            <strong>{files.length > 0 ? `${files.length} document${files.length === 1 ? '' : 's'} selected` : 'Add documents to begin'}</strong>
            <p>One PDF per document, bundled in a ZIP archive.</p>
          </div>
          <Button
            variant="primary"
            onClick={convert}
            loading={running}
            disabled={files.length === 0 || soffice === false}
          >
            Convert to PDF
          </Button>
          {error && <div className="note error">{error}</div>}
          <div className="step"><span>Results</span></div>
          {!snapshot && !error && (
            <EmptyState icon="file" title="Your PDFs will appear here">
              Add Word documents and start converting to track progress and download the results.
            </EmptyState>
          )}
          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                {result.done.length > 0 && (
                  <div className="note ok">
                    Converted {result.done.length} document{result.done.length === 1 ? '' : 's'}.
                  </div>
                )}
                {result.artifact_id && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    <Icon name="download" size={17} /> Download PDFs (.zip)
                  </Button>
                )}
                {failed.length > 0 && (
                  <details className="expander">
                    <summary>{failed.length} document{failed.length === 1 ? '' : 's'} could not be converted</summary>
                    <div className="body">
                      <CodeBox
                        text={failed
                          .map(([name, reason]) => `${name}: ${reason}`)
                          .join('\n')}
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
