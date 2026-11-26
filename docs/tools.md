# Toolkit tools

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
| Dependency Upgrader | Review and apply uv, npm, and pnpm package updates and Python and Node.js interpreter patches, with an optional commit. |
| WhatsApp Chat Exporter | Export one WhatsApp chat as a ZIP with JSONL, replies, reactions, and local attachments. |
| Optimized-IP Subscription | Replace server addresses in proxy configurations and generate subscription links. |

File Gatherer moves files, and Cache Purge permanently deletes them. Review the
selected folders and results carefully. Photos Library Filter offers a dry run
to preview changes before creating or updating the destination library.

## Additional apps

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
To omit this converter on any Mac, use the [`EXTRAS=` option](development.md#commands).

## Magnet Scraper

Configure automatic magnet scraping in **Magnet Scraper → Settings**. Set the
source website and last watched video URL, then save. Changes apply immediately
and are saved to `backend/.env`; automatic runs keep the stopping point updated.

## Dependency Upgrader

Scan results are grouped by project, even when you scan just one project.
Groups with proposed updates or scan errors open by default. Expand an
up-to-date project to see the checked files grouped by directory. **Apply**
uses the proposed updates from the whole scan.

When the selected folder contains multiple projects, scanning and applying
process up to four independent projects at a time. A `backend/` and `frontend/`
pair counts as one project, including inside a shared Git repository. Each
project keeps its usual order, including updating dependencies before changing
interpreter pins. Projects that share a workspace or lockfile run in sequence,
as do projects linked through uv path sources when applying. Optional Git
commits run in order after the updates finish.

After applying, the result shows actual version changes by project and file.
Open the notices to see any updates that failed or were skipped, and the
commit list to see the commit hashes and files.

Scanning previews uv dependency upgrades. The project’s `uv.lock` is written
only when you select **Apply** and there are uv dependency upgrades to apply.
A uv project that refers to an upgraded project through a path source has its
existing `uv.lock` refreshed as well, even with no upgrades of its own.
Scanning refreshes npm and pnpm lockfiles. Without a completed scan of the
folder (possible only through the API or MCP), Apply does not upgrade uv
dependencies; it only raises their minimum versions to the versions already
in `uv.lock`.

For npm and pnpm, only entries in `dependencies`, `devDependencies`, and
`optionalDependencies` written as `1.2.3`, `^1.2.3`, `~1.2.3`, or `>=1.2.3`
are upgraded. Anything else is left unchanged and is not listed, including
`peerDependencies`, partial versions such as `^1.2` or `1.x`, pnpm `catalog:`
entries (and the catalog versions in `pnpm-workspace.yaml`), `workspace:`
links, aliases, dist-tags, URLs, and compound ranges.

Dependency Upgrader checks exact interpreter pins in `.python-version`,
`.node-version`, and `.nvmrc`. It proposes the newest available stable patch
within each pin's current major and minor version. Python patches come from
uv's available Python versions, and Node.js patches come from the Node.js
release index. A newer minor release, such as Node.js 22.12 for a 22.11 pin,
is not proposed. Apply writes the updated version files. Use your project's
version manager to install or select the new interpreter.

## WhatsApp Chat Exporter

Open **WhatsApp Chat Exporter** from the home page or sidebar. Search or filter
the chats synced by the native WhatsApp app on the Mac running Toolkit. Select
one direct or group chat, then click **Export ZIP** to download
`WhatsApp_<chat name>.zip`. The ZIP contains a single folder with the same name,
with `messages.jsonl` and `media/` directly inside it. The first line of
`messages.jsonl` contains `chat_id` and `chat_name`. Every following line contains
one message with `sender_id` and `sender_name`. Your own messages use `Me` as
`sender_name`. For incoming messages, the exporter uses the local name of the
direct chat or group member when available, then tries the sender's WhatsApp
profile name by matching `sender_id`. If no name is found, including for group
system messages without a sender, `sender_name` is `null`.
Reactions are recorded on the message they apply to.
Locally available attachments, stickers, and thumbnails are saved under `media/`.
`attachment_name` keeps the original document filename when available, and
`attachment_path` points to the file relative to the conversation folder.
Files that are absent locally are marked `missing_local`. The ZIP and folder use
the chat name shown in the app, with underscores for characters such as `/`.
WhatsApp can stay open during the export.
