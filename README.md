# 🧰 Toolkit

![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-1.58-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-macOS-000000?style=for-the-badge&logo=apple&logoColor=white)
![License](https://img.shields.io/github/license/WainingCeoi/toolkit?style=for-the-badge&logo=gnu&logoColor=white)
![Stars](https://img.shields.io/github/stars/WainingCeoi/toolkit?style=for-the-badge&logo=github)

A local [Streamlit](https://streamlit.io/) app that bundles a handful of small
media & file utilities into one multipage interface.

> **macOS only.** Folder pickers use AppleScript (`osascript`).

## Tools

|     | Tool                | What it does                                                                         |
| --- | ------------------- | ----------------------------------------------------------------------------------- |
| 🧲  | **Magnet Scraper**  | Scrape unwatched video magnet links automatically, in bulk, or de-duplicate a list. |
| 🖼️  | **Image to PDF**    | Combine selected images into a single PDF.                                           |
| 🎬  | **Remux Processor** | Parallel, lossless remuxing (stream-copy) of videos with configurable tracks.       |
| 📦  | **File Gatherer**   | Recursively gather files by type from a folder and move them into one target.       |
| 🛰️  | **Optimized-IP Subscription** | Rewrite nodes with optimized Cloudflare IPs and serve LAN subscriptions (Shadowrocket / Clash / Surge). |
| 🧹  | **Cache Purge**     | Recursively find and delete cache / junk files from a folder.                       |
| 🌐  | **Web Images to PDF** | Open a web page, scroll to load its images, and capture them into a single PDF.    |
| 📄  | **Doc to PDF** | Clean a Word doc (accept changes, remove comments) and export it to PDF (LibreOffice). |
| 📝  | **Doc to Markdown** | Convert PDFs, Office docs, and images into Markdown — text, tables, formulas, images — with MinerU. |

## Requirements

- **macOS** (for the native folder pickers)
- [uv](https://docs.astral.sh/uv/)
- Python 3.14 — managed automatically by uv via `.python-version`
- [FFmpeg](https://ffmpeg.org/) on your `PATH` — required by **Remux Processor**
  (`brew install ffmpeg`)
- [Google Chrome](https://www.google.com/chrome/) — required by **Web Images to
  PDF** (the matching driver is downloaded automatically)
- [LibreOffice](https://www.libreoffice.org/) — required by **Doc to PDF**
  (`brew install --cask libreoffice`)
- [MinerU](https://github.com/opendatalab/MinerU) — required by **Doc to
  Markdown**; installed with the project via the `mineru[core]` dependency, and
  its ML models download automatically on first run (cached under
  `~/.cache/huggingface`)

## Install

```bash
uv sync
```

## Run

```bash
uv run streamlit run src/app.py
```

Then pick a tool from the sidebar. You can also launch a single tool directly,
e.g.:

```bash
uv run streamlit run src/pages/remux_processor.py
```

## Tools in detail

### 🧲 Magnet Scraper — `src/pages/magnet_scraper.py`

Three modes:

- **Automatic** — walks your source site page by page from a `.env` config until
  it reaches the last-seen video, then scrapes magnets for everything newer.
- **Manual** — paste video page URLs and scrape their magnets.
- **Remove Duplicated** — paste raw magnet links and get the unique set back.

Automatic mode reads/writes a `.env` file in the project root — copy the
template and fill it in:

```bash
cp .env.example .env
```

```dotenv
WEBSITE_URL=https://example.com
CUTOFF_VIDEO=https://example.com/last-watched-video
```

`CUTOFF_VIDEO` is advanced automatically to the newest link after each run. The
scrape stops at the cutoff (or after a page cap) so it never loops indefinitely.

### 🖼️ Image to PDF — `src/pages/img_to_pdf.py`

Upload one or more images (`png`, `jpg`, `jpeg`, `heic` — iPhone HEIC photos
supported via `pillow-heif`), name the output, and save a combined PDF to your
Desktop. Images are ordered by filename.

### 🎬 Remux Processor — `src/pages/remux_processor.py`

Lossless, parallel remuxing with FFmpeg (no re-encoding):

- Pick a **source folder**, then select which videos to process.
- Configure **video / audio / subtitle** track indices (single or multiple audio
  tracks) and the subtitle language tag.
- Optionally attach **external subtitle files**, matched by filename.
- Choose an output folder and the number of parallel workers; watch per-file
  progress bars and a success/failure summary.

### 📦 File Gatherer — `src/pages/file_gatherer.py`

Recursively collect files by type and move them into a single folder:

- Pick **source** and **target** folders.
- Choose file-type **categories** (Video, Audio, Image, Subtitle, Document,
  Archive) and/or add custom glob patterns.
- **Scan & Move** in one click — matches are moved immediately, with a progress
  bar and a moved/failed summary. Duplicate names are auto-numbered
  (`name_1.ext`), and the target is refused if it sits inside the source.

### 🛰️ Optimized-IP Subscription — `src/pages/optimized_ip_generator.py`

Engine lives in `src/lib/subgen/`. Batch-replace the server in your self-built
`vmess` / `vless` / `trojan` nodes with optimized Cloudflare IPs, then generate
subscriptions for Shadowrocket / Clash / Surge — as an auto-updating LAN link, a
QR code, or downloadable files. Everything is stored locally in `data/sub.db`;
nothing leaves your machine.

- Paste nodes plus optimized `host[:port][#remark]` addresses; base64
  subscriptions auto-expand and duplicates are removed.
- One click produces Raw / Clash / Surge output, a subscription link, and a QR
  code a phone on the same Wi-Fi can import directly.
- Identical inputs reuse the same short link (deduplicated by content hash);
  history is listed at the bottom to reload or delete.

**LAN sub-server.** Streamlit can't return raw subscription bodies, so the page
starts a small standard-library `http.server` in a background thread (default
port `8765`) serving `/sub/{id}`. It starts the first time you open the page and
stays up for the life of the app. Links point at your Mac's `.local` name (or a
LAN IP), e.g. `http://192.168.x.x:8765/sub/<id>?target=clash`; append
`&download=1` to download the file instead.

**Configuration** — all optional, via environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `SUB_HTTP_PORT` | `8765` | Subscription-link port |
| `SUB_HTTP_HOST` | `0.0.0.0` | Bind address for the subscription port |
| `SUB_PUBLIC_HOST` | empty | Host used in links; defaults to the Mac's `.local` name, then a LAN IP |
| `SUB_DB_PATH` | `data/sub.db` | SQLite database path |
| `SUB_ACCESS_TOKEN` | empty | Require `?token=…` on subscription links |
| `SUB_DISABLE_HTTP` | empty | Set to `1` to skip starting the sub-server |

```bash
SUB_ACCESS_TOKEN=your-token uv run streamlit run src/app.py
```

### 🧹 Cache Purge — `src/pages/cache_purge.py`

Recursively find and delete cache / junk files from a folder:

- Pick a **folder**, then edit the file-type globs (defaults cover `*.dwl`,
  `*.dwl2`, `*.bak`, `*.log`, `*.db`, `*.tmp`, `*.err`).
- **Scan** to preview every match (with total size), then **Delete** — files are
  removed in parallel. Deletion is permanent, so the preview is your safety net.

### 🌐 Web Images to PDF — `src/pages/web_images_to_pdf.py`

Capture a lazy-loaded web page's images into a single PDF (requires Google
Chrome):

- Enter the page **URL** and an output folder, then **Open in browser** — a real
  Chrome window opens (its driver is auto-managed by `webdriver-manager`).
- Scroll until every page/image has loaded, then **Capture & build PDF**. The
  page's images (`img[class*=bi]`) are downloaded, stitched into a PDF, and a
  bookmarked table of contents is added when the page exposes one.

### 📄 Doc to PDF — `src/pages/doc_to_pdf.py`

Clean Word documents and export them to PDF (no Microsoft Word needed):

- Upload one or more `.docx` files.
- Every tracked change is accepted and comments are removed at the XML level —
  so the document carries no revision markup — then LibreOffice renders the PDF.

All files are converted in one LibreOffice run and bundled into a single zip you
can download.

### 📝 Doc to Markdown — `src/pages/doc_to_markdown.py`

Convert documents to Markdown with
[MinerU](https://github.com/opendatalab/MinerU) — text, tables, formulas, and
extracted images:

- Upload one or more `pdf`, `png`, `jpg`, `docx`, `pptx`, or `xlsx` files.
- Each file is parsed by MinerU in a subprocess, with a progress bar tracking
  the batch (`2/3 — report.pdf …`).
- All output — the Markdown plus its `images/` and JSON sidecars — is bundled
  into a single zip you can download.

**Advanced options** pick the MinerU backend (`hybrid-engine` by default,
`pipeline` for a lighter/faster run, or `vlm-engine`), the pipeline parse method
(`auto` / `txt` / `ocr`), OCR language, hybrid effort, and formula/table
toggles.

> MinerU's models download on first run, so the first conversion takes longer.
> The `hybrid-engine` / `vlm-engine` backends need the VLM models pulled in by
> the `mineru[core]` dependency.

## Development

Common tasks are wrapped in the `Makefile`:

```bash
make run     # uv run streamlit run src/app.py
make lint    # uv run ruff check .
make fmt     # uv run ruff format .
make test    # uv run pytest
```

`tests/` covers the pure helper functions (natural sort, AppleScript escaping,
ffmpeg and MinerU command building).

## Project structure

```
toolkit/
├── data/                    # local SQLite store (git-ignored)
│   └── sub.db
├── src/
│   ├── app.py               # entry point — multipage navigation
│   ├── home.py              # landing / overview page
│   ├── pages/               # one self-contained script per tool
│   │   ├── magnet_scraper.py
│   │   ├── img_to_pdf.py
│   │   ├── remux_processor.py
│   │   ├── file_gatherer.py
│   │   ├── optimized_ip_generator.py
│   │   ├── cache_purge.py
│   │   ├── web_images_to_pdf.py
│   │   ├── doc_to_pdf.py
│   │   └── doc_to_markdown.py
│   └── lib/                 # engines for tools that need >1 module
│       └── subgen/          # Optimized-IP Subscription engine
│           ├── core.py      # parse / rewrite / render
│           ├── db.py        # SQLite store
│           ├── subserver.py # background /sub/{id} HTTP server
│           ├── netutil.py   # short id / dedup hash / LAN IP
│           └── config.py    # environment configuration
├── tests/                   # unit tests for the pure helpers
├── .env.example             # Magnet Scraper config template
├── Makefile · LICENSE
├── pyproject.toml · uv.lock
└── README.md
```

Most tools are a single self-contained script that can also run on its own (e.g.
`uv run streamlit run src/pages/remux_processor.py`). The **Optimized-IP
Subscription** tool is the exception: its engine lives in `src/lib/subgen/`, so
run it through the app entry (`src/app.py`) rather than standalone.

## License

Copyright (c) 2026 Waining Ceoi. Licensed under the
[GNU General Public License v3.0 or later](LICENSE) (GPL-3.0-or-later) — you may
use, modify, and redistribute this software, but derivative works that you
distribute must also be released under the GPL.
