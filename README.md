# Toolkit

Toolkit brings twelve media, file, and document utilities into one app.
It runs on your Mac and opens in your browser.

**Requires macOS.** Some tools use macOS folder pickers or control apps installed
on your Mac.

## Get started

Install these once:

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs and manages Python for Toolkit.
- [Node.js](https://nodejs.org/): 22.22.2+ within 22.x, 24.15.0+ within 24.x, or 26+.

Toolkit uses Python 3.14. You do not need to install Python separately.
From the `toolkit` project folder in Terminal, run:

```bash
make start
```

The first start installs the required packages. Later starts install only
missing or changed dependencies, then build the app and start Toolkit.
Doc to Markdown's optional converter is included on Apple silicon Macs.

Open the address printed in Terminal, normally [http://127.0.0.1:8000](http://127.0.0.1:8000).
If port 8000 is busy, Toolkit uses the next available port. Keep Terminal open
while using the app, and press **Ctrl-C** there to stop it.

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
this same FastAPI backend first, then point MCP at the address printed in Terminal
(the port can change). The adapter shares backend jobs, browser sessions, devices,
and scan records with the web app.

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
To omit this converter on any Mac, run `make start EXTRAS=` and use the same
option on later starts.

## Commands

One `Makefile` in the project folder provides the main commands:

| Command | When to use it |
| --- | --- |
| `make install` | Set up missing or changed dependencies without starting the app. |
| `make start` | Start Toolkit for everyday use. |
| `make dev` | Work on the code with automatic reloads. |
| `make test` | Run tests, code checks, and a build. |
| `make clean` | Remove build outputs and caches; keep installed dependencies and saved data. |

Run `make install` before testing a fresh checkout. Run `make` to see help,
or `make help-maintenance` for additional commands.
To choose a different starting port, use `make start PORT=9000`.

## Settings and saved data

Configure automatic magnet scraping in **Magnet Scraper → Settings**. Set the
source website and last watched video URL, then save. Changes apply immediately
and are saved to `backend/.env`; automatic runs keep the stopping point updated.

For other settings, copy
[backend/.env.example](backend/.env.example) to `backend/.env`, edit it, and restart
Toolkit. The example explains settings for subscription links, hiding tools you
do not use, and other optional customizations.

Saved subscriptions and BitComet connections are stored locally in `backend/data/`
by default. This folder can contain connection credentials; keep it private.
Running tasks continue when you switch tools or reload the browser, but stopping
Toolkit ends them. Restarting does not resume unfinished tasks.

To use Toolkit from another device on your network, run `make host` and open the
printed address. Use this only on a trusted network: the app has no login, and
other devices can run its tools, including those that move or delete files.

[GitHub Actions](.github/workflows/ci.yml) runs `make test` on pull requests,
pushes to `main`, and manual runs from the repository's Actions tab.

## License

Copyright (c) 2026 Waining Ceoi. Licensed under the
[GNU General Public License v3.0 or later](LICENSE).
