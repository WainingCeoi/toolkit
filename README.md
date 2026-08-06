# 🧰 Toolkit

![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![Platform](https://img.shields.io/badge/Platform-macOS-000000?style=for-the-badge&logo=apple&logoColor=white)
![License](https://img.shields.io/github/license/WainingCeoi/toolkit?style=for-the-badge&logo=gnu&logoColor=white)
![Stars](https://img.shields.io/github/stars/WainingCeoi/toolkit?style=for-the-badge&logo=github)

A local app bundling small media & file utilities — a FastAPI backend driving the
engines, and a React single-page UI.

> **macOS only.** Folder pickers use AppleScript (`osascript`), and several tools
> drive desktop apps (Chrome, LibreOffice) on this Mac.

A **monorepo** with one entrance:

```
toolkit/
├── Makefile        one entrance: install / dev / start / host / build / test / clean
├── backend/        FastAPI service + the engines (Python, src layout)
└── frontend/       React + Vite single-page UI (JavaScript)
```

## Tools

|     | Tool                | What it does                                                                         |
| --- | ------------------- | ----------------------------------------------------------------------------------- |
| 🧲  | **Magnet Scraper**  | Scrape unwatched video magnet links automatically, in bulk, or de-duplicate a list. |
| 🖼️  | **Image to PDF**    | Combine selected images into a single PDF.                                           |
| 🎬  | **Remux Processor** | Parallel, lossless remuxing (stream-copy) of videos with configurable tracks.       |
| 🌊  | **Torrent Downloader** | Add a magnet or `.torrent`, keep only the files worth keeping, and send it to BitComet to download. |
| 🧽  | **Watermark Remover** | Auto-detect watermarks, correct the mask by hand, and inpaint them away (LaMa or cv2). |
| 📦  | **File Gatherer**   | Recursively gather files by type from a folder and move them into one target.       |
| 🛰️  | **Optimized-IP Subscription** | Rewrite nodes with optimized Cloudflare IPs and serve LAN subscriptions (Shadowrocket / Clash / Surge). |
| 🧹  | **Cache Purge**     | Recursively find and delete cache / junk files from a folder.                       |
| 🌐  | **Web Images to PDF** | Open a web page, scroll to load its images, and capture them into a single PDF.    |
| 📄  | **Doc to PDF** | Clean a Word doc (accept changes, remove comments) and export it to PDF (LibreOffice). |
| 📝  | **Doc to Markdown** | Convert PDFs, Office docs, and images into Markdown — text, tables, formulas, images — with MinerU. |

## Quick start

```bash
make install     # backend deps (uv) + frontend deps (npm)
make dev         # backend :8000 + frontend :5173 together, hot-reload
```

Open **http://localhost:5173**. One command runs both servers; one Ctrl-C stops both.
The Vite dev server proxies `/api` to the backend, so the UI calls same-origin and
streaming needs no CORS.

Single-server (build the UI and serve it + the API from one process, loopback):

```bash
make start       # builds frontend/dist, then serves UI + API on 127.0.0.1:8000
```

### Host it on your LAN

To reach the app from another device on the same Wi-Fi (e.g. import a proxy
subscription on your phone):

```bash
make host                    # serves API + UI on http://<this-machine>.local:8000
make host PORT=9000          # different base port (auto-advances if busy)
HOST=127.0.0.1 make host     # local-only
```

> ⚠️ `make host` binds `0.0.0.0` — everyone on the Wi-Fi can reach the app. **This
> app has no authentication, and its tools move and permanently delete files on this
> Mac**, so anyone on the network has full access to those actions. It's plain HTTP;
> run it only on a network you trust.

## Architecture

```
frontend (React + Vite) ──/api (JSON + SSE)──▶ backend (FastAPI) ──▶ engines ──▶ ffmpeg / BitComet / Chrome / LibreOffice / MinerU / SQLite
```

- **`backend/src/subgen/`** — the Optimized-IP Subscription engine (parse / rewrite /
  render / SQLite store), lifted intact from the Streamlit app.
- **`backend/src/toolkit_engine/`** — the other tools' domain logic (framework-free,
  importable): ffmpeg command building, docx cleanup, scanning, scraping, PDF
  assembly, the native folder picker.
- **`backend/src/toolkit_api/`** — the web layer: `main.py` (app factory + lifespan
  builds the shared state on `app.state`), `deps.py`, `schemas.py`, `routers/` (one
  per tool), and a small job registry streaming long-running progress over SSE.
- **`frontend/src/`** — `api.js` (one HTTP + SSE wrapper) and the React components.

Long-running work (remux, conversions, scans, deletions) runs as **jobs**: the UI
submits a batch, then follows per-item progress over Server-Sent Events.

## Configuration

Settings are read from environment variables / `backend/.env` (copy
`backend/.env.example`). Everything is optional:

| Variable | Default | Description |
| --- | --- | --- |
| `WEBSITE_URL` | empty | Magnet Scraper: base URL walked by Automatic mode |
| `CUTOFF_VIDEO` | empty | Magnet Scraper: stopping anchor; auto-advanced after each run |
| `SUB_DB_PATH` | `backend/data/sub.db` | Optimized-IP Subscription: SQLite database path |
| `SUB_ACCESS_TOKEN` | empty | Require `?token=…` on subscription links |
| `SUB_PUBLIC_HOST` | empty | Host used in subscription links; defaults to the Mac's `.local` name, then a LAN IP |
| `WATERMARK_DEVICE` | auto-detected | Watermark Remover: pin the device LaMa runs on (`cpu`, `mps`, `cuda`); by default the best available is used |
| `WATERMARK_LAMA_MODEL` | empty | Watermark Remover: path to a pre-downloaded `big-lama.pt` (skips the first-use download) |
| `APP_CORS_ORIGINS` | Vite dev origins | CORS allowlist (only exercised when calling the API cross-origin) |
| `APP_STATIC_DIR` | `../frontend/dist` | Built UI served by the single-server modes |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | `make host` bind address / base port (shell env, not `.env`) |

## Development

```bash
uv run pytest                  # run ALL backend tests (from backend/) — the single test command
uv run ruff check src tests    # lint the backend: PEP 8 via ruff — zero errors
make test                      # both of the above + a frontend build check
make build                     # frontend/dist only
make clean                     # remove build artifacts
```

## Requirements

- **macOS** (native folder pickers, desktop-app integrations)
- [uv](https://docs.astral.sh/uv/) — Python 3.14 is managed automatically via `.python-version`
- [Node.js](https://nodejs.org/) ≥ 20 (frontend build)
- [FFmpeg](https://ffmpeg.org/) on your `PATH` — required by **Remux Processor** (`brew install ffmpeg`)
- [BitComet](https://www.bitcomet.com/) — required by **Torrent Downloader**. Turn on
  *Options → Remote Access* and enable **both** switches ("via BitComet Mobile App" and
  "via Web UI"), then set a username and password; the app reads them from BitComet's own
  config, so there is nothing to configure twice
- [torch](https://pytorch.org/) — required by **Watermark Remover**'s LaMa inpainter; installed with the backend via the `watermark` extra, and the big-lama checkpoint (~200 MB) downloads automatically on first use (cached under `~/.cache/torch`). Without it the tool still runs on its cv2 inpainter
- [Google Chrome](https://www.google.com/chrome/) — required by **Web Images to PDF** (the matching driver is downloaded automatically)
- [LibreOffice](https://www.libreoffice.org/) — required by **Doc to PDF** (`brew install --cask libreoffice`)
- [MinerU](https://github.com/opendatalab/MinerU) — required by **Doc to Markdown**; installed with the backend via the `mineru[core]` dependency, its ML models download automatically on first run (cached under `~/.cache/huggingface`)

## Tools in detail

### 🧲 Magnet Scraper

Three modes:

- **Automatic** — walks your source site page by page from the configured
  `WEBSITE_URL` until it reaches the last-seen video (`CUTOFF_VIDEO`), then scrapes
  magnets for everything newer. The cutoff is advanced automatically to the newest
  link after each successful run.
- **Manual** — paste video page URLs and scrape their magnets.
- **Remove Duplicated** — paste raw magnet links and get the unique set back.

### 🖼️ Image to PDF

Upload one or more images (`png`, `jpg`, `jpeg`, `heic` — iPhone HEIC photos
supported via `pillow-heif`), name the output, and download the combined PDF.
Images are ordered by filename.

### 🎬 Remux Processor

Lossless, parallel remuxing with FFmpeg (no re-encoding): pick a source folder,
select videos, configure video / audio / subtitle track indices and the subtitle
language tag, optionally attach external subtitle files (matched by filename stem),
choose an output folder and worker count, then watch per-file live progress and a
success/failure summary.

### 🌊 Torrent Downloader

Paste a magnet link or pick a `.torrent`, review every file inside it **before any
content downloads**, and fetch only what matches your filter — by default video
files over 100 MB, so the screenshots, samples and `RARBG.txt` are left behind.
Tick any row to override the rule.

The minimum size applies to video and audio only. A global floor would discard
every subtitle the moment you ticked that box, since they are ~40 KB.

This is a **dispatcher, not a download manager**. Once you hit *Send*, the task
belongs to BitComet: pause it, watch its progress and remove it in BitComet's own
window, which is the only thing that actually knows what the download is doing.
Nothing is stored on this side, so there is no second copy of BitComet's task list
here to drift out of date with it.

BitComet is yours — this tool never starts, stops or quits it. *Discard* on a
review card is the one exception, and it cancels a staging you never sent: a
magnet has to run while it fetches its metadata, so abandoning one without that
would leave it downloading with every file enabled.

Pick the destination folder *before* you resolve: BitComet fixes a torrent's save
folder as the task is created and cannot move it afterwards.

### 🧽 Watermark Remover

Drop up to 20 `png` / `jpg` / `webp` images and the backend proposes a
watermark mask per image. **You see every mask before anything changes**: each
image is drawn with its mask tinted red, over a sensitivity slider that
refetches the proposal. On run the masks are dilated a few pixels and inpainted
— **LaMa** (ML, best quality; the ~200 MB model downloads on first use) or
**cv2** (instant, rougher on large areas). Progress streams per file; results
are per-file downloads plus a zip of everything.

The mask is reviewed, not painted. There was a brush and an eraser, and dropping
them is deliberate: the detector masks the copies of a mark it actually
recovered, or reports that it recovered nothing and the image is **skipped**.
Hand-painting the second case masks whatever the person could see rather than
the watermark, and inpainting that damaged photographs while leaving the
watermark in place — measured at 2.6–6.8% of pixels moved by 31–58 grey levels,
with the watermark still there afterwards. Doing nothing is the better answer,
and the page says which images got it.

Only the pixels you marked are ever written, and each image becomes available
the moment it is done — so a run that stops early still hands back everything
it finished. Large photos are inpainted in tiles, which is what keeps a 36 MP
phone photo inside about 12 GB instead of the 100 GB+ it would otherwise ask
for.

There are two detectors, and the default — *Repeating pattern* — recovers the
mark itself rather than judging pixels. It finds the grid the overlay repeats
on and medians all the tiles together, so the mark, identical in every tile,
survives while the photograph cancels out. That is contrast amplification done
statistically, and it makes a mark far too faint to see anywhere on its own
perfectly legible: on one 36 MP photo, folding 29 sky tiles recovered a logo
and its lettering clearly enough to read. The recovered mark is then matched
back over the image and **only its copies are masked**, so seams, wires and
detail are untouched.

Every stamp is then checked against the photo, and kept only where the image
really does deviate there. That check is what lets this lead: it is how the
tool knows when the "repeat" it found was an artefact rather than ink. Any
image where little of the stamp survives falls back, on its own, to the second
detector — a dual top-hat filter normalised against each pixel's own
neighbourhood, which works on any watermark including a single corner logo, at
the cost of also flagging thin detail like tent seams and railings. A batch can
mix the two, and the page says which ran.

> Folding alone is not evidence of a watermark. A *clean* test photo passed a
> fold-significance check by locking onto its own sky gradient, scoring higher
> than genuinely watermarked photos. Verifying each stamp against the image is
> what separates them: 0.53–0.57 of the stamp survived where a mark was really
> recovered, against 0.00–0.20 where the period estimate had locked onto
> scenery — 0.00 on the watermark-free photo.

Recovery does not always work, and when it does not the image is skipped rather
than guessed at. Two things rescue images that would otherwise be: the mark is
shared across the batch, so an image that cannot recover its own is masked from
a sibling's (one watermarking tool usually ran over all of them), and
sensitivity widens each stamp. Coverage is still thinner over busy ground than
over sky, where the mark is genuinely buried in the texture.

A third route covers the mark too large for any single frame to fold: about six
copies of a ~300 px cell, where the fold needs nine. Instances are located
directly (a copy on smooth sky is a local response peak, and a patch cut around
one correlates at the others at 0.92–0.94), then **pooled across the batch** —
nine samples from nine different photographs, which is what makes averaging mean
anything. What certifies it is that a real overlay repeats at the *same* pitch in
every image: measured 294.0, 294.0, 294.0 across three photos, against
321/236/173 and 311/306/213 for coincidental runs in clean frames.

Measured on a sample of eight photos: **8 detected, 0 skipped**, with clean
control frames still masking nothing at all. Masks from the pooled route are
small — 0.20–0.27% of frame against 2.0–5.8% for the folded route — because only
the copies that correlate confidently are stamped.

> The pooled route's one gate is that pitch agreement, and it distinguishes a
> watermark from coincidence but **not** from a structure that genuinely recurs
> across a batch at one spacing — a tiled floor, a brick wall, a rank of windows,
> photographed three or more times. Two things blunt that: matches must be evenly
> spaced, which excludes repeats receding with perspective, and every mask still
> faces the per-pixel evidence check. Two stronger gates were tried and both
> measured worse than useless; see the notes in `pattern.py`.

LaMa runs on the best accelerator it finds — CUDA, then Apple's MPS, then CPU.
On an M-series Mac that is roughly 8× faster than CPU for output that differs
by at most one grey level, and it took a 36 MP photo from 118s to 22s. Pin it
with `WATERMARK_DEVICE` if you need a specific backend.


Cleaned images are written as PNG regardless of input — re-encoding inpainted
pixels as JPEG would stamp fresh artifacts right where the fill happened.

The engine also runs headless over a folder, no web app involved:

```bash
cd backend && uv run python -m watermark clean IN_DIR OUT_DIR --inpainter lama
```

The CLI sees the whole folder at once, so it shares marks across it exactly as
the web page does across a batch. It prints which images were skipped; a
watermark faint enough to hide inside the scene's own texture does not separate
at any sensitivity, and nothing is written for those.

> For images you own or are licensed to edit — removing someone else's
> watermark from content you have no rights to is not what this is for.

### 📦 File Gatherer

Recursively collect files by type and move them into a single folder. Pick source
and target folders, choose categories (Video, Audio, Image, Subtitle, Document,
Archive) and/or custom glob patterns, then **Scan & Move** in one click — with live
progress, auto-numbered duplicate names (`name_1.ext`), and a moved/failed summary.

### 🛰️ Optimized-IP Subscription

Engine in `backend/src/subgen/`. Batch-replace the server in your self-built
`vmess` / `vless` / `trojan` nodes with optimized Cloudflare IPs, then generate
subscriptions for Shadowrocket / Clash / Surge — as a LAN link, a QR code, or
downloadable files. Everything is stored locally in `backend/data/sub.db`; nothing
leaves your machine.

- Paste nodes plus optimized `host[:port][#remark]` addresses; base64 subscriptions
  auto-expand and duplicates are removed.
- One click produces Raw / Clash / Surge output, a subscription link
  (`/sub/{id}?target=…`, served natively by the backend), and a QR code a phone on
  the same Wi-Fi can import directly — use `make host` so the phone can reach it.
- Identical inputs reuse the same short link (deduplicated by content hash); history
  is listed to reload or delete.

### 🧹 Cache Purge

Recursively find and delete cache / junk files from a folder. Edit the file-type
globs (defaults cover `*.dwl`, `*.dwl2`, `*.bak`, `*.log`, `*.db`, `*.tmp`,
`*.err`; catch-all patterns are refused), **Scan** to preview every match with total
size, then **Delete** after an explicit confirmation. Deletion is permanent, so the
preview is your safety net.

### 🌐 Web Images to PDF

Capture a lazy-loaded web page's images into a single PDF (requires Google Chrome):
enter the page URL, **Open in browser** — a real Chrome window opens on this Mac —
scroll until every image has loaded, then **Capture & build PDF**. The page's images
are downloaded, stitched into a PDF, and a bookmarked table of contents is added
when the page exposes one.

### 📄 Doc to PDF

Clean Word documents and export them to PDF (no Microsoft Word needed): upload
`.docx` files; every tracked change is accepted and comments are removed at the XML
level, then LibreOffice renders the PDFs — bundled into a single zip download.

### 📝 Doc to Markdown

Convert documents to Markdown with MinerU — text, tables, formulas, and extracted
images. Upload `pdf`, `png`, `jpg`, `docx`, `pptx`, or `xlsx` files; each is parsed
in a subprocess with live batch progress; all output (Markdown + `images/` + JSON
sidecars) is bundled into a single zip download. Advanced options pick the MinerU
backend (`hybrid-engine` default, `pipeline`, `vlm-engine`), parse method, OCR
language, effort, and formula/table toggles.

> MinerU's models download on first run, so the first conversion takes longer.

## License

Copyright (c) 2026 Waining Ceoi. Licensed under the
[GNU General Public License v3.0 or later](LICENSE) (GPL-3.0-or-later) — you may
use, modify, and redistribute this software, but derivative works that you
distribute must also be released under the GPL.
