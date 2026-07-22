// Hand-written mirror of the backend's Pydantic models and job result dicts.
//
// IMPORTANT: nothing enforces that this file agrees with the backend. It is a
// mirror, maintained by hand, and it can drift. The durable fix is to give
// JobOut.result a real Pydantic type per tool (it is currently `dict`, see
// backend/src/toolkit_api/schemas.py) and generate this file from
// /openapi.json. Until then, changing a worker's result dict means changing
// the matching type here.
//
// Field names are snake_case because they come off the wire that way; they are
// deliberately NOT camelCased, so a reader can grep a name straight across into
// the Python source.

// --------------------------------------------------------------- job envelope

export type JobState = 'running' | 'done' | 'failed' | 'cancelled'
export type JobItemState = 'pending' | 'running' | 'done' | 'failed'

/** One tracked unit of work inside a job (a file, a video, a manifest). */
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

/**
 * A job snapshot, discriminated on `state`.
 *
 * This union is the point of the whole migration: reading `.result` while the
 * job is running, or `.error` when it succeeded, is now a compile error rather
 * than a silent `undefined` rendered into the DOM.
 *
 * `cancelled` carries `R | null` on purpose and is not merged into `done`:
 * workers are free to bail out and return nothing once they observe the cancel
 * flag, and several do — see the `return None` paths in
 * backend/src/toolkit_api/routers/depsync.py.
 */
export type Job<R> =
  | (JobBase & { state: 'running'; result: null; error: null })
  | (JobBase & { state: 'done'; result: R; error: null })
  | (JobBase & { state: 'cancelled'; result: R | null; error: null })
  | (JobBase & { state: 'failed'; result: null; error: string })

/** Every job-starting endpoint answers with this and nothing else. */
export interface JobStarted {
  job_id: string
}

// ---------------------------------------------------------- job result shapes

/**
 * NOTE: "a failure" has three different shapes across the API. They are typed
 * exactly as the backend sends them rather than smoothed over here, because
 * inventing a common shape in the frontend would hide the inconsistency
 * instead of surfacing it. Reconciling them is a backend change.
 */

/** Cache Purge and File Gatherer: objects keyed by name. */
export interface NamedFailure {
  name: string
  error: string
}

/** Remux: same idea, but the key is `title`. */
export interface TitledFailure {
  title: string
  error: string
}

/** Doc→PDF and Doc→Markdown: a positional (name, error) tuple, not an object. */
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

/** Shared by Doc→PDF and Doc→Markdown — identical result shape. */
export interface DocConvertResult {
  done: string[]
  failed: TupleFailure[]
  /** Absent entirely when nothing converted, so there is no archive to offer. */
  artifact_id?: string
}

/** A magnet fetch that found a link. Note: carries no `url`. */
export interface MagnetHit {
  success: true
  result: string
}

/** A magnet fetch that did not. Note: carries `url`, and `reason` not `error`. */
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
  /**
   * Present only on the automatic path, where pagination looks for a cutoff,
   * and only ever `true` — the false case is MagnetCutoffMiss below. Typed as
   * the literal so `cutoff_found === false` discriminates the union.
   */
  cutoff_found?: true
}

/**
 * The automatic path's early return when pagination never found the cutoff
 * video. It shares none of the fields above, which is why MagnetResult is a
 * union and the page must narrow on `cutoff_found` before touching `urls`.
 */
export interface MagnetCutoffMiss {
  cutoff_found: false
  warning: string
  error: string | null
}

export type MagnetResult = MagnetScrapeResult | MagnetCutoffMiss

/** One dependency bump found in a manifest. */
export interface Bump {
  name: string
  table: string
  old: string
  new: string
  major: boolean
}

/**
 * A scanned manifest. Deliberately NOT the same type as ApplyTargetResult:
 * scanning never writes, so it has no `written` count and no `skipped` list.
 */
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

/** Mirrors StartIn in backend/src/toolkit_api/routers/remux.py. */
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

export interface PurgeScanResult {
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
  /**
   * `list[dict]` on the backend with no declared shape, and the page renders it
   * by indexing arbitrary column names. `unknown` values force the page to
   * stringify rather than assume — which is what it already does.
   */
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
