"""Photos Library Filter: mirror a Photos library minus its caches, as a job."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import photofilter

from ..deps import JobsDep
from ..jobs import Job
from ..schemas import JobStartedOut

router = APIRouter(prefix="/photofilter", tags=["photofilter"])

# Destinations with a mirror in flight. Guard inside the worker, not at submit:
# a job cancelled while queued never runs its worker and would never release it.
_writing: set[str] = set()
_writing_lock = threading.Lock()


class PhotoFilterIn(BaseModel):
    source: str
    dest: str
    # None = shipped defaults; "" = exclude nothing.
    rules: str | None = None


@router.post("/dry-run", response_model=JobStartedOut)
def dry_run(req: PhotoFilterIn, jobs: JobsDep) -> JobStartedOut:
    """Plan and verify only. Nothing is written, not even the destination."""
    return _submit(req, jobs, dry_run=True)


@router.post("/run", response_model=JobStartedOut)
def run(req: PhotoFilterIn, jobs: JobsDep) -> JobStartedOut:
    """Mirror into the destination and delete what the plan does not contain."""
    return _submit(req, jobs, dry_run=False)


def _submit(req: PhotoFilterIn, jobs: JobsDep, *, dry_run: bool) -> JobStartedOut:
    src_raw = Path(req.source).expanduser()
    dest_raw = Path(req.dest).expanduser()
    # Relative paths would resolve against the app's CWD.
    if not (src_raw.is_absolute() and dest_raw.is_absolute()):
        raise HTTPException(
            status_code=400,
            detail=(
                "❌ Use absolute library paths "
                "(e.g. ~/Pictures/Photos Library.photoslibrary)."
            ),
        )
    try:
        src, dest = photofilter.check_paths(src_raw, dest_raw)
    except photofilter.PhotoFilterError as e:
        raise HTTPException(status_code=400, detail=f"❌ {e}") from e
    rules_text = photofilter.DEFAULT_RULES if req.rules is None else req.rules
    rules = photofilter.compile_rules(rules_text)
    key = str(dest)

    def worker(job: Job) -> dict:
        def on_progress(phase: str, done: int, total: int) -> bool:
            job.set_message(_message(phase, done, total))
            return job.cancelled

        if not dry_run:
            with _writing_lock:
                if key in _writing:
                    raise RuntimeError(
                        "⏳ Another run is already writing to this destination."
                    )
                _writing.add(key)
        started = time.monotonic()
        try:
            result = photofilter.run(
                src, dest, rules, dry_run=dry_run, on_progress=on_progress
            )
        finally:
            if not dry_run:
                with _writing_lock:
                    _writing.discard(key)
        return {
            "dry_run": dry_run,
            "source": str(src),
            "dest": str(dest),
            "seconds": round(time.monotonic() - started, 1),
            **photofilter.summary(result, rules),
        }

    job = jobs.submit("photos-library-filter", [], worker)
    return JobStartedOut(job_id=job.id)


def _message(phase: str, done: int, total: int) -> str:
    if phase == "copy":
        return f"Copying… {done}/{total}"
    if phase == "snapshot":
        return f"Snapshotting database {done + 1}/{total}…"
    if phase == "delete":
        return f"Deleting… {done}"
    return f"{phase.capitalize()}…"  # plan, verify
