"""App-level metadata: the tools manifest (nav + home cards) and health."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter

from ..schemas import CategoryOut, HealthOut, ToolOut

router = APIRouter(tags=["meta"])

# The single source of truth for navigation and the home grid — the React app
# renders this manifest, collapsing the app.py/home.py duplication of the old
# Streamlit shell. Titles/descriptions carry the old UI's brand verbatim.
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
        ],
    ),
    CategoryOut(
        name="🗂️ Documents & Files",
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


@router.get("/tools", response_model=list[CategoryOut])
def tools() -> list[CategoryOut]:
    return CATEGORIES


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(
        ffmpeg=shutil.which("ffmpeg") is not None,
        soffice=_soffice_available(),
        mineru=shutil.which("mineru") is not None,
    )
