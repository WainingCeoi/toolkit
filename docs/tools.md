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
| WhatsApp Chat Exporter | Export selected WhatsApp chats as one ZIP with JSONL, replies, reactions, and local attachments. |
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

After applying, the result shows actual version changes by project and file.
Open the notices to see any updates that failed or were skipped, and the
commit list to see the commit hashes and files.

Scanning previews uv dependency upgrades. The project’s `uv.lock` is written
only when you select **Apply** and there are uv dependency upgrades to apply.
Scanning refreshes npm and pnpm lockfiles.

For npm and pnpm, only versions written in `package.json` as `1.2.3`,
`^1.2.3`, `~1.2.3`, or `>=1.2.3` are upgraded. Other entries are left
unchanged and are not listed: pnpm `catalog:` entries (and the catalog
versions in `pnpm-workspace.yaml`), `workspace:` links, dist-tags, URLs, and
compound ranges.

Dependency Upgrader checks exact interpreter pins in `.python-version`,
`.node-version`, and `.nvmrc`. It proposes the newest available stable patch
within each pin's current major and minor version. Python patches come from
uv's available Python versions, and Node.js patches come from the Node.js
release index. Apply writes the updated version files. Use your project's
version manager to install or select the new interpreter.

## WhatsApp Chat Exporter

Open **WhatsApp Chat Exporter** from the home page or sidebar. Search or filter
the chats synced by the native WhatsApp app on the Mac running Toolkit, check
the chats you want, then click **Export ZIP** to download `WhatsApp_Chats.zip`.
Each conversation has a folder under `WhatsApp_Chats/` with one `messages.jsonl`
file. The first line contains `chat_id` and `chat_name`. Every following line
contains one message. Reactions are recorded on the message they apply to.
Locally available attachments, stickers, and thumbnails are saved under `media/`.
`attachment_name` keeps the original document filename when available, and
`attachment_path` points to the file relative to the conversation folder.
Files that are absent locally are marked `missing_local`. Folder names use the
chat names shown in the app, with underscores for characters such as `/` and a
chat ID added when names collide.
WhatsApp can stay open during the export.
