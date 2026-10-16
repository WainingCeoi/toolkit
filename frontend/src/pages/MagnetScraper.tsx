import { useEffect, useRef, useState, type CSSProperties, type FormEvent } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import JobPanel from '../components/JobPanel'
import CodeBox from '../components/CodeBox'
import Button from '../components/Button'
import ToolHeader from '../components/ToolHeader'
import EmptyState from '../components/EmptyState'
import Icon from '../components/Icon'
import type { DedupeResult, MagnetConfig, MagnetResult } from '../types/api'

const MODES = [
  { key: 'auto', label: 'Automatic' },
  { key: 'manual', label: 'Manual' },
  { key: 'cleanup', label: 'Deduplicate' },
] as const

type MagnetMode = (typeof MODES)[number]['key']

const BIG_TAP: CSSProperties = { minHeight: 44 }

interface MagnetDraft {
  website_url: string
  cutoff_video: string
  dirty: boolean
}

function configDraft(config: MagnetConfig | null): MagnetDraft {
  return {
    website_url: config?.website_url ?? '',
    cutoff_video: config?.cutoff_video ?? '',
    dirty: false,
  }
}

function splitLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map((line: string) => line.trim())
    .filter(Boolean)
}

function ScrapeResult({ result }: { result: MagnetResult | null }) {
  if (!result) return null

  if (result.cutoff_found === false) {
    return (
      <>
        {result.error && <div className="note error">{result.error}</div>}
        <div className="note warn">{result.warning}</div>
      </>
    )
  }

  if (!result.urls || result.urls.length === 0) {
    return <EmptyState icon="check" title="You're up to date">No new unwatched videos were found.</EmptyState>
  }

  const successful = result.successful || []
  const failed = result.failed || []
  const duplicates = result.duplicate_count || 0
  return (
    <>
      <div className="note ok">{successful.length} magnet link(s) found.</div>
      {duplicates > 0 && (
        <div className="note info">
          {duplicates} duplicate(s) removed. Every link below is unique.
        </div>
      )}
      <div className="metrics">
        <div className="metric">
          <div className="v">{result.urls.length}</div>
          <div className="k">Total found</div>
        </div>
        <div className="metric ok">
          <div className="v">{successful.length}</div>
          <div className="k">Successful</div>
        </div>
        <div className={failed.length ? 'metric bad' : 'metric'}>
          <div className="v">{failed.length}</div>
          <div className="k">Failed</div>
        </div>
      </div>
      {successful.length > 0 && (
        <div className="field">
          <span className="label">Magnet links</span>
          <CodeBox text={successful.map((s) => s.result).join('\n')} />
        </div>
      )}
      {failed.length > 0 && (
        <div className="field">
          <span className="label">Failed URLs</span>
          <CodeBox text={failed.map((f) => f.url).join('\n')} />
        </div>
      )}
    </>
  )
}

