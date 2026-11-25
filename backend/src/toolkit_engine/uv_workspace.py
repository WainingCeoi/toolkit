"""Discover uv lockfile owners and stage independent copies for resolution."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from glob import escape
from pathlib import Path

_PROJECT_CONFIG = {"pyproject.toml", "uv.toml", ".python-version", ".python-versions"}
# Directories uv does not need to resolve a project, some of them large.
_SKIP = {
    ".git",
    ".venv",
    ".nox",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "target",
}


def _uv_settings(path: Path) -> dict | None:
    with path.open("rb") as file:
        project = tomllib.load(file)
    tool = project.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    return uv if isinstance(uv, dict) else None


def _matches(path: Path, root: Path, patterns: object) -> bool:
    if not isinstance(patterns, list):
        return False
    for pattern in patterns:
        if isinstance(pattern, str) and pattern:
            absolute = os.path.normpath(Path(escape(str(root))) / pattern)
            if path.full_match(absolute, case_sensitive=True) or (
                absolute.endswith("/**")
                and path.full_match(absolute[:-3], case_sensitive=True)
            ):
                return True
    return False


def is_uv_workspace(root: Path) -> bool:
    """Whether the project explicitly declares a uv workspace at its root."""
    with suppress(OSError, ValueError):
        settings = _uv_settings(root / "pyproject.toml") or {}
        return isinstance(settings.get("workspace"), dict)
    return False


def find_uv_workspace(folder: Path) -> Path:
    """Find the explicit workspace that owns this project's lock, if any."""
    folder = folder.resolve()
    # The isolated uv command reports invalid manifests on their own scan target.
    with suppress(OSError, tomllib.TOMLDecodeError, UnicodeError):
        if "workspace" in (_uv_settings(folder / "pyproject.toml") or {}):
            return folder

        for parent in folder.parents:
            manifest = parent / "pyproject.toml"
            if not manifest.is_file():
                continue
            workspace = (_uv_settings(manifest) or {}).get("workspace")
            if (
                isinstance(workspace, dict)
                and _matches(folder, parent, workspace.get("members"))
                and not _matches(folder, parent, workspace.get("exclude"))
            ):
                return parent
            break  # uv stops at the first ancestor with a pyproject.toml.
    return folder


def _relative_sources(manifest: Path) -> Iterator[Path]:
    # uv reports malformed project settings if they affect the resolution.
    with suppress(tomllib.TOMLDecodeError, UnicodeError):
        sources = (_uv_settings(manifest) or {}).get("sources")
        if not isinstance(sources, dict):
            return
        for value in sources.values():
            for source in value if isinstance(value, list) else [value]:
                path = source.get("path") if isinstance(source, dict) else None
                if isinstance(path, str) and path and not Path(path).is_absolute():
                    yield Path(os.path.normpath(manifest.parent / path))


def _unusable_link(path: Path) -> bool:
    if not path.is_symlink():
        return False
    if not path.exists():
        return True
    target = path.resolve()
    return any(target == parent.resolve() for parent in path.parents)


def _workspace_projects(root: Path) -> set[Path]:
    projects = {root}
    workspace = (_uv_settings(root / "pyproject.toml") or {}).get("workspace")
    if not isinstance(workspace, dict):
        return projects
    members = workspace.get("members")
    if not isinstance(members, list):
        return projects
    for pattern in members:
        if not isinstance(pattern, str) or not pattern:
            continue
        absolute = Path(os.path.normpath(root / pattern))
        base = root if absolute.is_relative_to(root) else Path(absolute.anchor)
        for member in base.glob(absolute.relative_to(base).as_posix()):
            if (member / "pyproject.toml").is_file() and not _matches(
                member, root, workspace.get("exclude")
            ):
                projects.add(member)
    return projects


def _resolution_inputs(root: Path) -> tuple[set[Path], set[Path]]:
    """Collect workspace members, their local sources, and the shared lock."""
    projects: set[Path] = set()
    files = {root / name for name in {*_PROJECT_CONFIG, "uv.lock"}}
    pending = _workspace_projects(root)
    while pending:
        project = pending.pop()
        projects.add(project)
        manifest = project / "pyproject.toml"
        files.update(
            project / name for name in (*_PROJECT_CONFIG, "setup.py", "setup.cfg")
        )
        if manifest.is_file():
            for dependency in _relative_sources(manifest):
                if dependency.is_file():
                    files.add(dependency)
                elif dependency.is_dir() and dependency not in projects:
                    pending.add(dependency)
    return projects, {path for path in files if path.is_file()}


def _inherited_config(root: Path) -> tuple[Path, bytes] | None:
    """Account for inherited uv settings independently of parent dependencies."""
    for directory in (root, *root.parents):
        settings = directory / "uv.toml"
        if settings.is_file():
            return (settings, settings.read_bytes()) if directory != root else None
        manifest = directory / "pyproject.toml"
        if manifest.is_file():
            uv = _uv_settings(manifest)
            if uv is not None:
                return (manifest, repr(uv).encode()) if directory != root else None
    return None


def uv_workspace_fingerprint(
    root: Path, applied: Mapping[str, str | None] | None = None
) -> str:
    """Identify the current on-disk inputs for a reviewed uv upgrade.

    ``applied`` maps files this apply has already rewritten to their previous
    text, so a project depending on them still matches its preview.
    """
    root = root.resolve()
    _, files = _resolution_inputs(root)
    previous = {Path(path).resolve(): text for path, text in (applied or {}).items()}
    digest = hashlib.sha256()
    for path in sorted(files):
        if (resolved := path.resolve()) in previous:
            if (text := previous[resolved]) is None:
                continue  # Created by this apply, so the preview never saw it.
            content = hashlib.sha256(text.encode("utf-8")).digest()
        else:
            with path.open("rb") as file:
                content = hashlib.file_digest(file, "sha256").digest()
        digest.update(os.fsencode(path))
        digest.update(b"\0")
        digest.update(content)
    if inherited := _inherited_config(root):
        path, content = inherited
        digest.update(os.fsencode(path))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


@contextmanager
def stage_uv_workspace(root: Path) -> Iterator[Path]:
    """Copy working files under a temporary parent, preserving relative paths."""
    root = root.resolve()
    trees, files = _resolution_inputs(root)
    files.update(
        path
        for source in trees
        for parent in source.parents
        for name in _PROJECT_CONFIG
        if (path := parent / name).is_file()
    )
    with tempfile.TemporaryDirectory(prefix="toolkit-uv-") as temp:
        stage = Path(temp).resolve()

        def staged(path: Path) -> Path:
            return stage / path.relative_to(path.anchor)

        def ignore(directory: str, names: list[str]) -> set[str]:
            here = Path(directory)
            skipped = _SKIP.intersection(names)
            for name in names:
                path = here / name
                if path == stage or _unusable_link(path):
                    skipped.add(name)
            return skipped

        for source in trees:
            if not any(parent in trees for parent in source.parents):
                shutil.copytree(source, staged(source), ignore=ignore)
        for path in files:
            target = staged(path)
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

        yield staged(root)
