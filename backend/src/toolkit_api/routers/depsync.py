"""Dependency Upgrader: upgrade every uv/npm manifest under a folder, then commit."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import depsync

from ..deps import JobsDep
from ..schemas import JobStartedOut

router = APIRouter(prefix="/deps", tags=["deps"])

# Folders with an apply in flight: a concurrent apply's rollback clobbers the first.
_applying: set[str] = set()
_applying_lock = threading.Lock()


@contextmanager
def _exclusive_apply(root: str) -> Iterator[None]:
    with _applying_lock:
        if root in _applying:
            raise HTTPException(
                status_code=409,
                detail="⏳ An upgrade is already running for this folder.",
            )
        _applying.add(root)
    try:
        yield
    finally:
        with _applying_lock:
            _applying.discard(root)


_NO_MANIFESTS = (
    "❌ No pyproject.toml or package.json found under that folder "
    "(node_modules, .venv, .git, and build dirs are skipped)."
)
_NOT_A_REPO = (
    "❌ Not a git repository — uncheck “commit after applying” to write without "
    "committing, or run `git init` there first."
)


class ScanIn(BaseModel):
    folder: str


class ApplyIn(BaseModel):
    folder: str
    commit: bool = True
    message: str | None = None  # blank falls back to depsync.COMMIT_SUBJECT


class BumpOut(BaseModel):
    name: str
    table: str
    old: str
    new: str
    major: bool


class SkippedOut(BaseModel):
    name: str
    reason: str


class TargetResultOut(BaseModel):
    rel: str
    kind: str
    written: int
    bumps: list[BumpOut]
    skipped: list[SkippedOut]
    error: str | None = None


class CommitOut(BaseModel):
    sha: str | None = None
    files: list[str]


class ApplyOut(BaseModel):
    results: list[TargetResultOut]
    commits: list[CommitOut]
    written_total: int


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

    # Check git up front, so a non-repo never leaves manifests edited.
    if req.commit:
        for manifest in manifests:
            if depsync.git_root(str(manifest.path.parent)) is None:
                raise HTTPException(status_code=400, detail=_NOT_A_REPO)

    with _exclusive_apply(str(Path(req.folder).expanduser().resolve())):
        return _apply_manifests(manifests, req)


def _apply_manifests(manifests: list, req: ApplyIn) -> ApplyOut:
    results = [depsync.write_manifest(manifest) for manifest in manifests]
    commits: list[dict] = []

    if req.commit:
        subject = (req.message or "").strip() or depsync.COMMIT_SUBJECT
        groups: dict[str, list[Path]] = {}
        for result in results:
            for path in result["changed"]:
                root = depsync.git_root(str(path.parent))
                if root:
                    groups.setdefault(root, []).append(path)

        commit_error = None
        for root, paths in groups.items():
            sha, rels, git_err = depsync.commit_paths(root, subject, paths)
            if git_err:
                commit_error = git_err
                break
            commits.append({"sha": sha, "files": rels})

        if commit_error:
            # All-or-nothing: nothing lands unless the commit lands.
            for result in results:
                depsync.restore(result["originals"])
                result["written"] = 0
                result["bumps"] = []
                result["error"] = f"{commit_error} (rolled back)"
            commits = []

    return ApplyOut(
        results=[
            TargetResultOut(
                rel=r["rel"],
                kind=r["kind"],
                written=r["written"],
                bumps=[BumpOut(**b) for b in r["bumps"]],
                skipped=[SkippedOut(**s) for s in r["skipped"]],
                error=r["error"],
            )
            for r in results
        ],
        commits=[CommitOut(**c) for c in commits],
        written_total=sum(r["written"] for r in results),
    )
