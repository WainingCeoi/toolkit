import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useToolActive, useToolBusy } from '../toolHost'
import Button from '../components/Button'
import CodeBox from '../components/CodeBox'
import FileDrop from '../components/FileDrop'
import FolderField from '../components/FolderField'
import {
  CATEGORIES,
  DEFAULT_SAVE_DIR,
  addTorrent,
  formatBytes,
  magnetLink,
  parseMagnetLines,
  retryableSend,
  ruleKey,
  selectionUnder,
  truncateMiddle,
  updateTorrent,
  windowedRun,
} from '../torrent'
import type {
  TorrentDevice,
  TorrentDeviceList,
  TorrentDeviceTest,
  TorrentResolve,
  TorrentStatus,
} from '../types/api'

const NO_SELECTION: ReadonlySet<number> = new Set()
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const errMsg = (e: unknown, fallback: string) => (e as Error).message || fallback

// Whole-queue passes a timed-out send gets before it lands in Failed.
const SEND_PASSES = 3
// Waits before pass 2 and 3; the long one lets BitComet finish hash-checking a fresh batch.
const RETRY_WAITS_MS = [5_000, 15_000]
// Sends in flight at once; each answer admits the next (see windowedRun).
const SEND_WINDOW = 10
// Magnets fetching in BitComet at once; the rest of a paste is held here until a slot frees.
const RESOLVE_WINDOW = 10

interface Failure {
  id: string
  magnet: string | null
  reason: string | null
}

interface DeviceForm {
  id: string | null
  label: string
  url: string
  username: string
  password: string
}

const BLANK_DEVICE: DeviceForm = { id: null, label: '', url: '', username: '', password: '' }

function magnetLabel(uri: string): string {
  return uri.match(/btih:([a-z0-9]+)/i)?.[1]?.slice(0, 12) ?? uri.slice(0, 24)
}

// Memoized: a season pack is thousands of rows, and every page keystroke re-renders.
const FileList = memo(function FileList({
  torrent,
  selected,
  onToggle,
}: {
  torrent: TorrentResolve
  selected: ReadonlySet<number>
  onToggle: (t: TorrentResolve, index: number) => void
}) {
  return (
    <div className="tor-files">
      {torrent.files.map((file) => (
        <label key={file.index} className="tor-file" title={file.path}>
          <input
            type="checkbox"
            checked={selected.has(file.index)}
            onChange={() => onToggle(torrent, file.index)}
          />
          {/* Middle-truncated: the tail carries the extension and episode tag. */}
          <span className="tor-path">{truncateMiddle(file.path, 56)}</span>
          <span className="tor-cat">{file.category}</span>
          <span className="tor-size">{formatBytes(file.size)}</span>
        </label>
      ))}
    </div>
  )
})