export default function MagnetScraper() {
  const [mode, setMode] = useState<MagnetMode>('auto')

  const [startPage, setStartPage] = useState('1')
  const [config, setConfig] = useState<MagnetConfig | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)
  const [configLoading, setConfigLoading] = useState(true)
  const [configReload, setConfigReload] = useState(0)
  const configRequest = useRef(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [draft, setDraft] = useState<MagnetDraft>(() => configDraft(null))
  const [saving, setSaving] = useState(false)
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [settingsSaved, setSettingsSaved] = useState(false)

  const [manualRaw, setManualRaw] = useState('')

  const [dedupeRaw, setDedupeRaw] = useState('')
  const [dedupeResult, setDedupeResult] = useState<DedupeResult | null>(null)
  const [dedupeError, setDedupeError] = useState<string | null>(null)
  const [dedupeBusy, setDedupeBusy] = useState(false)

  const { start, snapshot, running, error, setError } = useToolJob<MagnetResult>('/tools/magnet-scraper')
  const finishedJobId = snapshot && snapshot.state !== 'running' ? snapshot.id : null

  useEffect(() => {
    let active = true
    const requestId = ++configRequest.current
    api
      .magnetConfig()
      .then((next) => {
        if (!active || requestId !== configRequest.current) return
        setConfig(next)
        setConfigError(null)
        // Automatic runs advance the cutoff; leave any unsaved edits intact.
        setDraft((current) => current.dirty ? current : configDraft(next))
      })
      .catch((e: Error) => {
        if (active && requestId === configRequest.current) setConfigError(e.message)
      })
      .finally(() => {
        if (active && requestId === configRequest.current) setConfigLoading(false)
      })
    return () => { active = false }
  }, [finishedJobId, configReload])

  function closeSettings() {
    setSettingsOpen(false)
    setDraft(configDraft(config))
    setSettingsError(null)
    setSettingsSaved(false)
  }

  function toggleSettings() {
    if (settingsOpen) {
      closeSettings()
      return
    }
    setDraft(configDraft(config))
    setSettingsError(null)
    setSettingsSaved(false)
    setSettingsOpen(true)
  }

  function editSetting(key: 'website_url' | 'cutoff_video', value: string) {
    setDraft((current) => ({ ...current, [key]: value, dirty: true }))
    setSettingsError(null)
    setSettingsSaved(false)
  }

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    // A delayed GET must not replace the settings returned by this save.
    ++configRequest.current
    setConfigLoading(false)
    setSaving(true)
    setSettingsError(null)
    setSettingsSaved(false)
    try {
      const next = await api.magnetSaveConfig({
        website_url: draft.website_url.trim(),
        cutoff_video: draft.cutoff_video.trim(),
      })
      // A run may finish and start another GET while the PUT is in flight.
      ++configRequest.current
      setConfigLoading(false)
      setConfig(next)
      setConfigError(null)
      setDraft(configDraft(next))
      setSettingsSaved(true)
    } catch (e) {
      setSettingsError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  function retryConfig() {
    setConfigError(null)
    setConfigLoading(true)
    setConfigReload((value) => value + 1)
  }

  function startAuto() {
    const page = Number(startPage)
    if (!Number.isInteger(page) || page < 1) {
      setError('Start page must be a whole number of 1 or more.')
      return
    }
    start(() => api.magnetAuto(page))
  }

  function startManual() {
    // An empty list goes through; the backend supplies the validation message.
    start(() => api.magnetManual(splitLines(manualRaw)))
  }

  async function runDedupe() {
    setDedupeBusy(true)
    setDedupeError(null)
    try {
      setDedupeResult(await api.magnetDedupe(splitLines(dedupeRaw)))
    } catch (e) {
      setDedupeResult(null)
      setDedupeError((e as Error).message)
    } finally {
      setDedupeBusy(false)
    }
  }

  const missingConfig = config && (!config.website_url_set || !config.cutoff_set)
  const autoReady = config && !missingConfig && !configError && !configLoading

  return (
    <div>
      <ToolHeader slug="magnet-scraper" />
      <p className="page-sub">
        Collect magnet links from video pages, or clean up an existing list.
        Choose automatic discovery or supply your own URLs.
      </p>

      <div className="station">
        <div className="panel">
          <div className="magnet-panel-heading">
            <div className="step">
              <span className="n">01</span><span>Source &amp; mode</span>
            </div>
            <Button
              aria-expanded={settingsOpen}
              aria-controls="magnet-settings"
              disabled={saving}
              onClick={toggleSettings}
            >
              <Icon name="settings" size={16} /> Settings
            </Button>
          </div>

          {configError && (
            <div className="note warn" role="alert">
              Could not load settings: {configError}
              <div className="row" style={{ marginTop: 8 }}>
                <Button onClick={retryConfig} loading={configLoading}>Retry</Button>
              </div>
            </div>
          )}

          <div id="magnet-settings" hidden={!settingsOpen}>
            {settingsOpen && (config ? (
              <form className="magnet-settings" aria-label="Magnet settings" onSubmit={saveSettings}>
                <h2>Automatic discovery</h2>
                <p className="hint">Choose where to look and the video to stop at. Settings are saved on this Mac.</p>
                <div className="field">
                  <label htmlFor="magnet-website">Source website</label>
                  <input
                    id="magnet-website"
                    type="url"
                    className="control"
                    placeholder="https://example.com"
                    value={draft.website_url}
                    onChange={(e) => editSetting('website_url', e.target.value)}
                    disabled={saving}
                    spellCheck={false}
                    autoComplete="off"
                    aria-describedby="magnet-website-hint"
                  />
                  <p id="magnet-website-hint" className="hint">The site's base URL, without /page/1/.</p>
                </div>
                <div className="field">
                  <label htmlFor="magnet-cutoff">Last watched video URL</label>
                  <input
                    id="magnet-cutoff"
                    type="url"
                    className="control"
                    placeholder="https://example.com/last-watched-video"
                    value={draft.cutoff_video}
                    onChange={(e) => editSetting('cutoff_video', e.target.value)}
                    disabled={saving}
                    spellCheck={false}
                    autoComplete="off"
                    aria-describedby="magnet-cutoff-hint"
                  />
                  <p id="magnet-cutoff-hint" className="hint">Discovery stops before this video. The stopping point advances after a successful run.</p>
                </div>
                {settingsError && <div className="note error" role="alert">{settingsError}</div>}
                {settingsSaved && <div className="note ok" role="status">Settings saved. Changes apply to your next automatic run.</div>}
                <div className="row">
                  <Button type="submit" variant="primary" loading={saving} disabled={!draft.dirty}>Save settings</Button>
                  <Button onClick={closeSettings} disabled={saving}>Cancel</Button>
                </div>
                {draft.dirty && <p className="hint">Save your changes before starting automatic discovery.</p>}
              </form>
            ) : (
              <div className="note info" role="status">{configLoading ? 'Loading settings…' : 'Retry loading settings to edit your source.'}</div>
            ))}
          </div>

          <div className="field">
            <span className="label" id="magnet-mode-label">
              Mode
            </span>
            <div className="row" role="group" aria-labelledby="magnet-mode-label">
              {MODES.map((m) => (
                <Button
                  key={m.key}
                  variant={mode === m.key ? 'primary' : 'secondary'}
                  aria-pressed={mode === m.key}
                  style={{ ...BIG_TAP, flex: '1 1 auto' }}
                  onClick={() => setMode(m.key)}
                >
                  {m.label}
                </Button>
              ))}
            </div>
          </div>

          {mode === 'auto' && (
            <>
              <div className="workflow-summary">
                <Icon name="magnet" size={18} />
                <span>Finds new videos from your configured site, stopping at the last
                watched video. Searches up to 100 pages and safely updates your cutoff.</span>
              </div>
              <div className="field">
                <label htmlFor="magnet-start-page">Start page</label>
                <input
                  id="magnet-start-page"
                  type="number"
                  min={1}
                  step={1}
                  className="control"
                  value={startPage}
                  onChange={(e) => setStartPage(e.target.value)}
                />
              </div>
              <div className="field">
                <span className="label">Source configuration</span>
                {config ? (
                  <div className="healthline" style={{ margin: 0 }}>
                    <span className={config.website_url_set ? 'lamp' : 'lamp off'}>
                      <i />
                      Website {config.website_url_set ? 'ready' : 'missing'}
                    </span>
                    <span className={config.cutoff_set ? 'lamp' : 'lamp off'}>
                      <i />
                      Stopping point {config.cutoff_set ? 'ready' : 'missing'}
                    </span>
                  </div>
                ) : (
                  <div className="healthline" style={{ margin: 0 }}>
                    {configLoading ? 'Checking settings…' : 'Settings unavailable'}
                  </div>
                )}
              </div>
              {missingConfig && (
                <div className="note warn">
                  Add a source website and last watched video URL in Settings to use Automatic mode.
                </div>
              )}
              <Button
                variant="primary"
                style={BIG_TAP}
                disabled={running || saving || !autoReady || (settingsOpen && draft.dirty)}
                loading={running}
                onClick={startAuto}
              >
                <Icon name="search" size={16} /> Find new magnet links
              </Button>
            </>
          )}

          {mode === 'manual' && (
            <>
              <div className="field">
                <label htmlFor="magnet-manual-urls">
                  Video page URLs · one per line
                </label>
                <textarea
                  id="magnet-manual-urls"
                  className="control"
                  rows={8}
                  value={manualRaw}
                  onChange={(e) => setManualRaw(e.target.value)}
                  placeholder={'https://…\nhttps://…'}
                />
              </div>
              <Button
                variant="primary"
                style={BIG_TAP}
                disabled={running}
                loading={running}
                onClick={startManual}
              >
                <Icon name="magnet" size={16} /> Collect magnet links
              </Button>
            </>
          )}

          {mode === 'cleanup' && (
            <>
              <div className="field">
                <label htmlFor="magnet-dedupe-links">
                  Magnet links · one per line
                </label>
                <textarea
                  id="magnet-dedupe-links"
                  className="control"
                  rows={8}
                  value={dedupeRaw}
                  onChange={(e) => setDedupeRaw(e.target.value)}
                  placeholder={'magnet:?xt=…\nmagnet:?xt=…'}
                />
              </div>
              <Button
                variant="primary"
                style={BIG_TAP}
                loading={dedupeBusy}
                onClick={runDedupe}
              >
                <Icon name="check" size={16} /> Remove duplicates
              </Button>
            </>
          )}
        </div>

        <div className="panel result-panel">
          <div className="step">
            <span className="n">02</span><span>Results</span>
          </div>

          {mode === 'cleanup' ? (
            <>
              {dedupeError && <div className="note error">{dedupeError}</div>}
              {dedupeResult ? (
                <>
                  <div className="note ok">Found {dedupeResult.count} unique links</div>
                  <CodeBox text={dedupeResult.unique.join('\n')} />
                </>
              ) : (
                !dedupeError && (
                  <EmptyState icon="magnet" title="A cleaner list, ready to copy">
                    Paste your magnet links and remove duplicates. The first-seen order is preserved.
                  </EmptyState>
                )
              )}
            </>
          ) : (
            <>
              {error && <div className="note error">{error}</div>}
              {snapshot ? (
                <JobPanel snapshot={snapshot}>
                  {['done', 'cancelled'].includes(snapshot.state) && (
                    <ScrapeResult result={snapshot.result} />
                  )}
                </JobPanel>
              ) : (
                !error && <EmptyState icon="magnet" title="Your magnet links appear here">
                  Choose a source and start collecting. You can copy the results and review any failed URLs.
                </EmptyState>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
