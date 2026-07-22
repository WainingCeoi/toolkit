// Doc to PDF: clean Word docs (accept tracked changes, strip comments), then
// render them to PDF with LibreOffice and bundle the results as one zip.

import { useEffect, useState } from 'react'
import { api, artifactUrl } from '../api'
import { useToolJob } from '../jobs'
import FileDrop from '../components/FileDrop'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'
import type { DocConvertResult } from '../types/api'

export default function DocToPdf() {
  const [files, setFiles] = useState<File[]>([])
  // Proactive dependency check, loaded independently (the old page warned on
  // load if LibreOffice was missing). soffice status lives on /api/health.
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

  const result = snapshot?.state === 'done' ? snapshot.result : null
  const failed = result?.failed ?? []

  return (
    <div>
      <div className="page-head"><h1>📄 Doc to PDF</h1></div>
      <p className="page-sub">
        Clean Word documents — accept all tracked changes and remove comments —
        then export them to PDF with no revision marks, bundled as a
        downloadable zip. Powered by LibreOffice.
      </p>

      {soffice === false && (
        <div className="note warn">
          Missing required tool: LibreOffice (<code>brew install --cask libreoffice</code>).
        </div>
      )}

      <div className="station">
        <div className="panel">
          <div className="step"><span className="n">01</span><span>SELECT WORD FILES</span></div>
          <FileDrop
            accept=".docx"
            files={files}
            onChange={setFiles}
            hint="Drop Word (.docx) files here or click to choose"
          />
          <div className="note info">
            Every tracked change is accepted and every comment stripped before
            LibreOffice renders the PDF — the output carries no revision marks.
          </div>
        </div>

        <div className="panel">
          <div className="step"><span className="n">02</span><span>CONVERT</span></div>
          <Button
            variant="primary"
            onClick={convert}
            disabled={running}
          >
            Convert to PDF
          </Button>
          {error && <div className="note error">{error}</div>}
          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                {result.done.length > 0 && (
                  <div className="note ok">
                    ✅ Converted {result.done.length} file(s).
                  </div>
                )}
                {result.artifact_id && (
                  <Button as="a" href={artifactUrl(result.artifact_id)}>
                    ⬇ Download PDFs (.zip)
                  </Button>
                )}
                {failed.length > 0 && (
                  <details className="expander">
                    <summary>❌ {failed.length} failed</summary>
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
