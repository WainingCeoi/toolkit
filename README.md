# 🧰 Toolkit

Twelve small media & file utilities in one local app — a FastAPI backend driving
the engines, a React single-page UI, and one `make` entrance.

> **macOS only.** Folder pickers use AppleScript (`osascript`), and several tools
> drive desktop apps (Chrome, LibreOffice, BitComet) on this Mac.

## Tools

**🎬 Media**

|     | Tool                   | What it does                                                                     |
| --- | ---------------------- | -------------------------------------------------------------------------------- |
| 🧲  | **Magnet Scraper**     | Scrape unwatched video magnets automatically, in bulk, or de-duplicate a list.    |
| 🎬  | **Remux Processor**    | Parallel, lossless remuxing (stream-copy) with configurable tracks.               |
| 🌊  | **Torrent Downloader** | Keep only the files worth keeping, then hand the torrent to BitComet.             |

**🗂️ Files & Tools**

|     | Tool                    | What it does                                                                    |
| --- | ----------------------- | ------------------------------------------------------------------------------- |
| 🌐  | **Web Images to PDF**   | Open a page, scroll to load its images, capture them into one PDF.               |
| 📦  | **File Gatherer**       | Recursively gather files by type and move them into one folder.                  |
| 🖼️  | **Image to PDF**        | Combine selected images (incl. iPhone HEIC) into a single PDF.                   |
| 🧽  | **Watermark Remover**   | Detect watermarks — tiled or once-per-photo — and inpaint them away.             |
| 📄  | **Doc to PDF**          | Accept tracked changes, strip comments, render to PDF via LibreOffice.           |
| 📝  | **Doc to Markdown**     | PDFs / Office docs / images → Markdown with MinerU (text, tables, formulas).     |
| 🧹  | **Cache Purge**         | Recursively find and delete cache / junk files.                                  |
| 📦  | **Dependency Upgrader** | Scan a project's `pyproject.toml` / `package.json`, upgrade and commit each one. |

**🌐 Network**

|     | Tool                          | What it does                                                          |
| --- | ----------------------------- | --------------------------------------------------------------------- |
| 🛰️  | **Optimized-IP Subscription** | Rewrite nodes with optimized Cloudflare IPs, serve LAN subscriptions.  |

## Quick start

```bash
make install     # backend deps (uv) + frontend deps (npm)
make dev         # → http://localhost:5173
```

