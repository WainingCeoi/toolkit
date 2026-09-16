"""Cache Purge: scan-to-preview, then permanently delete the previewed list."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import purge

from ..deps import JobsDep, PurgeScansDep
from ..jobs import Job
from ..schemas import JobStartedOut

router = APIRouter(prefix="/purge", tags=["purge"])


class PurgeScanIn(BaseModel):
    folder: str
    patterns_raw: str


class PurgeScanOut(BaseModel):
    scan_id: str
    files: list[str]
    errors: list[str]
    total_bytes: int
    rejected_tokens: list[str]


class PurgeDeleteIn(BaseModel):
    scan_id: str


@router.post("/scan", response_model=PurgeScanOut)
def scan_folder(req: PurgeScanIn, scans: PurgeScansDep) -> PurgeScanOut:
    src = Path(req.folder).expanduser()
    # Relative paths would resolve against the app's CWD.
    if not src.is_absolute():
        raise HTTPException(
            status_code=400,
            detail="❌ Use an absolute folder path (e.g. ~/Library/Caches).",
        )
    if not src.is_dir():
        raise HTTPException(status_code=400, detail="❌ Folder not found.")
    patterns, rejected = purge.parse_patterns(req.patterns_raw)
    if not patterns:
        raise HTTPException(
            status_code=400, detail="❌ Enter at least one extension / pattern."
        )
    try:
        files, errors, total_bytes = purge.scan_folder(src, patterns)
    except OSError as e:
        # The Rust glob matcher rejects malformed brackets like '*.[abc'.
        raise HTTPException(status_code=400, detail=f"❌ Invalid pattern: {e}") from e
    return PurgeScanOut(
        scan_id=scans.put(str(src), files),
        files=files,
        errors=[str(error) for error in errors],
        total_bytes=total_bytes,
        rejected_tokens=rejected,
    )


@router.post("/delete", response_model=JobStartedOut)
def delete_files(
    req: PurgeDeleteIn, jobs: JobsDep, scans: PurgeScansDep
) -> JobStartedOut:
    # Delete only file lists this server produced; never a client-supplied path.
    scan = scans.take(req.scan_id)
    if scan is None:
        raise HTTPException(
            status_code=409,
            detail="⌛ That scan has expired or was already used — scan again.",
        )
    files = scan["files"]

    def worker(job: Job) -> dict | None:
        job.set_message("Deleting…")

        def on_progress(done: int, total: int) -> bool:
            job.set_message(f"Deleting… {done}/{total}")
            return job.cancelled

        deleted, failed = purge.delete_files(files, on_progress)
        return {
            "deleted": deleted,
            "failed": [
                {"name": Path(path).name, "error": error} for path, error in failed
            ],
        }

    job = jobs.submit("cache-purge", [], worker)
    return JobStartedOut(job_id=job.id)
