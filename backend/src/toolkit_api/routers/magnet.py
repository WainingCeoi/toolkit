"""🧲 Magnet Scraper — auto/manual magnet scraping and de-duplication."""

from __future__ import annotations

import os
from collections.abc import Callable

from dotenv import dotenv_values, set_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from toolkit_engine import magnet

from ..deps import StateDep
from ..jobs import Job
from ..schemas import JobStartedOut

router = APIRouter(prefix="/magnet", tags=["magnet"])

TOOL_SLUG = "magnet-scraper"

ERR_NO_WEBSITE = "❌ WEBSITE_URL is not set in .env."
ERR_NO_CUTOFF = "❌ CUTOFF_VIDEO is not set in .env (no stopping point)."
WARN_CUTOFF_NOT_FOUND = (
    "Cutoff video not found — check CUTOFF_VIDEO or raise the page "
    "limit. Nothing was scraped and the cutoff was left unchanged."
)
WARN_NO_URLS = "Please enter at least one URL"
WARN_NO_MAGNETS = "Please enter at least one magnet link"


class MagnetConfigOut(BaseModel):
    website_url_set: bool
    cutoff_set: bool


class AutoScrapeIn(BaseModel):
    start_page: int = Field(default=1, ge=1)


class ManualScrapeIn(BaseModel):
    urls: list[str]


class DedupeIn(BaseModel):
    links: list[str]


class DedupeOut(BaseModel):
    unique: list[str]
    count: int


def _scrape(
    job: Job,
    urls: list[str],
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Fetch magnets for urls in parallel, then drop duplicate hrefs."""
    if not urls:
        # Same keys as the populated result: the client expects every count.
        return {
            "urls": [],
            "successful": [],
            "failed": [],
            "total": 0,
            "successful_count": 0,
            "failed_count": 0,
            "duplicate_count": 0,
        }
    total = len(urls)
    job.set_message(f"Fetching magnets… 0/{total}")

    def on_result(idx: int, result: dict) -> None:
        job.set_message(f"Fetching magnets… {idx}/{total}")

    successful, failed = magnet.scrape_magnets(
        urls, on_result=on_result, should_stop=should_stop
    )
    job.set_message(f"Fetched {total}/{total} link(s).")
    # Different URLs can serve the same magnet; keep one per href.
    unique_by_href = {r["result"]: r for r in successful}
    duplicate_count = len(successful) - len(unique_by_href)
    successful = list(unique_by_href.values())
    return {
        "urls": urls,
        "successful": successful,
        "failed": failed,
        "total": total,
        "successful_count": len(successful),
        "failed_count": len(failed),
        "duplicate_count": duplicate_count,
    }


@router.get("/config", response_model=MagnetConfigOut)
def get_config() -> MagnetConfigOut:
    # Re-read .env so a set_key cutoff advance in this process is seen.
    cfg = dotenv_values(magnet.ENV_PATH)
    website = cfg.get("WEBSITE_URL") or os.getenv("WEBSITE_URL")
    cutoff = cfg.get("CUTOFF_VIDEO") or os.getenv("CUTOFF_VIDEO")
    return MagnetConfigOut(
        website_url_set=bool(website),
        cutoff_set=bool(cutoff),
    )


@router.post("/auto", response_model=JobStartedOut)
def start_auto(req: AutoScrapeIn, state: StateDep) -> JobStartedOut:
    start_page = req.start_page

    def worker(job: Job) -> dict | None:
        # Re-read .env so a set_key cutoff advance in this process is seen.
        cfg = dotenv_values(magnet.ENV_PATH)
        cutoff_video_url = cfg.get("CUTOFF_VIDEO") or os.getenv("CUTOFF_VIDEO")
        source_website = cfg.get("WEBSITE_URL") or os.getenv("WEBSITE_URL")

        if not source_website:
            raise RuntimeError(ERR_NO_WEBSITE)
        if not cutoff_video_url:
            raise RuntimeError(ERR_NO_CUTOFF)

        def on_page(page_idx: int) -> None:
            job.set_message(f"Finding unwatched videos from page {page_idx}...")

        def should_stop() -> bool:
            return job.cancelled

        urls, found, error = magnet.find_unwatched_urls(
            source_website,
            cutoff_video_url,
            start_page,
            on_page=on_page,
            should_stop=should_stop,
        )

        if job.cancelled:
            return None

        # Never touch the cutoff unless it was found; a bad anchor scrapes everything.
        if not found:
            return {
                "cutoff_found": False,
                "warning": WARN_CUTOFF_NOT_FOUND,
                "error": error,
            }
        result = _scrape(job, urls, should_stop=should_stop)
        result["cutoff_found"] = True
        # A cancelled scrape must not advance the cutoff past unfetched videos.
        if job.cancelled:
            return result
        if urls:
            set_key(magnet.ENV_PATH, "CUTOFF_VIDEO", urls[0])
        return result

    job = state.jobs.submit(TOOL_SLUG, [], worker)
    return JobStartedOut(job_id=job.id)


@router.post("/manual", response_model=JobStartedOut)
def start_manual(req: ManualScrapeIn, state: StateDep) -> JobStartedOut:
    if not req.urls:
        raise HTTPException(status_code=400, detail=WARN_NO_URLS)
    urls = list(req.urls)

    def worker(job: Job) -> dict:
        return _scrape(job, urls, should_stop=lambda: job.cancelled)

    job = state.jobs.submit(TOOL_SLUG, [], worker)
    return JobStartedOut(job_id=job.id)


@router.post("/dedupe", response_model=DedupeOut)
def dedupe(req: DedupeIn) -> DedupeOut:
    if not req.links:
        raise HTTPException(status_code=400, detail=WARN_NO_MAGNETS)
    unique = list(dict.fromkeys(req.links))
    return DedupeOut(unique=unique, count=len(unique))
