"""Dependency Upgrader: bump a uv project's lagging >= floors to what uv resolves.

Two phases so the review-then-apply contract holds:
- POST /deps/scan runs as a job — ``uv sync -U`` (long, cancellable) then reads
  the resolved versions and returns the proposed floor bumps for review.
- POST /deps/apply is synchronous — it recomputes the bumps from the now-synced
  uv.lock (server-authoritative; the browser never sends a bump list to replay),
  rewrites pyproject.toml, and optionally commits pyproject.toml + uv.lock.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import depsync

from ..deps import JobsDep
from ..schemas import JobStartedOut

router = APIRouter(prefix="/deps", tags=["deps"])


class ScanIn(BaseModel):
    folder: str


class ApplyIn(BaseModel):
    folder: str
    commit: bool = True


class BumpOut(BaseModel):
    name: str
    table: str
    old: str
    new: str
    major: bool


class ApplyOut(BaseModel):
    written: int
    bumps: list[BumpOut]
    committed: bool
    commit_sha: str | None = None
    commit_message: str | None = None
    note: str | None = None


@router.post("/scan", response_model=JobStartedOut)
def scan(req: ScanIn, jobs: JobsDep) -> JobStartedOut:
    pyproject, err = depsync.find_pyproject(req.folder)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not depsync.uv_available():
        raise HTTPException(
            status_code=400, detail="❌ uv is not installed or not on PATH."
        )
    folder = str(Path(req.folder).expanduser())

    def worker(job):
        job.set_message("Running uv sync -U …")
        ok, output = depsync.run_uv_sync(
            folder,
            on_message=job.set_message,
            is_cancelled=lambda: job.cancelled,
        )
        if job.cancelled:
            return None
        if not ok:
            tail = "\n".join(output.splitlines()[-15:])
            raise RuntimeError(f"uv sync -U failed:\n{tail}")
        job.set_message("Reading resolved versions …")
        resolved, lock_err = depsync.resolved_versions(folder)
        if lock_err:
            raise RuntimeError(lock_err)
        bumps = depsync.compute_bumps(pyproject, resolved)
        return {
            "folder": folder,
            "pyproject": str(pyproject),
            "bumps": [depsync.bump_dict(b) for b in bumps],
            "count": len(bumps),
        }

    job = jobs.submit("dep-upgrade", [], worker)
    return JobStartedOut(job_id=job.id)


@router.post("/apply", response_model=ApplyOut)
def apply(req: ApplyIn) -> ApplyOut:
    pyproject, err = depsync.find_pyproject(req.folder)
    if err:
        raise HTTPException(status_code=400, detail=err)
    folder = str(Path(req.folder).expanduser())

    # Check git before writing anything, so a "not a repo" never leaves the file
    # edited but uncommitted.
    if req.commit and not depsync.is_git_repo(folder):
        raise HTTPException(
            status_code=400,
            detail=(
                "❌ Not a git repository — uncheck “commit after applying” to write "
                "without committing, or run `git init` there first."
            ),
        )

    resolved, lock_err = depsync.resolved_versions(folder)
    if lock_err:
        raise HTTPException(status_code=400, detail=lock_err)
    bumps = depsync.compute_bumps(pyproject, resolved)
    if not bumps:
        return ApplyOut(
            written=0,
            bumps=[],
            committed=False,
            note=(
                "Nothing to bump — every declared >= floor already matches "
                "the resolved version."
            ),
        )

    depsync.apply_bumps(pyproject, bumps)

    committed = False
    sha: str | None = None
    message: str | None = None
    if req.commit:
        sha, git_err = depsync.commit_bumps(folder, bumps)
        if git_err:
            raise HTTPException(
                status_code=500,
                detail=f"{git_err} (pyproject.toml was updated — commit it manually.)",
            )
        committed = True
        message = depsync.build_commit_message(bumps)

    return ApplyOut(
        written=len(bumps),
        bumps=[BumpOut(**depsync.bump_dict(b)) for b in bumps],
        committed=committed,
        commit_sha=sha,
        commit_message=message,
    )
