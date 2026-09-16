// Hand-maintained mirror of the backend models; update both sides together.

// --------------------------------------------------------------- job envelope

export type JobState = 'running' | 'done' | 'failed' | 'cancelled'
export type JobItemState = 'pending' | 'running' | 'done' | 'failed'

export interface JobItem {
  name: string
  pct: number
  state: JobItemState
  error: string | null
}

interface JobBase {
  id: string
  tool: string
  message: string
  items: JobItem[]
  created_at: string
}

/** cancelled, failed and running carry `R | null`: workers may publish partial results. */
export type Job<R> =
  | (JobBase & { state: 'running'; result: R | null; error: null })
  | (JobBase & { state: 'done'; result: R; error: null })
  | (JobBase & { state: 'cancelled'; result: R | null; error: null })
  | (JobBase & { state: 'failed'; result: R | null; error: string })

export interface JobStarted {
  job_id: string
}

/** `cancelling` is false when the job had already finished. */
export interface JobCancel {
  cancelling: boolean
}

// ---------------------------------------------------------- job result shapes

// Failure shapes differ per tool on purpose; they are typed as sent, not unified here.

export interface NamedFailure {
  name: string
  error: string
}

export interface TitledFailure {
  title: string
  error: string
}

export type TupleFailure = [name: string, error: string]

export interface PurgeResult {
  deleted: string[]
  failed: NamedFailure[]
}

export interface GatherResult {
  moved: string[]
  failed: NamedFailure[]
  scan_errors: string[]
  target: string
  warning: string | null
}

export interface RemuxResult {
  total: number
  successful: number
  failed: TitledFailure[]
  out_folder: string
}

export interface DocConvertResult {
  done: string[]
  failed: TupleFailure[]
  /** Absent when nothing converted. */
  artifact_id?: string
}

export interface MagnetHit {
  success: true
  result: string
}

export interface MagnetMiss {
  success: false
  url: string
  reason: string
}

export interface MagnetScrapeResult {
  urls: string[]
  successful: MagnetHit[]
  failed: MagnetMiss[]
  total: number
  successful_count: number
  failed_count: number
  /** Dropped by the backend's unique filter; absent on the empty-URL-list result. */
  duplicate_count?: number
  /** Literal `true` so `cutoff_found === false` narrows to MagnetCutoffMiss. */
  cutoff_found?: true
}

export interface MagnetCutoffMiss {
  cutoff_found: false
  warning: string
  error: string | null
}

export type MagnetResult = MagnetScrapeResult | MagnetCutoffMiss

export interface Bump {
  name: string
  table: string
  old: string
  new: string
  major: boolean
}

export interface ScanTarget {
  rel: string
  kind: string
  bumps: Bump[]
  error: string | null
}

export interface DepScanResult {
  root: string
  targets: ScanTarget[]
  total_bumps: number
}

// ------------------------------------------------------ synchronous endpoints

export interface Tool {
  slug: string
  title: string
  description: string
}

export interface Category {
  name: string
  tools: Tool[]
}

export interface Health {
  ok: boolean
  ffmpeg: boolean
  soffice: boolean
  mineru: boolean
}

export interface MarkdownHealth {
  mineru: boolean
  backend_ready: boolean
}

export interface PickFolderResult {
  /** null when the user cancels the native dialog. */
  path: string | null
}

export interface MagnetConfig {
  website_url_set: boolean
  cutoff_set: boolean
}

export interface DedupeResult {
  unique: string[]
  count: number
}

export interface RemuxVideo {
  path: string
  name: string
}

export interface RemuxScanResult {
  videos: RemuxVideo[]
}

export interface SubtitleMatch {
  video: string
  subtitle: string | null
}

export interface RemuxSubtitlesResult {
  matches: SubtitleMatch[]
}

/** Mirrors StartIn in routers/remux.py. */
export interface RemuxStartPayload {
  selected: string[]
  include_video?: boolean
  video_index?: number
  multi_audio?: boolean
  audio_value?: string
  include_subtitle?: boolean
  subtitle_index?: number
  sub_lang?: string
  use_external_sub?: boolean
  external_sub_map?: Record<string, string | null>
  out_folder: string
  max_workers?: number
}

/** Mirrors GatherStartIn. */
export interface GatherStartPayload {
  source: string
  target: string
  categories?: string[]
  custom?: string
}

export interface PhotoFilterCount {
  files: number
  bytes: number
}

export interface PhotoFilterRule {
  rule: string
  files: number
  bytes: number
}

/** Mirrors photofilter.summary() plus the router envelope; dry runs share the shape. */
export interface PhotoFilterResult {
  dry_run: boolean
  source: string
  dest: string
  seconds: number
  kept: PhotoFilterCount
  excluded: PhotoFilterCount
  /** Biggest saving first; a rule that matched nothing is still listed. */
  rules: PhotoFilterRule[]
  /** Live WAL-mode databases the plan snapshots (VACUUM INTO) instead of copying. */
  snapshots: string[]
  copied: number
  skipped: number
  snapshotted: number
  /** Library-relative paths removed from the destination; a directory ends in `/`. */
  deleted: string[]
  errors: string[]
  verify: {
    /** False when the run stopped before verifying — not the same as verified clean. */
    ran: boolean
    assets: number
    edited: number
    problems: string[]
  }
}

