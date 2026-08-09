// Torrent Downloader — pick which BitComet gets the job, add magnets or
// .torrent files, keep only the files worth keeping, and hand the task over.
// There is no queue on this page: once a torrent is sent it is BitComet's, and
// BitComet's own window is where it is paused, resumed, watched and removed.
// Mirrors backend/src/toolkit_api/routers/torrent.py.

import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { copyText } from '../clipboard'
import Button from '../components/Button'
import FileDrop from '../components/FileDrop'
import FolderField from '../components/FolderField'
import {
  CATEGORIES,
  DEFAULT_SAVE_DIR,
  MB,
  addTorrent,
  formatBytes,
  magnetLink,
  parseMagnetLines,
  retryableSend,
  ruleKey,
  selectionFor,
  truncateMiddle,
  updateTorrent,
} from '../torrent'
import type {
  TorrentDevice,
  TorrentDeviceList,
  TorrentDeviceTest,
  TorrentFileRow,
  TorrentResolve,
  TorrentStatus,
} from '../types/api'

const NO_OVERRIDES: ReadonlyMap<number, boolean> = new Map()
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))
const errMsg = (e: unknown, fallback: string) => (e as Error).message || fallback

// A send that timed out gets this many passes before it lands in Failed.
// Passes, not per-torrent retries: the whole queue is sent once, whatever
// timed out is harvested, and the survivors go again together — so torrent #1
// retrying never holds torrent #2's first attempt hostage.
const SEND_PASSES = 3
// The wait before pass 2 and pass 3. Short first — a hiccup clears fast —
// then long enough for a BitComet that has just started a batch of tasks to
// finish allocating and hash-checking them, which is what the timeouts
// actually are (see REMOTE_TIMEOUT in the backend).
const RETRY_WAITS_MS = [5_000, 15_000]

// A torrent that failed, with the link needed to try it again somewhere else.
// The magnet is the whole point of keeping the row: a dead tracker or a
// sleeping NAS is a reason to retry later, not to lose what was pasted.
interface Failure {
  id: string
  msg: string
  magnet: string | null
}

interface DeviceForm {
  id: string | null // null = adding a new one
  label: string
  url: string
  username: string
  password: string
}

const BLANK_DEVICE: DeviceForm = { id: null, label: '', url: '', username: '', password: '' }

// A magnet is too long to show whole in an error line; its btih is enough.
function magnetLabel(uri: string): string {
  return uri.match(/btih:([a-z0-9]+)/i)?.[1]?.slice(0, 12) ?? uri.slice(0, 24)
}

