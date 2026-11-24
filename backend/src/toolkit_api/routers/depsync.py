"""Dependency Upgrader: scan and update dependencies and project runtimes."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import depsync
from toolkit_engine.runtime_releases import RuntimeReleases
from toolkit_engine.uv_workspace import uv_workspace_fingerprint

from ..deps import JobsDep
from ..jobs import Job, JobRegistry
from ..schemas import JobStartedOut

router = APIRouter(prefix="/deps", tags=["deps"])

# Folders with a scan or an apply in flight, including shared workspace roots.
_busy_roots: set[str] = set()
_busy_lock = threading.Lock()
_BUSY = "⏳ An upgrade is already running for this folder."


class _BusyError(Exception):
    """Another scan or apply already owns this folder."""


class _StalePreviewError(ValueError):
    """The reviewed uv inputs changed after scanning."""


def _root_key(folder: str) -> str:
    return str(Path(folder).expanduser().resolve())


@contextmanager
def _exclusive_root(
    root: str, manifests: Iterable[depsync.Manifest] = ()
) -> Iterator[None]:
    roots = {root}
    for manifest in manifests:
        if manifest.workspace is not None:
            roots.add(str(manifest.workspace))
        if manifest.pnpm_lock is not None:
            roots.add(str(manifest.pnpm_lock.parent))
    with _busy_lock:
        # Nested scans and workspace siblings may write the same lockfile.
        if any(
            Path(here).is_relative_to(other) or Path(other).is_relative_to(here)
            for here in roots
            for other in _busy_roots
        ):
            raise _BusyError(_BUSY)
        _busy_roots.update(roots)
    try:
        yield
    finally:
        with _busy_lock:
            _busy_roots.difference_update(roots)


_NO_MANIFESTS = (
    "❌ No pyproject.toml or package.json targets or exact runtime pins found. "
    "Runtime pins are read from .python-version, .node-version, or .nvmrc."
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
    scan_job_id: str | None = None


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
        with _exclusive_root(_root_key(req.folder), manifests):
            targets = []
            releases = RuntimeReleases()
            uv_scans: dict[Path, depsync.UvScan] = {}
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
                        releases=releases,
                        uv_scans=uv_scans,
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
def apply(req: ApplyIn, jobs: JobsDep) -> ApplyOut:
    manifests = _apply_targets(req)
    preview = _scan_for_apply(req, jobs)
    try:
        with _exclusive_root(_root_key(req.folder), manifests):
            return _apply_manifests(manifests, req, preview=preview)
    except (_BusyError, _StalePreviewError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/apply/start", response_model=JobStartedOut)
def start_apply(req: ApplyIn, jobs: JobsDep) -> JobStartedOut:
    """Apply in the existing job pool; cancellation does not undo completed work."""
    manifests = _apply_targets(req)
    preview = _scan_for_apply(req, jobs)

    def worker(job: Job) -> dict:
        with _exclusive_root(_root_key(req.folder), manifests):
            result = _apply_manifests(manifests, req, preview=preview, job=job)
        if job.cancelled:
            job.set_message(
                "Cancelled between operations. Completed writes and commits remain."
            )
        return result.model_dump()

    job = jobs.submit("dep-upgrade", [], worker)
    return JobStartedOut(job_id=job.id)


def _apply_targets(req: ApplyIn) -> list[depsync.Manifest]:
    """Shared preflight for synchronous and job-based apply."""
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

    return manifests


def _scan_for_apply(req: ApplyIn, jobs: JobRegistry) -> dict | None:
    def matching_result(snapshot: dict) -> dict | None:
        result = snapshot["result"]
        if (
            snapshot["tool"] == "dep-upgrade"
            and snapshot["state"] == "done"
            and result is not None
            and "targets" in result
            and _root_key(result["root"]) == _root_key(req.folder)
        ):
            return result
        return None

    if req.scan_job_id is not None:
        job = jobs.get(req.scan_job_id)
        result = matching_result(job.snapshot()) if job is not None else None
        if result is None:
            raise HTTPException(
                status_code=409,
                detail="The preview is no longer available. Scan again.",
            )
        return result

    # Older callers leave out the job ID after previewing this folder.
    for snapshot in jobs.snapshots(tool="dep-upgrade", state="done"):
        if (result := matching_result(snapshot)) is not None:
            return result
    return None


def _reviewed_uv_workspaces(
    manifests: list[depsync.Manifest], preview: dict | None
) -> dict[Path, list[tuple[depsync.Manifest, dict]]]:
    if preview is None:
        return {}
    reviewed = {t["rel"]: t for t in preview["targets"] if t["kind"] == "uv"}
    uv_targets = [manifest for manifest in manifests if manifest.kind == "uv"]
    if reviewed.keys() != {manifest.rel for manifest in uv_targets}:
        raise _StalePreviewError(depsync.UV_PREVIEW_STALE)

    groups: dict[Path, list[tuple[depsync.Manifest, dict]]] = {}
    fingerprints: dict[Path, str] = {}
    for manifest in uv_targets:
        workspace = manifest.workspace or manifest.path.parent
        target = reviewed[manifest.rel]
        groups.setdefault(workspace, []).append((manifest, target))
        if target["error"]:
            continue
        if workspace not in fingerprints:
            try:
                fingerprints[workspace] = uv_workspace_fingerprint(workspace)
            except OSError:
                raise _StalePreviewError(depsync.UV_PREVIEW_STALE) from None
        if target.get("uv_fingerprint") != fingerprints[workspace]:
            raise _StalePreviewError(depsync.UV_PREVIEW_STALE)
    return groups


def _apply_manifests(
    manifests: list[depsync.Manifest],
    req: ApplyIn,
    *,
    preview: dict | None = None,
    job: Job | None = None,
) -> ApplyOut:
    # A manifest write/relock and each commit are indivisible cancellation units.
    # Never interrupt one halfway through or claim that cancellation rolls it back.
    results = []
    releases = RuntimeReleases()
    reviewed_uv = _reviewed_uv_workspaces(manifests, preview)
    # Resolve dependencies with the current pins before switching the project versions.
    for manifest in sorted(manifests, key=lambda target: target.is_runtime):
        if job is not None:
            if job.cancelled:
                break
            job.set_message(f"Applying {manifest.rel} …")
        if preview is not None and manifest.kind == "uv":
            workspace = manifest.workspace or manifest.path.parent
            if targets := reviewed_uv.pop(workspace, None):
                results.extend(depsync.write_reviewed_uv_workspace(workspace, targets))
        elif manifest.is_runtime:
            results.append(depsync.write_manifest(manifest, releases=releases))
        else:
            results.append(depsync.write_manifest(manifest))
    commits: list[dict] = []

    if req.commit and not (job is not None and job.cancelled):
        subject = (req.message or "").strip() or depsync.COMMIT_SUBJECT
        groups: dict[str, dict[Path, None]] = {}
        for result in results:
            for path in result["changed"]:
                root = depsync.git_root(str(path.parent))
                if root:
                    # A workspace lock is listed by every member; git wants it once.
                    groups.setdefault(root, {})[path] = None

        commit_error = None
        committed_paths: set[Path] = set()
        for root, paths in groups.items():
            if job is not None:
                if job.cancelled:
                    break
                job.set_message(f"Committing changes in {root} …")
            sha, rels, git_err = depsync.commit_paths(root, subject, list(paths))
            if git_err:
                commit_error = git_err
                break
            commits.append({"sha": sha, "files": rels})
            committed_paths.update(paths)

        if commit_error:
            pending = [
                result
                for result in results
                if result["changed"]
                and not all(path in committed_paths for path in result["changed"])
            ]
            originals: dict[str, str | None] = {}
            for result in pending:
                for path_str, text in result["originals"].items():
                    # Members snapshot a shared lock in turn; the first is the original.
                    if Path(path_str) not in committed_paths:
                        originals.setdefault(path_str, text)
            depsync.restore(originals)
            for result in pending:
                result["written"] = 0
                result["bumps"] = []
                result["error"] = f"{commit_error} (rolled back)"

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
