# Toolkit

Toolkit brings twelve media, file, and document utilities into one app.
It runs on your Mac and opens in your browser.

**Requires macOS.** Some tools use macOS folder pickers or control apps installed
on your Mac.

## Get started

Install the prerequisites before the first run.

- macOS Command Line Tools (`xcode-select --install`) for Git, Make, and `python3`.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs and manages Python for Toolkit.
- [Node.js](https://nodejs.org/) 22.22.2+ within 22.x, 24.15.0+ within 24.x, or 26+.

uv manages Toolkit's pinned Python 3.14.7 runtime.
Run this command from the `toolkit` project folder in Terminal.

```bash
make dev
```

The first start installs dependencies from the lockfiles and creates
`backend/.env` from the example if needed. Later starts reuse current
dependencies. The FastAPI backend and Vite frontend reload on source changes.

Open the frontend address printed in Terminal, normally
[http://127.0.0.1:5173](http://127.0.0.1:5173). The backend normally listens on
port 8000. Keep Terminal open while using the app and press **Ctrl-C** to stop
both services.

Choose a tool from the sidebar, or press **/** to search. Open tools appear as
tabs along the bottom, so you can switch tools while a task is running.

## Available tools

| Tool | What it does |
| --- | --- |
| Magnet Scraper | Collect magnet links from video pages and remove duplicates. |
| Remux Processor | Repackage videos without changing their quality; choose which tracks to include. |
| Torrent Downloader | Review a torrent's files, select what to keep, and send it to BitComet. |
| Web Images to PDF | Capture images from a web page into one PDF. |
| File Gatherer | Move files of selected types from nested folders into one folder. |
| Image to PDF | Combine images, including iPhone HEIC photos, into one PDF. |
| Doc to PDF | Convert Word documents to PDFs, accepting tracked changes and removing comments. |
| Doc to Markdown | Convert PDFs, Office documents, and images into Markdown text. |
| Cache Purge | Find cache and junk files, review the results, then delete them. |
| Photos Library Filter | Create a copy of a Photos library without its caches, preserving originals and edits. |
| Dependency Upgrader | Update a project's Python and Node packages and commit the changes. |
| Optimized-IP Subscription | Replace server addresses in proxy configurations and generate subscription links. |

File Gatherer moves files, and Cache Purge permanently deletes them. Review the
selected folders and results carefully. Photos Library Filter offers a dry run
to preview changes before creating or updating the destination library.

## AI / MCP access

Toolkit also provides a local stdio MCP server covering all twelve tools. Start
Toolkit with `make dev`, then point MCP at the backend address printed in
Terminal. The adapter shares backend jobs, browser sessions, devices, and scan
records with the web app.

```bash
backend/.venv/bin/python -m toolkit_mcp --api-base-url http://127.0.0.1:8000
```

Use the actual port above. For an AI client's stdio configuration, use the
absolute path to `backend/.venv/bin/python` as the command, with arguments
`["-m", "toolkit_mcp"]`, and set `TOOLKIT_API_BASE_URL` to the running backend's
address. Run `backend/.venv/bin/toolkit-mcp --help` for connection options.

Start with `toolkit_health`, then use the discovered tool descriptions for
parameters and operation effects. Upload paths and download destinations refer
to the MCP host; folders and project paths processed by the backend refer to the
API host. Downloads require an explicit output path and refuse overwriting by
default. Use `job_status` for progress, `job_cancel` for best-effort cancellation,
and `artifact_download` or `artifact_markdown` for results. Requests are not
automatically retried; after a timeout, check backend state before repeating an
operation. MCP-created jobs share backend state but do not automatically appear
in the current web task bar.

The MCP dependency stack has been tested on Apple silicon. Intel Mac installation
is unverified and may require native build tools for its cryptography dependency.

## Additional apps for specific tools

Install these only if you use the corresponding tool:

| Tool | Required app or package |
| --- | --- |
| Remux Processor | [FFmpeg](https://ffmpeg.org/) — with Homebrew: `brew install ffmpeg` |
| Torrent Downloader | [BitComet](https://www.bitcomet.com/) — enable both switches under **Options → Remote Access** and set a username and password. |
| Web Images to PDF | [Google Chrome](https://www.google.com/chrome/) |
| Doc to PDF | [LibreOffice](https://www.libreoffice.org/) — with Homebrew: `brew install --cask libreoffice` |

Doc to Markdown uses [MinerU](https://github.com/opendatalab/MinerU). Setup includes
it on Apple silicon Macs; its models download during the first conversion,
which can take longer. Intel Macs skip it because its dependencies are unsupported.
The converter uses MinerU 4's local parsing tiers: Standard by default for PDFs
and images, or native Flash parsing for Office files. Basic uses smaller OCR,
table, and formula models; Advanced spends more time on difficult layouts.
To omit this converter on any Mac, use `EXTRAS=` with `make dev` or `make init`.

## Commands

One `Makefile` in the project folder provides the main commands:

| Command | When to use it |
| --- | --- |
| `make` or `make help` | Show the commands and options. |
| `make dev` | Prepare dependencies and start both services with automatic reload. |
| `make init` | Preview and confirm a destructive local reset, rebuild the environment, and exit. |
| `make test` | Run both test suites, code checks, and the frontend build. |

Use `make dev PORT=9000 FRONTEND_PORT=5174` to change either port. Both services
bind to loopback. If a requested port is occupied, choose another port or stop
the process using it. Vite's API proxy and the backend's development CORS origins
follow these settings.

Run `make init` only when you want to reset this checkout. The preview uses
Git's ignore rules, including local and global exclusions. It can include
`backend/.env`, SQLite data and saved device settings, dependencies, build output,
caches, ignored local notes, and editor settings. Make a choice for any nested
repository in the preview, then type `reset` to approve the listed deletions.
The command rebuilds the dependencies, backend configuration, and local SQLite
stores.

Run `make dev` and stop it with **Ctrl-C** to prepare a fresh checkout for testing.
For setup without launching services, run `(cd backend && uv sync --locked --dev)`
and `(cd frontend && npm ci)` from the project root. `make test` prints its
temporary run directory, which contains `backend.log` and `frontend.log` for
the selected suites, including the results of the frontend build.
Use `TEST_SUITE=backend` or `TEST_SUITE=frontend` for one suite and its checks.
`TEST_WORKERS=auto` sizes the Vitest pool using available CPU and memory. A
positive integer sets the worker limit, and `TEST_WORKERS=0` runs serially.
Pytest always runs serially.

To opt into the tests for installed external converters, run
`TOOLKIT_TEST_EXTERNAL=1 uv run --no-sync pytest -q tests/test_mcp_e2e.py` from
`backend/`.

## Settings and saved data

Configure automatic magnet scraping in **Magnet Scraper → Settings**. Set the
source website and last watched video URL, then save. Changes apply immediately
and are saved to `backend/.env`; automatic runs keep the stopping point updated.

For other settings, copy
[backend/.env.example](backend/.env.example) to `backend/.env`, edit it, and restart
Toolkit. The example explains settings for subscription links, hiding tools you
do not use, and other optional customizations.

Saved subscriptions and BitComet connections are stored locally in `backend/data/`
by default. This folder can contain connection credentials, so keep it private.
For `make dev`, set a custom `SUB_DB_PATH` within this checkout in `backend/.env`.
Use the native launcher below for a data path outside the checkout.
Running tasks continue when you switch tools or reload the browser, but stopping
Toolkit ends them. Restarting does not resume unfinished tasks.

Prepare the dependencies, then run these commands from the project root to
serve the built frontend and API on your network.

```bash
(cd frontend && npm run build)
(cd backend && uv run --no-sync python -m toolkit_api.host)
```

Open the printed address. Set `HOST=127.0.0.1` on the launcher to serve only on
this Mac. Use LAN hosting only on a trusted network because the app has no login
and other devices can run its tools, including those that move or delete files.

[GitHub Actions](.github/workflows/ci.yml) runs `make test` on pull requests,
pushes to `main`, and manual runs from the repository's Actions tab.

## License

Copyright (c) 2026 Waining Ceoi. Licensed under the
[GNU General Public License v3.0 or later](LICENSE).
