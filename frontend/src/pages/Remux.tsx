// Remux Processor — scan a folder for videos, configure tracks, optionally
// attach external subtitles, then run a parallel lossless ffmpeg remux job.

import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { api } from '../api'
import { useToolJob } from '../jobs'
import Button from '../components/Button'
import FolderField from '../components/FolderField'
import JobPanel from '../components/JobPanel'
import type {
  RemuxResult,
  RemuxStartPayload,
  RemuxVideo,
  SubtitleMatch,
} from '../types/api'

const baseName = (p: string): string => p.split('/').pop() ?? p

const hintStyle: CSSProperties = { fontSize: 11.5, color: 'var(--faint)', marginTop: 4 }

// Module-level so the "external subtitles off" render keeps a stable identity
// and doesn't retrigger anything downstream that depends on `matches`.
const EMPTY_MATCHES: SubtitleMatch[] = []

export default function Remux() {
  // 01 — source folder + video selection
  const [folder, setFolder] = useState('')
  const [videos, setVideos] = useState<RemuxVideo[]>([]) // natural-sorted server-side
  const [selected, setSelected] = useState<string[]>([]) // paths
  const [scanned, setScanned] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [scanError, setScanError] = useState<string | null>(null)

  // 02 — track configuration
  const [includeVideo, setIncludeVideo] = useState(true)
  const [videoIdx, setVideoIdx] = useState('0')
  const [multiAudio, setMultiAudio] = useState(false)
  const [audioIdx, setAudioIdx] = useState('0')
  const [audioMulti, setAudioMulti] = useState('0')
  const [includeSubtitle, setIncludeSubtitle] = useState(true)
  const [subIdx, setSubIdx] = useState('0')
  const [subLang, setSubLang] = useState('chi')

  // 03 — external subtitles
  const [useExternalSub, setUseExternalSub] = useState(false)
  const [subFolder, setSubFolder] = useState('')
  const [fetched, setFetched] = useState<SubtitleMatch[]>([])
  const [fetchError, setFetchError] = useState<string | null>(null)
  // The inputs `fetched` was actually fetched for. Readiness is then a
  // comparison against the current inputs (below) rather than a flag an effect
  // has to keep in sync — so it cannot report ready for a stale match set.
  const [loadedFor, setLoadedFor] = useState<string | null>(null)

  // 04 — output & run
  const [outFolder, setOutFolder] = useState('~/Desktop/🎬')
  const [workers, setWorkers] = useState(4)

  const { start, snapshot, running, error } = useToolJob<RemuxResult>('/tools/remux')

  const selectedSet = useMemo(() => new Set(selected), [selected])
  // Payload order follows the natural-sorted scan list, not click order.
  const selectedPaths = useMemo(
    () => videos.filter((v) => selectedSet.has(v.path)).map((v) => v.path),
    [videos, selectedSet],
  )

  async function scan() {
    setScanning(true)
    setScanError(null)
    try {
      const { videos: found } = await api.remuxScan(folder)
      setVideos(found)
      setScanned(true)
      // Keep only selections that still exist in the rescanned list.
      setSelected((prev) => prev.filter((p) => found.some((v) => v.path === p)))
    } catch (err) {
      setVideos([])
      setSelected([])
      setScanned(false)
      setScanError((err as Error).message)
    } finally {
      setScanning(false)
    }
  }

  function toggle(path: string) {
    setSelected((prev) =>
      prev.includes(path) ? prev.filter((p) => p !== path) : [...prev, path],
    )
  }

  // Identity of the inputs a match set would be fetched for; null when external
  // subtitles are off and there is nothing to fetch. Empty subtitle folder falls
  // back to the source folder (old page rule).
  const subKey = useMemo(
    () =>
      useExternalSub
        ? JSON.stringify([(subFolder || folder).trim(), selectedPaths])
        : null,
    [useExternalSub, subFolder, folder, selectedPaths],
  )

  // All three are derived, never written back from an effect: with external
  // subtitles off there is nothing to match, and readiness is just "the loaded
  // set belongs to the current inputs". matchesReady gates Start so a run can't
  // fire with a stale/empty map while the debounced fetch below is in flight.
  const matches = useExternalSub ? fetched : EMPTY_MATCHES
  const subError = useExternalSub ? fetchError : null
  const matchesReady = subKey === null || loadedFor === subKey

  // Auto-preview subtitle matches whenever the folder or selection changes.
  useEffect(() => {
    if (subKey === null) return undefined
    let stale = false
    const timer = setTimeout(async () => {
      try {
        const { matches: m } = await api.remuxSubtitles(
          (subFolder || folder).trim(),
          selectedPaths,
        )
        if (stale) return
        setFetched(m)
        setFetchError(null)
      } catch (err) {
        if (stale) return
        setFetched([])
        setFetchError((err as Error).message)
      } finally {
        // Marks these inputs resolved either way — a failed lookup is still an
        // answer, and Start must not stay blocked on it forever.
        if (!stale) setLoadedFor(subKey)
      }
    }, 300)
    return () => {
      stale = true
      clearTimeout(timer)
    }
  }, [subKey, subFolder, folder, selectedPaths])

  function startRemux() {
    const payload: RemuxStartPayload = {
      selected: selectedPaths,
      include_video: includeVideo,
      video_index: parseInt(videoIdx, 10) || 0,
      multi_audio: multiAudio,
      // Multi mode sends the raw comma text (empty = no audio); single mode one index.
      audio_value: multiAudio ? audioMulti : String(parseInt(audioIdx, 10) || 0),
      include_subtitle: includeSubtitle,
      subtitle_index: parseInt(subIdx, 10) || 0,
      sub_lang: subLang,
      use_external_sub: useExternalSub,
      external_sub_map: Object.fromEntries(
        matches.map((m): [string, string | null] => [m.video, m.subtitle]),
      ),
      out_folder: outFolder,
      max_workers: workers,
    }
    start(() => api.remuxStart(payload))
  }

  // A cancelled run returns the tasks that already finished — render them too.
  const result =
    snapshot && (snapshot.state === 'done' || snapshot.state === 'cancelled')
      ? snapshot.result
      : null

  return (
    <div>
      <div className="page-head">
        <h1>🎬 Remux Processor</h1>
      </div>
      <p className="page-sub">
        Parallel, lossless remuxing (re-multiplexing) of videos with FFmpeg.
      </p>

      <div className="station">
        <div>
          {/* ---- 01 SELECT VIDEOS ---- */}
          <div className="panel">
            <div className="step">
              <span className="n">01</span>
              <span>Select videos</span>
            </div>
            <FolderField
              label="Source folder"
              value={folder}
              onChange={setFolder}
              placeholder="~/Desktop"
              startDir="~/Desktop"
            />
            <div className="row">
              <Button onClick={scan} loading={scanning}>
                🔍 Scan
              </Button>
              {videos.length > 0 && (
                <>
                  <Button
                    variant="ghost"
                    onClick={() => setSelected(videos.map((v) => v.path))}
                  >
                    Select all
                  </Button>
                  <Button variant="ghost" onClick={() => setSelected([])}>
                    None
                  </Button>
                  <span style={{ font: '11px var(--mono)', color: 'var(--muted)' }}>
                    {selected.length}/{videos.length} selected
                  </span>
                </>
              )}
            </div>
            {scanError && <div className="note error">{scanError}</div>}
            {scanned && videos.length === 0 && (
              <div className="note info">No video files found in this folder.</div>
            )}
            {videos.length > 0 && (
              <div
                style={{
                  maxHeight: 250,
                  overflowY: 'auto',
                  marginTop: 10,
                  border: '1px solid var(--edge)',
                  borderRadius: 'var(--radius-s)',
                  padding: '4px 10px',
                }}
              >
                {videos.map((v) => (
                  <label className="check" key={v.path} title={v.path}>
                    <input
                      type="checkbox"
                      checked={selectedSet.has(v.path)}
                      onChange={() => toggle(v.path)}
                    />
                    <span
                      style={{
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {v.name}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </div>

          {/* ---- 02 TRACK CONFIGURATION ---- */}
          <div className="panel">
            <div className="step">
              <span className="n">02</span>
              <span>Track configuration</span>
            </div>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
                gap: '4px 14px',
              }}
            >
              <div>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={includeVideo}
                    onChange={(e) => setIncludeVideo(e.target.checked)}
                  />
                  Include video
                </label>
                {includeVideo && (
                  <div className="field">
                    <label htmlFor="rx-video-idx">Video track index</label>
                    <input
                      id="rx-video-idx"
                      className="control"
                      type="number"
                      min="0"
                      step="1"
                      value={videoIdx}
                      onChange={(e) => setVideoIdx(e.target.value)}
                    />
                  </div>
                )}
              </div>
              <div>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={multiAudio}
                    onChange={(e) => setMultiAudio(e.target.checked)}
                  />
                  Multiple audio tracks
                </label>
                {multiAudio ? (
                  <div className="field">
                    <label htmlFor="rx-audio-multi">Audio track index(es)</label>
                    <input
                      id="rx-audio-multi"
                      className="control"
                      value={audioMulti}
                      onChange={(e) => setAudioMulti(e.target.value)}
                      placeholder="0,1"
                      spellCheck={false}
                    />
                    <div style={hintStyle}>
                      Comma-separated, e.g. 0,1. Leave empty for no audio.
                    </div>
                  </div>
                ) : (
                  <div className="field">
                    <label htmlFor="rx-audio-idx">Audio track index</label>
                    <input
                      id="rx-audio-idx"
                      className="control"
                      type="number"
                      min="0"
                      step="1"
                      value={audioIdx}
                      onChange={(e) => setAudioIdx(e.target.value)}
                    />
                  </div>
                )}
              </div>
              <div>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={includeSubtitle}
                    onChange={(e) => setIncludeSubtitle(e.target.checked)}
                  />
                  Include embedded subtitle
                </label>
                {includeSubtitle && (
                  <div className="field">
                    <label htmlFor="rx-sub-idx">Subtitle track index</label>
                    <input
                      id="rx-sub-idx"
                      className="control"
                      type="number"
                      min="0"
                      step="1"
                      value={subIdx}
                      onChange={(e) => setSubIdx(e.target.value)}
                    />
                  </div>
                )}
              </div>
            </div>
            <div className="field" style={{ marginTop: 6, marginBottom: 0 }}>
              <label htmlFor="rx-sub-lang">Subtitle language tag</label>
              <input
                id="rx-sub-lang"
                className="control"
                value={subLang}
                onChange={(e) => setSubLang(e.target.value)}
                style={{ maxWidth: 160 }}
                spellCheck={false}
              />
            </div>
          </div>

          {/* ---- 03 EXTERNAL SUBTITLES (OPTIONAL) ---- */}
          <div className="panel">
            <div className="step">
              <span className="n">03</span>
              <span>External subtitles (optional)</span>
            </div>
            <details className="expander" open={useExternalSub} style={{ margin: 0 }}>
              <summary
                onClick={(e) => {
                  e.preventDefault()
                  setUseExternalSub((v) => !v)
                }}
              >
                <input
                  type="checkbox"
                  checked={useExternalSub}
                  readOnly
                  tabIndex={-1}
                  aria-hidden="true"
                  style={{
                    pointerEvents: 'none',
                    accentColor: 'var(--amber)',
                    verticalAlign: '-2px',
                    marginRight: 7,
                  }}
                />
                Attach external subtitle files
              </summary>
              <div className="body">
                <FolderField
                  label="Subtitle folder"
                  value={subFolder}
                  onChange={setSubFolder}
                  placeholder="Defaults to the source folder"
                  startDir={folder || '~/Desktop'}
                />
                {subError && <div className="note error">{subError}</div>}
                {!subError && selectedPaths.length === 0 && (
                  <div className="note info">
                    Select videos in step 01 to preview subtitle matches.
                  </div>
                )}
                {!subError && selectedPaths.length > 0 && (
                  <>
                    <div style={{ font: '11px var(--mono)', color: 'var(--muted)', margin: '2px 0 6px' }}>
                      Matched by filename (external takes priority over embedded):
                    </div>
                    <div style={{ overflowX: 'auto' }}>
                      <table className="table">
                        <thead>
                          <tr>
                            <th>Video</th>
                            <th>Subtitle</th>
                          </tr>
                        </thead>
                        <tbody>
                          {matches.map((m) => (
                            <tr key={m.video}>
                              <td>{baseName(m.video)}</td>
                              <td>{m.subtitle ? baseName(m.subtitle) : '— none —'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </div>
            </details>
          </div>

          {/* ---- 04 OUTPUT ---- */}
          <div className="panel">
            <div className="step">
              <span className="n">04</span>
              <span>Output</span>
            </div>
            <FolderField
              label="Output folder"
              value={outFolder}
              onChange={setOutFolder}
              placeholder="~/Desktop/🎬"
              startDir="~/Desktop"
            />
            <div className="field" style={{ marginBottom: 0 }}>
              <label htmlFor="rx-workers">Parallel workers — {workers}</label>
              <input
                id="rx-workers"
                type="range"
                min="1"
                max="8"
                value={workers}
                onChange={(e) => setWorkers(Number(e.target.value))}
                style={{ width: '100%', accentColor: 'var(--amber)', minHeight: 28 }}
              />
            </div>
          </div>
        </div>

        {/* ---- RUN & RESULTS ---- */}
        <div className="panel">
          <Button
            variant="primary"
            onClick={startRemux}
            disabled={running || (useExternalSub && !matchesReady)}
            loading={useExternalSub && !matchesReady}
            style={{ width: '100%' }}
          >
            {useExternalSub && !matchesReady ? 'Matching subtitles…' : '🚀 Start remuxing'}
          </Button>
          <div style={{ ...hintStyle, marginTop: 8 }}>
            Lossless stream copy — one LED bar per file below.
          </div>
          {error && <div className="note error">{error}</div>}
          {!snapshot && !error && (
            <div className="note info">
              Scan a folder, tick the videos, tune the tracks, then start.
            </div>
          )}
          <JobPanel snapshot={snapshot}>
            {result && (
              <>
                <div className="metrics">
                  <div className="metric">
                    <div className="v">{result.total}</div>
                    <div className="k">Total</div>
                  </div>
                  <div className="metric ok">
                    <div className="v">{result.successful}</div>
                    <div className="k">Success ✅</div>
                  </div>
                  <div className={result.failed.length ? 'metric bad' : 'metric'}>
                    <div className="v">{result.failed.length}</div>
                    <div className="k">Failed ❌</div>
                  </div>
                </div>
                {result.failed.length > 0 && (
                  <div>
                    <div className="step" style={{ margin: '12px 0 6px' }}>
                      <span>⚠️ Failures</span>
                    </div>
                    {result.failed.map((f, i) => (
                      <div className="note error" key={i}>
                        🔴 {f.title}: {f.error}
                      </div>
                    ))}
                  </div>
                )}
                <div className="note ok">Done! Output saved to: {result.out_folder}</div>
              </>
            )}
          </JobPanel>
        </div>
      </div>
    </div>
  )
}
