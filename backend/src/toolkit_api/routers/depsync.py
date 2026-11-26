"""Dependency Upgrader: scan and update dependencies and project runtimes."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine import depsync
from toolkit_engine.runtime_releases import RuntimeReleases
from toolkit_engine.uv_workspace import (
    is_uv_workspace,
    uv_local_projects,
    uv_workspace_fingerprint,
)

from ..deps import JobsDep
from ..jobs import Job, JobRegistry
from ..schemas import JobStartedOut

router = APIRouter(prefix="/deps", tags=["deps"])

# Folders and lockfiles with a scan or an apply in flight.
_busy_roots: set[str] = set()
_busy_lock = threading.Lock()
_BUSY = "⏳ An upgrade is already running for this folder."
_MAX_PROJECT_WORKERS = 4
_PROJECT_COMPONENTS = {"backend", "frontend"}


class _BusyError(Exception):
    """Another scan or apply already owns this folder."""


class _StalePreviewError(ValueError):
    """The reviewed uv inputs changed after scanning."""


def _root_key(folder: str) -> str:
    return str(Path(folder).expanduser().resolve())


@contextmanager
def _exclusive_root(
    root: str, manifests: Iterable[depsync.Manifest] = (), *, legacy_uv: bool = False
) -> Iterator[dict[depsync.Manifest, set[Path]]]:
    roots = {root}
    locks = {}
    for manifest in manifests:
        paths = depsync.manifest_lock_paths(manifest, legacy_uv=legacy_uv)
        locks[manifest] = {path.resolve() for path in paths}
        roots.update(map(str, locks[manifest]))
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
        yield locks
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


def _project_git_root(folder: Path) -> Path | None:
    try:
        git_root = depsync.git_root(str(folder))
    except OSError:
        # A checkout can still be grouped if Git is unavailable on the backend.
        return next(
            (
                parent
                for parent in (folder, *folder.parents)
                if (parent / ".git").exists()
            ),
            None,
        )
    root = Path(git_root).resolve() if git_root else None
    return root if root is not None and folder.is_relative_to(root) else None


def _project_roots(root: Path, manifests: list[depsync.Manifest]) -> dict[Path, Path]:
    """Identify projects for scheduling and display."""
    folders = {manifest.path.parent for manifest in manifests}
    components = {folder.relative_to(root).parts[0] for folder in folders - {root}}
    single_project = (
        root in folders
        or any((root / name).is_file() for name in ("pyproject.toml", "package.json"))
        or components <= _PROJECT_COMPONENTS
    )

    component_parents = {
        directory.parent
        for folder in folders
        for directory in (folder, *folder.parents)
        if directory.name in _PROJECT_COMPONENTS
        and directory.parent.is_relative_to(root)
    }
    paired_projects = {
        parent
        for parent in component_parents
        if all((parent / name).is_dir() for name in _PROJECT_COMPONENTS)
    }

    workspaces: dict[Path, Path] = {}
    for manifest in manifests:
        folder, workspace = manifest.path.parent, manifest.workspace
        if workspace is None or (
            manifest.kind == "uv"
            and workspace == folder
            and not is_uv_workspace(folder)
        ):
            continue
        previous = workspaces.get(folder)
        if previous is None or workspace.is_relative_to(previous):
            workspaces[folder] = workspace
    workspace_roots = set(workspaces.values())

    projects = {}
    for folder in folders:
        # Keep a frontend/backend pair together, including inside a shared repo.
        paired = (
            folder
            if folder in paired_projects
            else next(
                (
                    component.parent
                    for component in (folder, *folder.parents)
                    if component.name in _PROJECT_COMPONENTS
                    and component.parent in paired_projects
                ),
                None,
            )
        )
        if paired is not None:
            projects[folder] = paired
            continue
        fallback = root if single_project else root / folder.relative_to(root).parts[0]
        workspace = workspaces.get(
            folder, folder if folder in workspace_roots else None
        )
        # A package-manager workspace can be one component of the larger project.
        if workspace in (fallback / "backend", fallback / "frontend"):
            workspace = None
        projects[folder] = _project_git_root(folder) or workspace or fallback
    return projects


def _project_batches(
    manifests: list[depsync.Manifest],
    projects: dict[Path, Path],
    locks: dict[depsync.Manifest, set[Path]],
    *,
    applying: bool = False,
) -> list[list[depsync.Manifest]]:
    """Join projects that must use a shared lock or apply a uv path source in order."""
    parents = {project: project for project in projects.values()}

    def leader(project: Path) -> Path:
        while parents[project] != project:
            project = parents[project]
        return project

    owners: dict[Path, Path] = {}
    uv_sources: dict[Path, set[Path]] = {}
    uv_config_folders = {m.path.parent for m in manifests if m.kind in {"uv", "python"}}
    for manifest in manifests:
        folder = manifest.path.parent
        project = projects[folder]
        resources = {folder, manifest.path, *locks[manifest]}
        if manifest.workspace is not None:
            resources.add(manifest.workspace)
        if applying and manifest.kind == "uv":
            workspace = manifest.workspace or folder
            if workspace not in uv_sources:
                sources = uv_local_projects(workspace) | {workspace}
                # uv staging reads ancestor manifests and interpreter pins too.
                inherited = {
                    parent
                    for source in sources
                    for parent in source.parents
                    if parent in uv_config_folders
                }
                uv_sources[workspace] = sources | inherited
            resources.update(uv_sources[workspace])
        for resource in resources:
            owner = owners.setdefault(resource.resolve(), project)
            parents[leader(project)] = leader(owner)

    batches: dict[Path, list[depsync.Manifest]] = {}
    for manifest in manifests:
        group = leader(projects[manifest.path.parent])
        batches.setdefault(group, []).append(manifest)
    return list(batches.values())


def _run_projects(
    batches: list[list[depsync.Manifest]],
    process: Callable[[list[depsync.Manifest]], list[dict]],
) -> list[dict]:
    if len(batches) == 1:
        return process(batches[0])
    with ThreadPoolExecutor(max_workers=_MAX_PROJECT_WORKERS) as pool:
        return [result for batch in pool.map(process, batches) for result in batch]


@router.post("/scan", response_model=JobStartedOut)
def scan(req: ScanIn, jobs: JobsDep) -> JobStartedOut:
    manifests, err = depsync.find_manifests(req.folder)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not manifests:
        raise HTTPException(status_code=400, detail=_NO_MANIFESTS)
    root = str(Path(req.folder).expanduser())
    canonical_root = _root_key(root)

    def worker(job):
        with _exclusive_root(canonical_root, manifests) as locks:
            releases = RuntimeReleases()
            projects = _project_roots(Path(canonical_root), manifests)

            def scan_project(batch: list[depsync.Manifest]) -> list[dict]:
                targets = []
                uv_scans: dict[Path, depsync.UvScan] = {}
                for manifest in batch:
                    if job.cancelled:
                        break
                    job.set_message(f"Scanning {manifest.rel} …")
                    target = depsync.scan_manifest(
                        manifest,
                        on_message=lambda line, rel=manifest.rel: job.set_message(
                            f"{rel}: {line}"
                        ),
                        is_cancelled=lambda: job.cancelled,
                        releases=releases,
                        uv_scans=uv_scans,
                    )
                    project = projects[manifest.path.parent]
                    target["project"] = {
                        "root": str(project),
                        "name": project.name or str(project),
                    }
                    target["project_rel"] = manifest.path.relative_to(
                        project
                    ).as_posix()
                    targets.append(target)
                return targets

            batches = _project_batches(manifests, projects, locks)
            targets = _run_projects(batches, scan_project)
            if job.cancelled:
                return None
            listed = {
                manifest.rel: position for position, manifest in enumerate(manifests)
            }
            targets.sort(key=lambda target: listed[target["rel"]])
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
    root = _root_key(req.folder)
    try:
        with _exclusive_root(root, manifests, legacy_uv=preview is None) as locks:
            return _apply_manifests(manifests, req, root, locks, preview=preview)
    except (_BusyError, _StalePreviewError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/apply/start", response_model=JobStartedOut)
def start_apply(req: ApplyIn, jobs: JobsDep) -> JobStartedOut:
    """Apply in the existing job pool; cancellation does not undo completed work."""
    manifests = _apply_targets(req)
    preview = _scan_for_apply(req, jobs)
    root = _root_key(req.folder)

    def worker(job: Job) -> dict:
        with _exclusive_root(root, manifests, legacy_uv=preview is None) as locks:
            result = _apply_manifests(
                manifests, req, root, locks, preview=preview, job=job
            )
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

    # Write path-source projects before the projects whose locks describe them.
    owners = {
        manifest.path.parent.resolve(): workspace
        for workspace, targets in groups.items()
        for manifest, _ in targets
    }
    ordered: dict[Path, list[tuple[depsync.Manifest, dict]]] = {}
    visiting: set[Path] = set()

    def place(workspace: Path) -> None:
        if workspace in ordered or workspace in visiting:
            return
        visiting.add(workspace)
        for project in sorted(uv_local_projects(workspace)):
            if (owner := owners.get(project)) is not None:
                place(owner)
        ordered[workspace] = groups[workspace]

    for workspace in groups:
        place(workspace)
    return ordered


def _apply_manifests(
    manifests: list[depsync.Manifest],
    req: ApplyIn,
    canonical_root: str,
    locks: dict[depsync.Manifest, set[Path]],
    *,
    preview: dict | None = None,
    job: Job | None = None,
) -> ApplyOut:
    # A manifest write/relock and each commit are indivisible cancellation units.
    # Never interrupt one halfway through or claim that cancellation rolls it back.
    releases = RuntimeReleases()
    reviewed_uv = _reviewed_uv_workspaces(manifests, preview)

    def turn(target: depsync.Manifest, uv_order: dict[Path, int]) -> tuple[bool, int]:
        workspace = target.workspace or target.path.parent
        # Resolve dependencies with the current pins before switching the
        # project versions, and path-source projects before their dependents.
        return target.is_runtime, uv_order.get(workspace, 0)

    def apply_project(batch: list[depsync.Manifest]) -> list[dict]:
        workspaces = {
            manifest.workspace or manifest.path.parent
            for manifest in batch
            if manifest.kind == "uv"
        }
        remaining_uv = {
            workspace: targets
            for workspace, targets in reviewed_uv.items()
            if workspace in workspaces
        }
        uv_order = {workspace: rank for rank, workspace in enumerate(remaining_uv)}
        results = []
        for manifest in sorted(batch, key=lambda target: turn(target, uv_order)):
            if job is not None:
                if job.cancelled:
                    break
                job.set_message(f"Applying {manifest.rel} …")
            if preview is not None and manifest.kind == "uv":
                workspace = manifest.workspace or manifest.path.parent
                if targets := remaining_uv.pop(workspace, None):
                    # Earlier path-source writes must not make this preview stale.
                    applied = {
                        path: text
                        for result in results
                        for path, text in result["originals"].items()
                    }
                    upcoming = [
                        target
                        for targets in remaining_uv.values()
                        for target in targets
                    ]
                    results.extend(
                        depsync.write_reviewed_uv_workspace(
                            workspace, targets, applied, upcoming
                        )
                    )
            elif manifest.is_runtime:
                results.append(depsync.write_manifest(manifest, releases=releases))
            else:
                results.append(depsync.write_manifest(manifest))
        return results

    projects = _project_roots(Path(canonical_root), manifests)
    batches = _project_batches(manifests, projects, locks, applying=True)
    results = _run_projects(batches, apply_project)

    # Preserve Git commit and rollback order regardless of when each batch finished.
    uv_order = {workspace: rank for rank, workspace in enumerate(reviewed_uv)}
    ordered = sorted(manifests, key=lambda target: turn(target, uv_order))
    positions = {}
    first_uv_position: dict[Path, int] = {}
    for position, manifest in enumerate(ordered):
        if preview is not None and manifest.kind == "uv":
            workspace = manifest.workspace or manifest.path.parent
            position = first_uv_position.setdefault(workspace, position)
        positions[manifest.rel] = position
    results.sort(key=lambda result: positions[result["rel"]])
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

    listed = {manifest.rel: position for position, manifest in enumerate(manifests)}
    results.sort(key=lambda result: listed[result["rel"]])
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
