// Single HTTP wrapper for the whole app. Components call `api.*` — never
// `fetch` directly — so the base path, error handling, and streaming live in
// one place. Same-origin '/api' works in dev (Vite proxy) and in
// single-origin production (served from the same server).

import type {
  Category,
  DedupeResult,
  DepApplyResult,
  DownloadedBlob,
  GatherStartPayload,
  Health,
  Job,
  JobCancel,
  JobStarted,
  MagnetConfig,
  MarkdownHealth,
  PickFolderResult,
  PurgeScanResult,
  RemuxScanResult,
  RemuxStartPayload,
  RemuxSubtitlesResult,
  Subscription,
  SubsGeneratePayload,
  SubsHistoryItem,
  TorrentDeviceInput,
  TorrentDeviceList,
  TorrentDeviceTest,
  TorrentResolve,
  TorrentSendPayload,
  TorrentSent,
  TorrentStatus,
  WatermarkBatch,
  WatermarkDetector,
  WatermarkHealth,
  WatermarkRunPayload,
  WebPdfCapture,
  WebPdfStatus,
} from './types/api'

const BASE = '/api'

// A non-2xx answer, with the status kept so callers can tell a failure that
// clears up on its own (503: the engine behind the API did not answer) from
// one that would repeat identically (400/404). A network-level failure never
// constructs this — fetch rejects with its own TypeError before a Response
// exists — so `instanceof ApiError` also separates "the server said no" from
// "the server never spoke".
export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
}

/**
 * The type parameter is an ASSERTION about what the server sends, not a
 * runtime check — nothing here validates the payload. It is the one place in
 * the app where types are taken on trust; every caller below names the type it
 * expects so at least that trust is stated in one readable list rather than
 * spread across the pages. See the drift note in types/api.ts.
 */
async function request<T>(path: string, { method = 'GET', body }: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}
  const opts: RequestInit = { method, headers }
  if (body instanceof FormData) {
    opts.body = body // let the browser set the multipart boundary
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, opts)
  if (!res.ok) {
    let detail = ''
    try {
      const parsed: unknown = await res.json()
      const maybeDetail = (parsed as { detail?: unknown } | null)?.detail
      detail = typeof maybeDetail === 'string' ? maybeDetail : JSON.stringify(parsed)
    } catch {
      detail = `${res.status} ${res.statusText}`
    }
    throw new ApiError(detail, res.status)
  }
  if (res.status === 204) return null as T
  const type = res.headers.get('content-type') || ''
  // Non-JSON falls through as the raw Response, unchanged from the JS version.
  return type.includes('application/json') ? ((await res.json()) as T) : (res as T)
}

function filenameFromDisposition(res: Response, fallback: string): string {
  const dispo = res.headers.get('content-disposition') || ''
  const star = /filename\*=utf-8''([^;]+)/i.exec(dispo)
  const plain = /filename="?([^";]+)"?/i.exec(dispo)
  return star ? decodeURIComponent(star[1]) : plain ? plain[1] : fallback
}

async function blobError(res: Response): Promise<Error> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const parsed = (await res.json()) as { detail?: string } | null
    detail = parsed?.detail ?? detail
  } catch {
    /* keep status text */
  }
  return new Error(detail)
}

// Binary POST (Image to PDF returns the file directly): resolves to a Blob +
// suggested filename from Content-Disposition.
async function requestBlob(path: string, formData: FormData): Promise<DownloadedBlob> {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', body: formData })
  if (!res.ok) throw await blobError(res)
  return { blob: await res.blob(), filename: filenameFromDisposition(res, 'download') }
}

// Binary GET (subscription file downloads): same shape, but a failed render
// (e.g. Surge can't express vless nodes) surfaces its reason as an Error the
// page can show inline instead of a broken browser download.
async function fetchBlob(path: string, fallbackName: string): Promise<DownloadedBlob> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw await blobError(res)
  return { blob: await res.blob(), filename: filenameFromDisposition(res, fallbackName) }
}

