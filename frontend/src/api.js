// Single HTTP wrapper for the whole app. Components call `api.*` — never
// `fetch` directly — so the base path, error handling, and streaming live in
// one place. Same-origin '/api' works in dev (Vite proxy) and in
// single-origin production (served from the same server).

const BASE = '/api'

async function request(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} }
  if (body instanceof FormData) {
    opts.body = body // let the browser set the multipart boundary
  } else if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, opts)
  if (!res.ok) {
    let detail = ''
    try {
      const parsed = await res.json()
      detail = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed)
    } catch {
      detail = `${res.status} ${res.statusText}`
    }
    throw new Error(detail)
  }
  if (res.status === 204) return null
  const type = res.headers.get('content-type') || ''
  return type.includes('application/json') ? res.json() : res
}

function filenameFromDisposition(res, fallback) {
  const dispo = res.headers.get('content-disposition') || ''
  const star = /filename\*=utf-8''([^;]+)/i.exec(dispo)
  const plain = /filename="?([^";]+)"?/i.exec(dispo)
  return star ? decodeURIComponent(star[1]) : plain ? plain[1] : fallback
}

async function blobError(res) {
  let detail = `${res.status} ${res.statusText}`
  try {
    detail = (await res.json()).detail ?? detail
  } catch { /* keep status text */ }
  return new Error(detail)
}

// Binary POST (Image to PDF returns the file directly): resolves to a Blob +
// suggested filename from Content-Disposition.
async function requestBlob(path, formData) {
  const res = await fetch(`${BASE}${path}`, { method: 'POST', body: formData })
  if (!res.ok) throw await blobError(res)
  return { blob: await res.blob(), filename: filenameFromDisposition(res, 'download') }
}

// Binary GET (subscription file downloads): same shape, but a failed render
// (e.g. Surge can't express vless nodes) surfaces its reason as an Error the
// page can show inline instead of a broken browser download.
async function fetchBlob(path, fallbackName) {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw await blobError(res)
  return { blob: await res.blob(), filename: filenameFromDisposition(res, fallbackName) }
}

// Save a Blob through the browser's download flow. The anchor must be in the
// document for the click to fire in some browsers, and the object URL is
// revoked only after the click has been processed (revoking it synchronously
// can cancel the download).
export function saveBlob(blob, filename) {
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

export const artifactUrl = (id) => `${BASE}/artifacts/${id}`

const TERMINAL_STATES = new Set(['done', 'failed', 'cancelled'])
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// Fallback when the SSE stream drops mid-job: poll the status endpoint until
// the job reaches a terminal state, then resolve from that. Rejects only if the
// job can't be reached at all (e.g. it was evicted -> 404).
async function pollJob(jobId, onSnapshot, maxTries = 1200) {
  for (let i = 0; i < maxTries; i++) {
    const snap = await request(`/jobs/${jobId}`)
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
export function followJob(jobId, onSnapshot) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`${BASE}/jobs/${jobId}/events`)
    let settled = false
    const finish = (final) => {
      if (settled) return
      settled = true
      onSnapshot(final)
      resolve(final)
    }
    es.addEventListener('progress', (e) => onSnapshot(JSON.parse(e.data)))
    es.addEventListener('done', (e) => {
      es.close()
      finish(JSON.parse(e.data))
    })
    es.onerror = () => {
      es.close()
      if (settled) return
      pollJob(jobId, onSnapshot)
        .then(finish)
        .catch((err) => {
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
  tools: () => request('/tools'),
  health: () => request('/health'),
  pickFolder: (startDir) =>
    request('/fs/pick-folder', { method: 'POST', body: { start_dir: startDir || null } }),

  // jobs
  job: (id) => request(`/jobs/${id}`),
  cancelJob: (id) => request(`/jobs/${id}/cancel`, { method: 'POST' }),

  // magnet scraper
  magnetConfig: () => request('/magnet/config'),
  magnetAuto: (startPage) =>
    request('/magnet/auto', { method: 'POST', body: { start_page: startPage } }),
  magnetManual: (urls) => request('/magnet/manual', { method: 'POST', body: { urls } }),
  magnetDedupe: (links) => request('/magnet/dedupe', { method: 'POST', body: { links } }),

  // remux
  remuxScan: (folder) => request('/remux/scan', { method: 'POST', body: { folder } }),
  remuxSubtitles: (subFolder, selected) =>
    request('/remux/subtitles', {
      method: 'POST',
      body: { sub_folder: subFolder, selected },
    }),
  remuxStart: (payload) => request('/remux/start', { method: 'POST', body: payload }),

  // file gatherer
  gatherStart: (payload) => request('/gather/start', { method: 'POST', body: payload }),

  // cache purge
  purgeScan: (folder, patternsRaw) =>
    request('/purge/scan', {
      method: 'POST',
      body: { folder, patterns_raw: patternsRaw },
    }),
  purgeDelete: (folder, files) =>
    request('/purge/delete', { method: 'POST', body: { folder, files } }),

  // image to pdf (direct download)
  imgToPdf: (formData) => requestBlob('/img-to-pdf', formData),

  // web images to pdf
  webpdfOpen: (url) => request('/webpdf/open', { method: 'POST', body: { url } }),
  webpdfStatus: () => request('/webpdf/status'),
  webpdfCapture: () => request('/webpdf/capture', { method: 'POST', body: {} }),
  webpdfClose: () => request('/webpdf/close', { method: 'POST' }),

  // doc conversions (multipart -> job)
  docToPdf: (formData) => request('/doc-to-pdf', { method: 'POST', body: formData }),
  docToMarkdown: (formData) =>
    request('/doc-to-markdown', { method: 'POST', body: formData }),
  docmdHealth: () => request('/doc-to-markdown/health'),

  // dependency upgrader (scan runs as a job, apply is synchronous)
  depsScan: (folder) => request('/deps/scan', { method: 'POST', body: { folder } }),
  depsApply: (folder, commit) =>
    request('/deps/apply', { method: 'POST', body: { folder, commit } }),

  // optimized-ip subscription
  subsGenerate: (payload) => request('/subs/generate', { method: 'POST', body: payload }),
  subsHistory: () => request('/subs/history'),
  subsGet: (id) => request(`/subs/${id}`),
  subsDelete: (id) => request(`/subs/${id}`, { method: 'DELETE' }),
  subsUrls: (id) => request(`/subs/${id}/urls`),
  subsQrUrl: (id) => `${BASE}/subs/${id}/qr.png`,
  subsRenderUrl: (id, target) => `${BASE}/subs/${id}/render?target=${target}`,
  subsDownload: (id, target) =>
    fetchBlob(`/subs/${id}/render?target=${target}`, `subscription-${target}`),
}
