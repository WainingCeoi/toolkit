"""🧲 Magnet Scraper — auto/manual magnet scraping and de-duplication."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from toolkit_engine import magnet

from ..deps import StateDep
from ..jobs import Job
from ..schemas import JobStartedOut

router = APIRouter(prefix="/magnet", tags=["magnet"])

TOOL_SLUG = "magnet-scraper"

ERR_NO_WEBSITE = "Add a source website in Settings before using Automatic mode."
ERR_NO_CUTOFF = "Add a last watched video URL in Settings before using Automatic mode."
WARN_CUTOFF_NOT_FOUND = (
    "Last watched video not found. Check its URL in Settings and the start page. "
    "Nothing was scraped and the stopping point was left unchanged."
)
WARN_NO_URLS = "Please enter at least one URL"
WARN_NO_MAGNETS = "Please enter at least one magnet link"

_config_lock = threading.Lock()
_config_revision = 0


class MagnetConfigIn(BaseModel):
    website_url: str
    cutoff_video: str


class MagnetConfigOut(BaseModel):
    website_url: str
    cutoff_video: str
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


def _read_config() -> MagnetConfigOut:
    """Read fresh values, with explicit empty file values overriding shell defaults."""
    try:
        cfg = dotenv_values(magnet.ENV_PATH)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="Could not read magnet settings from backend/.env."
        ) from exc
    website = (cfg.get("WEBSITE_URL", os.getenv("WEBSITE_URL")) or "").strip()
    cutoff = (cfg.get("CUTOFF_VIDEO", os.getenv("CUTOFF_VIDEO")) or "").strip()
    return MagnetConfigOut(
        website_url=website,
        cutoff_video=cutoff,
        website_url_set=bool(website),
        cutoff_set=bool(cutoff),
    )


def _validate_url(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        if not value.lower().startswith(("http://", "https://")) or any(
            char.isspace() or ord(char) < 32 for char in value
        ):
            raise ValueError
        parsed = urlsplit(value)
        if not parsed.hostname or "\\" in parsed.netloc:
            raise ValueError
        HttpUrl(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{label} must be a valid http:// or https:// URL.",
        ) from exc
    return value


def _write_config(values: dict[str, str]) -> None:
    """Update only these dotenv keys and replace the file once both writes succeed."""
    env_path = Path(magnet.ENV_PATH)
    temp_path: Path | None = None
    try:
        try:
            contents = env_path.read_bytes()
            file_mode = stat.S_IMODE(env_path.stat().st_mode)
        except FileNotFoundError:
            contents = b""
            file_mode = None
        with tempfile.NamedTemporaryFile(
            dir=env_path.parent, prefix=f"{env_path.name}.", delete=False
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(contents)
        for key, value in values.items():
            set_key(temp_path, key, value)
        if file_mode is not None:
            temp_path.chmod(file_mode)
        temp_path.replace(env_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Could not save magnet settings. Check that backend/.env is writable."
            ),
        ) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
    with _config_lock:
        return _read_config()


@router.put("/config", response_model=MagnetConfigOut)
def update_config(req: MagnetConfigIn) -> MagnetConfigOut:
    global _config_revision
    website = _validate_url(req.website_url, "Source website").rstrip("/")
    cutoff = _validate_url(req.cutoff_video, "Last watched video URL")
    with _config_lock:
        _write_config({"WEBSITE_URL": website, "CUTOFF_VIDEO": cutoff})
        _config_revision += 1
        return _read_config()


@router.post("/auto", response_model=JobStartedOut)
def start_auto(req: AutoScrapeIn, state: StateDep) -> JobStartedOut:
    start_page = req.start_page

    def worker(job: Job) -> dict | None:
        with _config_lock:
            config = _read_config()
            config_revision = _config_revision
        cutoff_video_url = config.cutoff_video
        source_website = config.website_url

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
            # Hold the cutoff behind the oldest failed fetch; urls[0] is the newest.
            failed_urls = {r["url"] for r in result["failed"]}
            oldest_failed = max(
                (i for i, url in enumerate(urls) if url in failed_urls), default=-1
            )
            if oldest_failed + 1 < len(urls):
                with _config_lock:
                    # Keep settings saved during this run, including website changes.
                    if _config_revision == config_revision and _read_config() == config:
                        _write_config({"CUTOFF_VIDEO": urls[oldest_failed + 1]})
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
