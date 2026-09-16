// The app's only HTTP layer: components call api.*, never fetch.

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
  PhotoFilterPayload,
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

// Only a non-2xx response constructs this; a network failure rejects with fetch's TypeError.
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

// T is an assertion about the wire shape; nothing here validates the payload.
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
  return type.includes('application/json') ? ((await res.json()) as T) : (res as T)
}

function filenameFromDisposition(res: Response, fallback: string): string {
  const dispo = res.headers.get('content-disposition') || ''
  const star = /filename\*=utf-8''([^;]+)/i.exec(dispo)
  if (star) return decodeURIComponent(star[1])
  // The quoted form may contain backslash-escaped quotes, so it cannot stop at the first `"`.
  const quoted = /filename="((?:[^"\\]|\\.)*)"/i.exec(dispo)
  if (quoted) return quoted[1].replace(/\\(.)/g, '$1')
  const plain = /filename=([^;]+)/i.exec(dispo)
  return plain ? plain[1].trim() : fallback
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

async function requestBlob(path: string, formData: FormData): Promise<DownloadedBlob> {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', body: formData })
  if (!res.ok) throw await blobError(res)
  return { blob: await res.blob(), filename: filenameFromDisposition(res, 'download') }
}

async function fetchBlob(path: string, fallbackName: string): Promise<DownloadedBlob> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw await blobError(res)
  return { blob: await res.blob(), filename: filenameFromDisposition(res, fallbackName) }
}

// Browser quirks: the anchor must be in the document for click() to work, and revoking
// the object URL synchronously can cancel the download.
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
// About a minute of unreachable server, given the backoff below.
const MAX_POLL_FAILURES = 8
const MAX_POLL_BACKOFF_MS = 15000

// SSE fallback. No overall time budget (jobs run for up to half an hour); only a 404 is fatal.
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

// Follows a job's SSE stream, falling back to polling on disconnect.
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
  // `packages` shows bundles (a *.photoslibrary) as selectable folders.
  pickFolder: (startDir?: string, packages = false) =>
    request<PickFolderResult>('/fs/pick-folder', {
      method: 'POST',
      body: { start_dir: startDir || null, packages },
    }),

  // jobs
  job: (id: string) => request<Job<unknown>>(`/jobs/${id}`),
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

  // photos library filter
  photofilterDryRun: (payload: PhotoFilterPayload) =>
    request<JobStarted>('/photofilter/dry-run', { method: 'POST', body: payload }),
  photofilterRun: (payload: PhotoFilterPayload) =>
    request<JobStarted>('/photofilter/run', { method: 'POST', body: payload }),

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

  // dependency upgrader
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

  // torrent downloader. /resolve is multipart on both paths (JSON would 422), and save_dir
  // travels with it: BitComet fixes a task's folder at creation.
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
  // A staged magnet keeps downloading in BitComet until discarded.
  torrentDiscard: (infohash: string) =>
    request<{ infohash: string; state: string }>(`/torrent/${infohash}`, {
      method: 'DELETE',
    }),
  // devices
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
