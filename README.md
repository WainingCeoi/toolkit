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
| 📄  | **Doc to PDF**          | Accept tracked changes, strip comments, render to PDF via LibreOffice.           |
| 📝  | **Doc to Markdown**     | PDFs / Office docs / images → Markdown with MinerU (text, tables, formulas).     |
| 🧹  | **Cache Purge**         | Recursively find and delete cache / junk files.                                  |
| 📸  | **Photos Library Filter** | Mirror a Photos library without its caches, safe while Photos is running.     |
| 📦  | **Dependency Upgrader** | Upgrade every uv / npm manifest under a folder, relock, and commit each repo.    |

**🌐 Network**

|     | Tool                          | What it does                                                          |
| --- | ----------------------------- | --------------------------------------------------------------------- |
| 🛰️  | **Optimized-IP Subscription** | Rewrite nodes with optimized Cloudflare IPs, serve LAN subscriptions.  |

## Quick start

Install [uv](https://docs.astral.sh/uv/) and a supported
[Node.js](https://nodejs.org/) version once, then run this from the project folder:

```bash
make start
```

This prepares the locked Python and Node dependencies, builds the frontend, and
serves the UI and API together on this Mac. The first run downloads dependencies;
later runs reuse the Python environment and dependency caches. Doc to Markdown's
MinerU extra is included on supported Macs.

Open the URL printed in the terminal, normally `http://127.0.0.1:8000`. If that
port is busy, Toolkit picks the next available one. Press **Ctrl-C** to stop.

For everyday use, `make start` is the only command to remember. `make` shows this
short usage guide; `make help-maintenance` lists the other commands.

## How it works

```mermaid
flowchart LR
    UI["React + Vite<br/>frontend/src"]
    subgraph one["one Python process"]
        API["FastAPI<br/>routers/ — one per tool"]
        REG["job registry<br/>bounded worker pool"]
        ENG["engines<br/>toolkit_engine · subgen"]
    end
    EXT["ffmpeg · Chrome · LibreOffice<br/>MinerU · BitComet · SQLite"]
    UI -- "JSON + SSE over /api" --> API
    API --> REG --> ENG --> EXT
```

| Path                            | Role                                                                    |
| ------------------------------- | ----------------------------------------------------------------------- |
| `backend/src/toolkit_engine/`   | Framework-free domain logic: ffmpeg, docx, scanning, scraping, PDFs.    |
| `backend/src/subgen/`           | Subscription engine — parse / rewrite / render / SQLite.                |
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

The server accepts at most 16 outstanding jobs by default, with up to eight worker
threads. When full it returns a retryable `503` instead of retaining more queued
uploads. Shutting down cancels queued jobs and releases their resources.

## Configuration

Environment variables, or `backend/.env` (copy `backend/.env.example`). All optional.

| Variable                 | Default               | What it does                                                     |
| ------------------------ | --------------------- | ---------------------------------------------------------------- |
| `WEBSITE_URL`            | empty                 | Magnet Scraper: base URL walked by Automatic mode                |
| `CUTOFF_VIDEO`           | empty                 | Magnet Scraper: stopping anchor, auto-advanced after each run    |
| `SUB_DB_PATH`            | `backend/data/sub.db` | Subscription: SQLite database path                               |
| `SUB_ACCESS_TOKEN`       | empty                 | Require `?token=…` on subscription links                         |
| `SUB_PUBLIC_HOST`        | `.local` name         | Host used in subscription links, then a LAN IP                   |
| `APP_CORS_ORIGINS`       | Vite dev origins      | CORS allowlist (cross-origin API calls only)                     |
| `APP_STATIC_DIR`         | `../frontend/dist`    | Built UI served by the single-server modes                       |
| `TOOLKIT_DISABLED_TOOLS` | empty                 | Slugs to switch off — hidden **and** unmounted (404). Read at startup |
| `HOST` / `PORT`          | `0.0.0.0` / `8000`    | `make host` bind / base port (shell env, not `.env`)             |

## Requirements

| Needed by             | Requirement                                            | Notes                                                             |
| --------------------- | ------------------------------------------------------ | ----------------------------------------------------------------- |
| everything            | [uv](https://docs.astral.sh/uv/)                       | Python 3.14, managed via `.python-version`                        |
| everything            | [Node.js](https://nodejs.org/) 22.22+, 24.15+ or 26+   | frontend build                                                    |
| Remux Processor       | [FFmpeg](https://ffmpeg.org/)                          | `brew install ffmpeg`                                             |
| Torrent Downloader    | [BitComet](https://www.bitcomet.com/)                  | *Options → Remote Access*: enable **both** switches, set user/pass |
| Web Images to PDF     | [Google Chrome](https://www.google.com/chrome/)        | matching driver downloaded automatically                          |
| Doc to PDF            | [LibreOffice](https://www.libreoffice.org/)            | `brew install --cask libreoffice`                                 |
| Doc to Markdown       | [MinerU](https://github.com/opendatalab/MinerU)        | the `docmd` extra (`uv sync --extra docmd`); models download on first run |

For BitComet on *this* Mac the app reads credentials from BitComet's own config —
nothing to configure twice.

## Maintenance and development

The root `Makefile` contains startup, setup, development, testing, hosting, and
cleanup commands. `make` shows the short everyday guide; `make help-maintenance`
lists the commands used by maintainers and coding agents.

| Command | Purpose |
| --- | --- |
| `make test` | Backend tests and Ruff; frontend type checks, lint, tests, and build |
| `make dev` | Prepare dependencies, then run API + Vite with hot reload |
| `make host` | Prepare dependencies, build, and serve the UI + API to the LAN |
| `make install` | Prepare locked dependencies without starting the app |
| `make build` | Build the frontend |
| `make lint` | Run lint, formatting, and type checks |
| `make clean` | Remove project build outputs and caches; keep dependencies and runtime data |

For separate development terminals, use `make backend` and `make frontend` after
`make install`. The backend records its chosen port in `backend/.port`; the
frontend proxies `/api` to that port. `make dev` normally serves the UI at
`http://localhost:5173`.

`make start PORT=9000` or `make host PORT=9000` changes the base port.
`HOST=127.0.0.1 make host` keeps hosting local. To omit the optional MinerU stack,
use `make start EXTRAS=`; Intel Macs omit it automatically.

> ⚠️ **`make host` has no authentication**, and these tools move and permanently
> delete files on this Mac. It's plain HTTP — run it only on a network you trust.

### Continuous integration

[GitHub Actions](.github/workflows/ci.yml) runs `make test` on pull requests,
pushes to `main`, and manual runs from the repository's Actions tab. It uses
macOS for the native Photos-library tests, installs the committed lockfiles,
and skips the optional MinerU stack. Local checks and CI share the same command:

```bash
make test
```

From `backend/`: `uv run pytest` is the single backend test command, and
`uv run ruff check src tests` the lint.

## Local data

Toolkit uses Python's built-in SQLite driver, with no separate database server.
The default data folder is `backend/data/`; `SUB_DB_PATH` changes the subscription
database path and moves the other persistent stores into its parent folder.

| Store | Contents | Survives an API restart? |
| --- | --- | --- |
| `data/sub.db` | `subscriptions`: generated nodes as JSON, unique input hash, options, counts, timestamp | Yes |
| `data/torrents.db` | `devices`: saved BitComet connections; `device_settings`: selected device | Yes |
| `data/bitcomet-device-id` | BitComet pairing identity | Yes |
| Job registry and temporary artifacts | Job status/results in memory; temporary output files eligible for cleanup after six idle hours | No |

Expiry cleanup runs on subsequent store access.

Keep **one API worker process**: jobs, staged uploads, browser sessions, and locks
are shared in memory. Browser reloads can reattach to jobs while that process is
alive; restarting the API does not resume them. SQLite fits this local metadata
workload; image processing and conversion work happen outside the database.

Both databases can contain credentials and use owner-only filesystem permissions.
For a live database backup, use SQLite's backup command/API; copying only the `.db`
file can omit uncheckpointed WAL data. Older `torrents.db` files may also contain
unused `torrents` and `torrent_files` tables from a previous implementation.

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
  stored here to drift out of date. *Discard* only cancels a staging you never sent,
  and only one this server staged: after a restart it leaves the task alone rather
  than deleting a download that may be yours.
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
<summary><b>📸 Photos Library Filter</b> — a cache-free mirror Photos can still open</summary>

Point it at the live library and a `*.photoslibrary` destination outside it,
**Start dry run** to see the plan, then flip the switch to **Mirror** and run it
for real. Re-running only copies what changed. What a run does, in order:

1. **plan** — walk the source and classify every file with the rules (below).
   Every directory is mirrored; an excluded one stays as an empty skeleton, so
   the bundle keeps its shape.
2. **snapshot** — any file with a `<name>-wal` sibling is a live WAL-mode SQLite
   database (`database/Photos.sqlite`, the analysis databases). It is written
   with `VACUUM INTO` from a read-only connection, so the copy is complete even
   while Photos is writing; `-wal` / `-shm` files are never copied.
3. **copy** — `copyfile(3)` with `COPYFILE_CLONE`: an APFS clone (instant, no
   extra space on the same volume) that keeps mtime and the
   `com.apple.assetsd.*` extended attributes Photos stores on originals. An
   unchanged file is skipped; a favourite flag Photos changed without touching
   mtime is still picked up.
4. **delete** — anything in the destination that is not in the plan is removed.
   The destination must end in `.photoslibrary` and must not overlap the source.
5. **verify** — the mirrored `Photos.sqlite` is opened read-only and every
   asset's original, and every edited asset's `resources/renders/<X>/<UUID>.plist`
   edit recipe, is checked for. Problems are listed in the report.

Rules are rsync-style, one per line; everything not matched is kept:

```
# unanchored: matches at any depth
.DS_Store
# trailing slash: a directory and everything inside it
database/search/
# * and ? stay inside one path component
database/*.lock
# ** crosses components
private/**/caches/
# leading slash: anchored to the library root
/top-level-only.txt
```

Never exclude `resources/renders/`: it is not a cache but the edit recipes and
rendered edits the database marks as present. `resources/derivatives/`
(thumbnails and previews) is a real cache; if thumbnails show blank after
restoring a mirror, hold Option+Command while opening Photos and choose Repair
Library. Nothing is ever written into the source library.

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
