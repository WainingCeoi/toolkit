"""Dependency Upgrader: upgrade every uv/npm manifest under a folder, then commit.

Two phases keep the review-then-apply contract:
- POST /deps/scan runs as a job — walks the folder for pyproject.toml/package.json
  and, per manifest, syncs (uv sync -U / npm install) and returns the proposed
  bumps for review.
- POST /deps/apply is synchronous — per manifest it recomputes from the synced
  state (server-authoritative), rewrites the manifest, refreshes the npm lock,
  and commits that manifest + its lockfile on its own.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import depsync

from ..deps import JobsDep
from ..schemas import JobStartedOut

router = APIRouter(prefix="/deps", tags=["deps"])

_NO_MANIFESTS = (
    "❌ No pyproject.toml or package.json found under that folder "
    "(node_modules, .venv, .git, and build dirs are skipped)."
)


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


class TargetResultOut(BaseModel):
    rel: str
    kind: str
    written: int
    bumps: list[BumpOut]
    committed: bool
    commit_sha: str | None = None
    error: str | None = None


class ApplyOut(BaseModel):
    results: list[TargetResultOut]
    written_total: int
    committed_count: int


@router.post("/scan", response_model=JobStartedOut)
def scan(req: ScanIn, jobs: JobsDep) -> JobStartedOut:
    manifests, err = depsync.find_manifests(req.folder)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not manifests:
        raise HTTPException(status_code=400, detail=_NO_MANIFESTS)
    root = str(Path(req.folder).expanduser())

    def worker(job):
        targets = []
        for manifest in manifests:
            if job.cancelled:
                return None
            job.set_message(f"Scanning {manifest.rel} …")
            targets.append(
                depsync.scan_manifest(
                    manifest,
                    on_message=lambda line, rel=manifest.rel: job.set_message(
                        f"{rel}: {line}"
                    ),
                    is_cancelled=lambda: job.cancelled,
                )
            )
        if job.cancelled:
            return None
        return {
            "root": root,
            "targets": targets,
            "total_bumps": sum(len(t["bumps"]) for t in targets),
        }

    job = jobs.submit("dep-upgrade", [], worker)
    return JobStartedOut(job_id=job.id)


@router.post("/apply", response_model=ApplyOut)
def apply(req: ApplyIn) -> ApplyOut:
    manifests, err = depsync.find_manifests(req.folder)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not manifests:
        raise HTTPException(status_code=400, detail=_NO_MANIFESTS)

    results = [depsync.apply_manifest(manifest, req.commit) for manifest in manifests]
    return ApplyOut(
        results=[
            TargetResultOut(
                rel=r["rel"],
                kind=r["kind"],
                written=r["written"],
                bumps=[BumpOut(**b) for b in r["bumps"]],
                committed=r["committed"],
                commit_sha=r["commit_sha"],
                error=r["error"],
            )
            for r in results
        ],
        written_total=sum(r["written"] for r in results),
        committed_count=sum(1 for r in results if r["committed"]),
    )
