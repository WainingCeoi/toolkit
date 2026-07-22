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
  WebPdfCapture,
  WebPdfStatus,
} from './types/api'

const BASE = '/api'

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
    throw new Error(detail)
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

// Fallback when the SSE stream drops mid-job: poll the status endpoint until
// the job reaches a terminal state, then resolve from that. Rejects only if the
// job can't be reached at all (e.g. it was evicted -> 404).
async function pollJob<R>(
  jobId: string,
  onSnapshot: (snapshot: Job<R>) => void,
  maxTries = 1200,
): Promise<Job<R>> {
  for (let i = 0; i < maxTries; i++) {
    const snap = await request<Job<R>>(`/jobs/${jobId}`)
    onSnapshot(snap)
    if (TERMINAL_STATES.has(snap.state)) return snap
    await sleep(500)
  }
  throw new Error('Timed out waiting for the job to finish.')
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
  cancelJob: (id: string) => request<null>(`/jobs/${id}/cancel`, { method: 'POST' }),

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
  purgeDelete: (folder: string, files: string[]) =>
    request<JobStarted>('/purge/delete', { method: 'POST', body: { folder, files } }),

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
}
