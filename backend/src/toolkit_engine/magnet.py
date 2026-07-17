"""Magnet Scraper engine — lifted from the Streamlit page magnet_scraper.py.

Pure scraping logic only: fetch a magnet link from a video page, walk the
source site's pagination until the cutoff video is found, and fan the magnet
fetches out over a thread pool. Callers (the API router's job workers) handle
env loading, cutoff persistence via dotenv.set_key, and progress reporting
through the optional callbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

MAX_PAGES = 100  # hard cap so Automatic mode can never loop forever

# Anchor .env to backend/ regardless of the launch directory (the page
# anchored it to the repo root; the backend owns it now).
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


# =======================================================
# CORE FUNCTIONS — fetch a magnet link from a video page URL
# =======================================================
def get_magnet_link(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        tag = soup.find("a", string="Magnet")
        if tag is None or not tag.get("href"):
            return {"success": False, "url": url, "reason": "no magnet link on page"}
        return {"success": True, "result": tag.get("href")}
    except Exception as e:
        return {"success": False, "url": url, "reason": str(e)}


def find_unwatched_urls(
    website_url: str,
    cutoff_video: str,
    start_page: int,
    on_page: Callable[[int], None] | None = None,
) -> tuple[list[str], bool, str | None]:
    """The page's Automatic-mode pagination loop.

    Walks {website_url}/page/{n}/ collecting <a rel="bookmark"> hrefs until the
    cutoff video is found, an empty page is hit, a request fails, or MAX_PAGES
    pages have been visited. `on_page(page_number)` fires before each fetch so
    a job can stream progress.

    Returns (urls_newer_than_cutoff, cutoff_found, error_message_or_None).
    Only when the cutoff is found is the collected list sliced down to the
    videos newer than the cutoff (the newest first — index 0 becomes the next
    cutoff); otherwise the raw accumulation is returned and the caller must
    not scrape or advance the cutoff.
    """
    unwatched_video_urls: list[str] = []
    page_idx = start_page
    last_page = start_page + MAX_PAGES
    found = False
    error: str | None = None

    while not found and page_idx < last_page:
        if on_page is not None:
            on_page(page_idx)
        try:
            page_url = f"{website_url}/page/{page_idx}/"
            response = requests.get(url=page_url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            on_page_links = soup.find_all("a", rel="bookmark")

            urls = [link.get("href") for link in on_page_links if link.get("href")]
            if not urls:
                break  # ran past the last page of results

            unwatched_video_urls += urls
            if cutoff_video in urls:
                found = True
            else:
                page_idx += 1

        except Exception as e:
            error = f"❌ Error on page {page_idx}: {e}"
            break

    if found:
        # Keep only the videos newer than the cutoff.
        cutoff_idx = unwatched_video_urls.index(cutoff_video)
        unwatched_video_urls = unwatched_video_urls[:cutoff_idx]

    return unwatched_video_urls, found, error


def scrape_magnets(
    urls: list[str],
    on_result: Callable[[int, dict], None] | None = None,
) -> tuple[list[dict], list[dict]]:
    """The page's execution block: fetch magnets simultaneously.

    `on_result(index, result)` fires per completed URL (index is 1-based, in
    input order, matching the page's progress bar). Returns
    (successful_magnets, failed_urls_with_reasons) preserving the page's
    result dict shapes: {"success": True, "result": href} and
    {"success": False, "url": url, "reason": str}.
    """
    results: list[dict] = []
    with ThreadPoolExecutor() as executor:
        for idx, result in enumerate(executor.map(get_magnet_link, urls), start=1):
            results.append(result)
            if on_result is not None:
                on_result(idx, result)
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    return successful, failed