function CopyButton({ text, label = 'Copy magnet' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'ok' | 'fail'>('idle')

  async function run() {
    try {
      await copyText(text)
      setState('ok')
    } catch {
      // Never silent: off a secure origin the copy can genuinely fail, and a
      // button that lies about it costs the user the link entirely.
      setState('fail')
    }
    setTimeout(() => setState('idle'), 1500)
  }

  return (
    <Button size="sm" onClick={() => void run()}>
      {state === 'ok' ? '✓ Copied' : state === 'fail' ? 'Select it instead' : label}
    </Button>
  )
}

function FileList({
  files,
  selected,
  onToggle,
}: {
  files: TorrentFileRow[]
  selected: Set<number>
  onToggle: (index: number) => void
}) {
  return (
    <div className="tor-files">
      {files.map((file) => (
        <label key={file.index} className="tor-file" title={file.path}>
          <input
            type="checkbox"
            checked={selected.has(file.index)}
            onChange={() => onToggle(file.index)}
          />
          {/* Middle-truncated, not end-truncated: the tail carries the
              extension and the quality/episode tag, which is exactly what
              tells two otherwise identical rows apart. Full path on hover. */}
          <span className="tor-path">{truncateMiddle(file.path, 56)}</span>
          <span className="tor-cat">{file.category}</span>
          <span className="tor-size">{formatBytes(file.size)}</span>
        </label>
      ))}
    </div>
  )
}

export default function TorrentDownloader() {
  const [status, setStatus] = useState<TorrentStatus | null>(null)

  // --- which BitComet (step 0) ---
  const [devices, setDevices] = useState<TorrentDeviceList | null>(null)
  const [picking, setPicking] = useState(false)
  const [form, setForm] = useState<DeviceForm | null>(null)
  const [testing, setTesting] = useState(false)
  const [tested, setTested] = useState<TorrentDeviceTest | null>(null)
  const [deviceBusy, setDeviceBusy] = useState(false)
  const [deviceError, setDeviceError] = useState<string | null>(null)

  // --- inputs (step 1) ---
  const [magnets, setMagnets] = useState('')
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [staging, setStaging] = useState(false)

  // --- shared filter + destination (step 2) ---
  const [categories, setCategories] = useState<Set<string>>(new Set(['video']))
  const [minMb, setMinMb] = useState(100)
  // Mirrors DEFAULT_SAVE_DIR in backend/src/toolkit_api/torrents.py. Prefilled
  // so downloads land in ~/Downloads with no extra click; the backend expands
  // the tilde. Browsing swaps in an absolute path. For a BitComet on the LAN
  // this is replaced by one of THAT machine's own folders — see the effect
  // below, and ensure_save_folder for why a local path cannot be used there.
  const [saveDir, setSaveDir] = useState(DEFAULT_SAVE_DIR)

  // --- resolved torrents under review (step 3) ---
  const [resolved, setResolved] = useState<TorrentResolve[]>([])
  const [resolvingHashes, setResolvingHashes] = useState<Set<string>>(new Set())
  // Collapsed by default. A review list is scanned far more often than it is
  // corrected — the filter usually got it right — so the summary is what the
  // row shows, and the file table opens only for the one being questioned.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [failures, setFailures] = useState<Failure[]>([])
  // Per-torrent file ticks, keyed by infohash so two torrents' index-1 files
  // never collide, then by the rule they were made against so a filter change
  // discards them.
  const [overrides, setOverrides] = useState<
    Map<string, { key: string; map: Map<number, boolean> }>
  >(new Map())

  // The magnet each infohash arrived as, so a failure can hand back the LINK
  // the user actually pasted — trackers and all — rather than a reconstructed
  // one. A ref, not state: nothing renders from it directly, and re-rendering
  // the page every time a magnet is staged would be pure churn.
  const sources = useRef<Map<string, string>>(new Map())

  // --- handed over ---
  // How many torrents this visit sent, and nothing else. Deliberately not a
  // list and not a queue: a receipt carries no progress and is never polled,
  // because the moment a task is sent BitComet is the only thing that knows
  // what it is doing.
  const [sentCount, setSentCount] = useState(0)
  // A batch (including its retry passes) runs one at a time; these keep the
  // buttons honest while it does. `sending` is the row in flight right now,
  // `retryNote` narrates the harvest so a 15s wait reads as patience, not a
  // hang.
  const [batching, setBatching] = useState(false)
  const [sending, setSending] = useState<Set<string>>(new Set())
  const [retryNote, setRetryNote] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      const [next, book] = await Promise.all([
        api.torrentStatus().catch(
          (): TorrentStatus => ({ running: false, server: null, detail: null, url: null }),
        ),
        api.torrentDevices().catch(() => null),
      ])
      if (cancelled) return
      setStatus(next)
      setDevices(book)
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // A destination is a path on a PARTICULAR machine: `~/Downloads` means
  // nothing on a NAS, and `/volume1/downloads` means nothing here. So each
  // device keeps its own, and switching swaps the box rather than carrying a
  // path across to a filesystem it does not exist on.
  const dirsByDevice = useRef<Map<string, string>>(new Map())
  const deviceId = status?.device?.id ?? null

  useEffect(() => {
    if (status === null || deviceId === null) return
    const folders = status.save_folders ?? []
    // What that device would pick for itself: its own first registered folder
    // when it is remote (nothing here can browse it), the usual default here.
    const fallback = status.is_local === false ? (folders[0] ?? '') : DEFAULT_SAVE_DIR
    setSaveDir(dirsByDevice.current.get(deviceId) ?? fallback)
    // Keyed on the device alone. Depending on `status` as well would re-run on
    // every re-probe and stamp over a folder the user was halfway through
    // typing; the values read from it are only ever needed at a switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId])

  function changeSaveDir(next: string) {
    setSaveDir(next)
    if (deviceId !== null) dirsByDevice.current.set(deviceId, next)
  }

  async function refreshStatus() {
    try {
      setStatus(await api.torrentStatus())
    } catch {
      setStatus({ running: false, server: null, detail: null, url: null })
    }
  }

  function pushFailure(id: string, msg: string, magnet: string | null = null) {
    setFailures((prev) => [...prev, { id, msg, magnet }])
  }

  // The best magnet available for a torrent: the one pasted if this page still
  // has it, otherwise the minimal form its infohash allows. A .torrent upload
  // never had a magnet, and after a failure the infohash is the only handle
  // left on it — so reconstructing beats offering nothing to copy.
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

  // =======================================================
  // DEVICES
  // =======================================================
  async function applyDevices(run: () => Promise<TorrentDeviceList>) {
    setDeviceBusy(true)
    setDeviceError(null)
    try {
      setDevices(await run())
      setForm(null)
      setTested(null)
      // Every device change re-points the backend at a different BitComet, so
      // the status on screen is about the wrong machine until this lands.
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
      // Left blank on purpose: the browser is never sent the stored password,
      // and blank means "keep it" all the way through to the device book.
      password: '',
    })
  }

  // =======================================================
  // RESOLVE
  // =======================================================
  // Selection for one torrent: shared rule + that torrent's own live ticks.
  function selectedFor(t: TorrentResolve): Set<number> {
    const entry = overrides.get(t.infohash)
    const active =
      entry && entry.key === ruleKey(t.infohash, categories, minMb) ? entry.map : NO_OVERRIDES
    return selectionFor(t, categories, minMb * MB, active)
  }

  async function pollUntilReady(infohash: string) {
    for (;;) {
      await sleep(1500)
      let next: TorrentResolve
      try {
        next = await api.torrentPollResolve(infohash)
      } catch (e) {
        clearResolving(infohash)
        pushFailure(
          infohash.slice(0, 12),
          errMsg(e, 'Could not reach BitComet.'),
          magnetFor(infohash),
        )
        return
      }
      if (next.state === 'error') {
        clearResolving(infohash)
        pushFailure(
          next.name ?? infohash.slice(0, 12),
          'Metadata fetch failed — dead magnet or no seeders.',
          magnetFor(infohash, next.name),
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
        void pollUntilReady(out.infohash) // background; don't block the others
      }
    } catch (e) {
      pushFailure(magnetLabel(uri), errMsg(e, 'Could not read that magnet link.'), uri)
    }
  }

  async function stageFile(file: File) {
    try {
      const out = await api.torrentResolveFile(file, saveDir.trim())
      setResolved((prev) => addTorrent(prev, out))
    } catch (e) {
      // No magnet to hand back: this one only ever existed as a file, and a
      // failed parse never produced an infohash to build one from.
      pushFailure(file.name, errMsg(e, 'Could not read that .torrent file.'))
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
    // Every line and file resolves on its own; one bad magnet does not sink the
    // rest (allSettled, never all).
    await Promise.allSettled([...lines.map(stageMagnet), ...files.map(stageFile)])
    setStaging(false)
  }

  // Drop a resolved torrent from the review list and forget its ticks.
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

  // One attempt at one torrent. 'retry' means it failed in the way that
  // clears up on its own (timeout, unreachable) and a later pass may try
  // again; anything else is final — success, or a failure worth reporting.
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
      const reason = errMsg(e, 'Could not send that torrent.')
      pushFailure(
        t.name ?? t.infohash.slice(0, 12),
        final && retryableSend(e)
          ? `still failing after ${SEND_PASSES} tries: ${reason}`
          : reason,
        magnetFor(t.infohash, t.name),
      )
      // The row stays in the review list on purpose: the selection is intact,
      // so once BitComet is back a manual Send needs no re-pasting.
      return 'failed'
    } finally {
      setSending((prev) => {
        const next = new Set(prev)
        next.delete(t.infohash)
        return next
      })
    }
  }

  // Send everything, then HARVEST what timed out and send it again — up to
  // SEND_PASSES passes. This exists because of a measured batch of 34 sends
  // where most "failures" were read timeouts against a BitComet that was
  // merely grinding through the tasks it had just been handed: the work
  // itself had usually landed, and a retry that finds it landed simply
  // succeeds (send is idempotent — see retryableSend). Only what still fails
  // on the last pass reaches the Failed panel.
  async function sendBatch(targets: TorrentResolve[]) {
    setBatching(true)
    try {
      let queue = targets.filter((t) => t.ready && selectedFor(t).size > 0)
      for (let pass = 0; pass < SEND_PASSES && queue.length > 0; pass++) {
        const final = pass === SEND_PASSES - 1
        const again: TorrentResolve[] = []
        for (const t of queue) {
          if ((await sendOne(t, final)) === 'retry') again.push(t)
        }
        queue = again
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

  // Cancelling a staging, not managing a task. A magnet is added RUNNING so it
  // can fetch its metadata, so simply closing the card would leave it
  // downloading in BitComet with every file still enabled.
  async function discardOne(t: TorrentResolve) {
    closeCard(t.infohash)
    try {
      await api.torrentDiscard(t.infohash)
    } catch (e) {
      pushFailure(
        t.name ?? t.infohash.slice(0, 12),
        errMsg(e, 'Could not discard that torrent.'),
        magnetFor(t.infohash, t.name),
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

  function toggleFile(t: TorrentResolve, index: number) {
    const key = ruleKey(t.infohash, categories, minMb)
    const current = selectedFor(t)
    setOverrides((prev) => {
      const entry = prev.get(t.infohash)
      const map = new Map(entry && entry.key === key ? entry.map : [])
      map.set(index, !current.has(index))
      const next = new Map(prev)
      next.set(t.infohash, { key, map })
      return next
    })
  }

  // Tick or untick every file at once, as an override over the shared rule.
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
  // The destination is needed to resolve, not to add: BitComet fixes a task's
  // save folder when the task is created and cannot move it afterwards.
  const noDestination = !saveDir.trim()
  const readyCount = resolved.filter((t) => t.ready && selectedFor(t).size > 0).length
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

      {/* ---------- which BitComet ---------- */}
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
              {deviceError && <div className="note error">{deviceError}</div>}
            </div>
          )}
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
              disabled={nothingToResolve || noDestination || bitcometDown}
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

          {/* The native picker browses THIS Mac, which is the wrong filesystem
              for a BitComet on the LAN — so a remote device gets that device's
              own registered folders instead of a Browse button. */}
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
              const selected = selectedFor(t)
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
                      {/* Always available, fetching or not: a magnet is already
                          running in BitComet while it looks for its metadata,
                          so this is the only way to call one off. */}
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
                      <FileList
                        files={t.files}
                        selected={selected}
                        onToggle={(index) => toggleFile(t, index)}
                      />
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
            {copyableFailures.length > 1 && (
              <CopyButton
                text={copyableFailures.map((f) => f.magnet).join('\n')}
                label={`Copy all ${copyableFailures.length} magnets`}
              />
            )}
            <Button size="sm" variant="ghost" onClick={() => setFailures([])}>
              Clear
            </Button>
          </div>

          {/* The link, not just the reason. A magnet that failed is almost
              always worth another try — later, or on a different device — and
              re-finding it means going back to wherever it was copied from. */}
          {failures.map((f, i) => (
            <div key={`${f.id}-${i}`} className="note error" style={{ margin: '0 0 6px' }}>
              <div className="row" style={{ gap: 8 }}>
                <span className="grow" style={{ minWidth: 0 }}>
                  <strong>{truncateMiddle(f.id, 48)}</strong> — {f.msg}
                </span>
              </div>
              {f.magnet && (
                <div className="row" style={{ gap: 8, marginTop: 6 }}>
                  <code className="tor-fail-link" title={f.magnet}>
                    {f.magnet}
                  </code>
                  <CopyButton text={f.magnet} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* One LINE, not a list. The per-task receipt rows grew to a screenful
          on a 34-torrent batch while saying the same thing 34 times, and this
          page deliberately has nothing further to show about a sent task --
          progress belongs to BitComet's own window. The count is the receipt;
          the button is the handover. */}
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
