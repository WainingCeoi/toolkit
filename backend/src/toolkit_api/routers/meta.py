"""App-level metadata: the tools manifest (nav + home cards) and health."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import APIRouter

from toolkit_engine import docmd

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
    CategoryOut(
        name="🛠️ Developer",
        tools=[
            ToolOut(
                slug="dep-upgrade",
                title="📦 Dependency Upgrader",
                description=(
                    "Point at a uv project: run uv sync -U, review the lagging >= "
                    "floors, then rewrite pyproject.toml and commit it with uv.lock."
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
    """Tool slugs switched off for this machine via TOOLKIT_DISABLED_TOOLS.

    Set it in backend/.env (comma- or space-separated slugs) to hide tools you
    can't or don't want to run here — e.g. Doc to Markdown on an Intel Mac,
    where MinerU's torch dependency has no macOS x86_64 wheel.
    """
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
        if tools_left:  # drop a category that ends up empty
            kept.append(CategoryOut(name=category.name, tools=tools_left))
    return kept


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(
        ffmpeg=shutil.which("ffmpeg") is not None,
        soffice=_soffice_available(),
        mineru=docmd.find_mineru() is not None,
    )
