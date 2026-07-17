"""🧲 Magnet Scraper — auto/manual magnet scraping and de-duplication.

Auto mode walks the configured site's pagination until CUTOFF_VIDEO is found,
advances the cutoff in backend/.env (exactly like the page: only after the
cutoff is located, before scraping), then fans out the magnet fetches. Manual
mode scrapes a pasted URL list. Both run as jobs; item names aren't known at
submit time, so progress streams through the job message instead of items.
"""

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

# The page's exact user-facing strings.
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
    """The page's execution block: parallel fetch with per-URL progress."""
    if not urls:
        # Page equivalent: {"urls": [], "successful": [], "failed": []} ->
        # rendered as "No new unwatched video found."
        return {"urls": [], "successful": [], "failed": [], "total": 0}
    total = len(urls)
    job.set_message(f"Fetching magnets… 0/{total}")

    def on_result(idx: int, result: dict) -> None:
        job.set_message(f"Fetching magnets… {idx}/{total}")

    successful, failed = magnet.scrape_magnets(
        urls, on_result=on_result, should_stop=should_stop
    )
    job.set_message(f"Fetched {total}/{total} link(s).")
    return {
        "urls": urls,
        "successful": successful,
        "failed": failed,
        "total": total,
        "successful_count": len(successful),
        "failed_count": len(failed),
    }


@router.get("/config", response_model=MagnetConfigOut)
def get_config() -> MagnetConfigOut:
    # Read the file directly so set_key's cutoff advance is seen within a
    # long-lived process (load_dotenv defaults override=False and never
    # refreshes os.environ); shell-exported values still fill any gaps.
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
        # Read the .env file directly so a cutoff advanced by set_key earlier
        # in this same process is seen; os.getenv only fills unset gaps (a
        # cached load_dotenv would keep re-scraping the whole batch forever).
        cfg = dotenv_values(magnet.ENV_PATH)
        cutoff_video_url = cfg.get("CUTOFF_VIDEO") or os.getenv("CUTOFF_VIDEO")
        source_website = cfg.get("WEBSITE_URL") or os.getenv("WEBSITE_URL")

        # Guard against missing config (otherwise URLs become "None/page/1/"
        # and the pagination loop has no valid stopping point).
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

        # A cancel during the walk leaves the cutoff untouched and marks the
        # job cancelled (None result); the registry reads job.cancelled.
        if job.cancelled:
            return None

        # Only save / advance the cutoff / scrape once the cutoff is located —
        # otherwise a stale CUTOFF_VIDEO or a network error would overwrite
        # the anchor and submit a huge/partial batch.
        if not found:
            return {
                "cutoff_found": False,
                "warning": WARN_CUTOFF_NOT_FOUND,
                "error": error,
            }
        result = _scrape(job, urls, should_stop=should_stop)
        result["cutoff_found"] = True
        # Advance the cutoff ONLY on a clean, uncancelled scrape. Cancelling
        # mid-scrape must not move the anchor past videos we never fetched —
        # otherwise they fall below the cutoff and are skipped forever. On
        # cancel we return the partial result (the registry marks the job
        # cancelled) and leave the next run to re-scrape the whole batch.
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
    # The page used set() (order lost); dict.fromkeys keeps first-seen order.
    unique = list(dict.fromkeys(req.links))
    return DedupeOut(unique=unique, count=len(unique))