export default function TorrentDownloader() {
  const [status, setStatus] = useState<TorrentStatus | null>(null)

  const [devices, setDevices] = useState<TorrentDeviceList | null>(null)
  const [picking, setPicking] = useState(false)
  const [form, setForm] = useState<DeviceForm | null>(null)
  const [testing, setTesting] = useState(false)
  const [tested, setTested] = useState<TorrentDeviceTest | null>(null)
  const [deviceBusy, setDeviceBusy] = useState(false)
  const [deviceError, setDeviceError] = useState<string | null>(null)

  const [magnets, setMagnets] = useState('')
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [staging, setStaging] = useState(false)
  const [heldCount, setHeldCount] = useState(0)

  const [categories, setCategories] = useState<Set<string>>(new Set(['video']))
  const [minMb, setMinMb] = useState(100)
  const [saveDir, setSaveDir] = useState(DEFAULT_SAVE_DIR)

  const [resolved, setResolved] = useState<TorrentResolve[]>([])
  const [resolvingHashes, setResolvingHashes] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [failures, setFailures] = useState<Failure[]>([])
  // Ticks per infohash, tagged with the rule they were made under; a filter change drops them.
  const [overrides, setOverrides] = useState<
    Map<string, { key: string; map: Map<number, boolean> }>
  >(new Map())

  // The pasted magnet per infohash, handed back on failure; a ref since nothing renders it.
  const sources = useRef<Map<string, string>>(new Map())

  const [sentCount, setSentCount] = useState(0)
  const [batching, setBatching] = useState(false)
  const [sending, setSending] = useState<Set<string>>(new Set())
  const [retryNote, setRetryNote] = useState<string | null>(null)

  // Resolving and sending live in this component, not the job registry; keep the tab open.
  useToolBusy(staging || batching || resolvingHashes.size > 0)

  // Keyed on tab visibility, not mount: keep-alive keeps this page mounted all session.
  const tabActive = useToolActive()
  // Probing a sleeping remote takes seconds; only the newest probe may write status.
  const statusSeq = useRef(0)
  useEffect(() => {
    if (!tabActive) return
    let cancelled = false
    const seq = (statusSeq.current += 1)
    void (async () => {
      const [next, book] = await Promise.all([
        api.torrentStatus().catch(
          (): TorrentStatus => ({ running: false, server: null, detail: null, url: null }),
        ),
        api.torrentDevices().catch(() => null),
      ])
      if (cancelled || seq !== statusSeq.current) return
      setStatus(next)
      setDevices(book)
    })()
    return () => {
      cancelled = true
    }
  }, [tabActive])

  // A save path belongs to one machine, so each device keeps its own.
  const dirsByDevice = useRef<Map<string, string>>(new Map())
  const deviceId = status?.device?.id ?? null

  useEffect(() => {
    if (status === null || deviceId === null) return
    const folders = status.save_folders ?? []
    const fallback = status.is_local === false ? (folders[0] ?? '') : DEFAULT_SAVE_DIR
    setSaveDir(dirsByDevice.current.get(deviceId) ?? fallback)
    // Keyed on the device alone: re-running on every status probe would clobber a typed folder.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId])

  function changeSaveDir(next: string) {
    setSaveDir(next)
    if (deviceId !== null) dirsByDevice.current.set(deviceId, next)
  }

  async function refreshStatus() {
    const seq = (statusSeq.current += 1)
    let next: TorrentStatus
    try {
      next = await api.torrentStatus()
    } catch {
      next = { running: false, server: null, detail: null, url: null }
    }
    if (seq === statusSeq.current) setStatus(next)
  }

  function pushFailure(id: string, magnet: string | null = null, reason: string | null = null) {
    setFailures((prev) => [...prev, { id, magnet, reason }])
  }

  function magnetFor(infohash: string, name?: string | null): string {
    return sources.current.get(infohash) ?? magnetLink(infohash, name)
  }

  function clearResolving(infohash: string) {
    setResolvingHashes((prev) => {
      const next = new Set(prev)
      next.delete(infohash)
      return next
    })
  }

  // --- DEVICES ---
  async function applyDevices(run: () => Promise<TorrentDeviceList>) {
    setDeviceBusy(true)
    setDeviceError(null)
    try {
      setDevices(await run())
      setForm(null)
      setTested(null)
      await refreshStatus()
    } catch (e) {
      setDeviceError(errMsg(e, 'Could not change the BitComet device.'))
    } finally {
      setDeviceBusy(false)
    }
  }

  async function testForm() {
    if (form === null) return
    setTesting(true)
    setTested(null)
    try {
      setTested(
        await api.torrentDeviceTest({
          url: form.url,
          username: form.username,
          password: form.password,
          id: form.id ?? undefined,
        }),
      )
    } catch (e) {
      setTested({ ok: false, server: null, detail: errMsg(e, 'Test failed.'), save_folders: [] })
    } finally {
      setTesting(false)
    }
  }

  function saveForm() {
    if (form === null) return
    const payload = {
      label: form.label,
      url: form.url,
      username: form.username,
      password: form.password,
    }
    void applyDevices(() =>
      form.id === null
        ? api.torrentDeviceAdd(payload)
        : api.torrentDeviceUpdate(form.id, payload),
    )
  }

  function editDevice(device: TorrentDevice) {
    setTested(null)
    setDeviceError(null)
    setForm({
      id: device.id,
      label: device.label,
      url: device.url ?? '',
      username: device.username,
      // Blank means "keep the stored password"; it is never sent to the browser.
      password: '',
    })
  }

  // --- RESOLVE ---
  // A ref so a retry pass sends the ticks as they are now, not as they were at click time.
  const liveSelection = useRef({ overrides, categories, minMb })
  liveSelection.current = { overrides, categories, minMb }

  function selectedFor(t: TorrentResolve): Set<number> {
    return selectionUnder(t, liveSelection.current)
  }

  // Rendered ticks, computed once per torrent: unrelated state must not re-tick every file.
  const selections = useMemo(() => {
    const rules = { overrides, categories, minMb }
    return new Map(resolved.map((t) => [t.infohash, selectionUnder(t, rules)]))
  }, [resolved, overrides, categories, minMb])

  // Discard deletes the task in BitComet, so a poll crossing it 404s; that is not a failure.
  const discarded = useRef<Set<string>>(new Set())

  async function pollUntilReady(infohash: string) {
    discarded.current.delete(infohash)
    for (;;) {
      await sleep(1500)
      if (discarded.current.delete(infohash)) return
      let next: TorrentResolve
      try {
        next = await api.torrentPollResolve(infohash)
      } catch (e) {
        if (discarded.current.delete(infohash)) return
        clearResolving(infohash)
        pushFailure(
          infohash.slice(0, 12),
          magnetFor(infohash),
          errMsg(e, 'That magnet could not be resolved.'),
        )
        return
      }
      if (next.state === 'error') {
        clearResolving(infohash)
        pushFailure(
          next.name ?? infohash.slice(0, 12),
          magnetFor(infohash, next.name),
          'No metadata arrived in time — no peer answered for it.',
        )
        return
      }
      if (next.ready) {
        setResolved((prev) => updateTorrent(prev, next))
        clearResolving(infohash)
        return
      }
    }
  }

  async function stageMagnet(uri: string) {
    try {
      const out = await api.torrentResolveMagnet(uri, saveDir.trim())
      sources.current.set(out.infohash, uri)
      setResolved((prev) => addTorrent(prev, out))
      if (!out.ready) {
        setResolvingHashes((prev) => new Set(prev).add(out.infohash))
        // Awaited: the window slot is held until the metadata lands, not until the add returns.
        await pollUntilReady(out.infohash)
      }
    } catch (e) {
      pushFailure(magnetLabel(uri), uri, errMsg(e, 'That magnet could not be staged.'))
    }
  }

  async function stageFile(file: File) {
    try {
      const out = await api.torrentResolveFile(file, saveDir.trim())
      setResolved((prev) => addTorrent(prev, out))
    } catch (e) {
      pushFailure(file.name, null, errMsg(e, 'That .torrent could not be read.'))
    }
  }

  async function resolveAll() {
    const lines = parseMagnetLines(magnets)
    const files = pendingFiles
    if ((!lines.length && !files.length) || !saveDir.trim()) return
    setFailures([])
    setStaging(true)
    setMagnets('')
    setPendingFiles([])
    // Files first: each frees its window slot in one round trip.
    const work = [
      ...files.map((file) => () => stageFile(file)),
      ...lines.map((uri) => () => stageMagnet(uri)),
    ]
    let started = 0
    try {
      await windowedRun(
        work,
        (job) => {
          started += 1
          setHeldCount(work.length - started)
          return job()
        },
        RESOLVE_WINDOW,
      )
    } finally {
      setStaging(false)
      setHeldCount(0)
    }
  }

  function closeCard(infohash: string) {
    setResolved((prev) => prev.filter((x) => x.infohash !== infohash))
    setOverrides((prev) => {
      const next = new Map(prev)
      next.delete(infohash)
      return next
    })
    setExpanded((prev) => {
      const next = new Set(prev)
      next.delete(infohash)
      return next
    })
  }

  async function sendOne(t: TorrentResolve, final: boolean): Promise<'ok' | 'retry' | 'failed'> {
    const selected = selectedFor(t)
    if (selected.size === 0) return 'ok'
    setSending((prev) => new Set(prev).add(t.infohash))
    try {
      await api.torrentSend({
        infohash: t.infohash,
        selected: [...selected].sort((a, b) => a - b),
      })
      setSentCount((n) => n + 1)
      closeCard(t.infohash)
      return 'ok'
    } catch (e) {
      if (!final && retryableSend(e)) return 'retry'
      pushFailure(
        t.name ?? t.infohash.slice(0, 12),
        magnetFor(t.infohash, t.name),
        errMsg(e, 'BitComet would not take it.'),
      )
      // The row stays in the review list so a manual Send needs no re-pasting.
      return 'failed'
    } finally {
      setSending((prev) => {
        const next = new Set(prev)
        next.delete(t.infohash)
        return next
      })
    }
  }

  async function sendPass(queue: TorrentResolve[], final: boolean): Promise<TorrentResolve[]> {
    const again: TorrentResolve[] = []
    let answered = 0
    const narrate = queue.length > SEND_WINDOW
    if (narrate) setRetryNote(`sending ${SEND_WINDOW} at a time — 0 of ${queue.length} answered`)
    await windowedRun(
      queue,
      async (t) => {
        if ((await sendOne(t, final)) === 'retry') again.push(t)
        answered += 1
        if (narrate) {
          setRetryNote(`sending ${SEND_WINDOW} at a time — ${answered} of ${queue.length} answered`)
        }
      },
      SEND_WINDOW,
    )
    return again
  }

  // A timed-out send usually landed anyway; send is idempotent, so retry passes are safe.
  async function sendBatch(targets: TorrentResolve[]) {
    setBatching(true)
    try {
      let queue = targets.filter((t) => t.ready && selectedFor(t).size > 0)
      for (let pass = 0; pass < SEND_PASSES && queue.length > 0; pass++) {
        const final = pass === SEND_PASSES - 1
        queue = await sendPass(queue, final)
        if (queue.length > 0 && !final) {
          const wait = RETRY_WAITS_MS[pass] ?? 15_000
          setRetryNote(
            `${queue.length} timed out — BitComet is busy, retrying in ${wait / 1000}s ` +
              `(pass ${pass + 2} of ${SEND_PASSES})`,
          )
          await sleep(wait)
          setRetryNote(`retrying ${queue.length}…`)
        }
      }
    } finally {
      setBatching(false)
      setRetryNote(null)
    }
  }

  // A staged magnet already runs in BitComet; closing the card alone would leave it downloading.
  async function discardOne(t: TorrentResolve) {
    discarded.current.add(t.infohash)
    clearResolving(t.infohash)
    closeCard(t.infohash)
    try {
      await api.torrentDiscard(t.infohash)
    } catch (e) {
      pushFailure(
        t.name ?? t.infohash.slice(0, 12),
        magnetFor(t.infohash, t.name),
        errMsg(e, 'BitComet would not drop it.'),
      )
    }
  }

  function toggleCategory(key: string) {
    setCategories((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleFile = useCallback((t: TorrentResolve, index: number) => {
    const { categories: cats, minMb: floor } = liveSelection.current
    const key = ruleKey(t.infohash, cats, floor)
    const current = selectionUnder(t, liveSelection.current)
    setOverrides((prev) => {
      const entry = prev.get(t.infohash)
      const map = new Map(entry && entry.key === key ? entry.map : [])
      map.set(index, !current.has(index))
      const next = new Map(prev)
      next.set(t.infohash, { key, map })
      return next
    })
  }, [])

  function setAllFiles(t: TorrentResolve, on: boolean) {
    const key = ruleKey(t.infohash, categories, minMb)
    setOverrides((prev) => {
      const next = new Map(prev)
      next.set(t.infohash, { key, map: new Map(t.files.map((f) => [f.index, on])) })
      return next
    })
  }

  function toggleExpanded(infohash: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(infohash)) next.delete(infohash)
      else next.add(infohash)
      return next
    })
  }

  const bitcometDown = status !== null && !status.running
  const nothingToResolve = parseMagnetLines(magnets).length === 0 && pendingFiles.length === 0
  const noDestination = !saveDir.trim()
  const readyCount = resolved.filter(
    (t) => t.ready && (selections.get(t.infohash) ?? NO_SELECTION).size > 0,
  ).length
  const active = status?.device ?? devices?.devices.find((d) => d.id === devices.active) ?? null
  const remote = status?.is_local === false
  const folders = status?.save_folders ?? []
  const copyableFailures = failures.filter((f) => f.magnet !== null)

  return (
    <>
      <div className="page-head">
        <h1>🌊 Torrent Downloader</h1>
      </div>
      <p className="page-sub">
        Paste magnets or pick .torrent files, review what is inside them, and send only the files
        worth keeping to BitComet. From there the download is BitComet's — pause it, watch it and
        remove it in its own window.
      </p>

      <div className="tor-device">
        <span className={`lamp${status?.running ? '' : ' off'}`}>
          <i />
        </span>
        <span className="grow" style={{ minWidth: 0 }}>
          <span className="tor-device-name">
            {active?.label ?? 'BitComet'}
            {status?.server && (
              <span className="tor-device-url"> · {status.server}</span>
            )}
          </span>
          <br />
          <span className="tor-device-url">{status?.url ?? 'not connected'}</span>
        </span>
        <Button size="sm" onClick={() => setPicking((p) => !p)}>
          {picking ? 'Done' : 'Change device'}
        </Button>
        {status?.url && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => window.open(status.url!, '_blank', 'noopener')}
          >
            Open ↗
          </Button>
        )}
      </div>

      {picking && (
        <div className="panel">
          <div className="step">Which BitComet gets the download</div>
          <p style={{ color: 'var(--muted)', fontSize: 13, margin: '0 0 10px' }}>
            BitComet's Remote Access answers on the network, so a torrent can go to another
            machine on this Wi-Fi — the one with the disk space, or the one that stays awake.
            It needs that machine's own Web UI username and password.
          </p>

          <div className="tor-device-list">
            {devices?.devices.map((device) => (
              <label
                key={device.id}
                className={`tor-device-row${device.id === devices.active ? ' active' : ''}`}
              >
                <input
                  type="radio"
                  name="bitcomet-device"
                  checked={device.id === devices.active}
                  disabled={deviceBusy}
                  onChange={() => void applyDevices(() => api.torrentDeviceSelect(device.id))}
                />
                <span className="grow" style={{ minWidth: 0 }}>
                  <span className="tor-device-name">{device.label}</span>
                  <br />
                  <span className="tor-device-url">
                    {device.is_local ? 'this machine · read from BitComet’s own settings' : device.url}
                  </span>
                </span>
                {!device.is_local && (
                  <>
                    <Button size="sm" variant="ghost" onClick={() => editDevice(device)}>
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={deviceBusy}
                      onClick={() => void applyDevices(() => api.torrentDeviceRemove(device.id))}
                    >
                      Forget
                    </Button>
                  </>
                )}
              </label>
            ))}
          </div>

          {form === null ? (
            <Button size="sm" onClick={() => setForm({ ...BLANK_DEVICE })}>
              + Add a device
            </Button>
          ) : (
            <div className="tor-form">
              <div className="step">{form.id === null ? 'Add a device' : 'Edit device'}</div>
              <div className="field">
                <label htmlFor="dev-url">Address</label>
                <input
                  id="dev-url"
                  className="control"
                  value={form.url}
                  placeholder="192.168.1.50:19377"
                  spellCheck={false}
                  onChange={(e) => setForm({ ...form, url: e.target.value })}
                />
                <p style={{ font: '12px var(--mono)', color: 'var(--faint)', margin: '4px 0 0' }}>
                  Host and port, or a full http:// address. Port 19377 is assumed if you leave
                  it off.
                </p>
              </div>
              <div className="row">
                <div className="field grow">
                  <label htmlFor="dev-user">Web UI username</label>
                  <input
                    id="dev-user"
                    className="control"
                    value={form.username}
                    spellCheck={false}
                    autoComplete="off"
                    onChange={(e) => setForm({ ...form, username: e.target.value })}
                  />
                </div>
                <div className="field grow">
                  <label htmlFor="dev-pass">Web UI password</label>
                  <input
                    id="dev-pass"
                    className="control"
                    type="password"
                    value={form.password}
                    autoComplete="new-password"
                    placeholder={form.id === null ? '' : 'unchanged'}
                    onChange={(e) => setForm({ ...form, password: e.target.value })}
                  />
                </div>
              </div>
              <div className="field">
                <label htmlFor="dev-label">Name (optional)</label>
                <input
                  id="dev-label"
                  className="control"
                  value={form.label}
                  placeholder="Basement NAS"
                  onChange={(e) => setForm({ ...form, label: e.target.value })}
                />
              </div>

              <div className="row">
                <Button size="sm" loading={testing} disabled={!form.url} onClick={() => void testForm()}>
                  Test connection
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  loading={deviceBusy}
                  disabled={!form.url || !form.username}
                  onClick={saveForm}
                >
                  {form.id === null ? 'Add & use' : 'Save'}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => { setForm(null); setTested(null) }}>
                  Cancel
                </Button>
              </div>

              {tested && (
                <div className={`note ${tested.ok ? 'ok' : 'error'}`}>
                  {tested.ok ? (
                    <>
                      Reached {tested.server}.{' '}
                      {tested.save_folders.length > 0
                        ? `Download folders there: ${tested.save_folders.join(', ')}`
                        : 'It has no download folder configured yet.'}
                    </>
                  ) : (
                    tested.detail
                  )}
                </div>
              )}
            </div>
          )}
          {deviceError && <div className="note error">{deviceError}</div>}
        </div>
      )}

      {bitcometDown && (
        <div className="note error">
          {status?.detail ?? 'BitComet is not answering. Start it and turn on Remote Access.'}
        </div>
      )}

      <div className="station">
        <div className="panel">
          <div className="step">1 · Add torrents</div>

          <div className="field">
            <label htmlFor="magnets">Magnet links</label>
            <textarea
              id="magnets"
              className="control"
              rows={4}
              value={magnets}
              placeholder={'magnet:?xt=urn:btih:…\none per line'}
              onChange={(e) => setMagnets(e.target.value)}
            />
          </div>

          <div className="field">
            <label>…or .torrent files</label>
            <FileDrop
              accept=".torrent,application/x-bittorrent"
              files={pendingFiles}
              onChange={setPendingFiles}
              hint="Drop .torrent files here or click to choose"
            />
          </div>

          <div className="row">
            <Button
              variant="primary"
              loading={staging}
              disabled={nothingToResolve || noDestination || bitcometDown || staging}
              onClick={resolveAll}
            >
              Resolve
            </Button>
            {resolvingHashes.size > 0 && (
              <span className="label" style={{ margin: 0 }}>
                fetching metadata for {resolvingHashes.size} magnet
                {resolvingHashes.size === 1 ? '' : 's'}…
              </span>
            )}
            {heldCount > 0 && (
              <span className="label" style={{ margin: 0 }}>
                {heldCount} held on this side — {RESOLVE_WINDOW} fetch at a time, each answer
                admits the next
              </span>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="step">2 · Choose what to download</div>

          <div className="field">
            <label>File types</label>
            <div className="row">
              {CATEGORIES.map((category) => (
                <label key={category.key} className="check">
                  <input
                    type="checkbox"
                    checked={categories.has(category.key)}
                    onChange={() => toggleCategory(category.key)}
                  />
                  {category.label}
                </label>
              ))}
            </div>
          </div>

          <div className="field">
            <label htmlFor="minmb">Minimum size</label>
            <div className="row">
              <input
                id="minmb"
                type="number"
                min={0}
                className="control"
                value={minMb}
                onChange={(e) => setMinMb(Math.max(0, Number(e.target.value) || 0))}
                style={{ width: 110 }}
              />
              <span style={{ color: 'var(--muted)', fontSize: 13 }}>MB</span>
            </div>
            <p style={{ font: '12px var(--mono)', color: 'var(--faint)', margin: '4px 0 0' }}>
              Applies to video and audio only, so subtitles and small extras are never filtered
              out by size.
            </p>
          </div>

          {/* The folder picker browses this Mac, the wrong filesystem for a remote BitComet. */}
          {remote ? (
            <div className="field">
              <label htmlFor="savedir">Save to (on {active?.label ?? 'that device'})</label>
              <input
                id="savedir"
                className="control"
                value={saveDir}
                list="tor-remote-folders"
                spellCheck={false}
                placeholder={folders[0] ?? '/volume1/downloads'}
                onChange={(e) => changeSaveDir(e.target.value)}
              />
              <datalist id="tor-remote-folders">
                {folders.map((folder) => (
                  <option key={folder} value={folder} />
                ))}
              </datalist>
              <p style={{ font: '12px var(--mono)', color: 'var(--faint)', margin: '4px 0 0' }}>
                {folders.length > 0
                  ? `A folder on that machine — its own are ${folders.join(', ')}.`
                  : 'A full path on that machine. It has no download folder configured yet.'}
              </p>
            </div>
          ) : (
            <FolderField label="Save to" value={saveDir} onChange={changeSaveDir} />
          )}
          <p style={{ font: '12px var(--mono)', color: 'var(--faint)', margin: '4px 0 0' }}>
            Applied when you resolve. BitComet fixes a torrent's folder as it is added, so
            changing this afterwards only affects the next one.
          </p>
          {noDestination && <div className="note info">Choose a destination folder.</div>}
        </div>
      </div>

      {resolved.length > 0 && (
        <div className="panel">
          <div className="row" style={{ marginBottom: 8 }}>
            <div className="step grow" style={{ margin: 0 }}>
              3 · Review ({resolved.length})
            </div>
            {retryNote && (
              <span style={{ font: '11px var(--mono)', color: 'var(--amber-text)' }}>
                {retryNote}
              </span>
            )}
            <Button
              variant="primary"
              size="sm"
              disabled={readyCount === 0 || batching}
              loading={batching}
              onClick={() => void sendBatch(resolved)}
            >
              Send all to BitComet
            </Button>
          </div>

          <div className="tor-list">
            {resolved.map((t) => {
              const selected = selections.get(t.infohash) ?? NO_SELECTION
              const bytes = t.files
                .filter((f) => selected.has(f.index))
                .reduce((sum, f) => sum + f.size, 0)
              const fetching = resolvingHashes.has(t.infohash)
              const open = expanded.has(t.infohash)
              const name = t.name ?? t.infohash.slice(0, 16)
              return (
                <div key={t.infohash} className={`tor-item${open ? ' open' : ''}`}>
                  <div className="tor-head">
                    <button
                      type="button"
                      className="tor-toggle"
                      disabled={!t.ready}
                      aria-expanded={open}
                      aria-controls={`tor-body-${t.infohash}`}
                      onClick={() => toggleExpanded(t.infohash)}
                    >
                      <span className="tor-chev" aria-hidden="true">
                        {t.ready ? '▶' : '·'}
                      </span>
                      <span className="tor-name" title={name}>
                        {truncateMiddle(name, 60)}
                      </span>
                    </button>

                    {fetching ? (
                      <span className="tor-meta">fetching metadata…</span>
                    ) : (
                      <span className={`tor-meta${selected.size === 0 ? ' none' : ''}`}>
                        {selected.size} of {t.files.length} · {formatBytes(bytes)}
                      </span>
                    )}

                    <span className="tor-actions">
                      {!fetching && (
                        <Button
                          variant="primary"
                          size="sm"
                          disabled={selected.size === 0 || batching}
                          loading={sending.has(t.infohash)}
                          onClick={() => void sendBatch([t])}
                        >
                          Send
                        </Button>
                      )}
                      {/* Shown while fetching too: the only way to call off a running magnet. */}
                      <Button size="sm" variant="ghost" onClick={() => void discardOne(t)}>
                        Discard
                      </Button>
                    </span>
                  </div>

                  {open && t.ready && (
                    <div className="tor-body" id={`tor-body-${t.infohash}`}>
                      <div className="tor-bulk">
                        <Button size="sm" variant="ghost" onClick={() => setAllFiles(t, true)}>
                          All
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setAllFiles(t, false)}>
                          None
                        </Button>
                        <span style={{ font: '11px var(--mono)', color: 'var(--faint)' }}>
                          ticks override the filter above
                        </span>
                      </div>
                      <FileList torrent={t} selected={selected} onToggle={toggleFile} />
                    </div>
                  )}

                  {selected.size === 0 && t.ready && (
                    <div className="note warn" style={{ margin: '0 10px 10px' }}>
                      Nothing selected — a torrent with everything deselected finishes instantly
                      having downloaded nothing.
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {failures.length > 0 && (
        <div className="panel">
          <div className="row" style={{ marginBottom: 8 }}>
            <div className="step grow" style={{ margin: 0 }}>
              ⚠ Failed ({failures.length})
            </div>
            <Button size="sm" variant="ghost" onClick={() => setFailures([])}>
              Clear
            </Button>
          </div>

          {copyableFailures.length > 0 && (
            <CodeBox text={copyableFailures.map((f) => f.magnet).join('\n')} />
          )}
          {failures.map((f, i) => (
            <div key={`${f.id}-${i}`} className="note error" style={{ margin: '6px 0 0' }}>
              {truncateMiddle(f.id, 60)}
              {f.reason && ` — ${f.reason}`}
            </div>
          ))}
        </div>
      )}

      {sentCount > 0 && (
        <div className="note ok" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="grow">
            ✓ {sentCount} torrent{sentCount === 1 ? '' : 's'} handed to{' '}
            {active?.label ?? 'BitComet'} — watch, pause and finish them in BitComet's own
            window.
          </span>
          {status?.url && (
            <Button size="sm" onClick={() => window.open(status.url!, '_blank', 'noopener')}>
              Open BitComet
            </Button>
          )}
        </div>
      )}
    </>
  )
}
