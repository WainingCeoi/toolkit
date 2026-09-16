"""App-level metadata: the tools manifest (nav + home cards) and health."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import APIRouter

from toolkit_engine import bitcomet, docmd

from ..schemas import CategoryOut, HealthOut, ToolOut

router = APIRouter(tags=["meta"])

CATEGORIES = [
    CategoryOut(
        name="🎬 Media",
        tools=[
            ToolOut(
                slug="magnet-scraper",
                title="🧲 Magnet Scraper",
                description=(
                    "Scrape unwatched video magnet links automatically, in bulk, "
                    "or de-duplicate a pasted list."
                ),
            ),
            ToolOut(
                slug="remux",
                title="🎬 Remux Processor",
                description=(
                    "Parallel, lossless remuxing (stream-copy) of videos with FFmpeg."
                ),
            ),
            ToolOut(
                slug="torrent-downloader",
                title="🌊 Torrent Downloader",
                description=(
                    "Add a magnet or .torrent, keep only the files worth "
                    "keeping, and send it to BitComet — here or on the LAN."
                ),
            ),
        ],
    ),
    CategoryOut(
        name="🗂️ Files & Tools",
        tools=[
            ToolOut(
                slug="web-images-to-pdf",
                title="🌐 Web Images to PDF",
                description=(
                    "Open a web page, scroll to load its images, and capture them "
                    "into a single PDF."
                ),
            ),
            ToolOut(
                slug="file-gatherer",
                title="📦 File Gatherer",
                description=(
                    "Recursively gather files by type and move them into one folder."
                ),
            ),
            ToolOut(
                slug="image-to-pdf",
                title="🖼️ Image to PDF",
                description="Combine selected images into a single downloadable PDF.",
            ),
            ToolOut(
                slug="watermark-remover",
                title="🧽 Watermark Remover",
                description=(
                    "Auto-detect watermarks — tiled, or stamped once per "
                    "photo across a batch — review the masks, and inpaint "
                    "them away. For images you own or are licensed to edit."
                ),
            ),
            ToolOut(
                slug="doc-to-pdf",
                title="📄 Doc to PDF",
                description=(
                    "Clean a Word doc (accept changes, remove comments) and export "
                    "it to PDF."
                ),
            ),
            ToolOut(
                slug="doc-to-markdown",
                title="📝 Doc to Markdown",
                description=(
                    "Convert PDFs, Office docs, and images into clean Markdown "
                    "with MinerU."
                ),
            ),
            ToolOut(
                slug="cache-purge",
                title="🧹 Cache Purge",
                description=(
                    "Recursively find and delete cache / junk files from a folder."
                ),
            ),
            ToolOut(
                slug="photos-library-filter",
                title="📸 Photos Library Filter",
                description=(
                    "Mirror a Photos library without its caches while Photos is "
                    "running — live database snapshotted, originals cloned, "
                    "every asset verified."
                ),
            ),
            ToolOut(
                slug="dep-upgrade",
                title="📦 Dependency Upgrader",
                description=(
                    "Scan a project for uv (pyproject.toml) and npm (package.json) "
                    "manifests, review the outdated dependencies, then upgrade and "
                    "commit each one."
                ),
            ),
        ],
    ),
    CategoryOut(
        name="🌐 Network",
        tools=[
            ToolOut(
                slug="subscription",
                title="🛰️ Optimized-IP Subscription",
                description=(
                    "Rewrite vmess/vless/trojan nodes with optimized Cloudflare IPs "
                    "and serve Shadowrocket / Clash / Surge subscriptions over your "
                    "LAN."
                ),
            ),
        ],
    ),
]


def _soffice_available() -> bool:
    if shutil.which("soffice") or shutil.which("libreoffice"):
        return True
    return Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists()


def disabled_slugs() -> set[str]:
    """Tool slugs switched off for this machine via TOOLKIT_DISABLED_TOOLS."""
    raw = os.environ.get("TOOLKIT_DISABLED_TOOLS", "")
    return {slug.lower() for slug in raw.replace(",", " ").split()}


@router.get("/tools", response_model=list[CategoryOut])
def tools() -> list[CategoryOut]:
    """The nav + home manifest, minus any tool disabled for this machine."""
    disabled = disabled_slugs()
    if not disabled:
        return CATEGORIES
    kept = []
    for category in CATEGORIES:
        tools_left = [t for t in category.tools if t.slug not in disabled]
        if tools_left:
            kept.append(CategoryOut(name=category.name, tools=tools_left))
    return kept


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(
        ffmpeg=shutil.which("ffmpeg") is not None,
        soffice=_soffice_available(),
        mineru=docmd.find_mineru() is not None,
        # BitComet is a .app, not on PATH; its config file is the install signal.
        bitcomet=bitcomet.CONFIG_PATH.exists(),
    )
