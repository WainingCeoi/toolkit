"""🧲 Magnet Scraper — auto/manual magnet scraping and de-duplication.

Auto mode walks the configured site's pagination until CUTOFF_VIDEO is found,
advances the cutoff in backend/.env (exactly like the page: only after the
cutoff is located, before scraping), then fans out the magnet fetches. Manual
mode scrapes a pasted URL list. Both run as jobs; item names aren't known at
submit time, so progress streams through the job message instead of items.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv, set_key
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


def _scrape(job: Job, urls: list[str]) -> dict:
    """The page's execution block: parallel fetch with per-URL progress."""
    if not urls:
        # Page equivalent: {"urls": [], "successful": [], "failed": []} ->
        # rendered as "No new unwatched video found."
        return {"urls": [], "successful": [], "failed": [], "total": 0}
    total = len(urls)
    job.set_message(f"Fetching magnets… 0/{total}")

    def on_result(idx: int, result: dict) -> None:
        job.set_message(f"Fetching magnets… {idx}/{total}")

    successful, failed = magnet.scrape_magnets(urls, on_result=on_result)
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
    load_dotenv(magnet.ENV_PATH)
    return MagnetConfigOut(
        website_url_set=bool(os.getenv("WEBSITE_URL")),
        cutoff_set=bool(os.getenv("CUTOFF_VIDEO")),
    )


@router.post("/auto", response_model=JobStartedOut)
def start_auto(req: AutoScrapeIn, state: StateDep) -> JobStartedOut:
    start_page = req.start_page

    def worker(job: Job) -> dict | None:
        load_dotenv(magnet.ENV_PATH)
        cutoff_video_url = os.getenv("CUTOFF_VIDEO")
        source_website = os.getenv("WEBSITE_URL")

        # Guard against missing config (otherwise URLs become "None/page/1/"
        # and the pagination loop has no valid stopping point).
        if not source_website:
            raise RuntimeError(ERR_NO_WEBSITE)
        if not cutoff_video_url:
            raise RuntimeError(ERR_NO_CUTOFF)

        def on_page(page_idx: int) -> None:
            job.set_message(f"Finding unwatched videos from page {page_idx}...")

        urls, found, error = magnet.find_unwatched_urls(
            source_website, cutoff_video_url, start_page, on_page=on_page
        )

        # Only save / advance the cutoff / scrape once the cutoff is located —
        # otherwise a stale CUTOFF_VIDEO or a network error would overwrite
        # the anchor and submit a huge/partial batch.
        if not found:
            return {
                "cutoff_found": False,
                "warning": WARN_CUTOFF_NOT_FOUND,
                "error": error,
            }
        if job.cancelled:
            return None
        if urls:
            # Advance the cutoff to the newest so the next run stops here.
            set_key(magnet.ENV_PATH, "CUTOFF_VIDEO", urls[0])
        result = _scrape(job, urls)
        result["cutoff_found"] = True
        return result

    job = state.jobs.submit(TOOL_SLUG, [], worker)
    return JobStartedOut(job_id=job.id)


@router.post("/manual", response_model=JobStartedOut)
def start_manual(req: ManualScrapeIn, state: StateDep) -> JobStartedOut:
    if not req.urls:
        raise HTTPException(status_code=400, detail=WARN_NO_URLS)
    urls = list(req.urls)

    def worker(job: Job) -> dict:
        return _scrape(job, urls)

    job = state.jobs.submit(TOOL_SLUG, [], worker)
    return JobStartedOut(job_id=job.id)


@router.post("/dedupe", response_model=DedupeOut)
def dedupe(req: DedupeIn) -> DedupeOut:
    if not req.links:
        raise HTTPException(status_code=400, detail=WARN_NO_MAGNETS)
    # The page used set() (order lost); dict.fromkeys keeps first-seen order.
    unique = list(dict.fromkeys(req.links))
    return DedupeOut(unique=unique, count=len(unique))
