"""Cache Purge: scan-to-preview, then permanently delete the previewed list."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import purge

from ..deps import JobsDep
from ..jobs import Job
from ..schemas import JobStartedOut

router = APIRouter(prefix="/purge", tags=["purge"])


class PurgeScanIn(BaseModel):
    folder: str
    patterns_raw: str


class PurgeScanOut(BaseModel):
    files: list[str]
    errors: list[str]
    total_bytes: int
    rejected_tokens: list[str]


class PurgeDeleteIn(BaseModel):
    folder: str
    files: list[str]


@router.post("/scan", response_model=PurgeScanOut)
def scan_folder(req: PurgeScanIn) -> PurgeScanOut:
    src = Path(req.folder).expanduser()
    # A relative (or empty) typed path would resolve against the app's CWD —
    # refuse it before the delete flow can target the wrong tree.
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
    files, errors, total_bytes = purge.scan_folder(src, patterns)
    return PurgeScanOut(
        files=files,
        errors=[str(error) for error in errors],
        total_bytes=total_bytes,
        rejected_tokens=rejected,
    )


@router.post("/delete", response_model=JobStartedOut)
def delete_files(req: PurgeDeleteIn, jobs: JobsDep) -> JobStartedOut:
    # The client sends back the previewed list from /scan plus the folder it was
    # scanned from. Confine the (irreversible) delete to that tree server-side:
    # every path must be absolute and resolve to a file under the scanned folder,
    # so a tampered or arbitrary path list can't reach files outside it.
    base = Path(req.folder).expanduser()
    if not base.is_absolute() or not base.is_dir():
        raise HTTPException(
            status_code=400, detail="❌ Invalid or missing scan folder."
        )
    base_resolved = base.resolve()
    files: list[str] = []
    for raw in req.files:
        candidate = Path(raw).expanduser()
        resolved = candidate.resolve()
        if not candidate.is_absolute() or not (
            resolved == base_resolved or base_resolved in resolved.parents
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "❌ Refusing to delete a path outside the "
                    f"scanned folder: {raw}"
                ),
            )
        files.append(str(candidate))

    def worker(job: Job) -> dict | None:
        job.set_message("Deleting…")

        def on_progress(done: int, total: int) -> bool:
            job.set_message(f"Deleting… {done}/{total}")
            return job.cancelled

        # On cancel, delete_files returns the partial deleted/failed already
        # collected; keep them so a cancelled run still reports what it deleted.
        deleted, failed = purge.delete_files(files, on_progress)
        return {
            "deleted": deleted,
            "failed": [
                {"name": Path(path).name, "error": error} for path, error in failed
            ],
        }

    job = jobs.submit("cache-purge", [], worker)
    return JobStartedOut(job_id=job.id)
