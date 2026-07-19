"""File Gatherer: recursively gather files by type into one target folder."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import gather

from ..deps import JobsDep
from ..jobs import Job
from ..schemas import JobStartedOut

router = APIRouter(prefix="/gather", tags=["gather"])


class GatherStartIn(BaseModel):
    source: str
    target: str
    categories: list[str] = []
    custom: str = ""


@router.post("/start", response_model=JobStartedOut)
def start_gather(req: GatherStartIn, jobs: JobsDep) -> JobStartedOut:
    src_raw = Path(req.source).expanduser()
    tgt_raw = Path(req.target).expanduser()
    src, tgt = src_raw.resolve(), tgt_raw.resolve()
    patterns = gather.build_patterns(req.categories, req.custom)

    # A relative (or empty) typed path would resolve against the app's CWD —
    # refuse it before it can target the wrong tree.
    if not (src_raw.is_absolute() and tgt_raw.is_absolute()):
        raise HTTPException(
            status_code=400,
            detail="❌ Use absolute folder paths (e.g. ~/Movies or /Volumes/T7).",
        )
    if not src.is_dir():
        raise HTTPException(status_code=400, detail="❌ Source folder not found.")
    if not patterns:
        raise HTTPException(status_code=400, detail="❌ Select at least one file type.")
    if tgt == src or src in tgt.parents:
        raise HTTPException(
            status_code=400,
            detail="❌ Target must be a different folder, outside the source.",
        )

    def worker(job: Job) -> dict | None:
        job.set_message("Scanning source folder…")
        files, errors = gather.scan_source(src, patterns)
        scan_errors = [str(error) for error in errors]

        if not files:
            # No matches: the page's "No matching files found." state never
            # created the target folder — report the empty result, untouched.
            return {
                "moved": [],
                "failed": [],
                "scan_errors": scan_errors,
                "target": str(tgt),
                "warning": None,
            }

        moved, failed = [], []
        if not job.cancelled:
            # Only create the target once there is something to move — the
            # page mkdir'd here too, never on a no-match (or early-cancel) run.
            try:
                tgt.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                # e.g. the typed target (or one of its parents) is a file
                raise RuntimeError(f"❌ Cannot create the target folder: {e}") from e

            job.set_message(f"Moving… 0/{len(files)}")

            def on_progress(done: int, total: int) -> bool:
                job.set_message(f"Moving… {done}/{total}")
                return job.cancelled

            moved, failed = gather.move_files(files, tgt, on_progress)

        # A scan error means part of the tree was unreadable, so the gather
        # may be incomplete — surface the page's warning in the result. On
        # cancel we still return the partial moved/failed already collected.
        warning = None
        if scan_errors:
            warning = (
                f"Moved to {tgt}, but {len(scan_errors)} location(s) couldn't be "
                "scanned — matching files may remain in the source."
            )
        return {
            "moved": moved,
            "failed": [{"name": name, "error": error} for name, error in failed],
            "scan_errors": scan_errors,
            "target": str(tgt),
            "warning": warning,
        }

    job = jobs.submit("file-gatherer", [], worker)
    return JobStartedOut(job_id=job.id)