// Save a Blob through the browser's download flow. The anchor must be in the
// document for the click to fire in some browsers, and the object URL is
// revoked only after the click has been processed (revoking it synchronously
// can cancel the download).
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 10000)
}

export const artifactUrl = (id: string): string => `${BASE}/artifacts/${id}`

const TERMINAL_STATES = new Set(['done', 'failed', 'cancelled'])
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

const POLL_INTERVAL_MS = 500
// Roughly a minute of unreachable server before giving up, given the backoff
// below. Long enough to ride out a Wi-Fi handover, short enough that a truly
// dead backend still resolves the job instead of polling forever.
const MAX_POLL_FAILURES = 8
const MAX_POLL_BACKOFF_MS = 15000

/**
 * Fallback when the SSE stream drops mid-job: poll until the job reaches a
 * terminal state.
 *
 * There is deliberately NO overall time budget. Jobs here legitimately run for
 * half an hour (a MinerU conversion), and a poller that gives up first would
 * report a still-running job as failed — re-enabling Start, inviting a
 * duplicate run, and losing the artifact id, which only ever travels inside
 * the snapshot. The only fatal answer is 404: the registry keeps recent jobs,
 * so a missing one is genuinely gone rather than slow. Everything else is
 * treated as transient and retried with backoff.
 */
async function pollJob<R>(
  jobId: string,
  onSnapshot: (snapshot: Job<R>) => void,
): Promise<Job<R>> {
  let failures = 0
  let last: string | null = null
  for (;;) {
    try {
      const snap = await request<Job<R>>(`/jobs/${jobId}`)
      failures = 0
      // Deduped like the SSE side (routers/jobs.py only pushes on change), so
      // a quiet job doesn't churn the jobs context twice a second for an hour.
      const payload = JSON.stringify(snap)
      if (payload !== last) {
        last = payload
        onSnapshot(snap)
      }
      if (TERMINAL_STATES.has(snap.state)) return snap
      await sleep(POLL_INTERVAL_MS)
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) throw err
      failures += 1
      if (failures >= MAX_POLL_FAILURES) throw err
      await sleep(Math.min(POLL_INTERVAL_MS * 2 ** failures, MAX_POLL_BACKOFF_MS))
    }
  }
}

// Follow a job's SSE progress stream. Calls onSnapshot(snapshot) for every
// progress frame and resolves with the final snapshot on the terminal frame. A
// transient disconnect is NOT fatal — it falls back to polling so a running
// job is never stranded as "failed" (matters most under LAN hosting).
export function followJob<R>(
  jobId: string,
  onSnapshot: (snapshot: Job<R>) => void,
): Promise<Job<R>> {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`${BASE}/jobs/${jobId}/events`)
    let settled = false
    const finish = (final: Job<R>) => {
      if (settled) return
      settled = true
      onSnapshot(final)
      resolve(final)
    }
    es.addEventListener('progress', (e: MessageEvent<string>) =>
      onSnapshot(JSON.parse(e.data) as Job<R>),
    )
    es.addEventListener('done', (e: MessageEvent<string>) => {
      es.close()
      finish(JSON.parse(e.data) as Job<R>)
    })
    es.onerror = () => {
      es.close()
      if (settled) return
      pollJob<R>(jobId, onSnapshot)
        .then(finish)
        .catch((err: Error) => {
          if (!settled) {
            settled = true
            reject(err)
          }
        })
    }
  })
}