/** Mirrors PhotoFilterIn in routers/photofilter.py. */
export interface PhotoFilterPayload {
  source: string
  dest: string
  /** Omitted means the backend's shipped defaults; '' means exclude nothing. */
  rules?: string
}

export interface PurgeScanResult {
  /** Single-use, time-limited handle; the server owns the file list. */
  scan_id: string
  files: string[]
  errors: string[]
  total_bytes: number
  rejected_tokens: string[]
}

export interface WebPdfStatus {
  open: boolean
}

export interface WebPdfCapture {
  artifact_id: string
  name: string
  pages: number
  skipped: number
  /** Set when the PDF was built but bookmarks could not be added. */
  warn: string | null
}

/** Mirrors GenerateIn. */
export interface SubsGeneratePayload {
  node_links: string
  preferred_ips: string
  name_prefix?: string
  keep_original_host?: boolean
}

export interface SubsCounts {
  /** null on legacy payloads stored before counts were recorded. */
  input_nodes: number | null
  endpoints: number | null
  output_nodes: number
}

export interface Subscription {
  sub_id: string
  dedup: boolean
  loaded: boolean
  counts: SubsCounts
  warnings: string[]
  /** Undeclared shape on the backend; values are `unknown` on purpose. */
  preview: Record<string, unknown>[]
  urls: Record<string, string>
}

export interface SubsHistoryItem {
  id: string
  node_count: number
  name_prefix: string
  created_at: string
}

/** Mirrors ApplyIn / the /deps/apply response. */
export interface ApplyTargetResult {
  rel: string
  kind: string
  written: number
  bumps: Bump[]
  skipped: { name: string; reason: string }[]
  error: string | null
}

export interface DepApplyResult {
  results: ApplyTargetResult[]
  commits: { sha: string | null; files: string[] }[]
  written_total: number
}

/** A downloaded file plus the name parsed out of Content-Disposition. */
export interface DownloadedBlob {
  blob: Blob
  filename: string
}

// --- Watermark Remover ------------------------------------------------------

/** Mirrors WatermarkImageOut. */
export interface WatermarkImage {
  id: string
  name: string
  /** Dimensions of the normalized (EXIF-upright) working copy — the canvas size. */
  width: number
  height: number
}

/** Mirrors WatermarkBatchOut. */
export interface WatermarkBatch {
  batch_id: string
  images: WatermarkImage[]
}

/** `auto` recovers a repeating mark, falling back to `texture`; the others run one detector. */
export type WatermarkDetector = 'auto' | 'texture' | 'pattern'

/** Mirrors WatermarkHealthOut. */
export interface WatermarkHealth {
  /** torch importable — the LaMa inpainter can run (cv2 always can). */
  lama: boolean
  device: string
}

/** Mirrors WatermarkRunIn. `masks` maps image id -> base64 PNG (white = remove). */
export interface WatermarkRunPayload {
  batch_id: string
  inpainter: 'lama' | 'cv2'
  masks: Record<string, string>
  dilate_px?: number
}

export interface WatermarkResult {
  /** Tells the page a stale snapshot from the batch currently staged. */
  batch_id: string
  done: string[]
  failed: TupleFailure[]
  /** No mask was proposed, so nothing was inpainted. */
  skipped: string[]
  /** A mark was found, but removing it would destroy the picture under it. */
  protected: string[]
  /** Zip of everything cleaned so far, republished under one id; absent until the first. */
  artifact_id?: string
  filename?: string
}

// --- Torrent Downloader ---------------------------------------------------

export interface TorrentFileRow {
  index: number // 1-based, sent back verbatim
  path: string
  size: number
  category: string
}

export interface TorrentResolve {
  infohash: string
  ready: boolean
  name: string | null
  files: TorrentFileRow[]
  state: string
}

/** Mirrors DeviceOut in routers/torrent.py. */
export interface TorrentDevice {
  id: string
  label: string
  /** null for the local BitComet, whose address comes from its own config. */
  url: string | null
  username: string
  /** The password itself is never sent to the browser — only whether one is set. */
  has_password: boolean
  is_local: boolean
}

export interface TorrentDeviceList {
  active: string
  devices: TorrentDevice[]
}

export interface TorrentDeviceInput {
  label?: string
  url?: string
  username?: string
  /** Blank on an edit means "keep the stored password". */
  password?: string
}

export interface TorrentDeviceTest {
  ok: boolean
  server: string | null
  detail: string | null
  save_folders: string[]
}

export interface TorrentStatus {
  running: boolean
  server: string | null
  detail: string | null
  url: string | null // BitComet's own Web UI
  device?: TorrentDevice | null
  /** False for a LAN BitComet: the native folder picker browses the wrong machine. */
  is_local?: boolean
  save_folders?: string[]
}

export interface TorrentSendPayload {
  infohash: string
  selected: number[]
}

/** Handover receipt; there is nothing to poll afterwards. */
export interface TorrentSent {
  infohash: string
  task_id: string
  name: string | null
}
