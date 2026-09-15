"""Photos Library Filter: mirror a Photos library minus its caches, as a job.

Two endpoints, one worker. /dry-run plans and verifies against the plan
without touching the destination; /run mirrors, deletes whatever the plan
does not contain, then verifies what landed. Both report through the job
registry -- a library is hundreds of thousands of files, and even the walk
alone is too long for a request to wait on.
"""

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

# Destinations with a mirror in flight. Two runs writing and deleting inside
# the same bundle would undo each other's work, so the second is refused. The
# guard is taken inside the worker, not at submit: a job cancelled while it
# sits in the queue never runs its worker, and a guard taken earlier would
# then never be released.
_writing: set[str] = set()
_writing_lock = threading.Lock()


class PhotoFilterIn(BaseModel):
    source: str
    dest: str
    # None means the shipped defaults (photofilter.DEFAULT_RULES); an empty
    # string is a choice -- exclude nothing -- rather than an omission.
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
    # A relative (or empty) typed path would resolve against the app's CWD --
    # refuse it before a mirror-and-delete can target the wrong tree.
    if not (src_raw.is_absolute() and dest_raw.is_absolute()):
        raise HTTPException(
            status_code=400,
            detail=(
                "❌ Use absolute library paths "
                "(e.g. ~/Pictures/Photos Library.photoslibrary)."
            ),
        )
    # The engine's own guards, run here too so a refused run is a 400 with the
    # reason rather than a job that fails a moment later.
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
        # A cancelled run returns its partial result too: what was copied
        # before the stop is on disk, and the report should say so.
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