export const api = {
  // meta
  tools: () => request<Category[]>('/tools'),
  health: () => request<Health>('/health'),
  pickFolder: (startDir?: string) =>
    request<PickFolderResult>('/fs/pick-folder', {
      method: 'POST',
      body: { start_dir: startDir || null },
    }),

  // jobs
  job: (id: string) => request<Job<unknown>>(`/jobs/${id}`),
  // `cancelling: false` means the job had already finished — a refused cancel,
  // not an error, so callers can say so instead of leaving the button dead.
  cancelJob: (id: string) => request<JobCancel>(`/jobs/${id}/cancel`, { method: 'POST' }),

  // magnet scraper
  magnetConfig: () => request<MagnetConfig>('/magnet/config'),
  magnetAuto: (startPage: number) =>
    request<JobStarted>('/magnet/auto', { method: 'POST', body: { start_page: startPage } }),
  magnetManual: (urls: string[]) =>
    request<JobStarted>('/magnet/manual', { method: 'POST', body: { urls } }),
  magnetDedupe: (links: string[]) =>
    request<DedupeResult>('/magnet/dedupe', { method: 'POST', body: { links } }),

  // remux
  remuxScan: (folder: string) =>
    request<RemuxScanResult>('/remux/scan', { method: 'POST', body: { folder } }),
  remuxSubtitles: (subFolder: string, selected: string[]) =>
    request<RemuxSubtitlesResult>('/remux/subtitles', {
      method: 'POST',
      body: { sub_folder: subFolder, selected },
    }),
  remuxStart: (payload: RemuxStartPayload) =>
    request<JobStarted>('/remux/start', { method: 'POST', body: payload }),

  // file gatherer
  gatherStart: (payload: GatherStartPayload) =>
    request<JobStarted>('/gather/start', { method: 'POST', body: payload }),

  // cache purge
  purgeScan: (folder: string, patternsRaw: string) =>
    request<PurgeScanResult>('/purge/scan', {
      method: 'POST',
      body: { folder, patterns_raw: patternsRaw },
    }),
  purgeDelete: (scanId: string) =>
    request<JobStarted>('/purge/delete', { method: 'POST', body: { scan_id: scanId } }),

  // image to pdf (direct download)
  imgToPdf: (formData: FormData) => requestBlob('/img-to-pdf', formData),

  // web images to pdf
  webpdfOpen: (url: string) =>
    request<WebPdfStatus>('/webpdf/open', { method: 'POST', body: { url } }),
  webpdfStatus: () => request<WebPdfStatus>('/webpdf/status'),
  webpdfCapture: () => request<WebPdfCapture>('/webpdf/capture', { method: 'POST', body: {} }),
  webpdfClose: () => request<WebPdfStatus>('/webpdf/close', { method: 'POST' }),

  // doc conversions (multipart -> job)
  docToPdf: (formData: FormData) =>
    request<JobStarted>('/doc-to-pdf', { method: 'POST', body: formData }),
  docToMarkdown: (formData: FormData) =>
    request<JobStarted>('/doc-to-markdown', { method: 'POST', body: formData }),
  docmdHealth: () => request<MarkdownHealth>('/doc-to-markdown/health'),

  // dependency upgrader (scan runs as a job, apply is synchronous)
  depsScan: (folder: string) =>
    request<JobStarted>('/deps/scan', { method: 'POST', body: { folder } }),
  depsApply: (folder: string, commit: boolean, message: string | null) =>
    request<DepApplyResult>('/deps/apply', { method: 'POST', body: { folder, commit, message } }),

  // optimized-ip subscription
  subsGenerate: (payload: SubsGeneratePayload) =>
    request<Subscription>('/subs/generate', { method: 'POST', body: payload }),
  subsHistory: () => request<SubsHistoryItem[]>('/subs/history'),
  subsGet: (id: string) => request<Subscription>(`/subs/${id}`),
  subsDelete: (id: string) => request<null>(`/subs/${id}`, { method: 'DELETE' }),
  subsUrls: (id: string) => request<Record<string, string>>(`/subs/${id}/urls`),
  subsQrUrl: (id: string) => `${BASE}/subs/${id}/qr.png`,
  subsRenderUrl: (id: string, target: string) => `${BASE}/subs/${id}/render?target=${target}`,
  subsDownload: (id: string, target: string) =>
    fetchBlob(`/subs/${id}/render?target=${target}`, `subscription-${target}`),

  // torrent downloader — a dispatcher, not a queue. There is no list/pause/
  // resume endpoint to call: once torrentSend resolves, the task is BitComet's.
  // /resolve is multipart on BOTH paths: one endpoint accepts a pasted magnet
  // or an uploaded .torrent, so a JSON body would 422.
  // save_dir rides along with /resolve, not with the send: BitComet fixes a
  // task's save folder when the task is created and cannot move it afterwards.
  torrentStatus: () => request<TorrentStatus>('/torrent/status'),
  torrentResolveMagnet: (magnet: string, saveDir = '') => {
    const body = new FormData()
    body.append('magnet', magnet)
    body.append('save_dir', saveDir)
    return request<TorrentResolve>('/torrent/resolve', { method: 'POST', body })
  },
  torrentResolveFile: (file: File, saveDir = '') => {
    const body = new FormData()
    body.append('file', file)
    body.append('save_dir', saveDir)
    return request<TorrentResolve>('/torrent/resolve', { method: 'POST', body })
  },
  torrentPollResolve: (infohash: string) =>
    request<TorrentResolve>(`/torrent/resolve/${infohash}`),
  torrentSend: (payload: TorrentSendPayload) =>
    request<TorrentSent>('/torrent', { method: 'POST', body: payload }),
  // Cancels a staging that was never sent. A magnet runs while it fetches
  // metadata, so abandoning one without this leaves it downloading in BitComet.
  torrentDiscard: (infohash: string) =>
    request<{ infohash: string; state: string }>(`/torrent/${infohash}`, {
      method: 'DELETE',
    }),
  // Which BitComet gets the task. Every one of these returns the whole list
  // back, so the page never has to guess what changed and re-fetch.
  torrentDevices: () => request<TorrentDeviceList>('/torrent/devices'),
  torrentDeviceAdd: (payload: TorrentDeviceInput) =>
    request<TorrentDeviceList>('/torrent/devices', { method: 'POST', body: payload }),
  torrentDeviceUpdate: (id: string, payload: TorrentDeviceInput) =>
    request<TorrentDeviceList>(`/torrent/devices/${id}`, {
      method: 'PATCH',
      body: payload,
    }),
  torrentDeviceRemove: (id: string) =>
    request<TorrentDeviceList>(`/torrent/devices/${id}`, { method: 'DELETE' }),
  torrentDeviceSelect: (id: string) =>
    request<TorrentDeviceList>(`/torrent/devices/${id}/select`, { method: 'POST' }),
  // Answers with ok:false and a reason rather than throwing — a failed test is
  // the expected outcome of typing an address, not an exception.
  torrentDeviceTest: (payload: TorrentDeviceInput & { id?: string }) =>
    request<TorrentDeviceTest>('/torrent/devices/test', {
      method: 'POST',
      body: payload,
    }),

  // watermark remover
  watermarkHealth: () => request<WatermarkHealth>('/watermark/health'),
  watermarkUpload: (formData: FormData) =>
    request<WatermarkBatch>('/watermark/batch', { method: 'POST', body: formData }),
  watermarkRun: (payload: WatermarkRunPayload) =>
    request<JobStarted>('/watermark/run', { method: 'POST', body: payload }),
}

// The canvas editor loads these as <img>/fetch sources, not through request<T>.
export const watermarkImageUrl = (batchId: string, imageId: string): string =>
  `${BASE}/watermark/${batchId}/${imageId}/image`
export const watermarkMaskUrl = (
  batchId: string,
  imageId: string,
  sensitivity: number,
  detector: WatermarkDetector = 'auto',
): string =>
  `${BASE}/watermark/${batchId}/${imageId}/mask` +
  `?sensitivity=${sensitivity}&detector=${detector}`
