"""Magnet Scraper engine: collect magnet links from a site's video pages."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

MAX_PAGES = 100  # hard cap so Automatic mode can never loop forever

# Anchored to backend/ regardless of the launch directory.
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


# --- CORE FUNCTIONS ---
def get_magnet_link(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
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
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[str], bool, str | None]:
    """Walk the site's pages until the cutoff video; urls are trimmed only if found."""
    unwatched_video_urls: list[str] = []
    page_idx = start_page
    last_page = start_page + MAX_PAGES
    found = False
    error: str | None = None

    while not found and page_idx < last_page:
        if should_stop is not None and should_stop():
            return unwatched_video_urls, False, None
        if on_page is not None:
            on_page(page_idx)
        try:
            page_url = f"{website_url}/page/{page_idx}/"
            response = requests.get(url=page_url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
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
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fetch magnets in a thread pool; return (successful, failed) in input order."""
    results_by_idx: dict[int, dict] = {}
    stopped = False
    executor = ThreadPoolExecutor()
    try:
        future_to_idx = {
            executor.submit(get_magnet_link, url): idx for idx, url in enumerate(urls)
        }
        for count, future in enumerate(as_completed(future_to_idx), start=1):
            result = future.result()
            results_by_idx[future_to_idx[future]] = result
            if on_result is not None:
                on_result(count, result)
            if should_stop is not None and should_stop():
                stopped = True
                break
    finally:
        executor.shutdown(wait=not stopped, cancel_futures=stopped)
    results = [results_by_idx[idx] for idx in sorted(results_by_idx)]
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    return successful, failed