`make install` picks the optional backend extras for the machine it runs on:
Apple silicon gets both (MinerU for Doc to Markdown, torch for the watermark
inpainter), Intel Macs get neither, because no macOS x86_64 wheel exists for
either — see [Requirements](#requirements) for what changes without them. Force
a subset with `make install EXTRAS="docmd watermark"`, or none with `EXTRAS=`.

| Command      | What runs                                | Reachable from            |
| ------------ | ---------------------------------------- | ------------------------- |
| `make dev`   | Vite `:5173` + API `:8000`, hot-reload   | this Mac                  |
| `make start` | built UI + API in one process, `:8000`   | this Mac (loopback)       |
| `make host`  | the same, bound to `0.0.0.0`             | every device on the Wi-Fi |

One Ctrl-C stops everything. In dev, Vite proxies `/api` to the backend, so the UI
calls same-origin and streaming needs no CORS. `PORT=9000` moves the base port in
any mode — each advances to the first free port at or above it and announces where
it landed; `HOST=127.0.0.1 make host` keeps it local.

> ⚠️ **`make host` has no authentication**, and these tools move and permanently
> delete files on this Mac. It's plain HTTP — run it only on a network you trust.

## How it works

```mermaid
flowchart LR
    UI["React + Vite<br/>frontend/src"]
    subgraph one["one Python process"]
        API["FastAPI<br/>routers/ — one per tool"]
        REG["job registry<br/>bounded worker pool"]
        ENG["engines<br/>toolkit_engine · subgen · watermark"]
    end
    EXT["ffmpeg · Chrome · LibreOffice<br/>MinerU · BitComet · SQLite"]
    UI -- "JSON + SSE over /api" --> API
    API --> REG --> ENG --> EXT
```

| Path                            | Role                                                                    |
| ------------------------------- | ----------------------------------------------------------------------- |
| `backend/src/toolkit_engine/`   | Framework-free domain logic: ffmpeg, docx, scanning, scraping, PDFs.    |
| `backend/src/subgen/`           | Subscription engine — parse / rewrite / render / SQLite.                |
| `backend/src/watermark/`        | Watermark detection and inpainting.                                     |
| `backend/src/toolkit_api/`      | App factory, `deps.py`, `schemas.py`, `routers/`, the job registry.     |
| `frontend/src/`                 | `api.ts` (one HTTP + SSE wrapper) and the React pages.                  |

Anything long-running — remux, conversions, scans, deletions — is a **job**:

```mermaid
sequenceDiagram
    participant UI as React page
    participant API as FastAPI
    participant W as worker thread
    UI->>API: POST /api/remux/start
    API-->>UI: { job_id }
    API->>W: queued on the pool
    UI->>API: GET /api/jobs/{id}/events (SSE)
    loop per item
        W->>API: pct / state
        API-->>UI: progress frame
    end
    W->>API: result + artifact
    API-->>UI: done
    UI->>API: GET /api/artifacts/{id}
```

Jobs are tracked app-wide, not per page: every open tool keeps a tab in the bottom
dock with its running jobs, so switching tools — or reloading — never loses a run.
The nav and home grid are rendered from one server-side manifest (`/api/tools`),
which is also what `TOOLKIT_DISABLED_TOOLS` filters — a tool switched off there is
dropped from the manifest *and* never mounted, so its endpoints 404 instead of
staying quietly callable. The UI follows the system light/dark theme, with a
toggle that overrides and remembers.

## Configuration

Environment variables, or `backend/.env` (copy `backend/.env.example`). All optional.

| Variable                 | Default               | What it does                                                     |
| ------------------------ | --------------------- | ---------------------------------------------------------------- |
| `WEBSITE_URL`            | empty                 | Magnet Scraper: base URL walked by Automatic mode                |
| `CUTOFF_VIDEO`           | empty                 | Magnet Scraper: stopping anchor, auto-advanced after each run    |
| `SUB_DB_PATH`            | `backend/data/sub.db` | Subscription: SQLite database path                               |
| `SUB_ACCESS_TOKEN`       | empty                 | Require `?token=…` on subscription links                         |
| `SUB_PUBLIC_HOST`        | `.local` name         | Host used in subscription links, then a LAN IP                   |
| `WATERMARK_DEVICE`       | auto                  | Pin LaMa to `cpu` / `mps` / `cuda`                               |
| `WATERMARK_LAMA_MODEL`   | empty                 | Path to a pre-downloaded `big-lama.pt` (skips the download)      |
| `APP_CORS_ORIGINS`       | Vite dev origins      | CORS allowlist (cross-origin API calls only)                     |
| `APP_STATIC_DIR`         | `../frontend/dist`    | Built UI served by the single-server modes                       |
| `TOOLKIT_DISABLED_TOOLS` | empty                 | Slugs to switch off — hidden **and** unmounted (404). Read at startup |
| `HOST` / `PORT`          | `0.0.0.0` / `8000`    | `make host` bind / base port (shell env, not `.env`)             |

## Requirements

| Needed by             | Requirement                                            | Notes                                                             |
| --------------------- | ------------------------------------------------------ | ----------------------------------------------------------------- |
| everything            | [uv](https://docs.astral.sh/uv/)                       | Python 3.14, managed via `.python-version`                        |
| everything            | [Node.js](https://nodejs.org/) ≥ 20                    | frontend build                                                    |
| Remux Processor       | [FFmpeg](https://ffmpeg.org/)                          | `brew install ffmpeg`                                             |
| Torrent Downloader    | [BitComet](https://www.bitcomet.com/)                  | *Options → Remote Access*: enable **both** switches, set user/pass |
| Watermark Remover     | [torch](https://pytorch.org/)                          | `watermark` extra, Apple silicon; big-lama (~200 MB) auto-downloads |
| Web Images to PDF     | [Google Chrome](https://www.google.com/chrome/)        | matching driver downloaded automatically                          |
| Doc to PDF            | [LibreOffice](https://www.libreoffice.org/)            | `brew install --cask libreoffice`                                 |
| Doc to Markdown       | [MinerU](https://github.com/opendatalab/MinerU)        | `docmd` extra, Apple silicon; models download on first run        |

For BitComet on *this* Mac the app reads credentials from BitComet's own config —
nothing to configure twice.

**On an Intel Mac** neither extra installs, so Doc to Markdown is unavailable —
hide it with `TOOLKIT_DISABLED_TOOLS=doc-to-markdown` — and Watermark Remover
falls back to its cv2 inpainter. Nothing else changes: BitComet's login cipher,
the one remaining dependency with no x86_64 wheel, is supplied in pure Python
(`backend/src/toolkit_engine/aescbc.py`, held to the NIST vectors and to
byte-equality with `cryptography`) rather than pulling in a Rust toolchain for
one call per login.

## Development

```bash
make test        # backend pytest + ruff (check + format), then frontend typecheck + eslint + vitest + build
make lint        # the same gates without the tests
make backend     # the API alone, hot-reload
make frontend    # Vite alone, against an API already on PORT
make build       # frontend/dist only
make clean       # remove build artifacts and caches
```

The gate short-circuits, so its order decides what you learn when it fails: the
backend runs its tests before its linters on purpose, and the frontend gates run
cheapest-first. `typecheck` is the real frontend gate — Vite strips TypeScript
types without checking them, so a type-broken app builds perfectly clean.

From `backend/`, `uv run pytest` is the single backend test command and
`uv run ruff check src tests` the lint. The LaMa inpainting tests are marked
`slow` and deselected by default (the first run downloads the ~200 MB
checkpoint); run them with `uv run pytest -m slow`.

## Tool notes

<details>
<summary><b>🧲 Magnet Scraper</b> — three modes</summary>

| Mode                  | What it does                                                                      |
| --------------------- | --------------------------------------------------------------------------------- |
| **Automatic**         | Walks `WEBSITE_URL` page by page until it reaches `CUTOFF_VIDEO`, scrapes everything newer, then advances the cutoff. |
| **Manual**            | Paste video page URLs, get their magnets.                                          |
| **Remove Duplicated** | Paste raw magnets, get the unique set back.                                        |

</details>

<details>
<summary><b>🎬 Remux Processor</b></summary>

Pick a source folder and videos, set video / audio / subtitle track indices and the
subtitle language tag, optionally attach external subtitles (matched by filename
stem), choose an output folder and worker count. Live per-file progress, then a
success/failure summary. No re-encoding.

</details>

<details>
<summary><b>🌊 Torrent Downloader</b> — a dispatcher, not a download manager</summary>

```mermaid
flowchart TD
    P["paste magnets / pick .torrent"] --> Q["queue held on this side"]
    Q -- "10 in flight — each answer admits the next" --> R["BitComet fetches metadata"]
    R --> V["review, folded one row each<br/>default filter: video &gt; 100 MB"]
    V --> S["Send — same 10-wide window"]
    S -- "timed out" --> T["retry, up to 3 passes"]
    T --> S
    S -- ok --> B["BitComet owns the task"]
    S -- "failed for real" --> F["Failed panel + Copy magnet"]
```

- Every file is reviewed **before any content downloads**. Tick a row to override
  the filter. The minimum size applies to video and audio only — a global floor
  would discard every ~40 KB subtitle.
- Windowing exists because a magnet must be staged *running* to learn its file
  list: an unwindowed paste of thirty would put thirty swarm fetches on BitComet in
  one click. A client that starts grinding simply stops being fed.
- Retries are safe because a send is idempotent — during a batch BitComet's API can
  answer so slowly that a send reads as failed when the task actually landed.
- Once sent, the task is BitComet's: pause, watch and remove it there. Nothing is
  stored here to drift out of date. *Discard* only cancels a staging you never sent.
- **Pick the destination folder before you resolve** — BitComet fixes a task's save
  folder at creation and cannot move it after.

**Another machine's BitComet.** *Change device* → its address (`192.168.1.50:19377`,
or just the host; port 19377 assumed) + the Web UI credentials **it** uses → *Test
connection*. Remembered across restarts. Two differences, both because its disk is
not this one: *Save to* offers that machine's registered folders instead of Browse,
and `~/…` is refused rather than expanded to this Mac's home.

Devices live in `backend/data/torrents.db` (mode `0600`). Remote passwords are
stored in plaintext because BitComet's login needs the password itself, not a digest
— the same exposure as `BitComet.xml`, which holds the local one in plaintext too.

</details>

<details>
<summary><b>🧽 Watermark Remover</b> — the mark repeats, or the batch proves it</summary>

```mermaid
flowchart TD
    A["up to 20 png / jpg / webp"] --> D["detect — chosen automatically"]
    D --> D1["fold the repeating grid<br/>median all tiles together"]
    D --> D2["pool sparse instances<br/>across the batch"]
    D --> D4["stack the batch<br/>mark stamped once per photo"]
    D --> D3["dual top-hat fallback<br/>evidence-gated"]
    D1 --> V{"does the photo really<br/>deviate under the stamp?"}
    D2 --> V
    D4 --> V
    D3 --> V
    V -- yes --> M["mask, previewed in red<br/>sensitivity slider widens it"]
    V -- no --> K["image skipped — nothing written"]
    M --> I["dilate → inpaint<br/>LaMa, else cv2 · tiled for big photos"]
    I --> Z["one zip of cleaned PNGs"]
```

- **You see every mask before anything changes.** There is no brush: the
  detector masks what it actually proved, or reports that it proved nothing and
  the image is skipped — a mask hand-drawn over a watermark detection could not
  find is a mask over whatever the person could see, and inpainting that
  damaged photographs while leaving the watermark in place.
- **A one-off mark is proven by the batch, not painted.** A mark that never
  repeats has no copies to fold within its own frame — but a supplier stamps
  the same mark at the same place on every photo, so the batch stacks its
  frames' detail fields and keeps what they all deviate on together
  (`backend/src/watermark/stacked.py`). Every constant is measured: 70/75
  synthetic marked batches detected with 0/286 false fires. It needs company —
  three frames minimum, and a lone image with a one-off mark is skipped, not
  guessed at — and it refuses a batch of near-identical shots, where
  "everything agrees" describes the scene rather than a mark. The run's
  destruction guard (`would_destroy_content` in
  `backend/src/watermark/pipeline.py`) still vetoes any mask that would take
  the picture with it.
- Folding recovers the mark itself rather than judging pixels — the overlay is
  identical in every tile, so it survives the median while the photograph cancels
  out. That makes a mark far too faint to see anywhere on its own legible.
- Folding alone is **not** evidence: a clean photo can lock onto its own sky
  gradient. Verifying each stamp against the image is what separates them, and it
  is why a mark buried in busy texture is skipped rather than guessed at. Evidence
  and the gates that were tried and rejected are in `backend/src/watermark/pattern.py`.
- Marks are shared across the batch — one watermarking tool usually ran over all of
  them — so an image that cannot recover its own is masked from a sibling's.
- Only marked pixels are ever written, always as PNG (re-encoding inpainted pixels
  as JPEG would stamp fresh artifacts right where the fill happened). LaMa picks
  CUDA → MPS → CPU; on an M-series Mac MPS is ~8× faster for output differing by at
  most one grey level.

Headless over a folder, no web app involved — it shares marks across the folder the
same way, and prints what it skipped:

```bash
cd backend && uv run python -m watermark clean IN_DIR OUT_DIR --inpainter lama
```

> For images you own or are licensed to edit — removing someone else's watermark
> from content you have no rights to is not what this is for.

</details>

<details>
<summary><b>🧹 Cache Purge</b> and <b>📦 File Gatherer</b></summary>

**Cache Purge** — edit the globs (defaults `*.dwl`, `*.dwl2`, `*.bak`, `*.log`,
`*.db`, `*.tmp`, `*.err`; catch-all patterns are refused), **Scan** to preview every
match with its total size, then **Delete** behind an explicit confirmation. Deletion
is permanent — the preview is the safety net.

**File Gatherer** — pick source and target, choose categories (Video, Audio, Image,
Subtitle, Document, Archive) and/or custom globs, then **Scan & Move** in one click.
Duplicates are auto-numbered (`name_1.ext`); you get a moved/failed summary.

</details>

<details>
<summary><b>📦 Dependency Upgrader</b> — reviewed, then one commit</summary>

Point it at a folder and it walks the tree for every `pyproject.toml` (uv) and
`package.json` (npm) — up to 40, skipping `node_modules`, `.venv`, build output —
then runs the real resolvers (`uv sync -U`, `npm install` + `npm outdated`) to
learn what is actually installable. You review the proposed bumps per manifest,
`old → new`, majors flagged.

- **Scanning and applying are separate**, and apply recomputes from the synced
  state on the server — what lands is what the resolver found, not what a page
  left open since yesterday still displays.
- Rewrites are surgical text edits, never a re-serialize, so comments and
  formatting survive. Only lagging `>=` floors are raised (`==`, `~=`, ranges and
  markered deps are left alone); npm ranges keep their `^` / `~`.
- Each rewrite re-resolves its lockfile, so manifest and lock always land
  together, agreeing with each other — every changed file across every manifest in
  **one commit** (`chore(deps): update dependencies`, editable, or untick to write
  without committing). If the commit fails, the rewrites are rolled back rather
  than left behind uncommitted.

</details>

<details>
<summary><b>🛰️ Optimized-IP Subscription</b></summary>

Batch-replace the server in your self-built `vmess` / `vless` / `trojan` nodes with
optimized Cloudflare IPs, then serve subscriptions for Shadowrocket / Clash / Surge.
Everything stays in `backend/data/sub.db`; nothing leaves the machine.

- Paste nodes plus `host[:port][#remark]` addresses — base64 subscriptions
  auto-expand, duplicates are dropped.
- One click yields Raw / Clash / Surge output, a link (`/sub/{id}?target=…`) and a
  QR code a phone on the same Wi-Fi can import — run `make host` so it can reach it.
  With no `target`, the link reads the client's User-Agent and serves the format
  that client wants.
- Identical inputs reuse the same short link (deduplicated by content hash); history
  is listed to reload or delete.

</details>

<details>
<summary><b>📄 Doc to PDF</b>, <b>📝 Doc to Markdown</b>, <b>🌐 Web Images to PDF</b>, <b>🖼️ Image to PDF</b></summary>

- **Doc to PDF** — upload `.docx`; tracked changes are accepted and comments removed
  at the XML level, then LibreOffice renders the PDFs into one zip. No Word needed.
- **Doc to Markdown** — `pdf`, `png`, `jpg`, `docx`, `pptx`, `xlsx`; each is parsed
  in a subprocess with live batch progress, and Markdown + `images/` + JSON sidecars
  come back as one zip. Advanced options pick the backend (`hybrid-engine` default,
  `pipeline`, `vlm-engine`), parse method, OCR language, effort, formula/table
  toggles. MinerU's models download on first run, so the first conversion is slower.
- **Web Images to PDF** — enter a URL, **Open in browser** (a real Chrome window
  opens on this Mac), scroll until every image has loaded, then **Capture & build
  PDF**. A bookmarked table of contents is added when the page exposes one.
- **Image to PDF** — `png` / `jpg` / `jpeg` / `heic`, ordered by filename.

</details>

## License

Copyright (c) 2026 Waining Ceoi. Licensed under the
[GNU General Public License v3.0 or later](LICENSE) — derivative works you
distribute must also be released under the GPL.
