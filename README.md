# 🧰 Toolkit

A local [Streamlit](https://streamlit.io/) app that bundles a handful of small
media & file utilities into one multipage interface.

> **macOS only.** Folder pickers use AppleScript (`osascript`) and completion
> chimes use `afplay`.

## Tools

|     | Tool                | What it does                                                                         |
| --- | ------------------- | ----------------------------------------------------------------------------------- |
| 🧲  | **Magnet Scraper**  | Scrape unwatched video magnet links automatically, in bulk, or de-duplicate a list. |
| 🖼️  | **Image to PDF**    | Combine selected images into a single PDF.                                           |
| 🎬  | **Remux Processor** | Parallel, lossless remuxing (stream-copy) of videos with configurable tracks.       |
| 📦  | **File Gatherer**   | Recursively gather files by type from a folder and move them into one target.       |

## Requirements

- **macOS** (for the native folder pickers and completion sound)
- [uv](https://docs.astral.sh/uv/)
- Python 3.14 — managed automatically by uv via `.python-version`
- [FFmpeg](https://ffmpeg.org/) on your `PATH` — required by **Remux Processor**
  (`brew install ffmpeg`)

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
- **Scan** to preview the matched files, then **Move** them. Duplicate names are
  auto-numbered (`name_1.ext`), and the target is refused if it sits inside the
  source.

## Development

Common tasks are wrapped in the `Makefile`:

```bash
make run     # uv run streamlit run src/app.py
make lint    # uv run ruff check .
make fmt     # uv run ruff format .
make test    # uv run pytest
```

`tests/` covers the pure helper functions (natural sort, AppleScript escaping,
ffmpeg command building).

## Project structure

```
toolkit/
├── src/
│   ├── app.py               # entry point — multipage navigation
│   ├── home.py              # landing / overview page
│   └── pages/
│       ├── magnet_scraper.py
│       ├── img_to_pdf.py
│       ├── remux_processor.py
│       └── file_gatherer.py
├── tests/                   # unit tests for the pure helpers
├── .env.example             # Magnet Scraper config template
├── Makefile · LICENSE
├── pyproject.toml · uv.lock
└── README.md
```

Each tool is a self-contained script — it can run on its own or as a page in the
app.

## License

Copyright (c) 2026 Waining Ceoi. Licensed under the
[GNU General Public License v3.0 or later](LICENSE) (GPL-3.0-or-later) — you may
use, modify, and redistribute this software, but derivative works that you
distribute must also be released under the GPL.
