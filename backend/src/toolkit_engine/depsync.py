"""Upgrade dependencies and runtime pins in uv, npm, and pnpm projects."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .runtime_releases import RuntimeReleases
from .uv_workspace import (
    find_uv_workspace,
    stage_uv_workspace,
    uv_local_projects,
    uv_workspace_fingerprint,
)

_SKIP_DIRS = {
    "node_modules",
    "venv",
    "dist",
    "build",
    "__pycache__",
    "site-packages",
    "vendor",
    "target",
    "coverage",
    "htmlcov",
}
_MAX_MANIFESTS = 40
_RUNTIME_FILES = {
    ".python-version": "python",
    ".node-version": "node",
    ".nvmrc": "node",
}
_PATCH_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


@dataclass(frozen=True)
class Bump:
    """One dependency to raise: ``name old → new`` in ``table``."""

    name: str
    table: str  # e.g. "project.dependencies" or "devDependencies"
    old: str
    new: str
    major: bool
    raw: str  # the on-disk string verbatim (server-side only)
    raw_new: str  # its replacement (server-side only)


def bump_dict(bump: Bump) -> dict:
    """The wire shape the router returns (raw/raw_new stay server-side)."""
    return {
        "name": bump.name,
        "table": bump.table,
        "old": bump.old,
        "new": bump.new,
        "major": bump.major,
    }


@dataclass(frozen=True)
class Manifest:
    """A dependency manifest or runtime pin found under the scanned root."""

    path: Path
    kind: str  # "uv" | "npm" | "pnpm" | "python" | "node"
    rel: str  # display path relative to the root, e.g. "backend/pyproject.toml"
    workspace: Path | None = None
    pnpm_lock: Path | None = None

    @property
    def is_runtime(self) -> bool:
        return self.kind in {"python", "node"}


# --- Discovery ---


def _validate_folder(folder: str) -> tuple[Path | None, str | None]:
    """A real, absolute directory only: "" or "." would resolve to the server's CWD."""
    if not folder or not folder.strip():
        return None, "❌ No folder given."
    base = Path(folder).expanduser()
    if not base.is_absolute():
        return None, f"❌ Please give an absolute folder path, not: {folder}"
    if not base.is_dir():
        return None, f"❌ Not a folder: {folder}"
    return base, None


def _is_uv_project(path: Path) -> bool:
    """True if a pyproject.toml is a uv-manageable project (not tool-config only)."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return False
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        tool = {}
    # Poetry/PDM resolve their own lockfile; uv would write a second one beside it.
    if ("poetry" in tool or "pdm" in tool) and "uv" not in tool:
        return (path.parent / "uv.lock").is_file()
    return "project" in data or "dependency-groups" in data or "uv" in tool


def _workspace_match(folder: Path, pattern: str) -> bool:
    alternatives = re.search(r"\{([^{}]*,[^{}]*)\}", pattern)
    if alternatives:
        return any(
            _workspace_match(
                folder,
                pattern[: alternatives.start()] + part + pattern[alternatives.end() :],
            )
            for part in alternatives[1].split(",")
        )
    return bool(pattern) and (
        folder.full_match(pattern)
        or (pattern.endswith("/**") and folder.full_match(pattern[:-3]))
    )


def _pnpm_workspace(folder: Path) -> tuple[Path | None, dict]:
    """Find the nearest pnpm workspace and check its package patterns."""
    for root in (folder, *folder.parents):
        config = root / "pnpm-workspace.yaml"
        if not config.is_file():
            continue
        try:
            data = yaml.safe_load(config.read_text(encoding="utf-8"))
        except OSError, ValueError, yaml.YAMLError:
            data = None  # Let pnpm report invalid workspace configuration.
        if not isinstance(data, dict):
            data = {}
        if root == folder:
            return root, data
        packages = data.get("packages")
        if isinstance(packages, list):
            relative = folder.relative_to(root)
            matches = [
                pattern
                for pattern in packages
                if isinstance(pattern, str)
                and _workspace_match(relative, pattern.removeprefix("!"))
            ]
            if any(not p.startswith("!") for p in matches) and not any(
                p.startswith("!") for p in matches
            ):
                return root, data
        break
    return None, {}


def _js_manifest(path: Path, base: Path) -> Manifest | None:
    """Use the declared manager first, then the project or workspace lockfile."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict) or not any(
        isinstance(data.get(t), dict) and data[t] for t in _NPM_TABLES
    ):
        return None

    folder = path.parent
    rel = str(path.relative_to(base))
    declared = data.get("packageManager")
    manager = declared.partition("@")[0] if isinstance(declared, str) else ""
    npm_lock = (folder / "package-lock.json").is_file()
    if manager in {"yarn", "bun"}:
        return None
    if manager == "npm":
        return Manifest(path, "npm", rel)

    workspace, settings = _pnpm_workspace(folder)
    if (
        manager == "pnpm"
        or workspace
        or (not npm_lock and (folder / _PNPM_LOCKFILE).is_file())
    ):
        lock_dir = workspace or folder
        if settings.get("sharedWorkspaceLockfile") is False:
            lock_dir = folder
        custom_dir = settings.get("lockfileDir")
        if isinstance(custom_dir, str) and custom_dir:
            lock_dir = ((workspace or folder) / custom_dir).resolve()
        return Manifest(path, "pnpm", rel, workspace, lock_dir / _PNPM_LOCKFILE)
    if not npm_lock and any((folder / name).is_file() for name in _NPM_ALT_LOCKS):
        return None
    return Manifest(path, "npm", rel)


def _pin_span(text: str, kind: str) -> tuple[int, int] | None:
    """Locate a single exact version while preserving comments and line endings."""
    span = None
    offset = 0
    for line in text.splitlines(keepends=True):
        code = line.partition("#")[0]
        pin = code.strip()
        if pin:
            version = pin.removeprefix("v") if kind == "node" else pin
            if span is not None or _PATCH_VERSION.fullmatch(version) is None:
                return None
            start = offset + len(code) - len(code.lstrip())
            span = (start, start + len(pin))
        offset += len(line)
    return span


def find_manifests(folder: str) -> tuple[list[Manifest], str | None]:
    """Every supported manifest under ``folder`` (heavy/hidden dirs pruned)."""
    base, err = _validate_folder(folder)
    if err:
        return [], err
    base = base.resolve()  # git reports realpaths; a symlinked root must still match
    found: list[Manifest] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        here = Path(dirpath)
        if "pyproject.toml" in filenames and _is_uv_project(here / "pyproject.toml"):
            p = here / "pyproject.toml"
            workspace = find_uv_workspace(here)
            found.append(Manifest(p, "uv", str(p.relative_to(base)), workspace))
        if "package.json" in filenames:
            manifest = _js_manifest(here / "package.json", base)
            if manifest is not None:
                found.append(manifest)
        for name, kind in _RUNTIME_FILES.items():
            if name not in filenames:
                continue
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            with contextlib.suppress(OSError, UnicodeError):
                if _pin_span(path.read_text(encoding="utf-8"), kind) is not None:
                    found.append(Manifest(path, kind, str(path.relative_to(base))))
    found.sort(key=lambda m: m.rel)
    return found[:_MAX_MANIFESTS], None


def _lock_path(folder: str, name: str) -> Path:
    """The folder's lockfile; a workspace member has none, so the root's is used."""
    start = Path(folder).expanduser()
    root = git_root(folder)
    if root is None:
        return start / name
    here, top = start.resolve(), Path(root).resolve()
    while not (here / name).is_file():
        if here == top or here.parent == here:
            return start / name
        here = here.parent
    return here / name


# --- Subprocess streaming (uv lock / npm install) ---


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _stream(
    cmd: list[str],
    folder: str,
    on_message=None,
    is_cancelled=None,
    poll: float = 0.2,
    *,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run ``cmd`` in ``folder``, streaming output lines to ``on_message``."""
    proc = subprocess.Popen(
        cmd,
        cwd=folder,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",  # a native build can emit bytes that are not UTF-8
        bufsize=1,
    )
    lines: list[str] = []
    q: queue.Queue[str | None] = queue.Queue()

    # A reader thread keeps the cancel poll on a fixed cadence when output stalls.
    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                q.put(line)
        finally:
            proc.stdout.close()
            q.put(None)  # EOF sentinel, even if the read blew up

    threading.Thread(target=_reader, daemon=True).start()

    while True:
        if is_cancelled is not None and is_cancelled():
            _terminate(proc)
            return False, "\n".join(lines)
        try:
            item = q.get(timeout=poll)
        except queue.Empty:
            continue
        if item is None:
            break
        text = item.rstrip()
        lines.append(text)
        if text and on_message is not None:
            on_message(text)

    proc.wait()
    return proc.returncode == 0, "\n".join(lines)


_LOCK_TIMEOUT = 300


def _lock_refresh(cmd: list[str], folder: str, tool: str) -> tuple[bool, str]:
    """Lockfile-only resolve; a timeout returns False so apply can roll back."""
    exe = shutil.which(cmd[0])
    if exe is None:
        return False, f"❌ {tool} is not installed or not on PATH."
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            cwd=folder,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_LOCK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_LOCK_TIMEOUT}s"
    return proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


# --- uv (pyproject.toml + uv.lock) ---


def run_uv_upgrade(folder, on_message=None, is_cancelled=None) -> tuple[bool, str]:
    """Resolve a candidate lock in the staged project's directory."""
    uv = shutil.which("uv")
    if uv is None:
        return False, "❌ uv is not installed or not on PATH."
    return _stream(
        [uv, "lock", "-U"],
        folder,
        on_message,
        is_cancelled,
        env=dict(os.environ, UV_PROJECT=folder, UV_WORKING_DIR=folder),
    )


def uv_lock_refresh(folder: str) -> tuple[bool, str]:
    """Re-resolve uv.lock: it stores the declared floors, so a bump makes it stale."""
    return _lock_refresh(
        ["uv", "lock", "--project", folder, "--directory", folder], folder, "uv"
    )


def resolved_versions(folder: str) -> tuple[dict[str, str], str | None]:
    """Canonical-name → version, read from the folder's ``uv.lock``."""
    return _resolved_uv_versions(_lock_path(folder, "uv.lock"))


def _resolved_uv_versions(lock: Path) -> tuple[dict[str, str], str | None]:
    if not lock.is_file():
        return {}, f"❌ No uv.lock in {lock.parent}."
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, f"❌ Could not read uv.lock: {exc}"
    # A forked resolution can list several versions; those floors are left alone.
    by_name: dict[str, set[str]] = {}
    for pkg in data.get("package", []):
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            by_name.setdefault(canonicalize_name(name), set()).add(version)
    versions = {name: v.pop() for name, v in by_name.items() if len(v) == 1}
    return versions, None


def _declared_entries(data: dict) -> list[tuple[str, str]]:
    """Every declared requirement string across the three uv tables we track."""
    entries: list[tuple[str, str]] = []
    project = data.get("project", {})
    for req in project.get("dependencies", []):
        if isinstance(req, str):
            entries.append(("project.dependencies", req))
    for extra, reqs in project.get("optional-dependencies", {}).items():
        for req in reqs:
            if isinstance(req, str):
                entries.append((f"project.optional-dependencies.{extra}", req))
    # PEP 735 groups may hold {"include-group": ...} tables — keep only strings.
    for group, reqs in data.get("dependency-groups", {}).items():
        for req in reqs:
            if isinstance(req, str):
                entries.append((f"dependency-groups.{group}", req))
    return entries


def _uv_bump(table: str, req_str: str, resolved: dict[str, str]) -> Bump | None:
    """A Bump for one requirement, or None if it should be left alone."""
    try:
        req = Requirement(req_str)
    except InvalidRequirement:
        return None  # URL/path/otherwise-unparseable — never touch it
    if req.marker is not None:
        return None
    specs = list(req.specifier)
    if len(specs) != 1:
        return None
    spec = specs[0]
    if spec.operator != ">=":
        return None
    installed = resolved.get(canonicalize_name(req.name))
    if installed is None:
        return None
    try:
        floor, target = Version(spec.version), Version(installed)
    except InvalidVersion:
        return None
    # A local tag ("2.1.0+cpu") makes an invalid ">="; bump to the public version only.
    if target.local is not None:
        installed = target.public
        target = Version(installed)
    if target <= floor:
        return None

    match = re.search(r">=\s*" + re.escape(spec.version), req_str)
    if match is None:
        return None  # couldn't locate it to rewrite safely — skip
    raw_new = (
        req_str[: match.start()]
        + match.group(0).replace(spec.version, installed, 1)
        + req_str[match.end() :]
    )
    return Bump(
        name=req.name,
        table=table,
        old=f">={spec.version}",
        new=f">={installed}",
        major=target.major > floor.major,
        raw=req_str,
        raw_new=raw_new,
    )


def compute_uv_bumps(pyproject_path: Path, resolved: dict[str, str]) -> list[Bump]:
    """The lagging ``>=`` floors across all three uv dependency tables."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    bumps = []
    for table, req_str in _declared_entries(data):
        bump = _uv_bump(table, req_str, resolved)
        if bump is not None:
            bumps.append(bump)
    return bumps


# apply only edits lines inside these tables; "pkg>=x" elsewhere is never touched.
_SCANNED_SECTIONS = {
    "project",
    "project.optional-dependencies",
    "dependency-groups",
}


def _section_header(stripped: str) -> str | None:
    if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
        return stripped.strip("[]").strip()
    return None


def apply_uv_bumps(pyproject_path: Path, bumps: list[Bump]) -> None:
    """Rewrite the lagging floors in place, byte-for-byte except the quoted specs."""
    replacements = {b.raw: b.raw_new for b in bumps}  # dedup: same req, two tables
    lines = pyproject_path.read_text(encoding="utf-8").split("\n")
    section: str | None = None
    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        header = _section_header(stripped)
        if header is not None:
            section = header
            continue
        if section not in _SCANNED_SECTIONS or stripped.startswith("#"):
            continue
        for raw, raw_new in replacements.items():
            for quote in ('"', "'"):
                token = f"{quote}{raw}{quote}"
                if token in line:
                    line = line.replace(token, f"{quote}{raw_new}{quote}")
                    seen.add(raw)
        lines[i] = line
    missing = set(replacements) - seen
    if missing:
        raise ValueError(
            f"could not locate {', '.join(sorted(missing))} in {pyproject_path.name}"
        )
    pyproject_path.write_text("\n".join(lines), encoding="utf-8")


# --- npm (package.json + package-lock.json) ---

_NPM_TABLES = ("dependencies", "devDependencies", "optionalDependencies")
_NPM_ALT_LOCKS = ("yarn.lock", "bun.lock", "bun.lockb")
_PNPM_LOCKFILE = "pnpm-lock.yaml"
# Bumpable ranges: optional ^, ~ or >= before x.y.z; anything fancier is left alone.
_JS_VERSION = r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?"
_NPM_RANGE = re.compile(rf"^([~^]|>=)?\s*({_JS_VERSION})$")
_PNPM_VERSION = re.compile(_JS_VERSION)


def run_npm_upgrade(folder, on_message=None, is_cancelled=None) -> tuple[bool, str]:
    """Resolve package-lock.json only: npm outdated reads the registry, not the tree."""
    npm = shutil.which("npm")
    if npm is None:
        return False, "❌ npm is not installed or not on PATH."
    cmd = [npm, "install", "--package-lock-only"]
    return _stream(cmd, folder, on_message, is_cancelled)


def npm_outdated(folder: str) -> tuple[dict, str | None]:
    """Parse ``npm outdated --json``; exit 1 just means something is outdated."""
    npm = shutil.which("npm")
    if npm is None:
        return {}, "❌ npm is not installed or not on PATH."
    try:
        proc = subprocess.run(
            [npm, "outdated", "--json"],
            cwd=folder,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_LOCK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {}, f"❌ npm outdated timed out after {_LOCK_TIMEOUT}s."
    out = proc.stdout.strip()
    if not out:
        return {}, None  # nothing outdated
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        return {}, f"❌ Could not parse npm outdated: {exc}"
    # npm prints registry/auth failures as {"error": …} on stdout, also with exit 1.
    failure = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(failure, dict) and "latest" not in failure:
        detail = failure.get("summary") or failure.get("code") or "unknown error"
        return {}, f"❌ npm outdated failed: {detail}"
    return parsed, None


def npm_installed(folder: str) -> dict[str, str]:
    """Direct-dependency name → installed version, read from package-lock.json."""
    lock = _lock_path(folder, "package-lock.json")
    if not lock.is_file():
        return {}
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    # lockfile v2/v3: `packages` keyed by "node_modules/<name>".
    for key, info in data.get("packages", {}).items():
        if not key.startswith("node_modules/"):
            continue
        name = key[len("node_modules/") :]
        if "/node_modules/" in name:  # nested (transitive) copy — skip
            continue
        version = info.get("version") if isinstance(info, dict) else None
        if version:
            out[name] = version
    # lockfile v1 fallback: top-level `dependencies`.
    for name, info in data.get("dependencies", {}).items():
        version = info.get("version") if isinstance(info, dict) else None
        if version and name not in out:
            out[name] = version
    return out


def npm_latest(folder: str) -> tuple[dict[str, str], str | None]:
    """name → latest version: ``npm outdated`` merged over the installed versions."""
    outdated, err = npm_outdated(folder)
    if err:
        return {}, err
    latest = npm_installed(folder)
    for name, info in outdated.items():
        if isinstance(info, list):  # npm workspaces can nest a list
            info = info[0] if info else {}
        value = info.get("latest") if isinstance(info, dict) else None
        if value:
            latest[name] = value  # outdated's latest wins over the in-range one
    return latest, None


def npm_lock_refresh(folder: str) -> tuple[bool, str]:
    """Resolve package-lock.json without installing node_modules."""
    return _lock_refresh(["npm", "install", "--package-lock-only"], folder, "npm")


# --- pnpm (package.json + pnpm-lock.yaml) ---


def _pnpm_install_command(manifest: Manifest) -> list[str]:
    cmd = [
        "pnpm",
        "install",
        "--lockfile-only",
        "--no-frozen-lockfile",
        "--ignore-scripts",
    ]
    if manifest.workspace is None:
        cmd.append("--ignore-workspace")
    else:
        # Limit installs to the selected project when each member has its own lock.
        cmd.extend(["--filter", "{.}"])
    return cmd


def _pnpm_lock_path(manifest: Manifest) -> Path:
    return manifest.pnpm_lock or manifest.path.with_name(_PNPM_LOCKFILE)


def run_pnpm_upgrade(
    manifest: Manifest, on_message=None, is_cancelled=None
) -> tuple[bool, str]:
    """Refresh pnpm's lockfile without installing node_modules or running scripts."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        return False, "❌ pnpm is not installed or not on PATH."
    cmd = _pnpm_install_command(manifest)
    return _stream(
        [pnpm, *cmd[1:]], str(manifest.path.parent), on_message, is_cancelled
    )


def _pnpm_locked_versions(manifest: Manifest) -> dict[str, str]:
    """Direct registry versions from this project's importer, without node_modules."""
    lock = _pnpm_lock_path(manifest)
    if not lock.is_file():
        return {}
    importer_id = Path(os.path.relpath(manifest.path.parent, lock.parent)).as_posix()
    versions: dict[str, str] = {}
    with lock.open(encoding="utf-8") as file:
        for data in yaml.safe_load_all(file):
            if not isinstance(data, dict):
                continue
            importers = data.get("importers")
            if isinstance(importers, dict):
                importer = importers.get(importer_id)
            else:  # Older single-project locks keep dependency tables at the top level.
                importer = data if importer_id == "." else None
            if not isinstance(importer, dict):
                continue
            for table in _NPM_TABLES:
                entries = importer.get(table)
                if not isinstance(entries, dict):
                    continue
                for name, info in entries.items():
                    version = info.get("version") if isinstance(info, dict) else info
                    if isinstance(version, str):
                        release = version.split("(", 1)[0]
                        if _PNPM_VERSION.fullmatch(release):
                            versions[name] = release
    return versions


def pnpm_latest(manifest: Manifest) -> tuple[dict[str, str], str | None]:
    """Merge registry updates over the versions already resolved in the lockfile."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        return {}, "❌ pnpm is not installed or not on PATH."
    cmd = [pnpm, "outdated", "--json", "--loglevel", "error"]
    if manifest.workspace is None:
        cmd.append("--ignore-workspace")
    try:
        proc = subprocess.run(
            cmd,
            cwd=manifest.path.parent,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_LOCK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {}, f"❌ pnpm outdated timed out after {_LOCK_TIMEOUT}s."
    try:
        outdated = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}, f"❌ pnpm outdated failed:\n{_tail(proc.stderr or proc.stdout)}"
    if not isinstance(outdated, dict):
        return {}, "❌ Unexpected JSON from pnpm outdated."
    failure = outdated.get("error")
    if isinstance(failure, dict) and "latest" not in failure:
        detail = failure.get("message") or failure.get("code") or "unknown error"
        return {}, f"❌ pnpm outdated failed: {detail}"
    updates = {}
    for name, info in outdated.items():
        version = info.get("latest") if isinstance(info, dict) else None
        if isinstance(version, str) and _PNPM_VERSION.fullmatch(version):
            updates[name] = version
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and not outdated):
        detail = _tail(proc.stderr) or f"exit status {proc.returncode}"
        return {}, f"❌ pnpm outdated failed: {detail}"
    try:
        latest = _pnpm_locked_versions(manifest)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return {}, f"❌ Could not read {_PNPM_LOCKFILE}: {exc}"
    latest.update(updates)
    return latest, None


def pnpm_lock_refresh(manifest: Manifest) -> tuple[bool, str]:
    return _lock_refresh(
        _pnpm_install_command(manifest), str(manifest.path.parent), "pnpm"
    )


# --- Shared package.json bump rules ---


def _semver_release(version: str) -> tuple[int, int, int]:
    """(major, minor, patch) from a semver string, ignoring pre-release/build."""
    core = version.split("+", 1)[0].split("-", 1)[0]
    nums = [int(p) if p.isdigit() else 0 for p in core.split(".")[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _npm_bump(table: str, name: str, declared, latest: str) -> Bump | None:
    """Bump ``name``'s range to ``latest`` keeping its operator, or None."""
    if not isinstance(declared, str) or not isinstance(latest, str):
        return None
    match = _NPM_RANGE.match(declared.strip())
    if match is None:
        return None  # complex range / dist-tag / url / workspace — leave alone
    operator, current = match.group(1) or "", match.group(2)
    if _semver_release(latest) <= _semver_release(current):
        return None
    old = declared.strip()
    new = f"{operator}{latest}"
    if new == old:
        return None
    return Bump(
        name=name,
        table=table,
        old=old,
        new=new,
        major=_semver_release(latest)[0] > _semver_release(current)[0],
        raw=f'"{name}": "{old}"',
        raw_new=f'"{name}": "{new}"',
    )


def compute_npm_bumps(package_json_path: Path, latest: dict[str, str]) -> list[Bump]:
    """Bumps for every declared dependency whose floor lags the latest version."""
    data = json.loads(package_json_path.read_text(encoding="utf-8"))
    bumps = []
    for table in _NPM_TABLES:
        deps = data.get(table)
        if not isinstance(deps, dict):
            continue
        for name, declared in deps.items():
            newest = latest.get(name)
            if not newest:
                continue
            bump = _npm_bump(table, name, declared, newest)
            if bump is not None:
                bumps.append(bump)
    return bumps


def _table_span(text: str, table: str) -> tuple[int, int] | None:
    """The body of the ``"table": { … }`` object, so no other table is rewritten."""
    opening = re.search(r'"' + re.escape(table) + r'"\s*:\s*\{', text)
    if opening is None:
        return None
    depth = 1
    for token in re.finditer(r'"(?:[^"\\]|\\.)*"|[{}]', text[opening.end() :]):
        depth += {"{": 1, "}": -1}.get(token.group(), 0)
        if depth == 0:
            return opening.end(), opening.end() + token.start()
    return None


def apply_npm_bumps(package_json_path: Path, bumps: list[Bump]) -> None:
    """Rewrite each `"name": "range"` value in place, tolerant of JSON spacing."""
    text = package_json_path.read_text(encoding="utf-8")
    for bump in bumps:
        pattern = re.compile(
            r'("' + re.escape(bump.name) + r'"\s*:\s*)"' + re.escape(bump.old) + r'"'
        )
        span, n = _table_span(text, bump.table), 0
        if span is not None:
            start, end = span
            body, n = pattern.subn(
                lambda m, new=bump.new: m.group(1) + f'"{new}"',
                text[start:end],
                count=1,
            )
            text = text[:start] + body + text[end:]
        if n == 0:
            raise ValueError(
                f'could not locate "{bump.name}": "{bump.old}" '
                f"in {package_json_path.name}"
            )
    package_json_path.write_text(text, encoding="utf-8")


# --- git ---


COMMIT_SUBJECT = "chore(deps): update dependencies"


def git_root(folder: str) -> str | None:
    """The repo root containing ``folder``, or None if it isn't a git repo."""
    proc = subprocess.run(
        ["git", "-C", folder, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def commit_paths(
    repo_root: str, message: str, paths: list[Path]
) -> tuple[str | None, list[str], str | None]:
    """Commit exactly ``paths``, together, in one commit. (sha, rels, error)."""
    candidates: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            candidates.append(str(path.relative_to(repo_root)))
        except ValueError:
            continue  # outside this repo

    nothing = "❌ Nothing to commit — the manifests and lockfiles are unchanged."
    if not candidates:
        return None, [], nothing
    # Check every target before staging any: a failed commit resets these paths.
    staged = subprocess.run(
        ["git", "-C", repo_root, "diff", "--cached", "--quiet", "--", *candidates],
        capture_output=True,
        text=True,
    )
    if staged.returncode == 1:
        return (
            None,
            [],
            (
                "❌ Target files already have staged changes. "
                "Commit or unstage those changes before applying with a commit."
            ),
        )
    if staged.returncode != 0:
        return None, [], f"❌ Could not check staged changes: {staged.stderr.strip()}"

    rels: list[str] = []
    for rel in candidates:
        added = subprocess.run(
            ["git", "-C", repo_root, "add", "--", rel],
            capture_output=True,
            text=True,
        )
        if added.returncode == 0:  # a gitignored file fails here and is skipped
            rels.append(rel)

    if not rels:
        return None, [], nothing
    diff = subprocess.run(
        ["git", "-C", repo_root, "diff", "--cached", "--quiet", "--", *rels]
    )
    if diff.returncode == 0:  # 0 == no staged changes for these paths
        return None, [], nothing

    commit = subprocess.run(
        ["git", "-C", repo_root, "commit", "-m", message, "--", *rels],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        # git add staged them; a rejected commit must not leave them in the index.
        subprocess.run(
            ["git", "-C", repo_root, "reset", "-q", "--", *rels], capture_output=True
        )
        detail = (
            commit.stderr.strip()
            or commit.stdout.strip()
            or "a git hook rejected the commit"
        )
        return None, [], f"❌ git commit failed: {detail}"
    sha = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return sha.stdout.strip() or None, rels, None


# --- Runtime pins ---


def _requires_python(data: dict) -> str:
    return data.get("project", {}).get("requires-python", "")


def _python_requirement(folder: Path) -> SpecifierSet:
    nearest: str | None = None
    for directory in (folder, *folder.parents):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        if nearest is None:
            nearest = _requires_python(data)
        if "workspace" not in data.get("tool", {}).get("uv", {}):
            continue
        # uv combines the Python requirements of all workspace members.
        proc = subprocess.run(
            [
                "uv",
                "workspace",
                "list",
                "--paths",
                "--offline",
                "--no-cache",
                "--no-python-downloads",
            ],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode:
            raise ValueError(
                _tail(proc.stderr) or "Could not list uv workspace members."
            )
        requirements = []
        for member in proc.stdout.splitlines():
            member_path = folder / member / "pyproject.toml"
            member_data = tomllib.loads(member_path.read_text(encoding="utf-8"))
            requirements.append(_requires_python(member_data))
        return SpecifierSet(",".join(filter(None, requirements)))
    return SpecifierSet(nearest or "")


def _runtime_bump(
    manifest: Manifest, releases: RuntimeReleases
) -> tuple[Bump | None, str | None]:
    path = manifest.path
    if path.is_symlink():
        return None, "The runtime pin is a symbolic link."
    try:
        original = path.read_text(encoding="utf-8", newline="")
        requirement = (
            _python_requirement(path.parent)
            if manifest.kind == "python"
            else SpecifierSet()
        )
    except (OSError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, f"❌ Could not read runtime settings: {exc}"
    span = _pin_span(original, manifest.kind)
    if span is None:
        return None, None
    start, end = span
    old = original[start:end]
    prefix = "v" if old.startswith("v") else ""
    current = Version(old.removeprefix(prefix))
    if manifest.kind == "python":
        series = f"{current.major}.{current.minor}"
        versions, error = releases.python(path.parent, series)
        name = "Python"
    else:
        versions, error = releases.node
        name = "Node.js"
    if error:
        return None, error

    stable = (Version(item) for item in versions if _PATCH_VERSION.fullmatch(item))
    latest = max(
        (
            v
            for v in stable
            if v.release[:2] == current.release[:2] and v in requirement
        ),
        default=current,
    )
    if latest <= current:
        return None, None
    new = f"{prefix}{latest}"
    return (
        Bump(
            name,
            path.name,
            old,
            new,
            False,
            original,
            original[:start] + new + original[end:],
        ),
        None,
    )


# --- Per-manifest orchestration (used by the router) ---

_LOCKS = {"uv": "uv.lock", "npm": "package-lock.json"}
_NPM_RETRY_ROUNDS = 3
UV_PREVIEW_STALE = "The uv project changed since the preview. Scan again."


@dataclass(frozen=True)
class UvScan:
    versions: dict[str, str]
    fingerprint: str = ""
    error: str | None = None


def _scan_uv_workspace(workspace: Path, on_message, is_cancelled) -> UvScan:
    try:
        fingerprint = uv_workspace_fingerprint(workspace)
        with stage_uv_workspace(workspace) as staged:
            ok, log = run_uv_upgrade(str(staged), on_message, is_cancelled)
            if is_cancelled is not None and is_cancelled():
                return UvScan({})
            if not ok:
                return UvScan({}, error=f"uv lock -U failed:\n{_tail(log)}")
            versions, error = _resolved_uv_versions(staged / "uv.lock")
        if uv_workspace_fingerprint(workspace) != fingerprint:
            return UvScan({}, error=UV_PREVIEW_STALE)
        return UvScan(versions, fingerprint, error)
    except (OSError, ValueError) as exc:
        return UvScan({}, error=f"Could not preview uv dependencies: {exc}")


def scan_manifest(
    manifest: Manifest,
    on_message=None,
    is_cancelled=None,
    *,
    releases: RuntimeReleases | None = None,
    uv_scans: dict[Path, UvScan] | None = None,
) -> dict:
    """Preview version updates. Only JavaScript managers refresh live locks here."""
    folder = str(manifest.path.parent)
    out = {"rel": manifest.rel, "kind": manifest.kind, "bumps": [], "error": None}
    if manifest.is_runtime:
        bump, out["error"] = _runtime_bump(manifest, releases or RuntimeReleases())
        bumps = [bump] if bump else []
    elif manifest.kind == "uv":
        workspace = manifest.workspace or find_uv_workspace(manifest.path.parent)
        if uv_scans is None:
            uv_scans = {}
        if workspace not in uv_scans:
            uv_scans[workspace] = _scan_uv_workspace(
                workspace, on_message, is_cancelled
            )
        preview = uv_scans[workspace]
        if is_cancelled is not None and is_cancelled():
            return out
        if preview.error:
            out["error"] = preview.error
            return out
        bumps = compute_uv_bumps(manifest.path, preview.versions)
        out["uv_fingerprint"] = preview.fingerprint
    else:
        if manifest.kind == "pnpm":
            ok, log = run_pnpm_upgrade(manifest, on_message, is_cancelled)
            install = "pnpm install --lockfile-only"
        else:
            ok, log = run_npm_upgrade(folder, on_message, is_cancelled)
            install = "npm install --package-lock-only"
        if is_cancelled is not None and is_cancelled():
            return out
        if not ok:
            out["error"] = f"{install} failed:\n{_tail(log)}"
            return out
        latest, err = _js_latest(manifest)
        if err:
            out["error"] = err
            return out
        bumps = compute_npm_bumps(manifest.path, latest)
    out["bumps"] = [bump_dict(b) for b in bumps]
    return out


def _js_latest(manifest: Manifest) -> tuple[dict[str, str], str | None]:
    if manifest.kind == "pnpm":
        return pnpm_latest(manifest)
    return npm_latest(str(manifest.path.parent))


def _eresolve_culprits(log: str, candidates: set[str]) -> set[str]:
    """Which of the packages we bumped does npm name in its ERESOLVE report."""
    found = set()
    for name in candidates:
        # Anchored so "eslint" does not match "eslint-plugin-react@...".
        if re.search(r'(?:^|[\s/"])' + re.escape(name) + r"@", log):
            found.add(name)
    return found


def _npm_write_with_retry(
    package_json_path: Path, folder: str, bumps: list[Bump], original: str
) -> tuple[list[Bump], list[dict], str | None]:
    """Write npm bumps, refresh the lock, and retry without any ERESOLVE culprits."""
    remaining = list(bumps)
    skipped: list[dict] = []
    for _ in range(_NPM_RETRY_ROUNDS):
        package_json_path.write_text(original, encoding="utf-8")
        if not remaining:
            return [], skipped, None
        apply_npm_bumps(package_json_path, remaining)
        ok, log = npm_lock_refresh(folder)
        if ok:
            return remaining, skipped, None
        culprits = _eresolve_culprits(log, {b.name for b in remaining})
        if not culprits:
            return [], skipped, f"npm lock refresh failed:\n{_tail(log)}"
        skipped.extend(
            {"name": b.name, "reason": "peer-dependency conflict"}
            for b in remaining
            if b.name in culprits
        )
        remaining = [b for b in remaining if b.name not in culprits]
    return [], skipped, "npm could not resolve these upgrades after several tries."


def _write_result(manifest: Manifest) -> dict:
    return {
        "rel": manifest.rel,
        "kind": manifest.kind,
        "written": 0,
        "bumps": [],
        "skipped": [],
        "error": None,
        "changed": [],
        "originals": {},
    }


def _reviewed_uv_bumps(path: Path, preview: dict) -> list[Bump]:
    versions = {
        canonicalize_name(bump["name"]): bump["new"].removeprefix(">=")
        for bump in preview["bumps"]
    }
    return compute_uv_bumps(path, versions)


def write_reviewed_uv_workspace(
    workspace: Path,
    targets: list[tuple[Manifest, dict]],
    applied: dict[str, str | None],
    upcoming: list[tuple[Manifest, dict]],
) -> list[dict]:
    """Relock reviewed changes in a copy before publishing the updated files.

    ``applied`` holds the previous text of files this apply already rewrote in
    other uv projects; they do not make this project's preview stale.
    ``upcoming`` lists the reviewed uv targets this apply writes later, so the
    lock already matches projects that reference each other through path sources.
    """
    results = [_write_result(manifest) for manifest, _ in targets]
    pending = []
    reviewed = []
    for (manifest, preview), result in zip(targets, results, strict=True):
        result["error"] = preview["error"]
        if preview["bumps"]:
            pending.append((manifest.path, preview, result))
        if not preview["error"]:
            reviewed.append((manifest.path, preview, result))
    # With no updates of its own, a project's existing lock is refreshed only
    # to follow the path sources this apply rewrites.
    owners = pending or reviewed[:1]
    if not owners or not (pending or (workspace / "uv.lock").is_file()):
        return results

    originals: dict[str, str | None] = {}
    try:
        fingerprint = owners[0][1]["uv_fingerprint"]
        if uv_workspace_fingerprint(workspace, applied) != fingerprint:
            raise ValueError(UV_PREVIEW_STALE)
        if not pending and uv_workspace_fingerprint(workspace) == fingerprint:
            local = uv_local_projects(workspace)
            if not any(
                preview["bumps"] and manifest.path.parent.resolve() in local
                for manifest, preview in upcoming
            ):
                return results
        with stage_uv_workspace(workspace) as staged:
            edits = []
            for path, preview, result in pending:
                staged_path = staged / path.relative_to(workspace)
                bumps = _reviewed_uv_bumps(staged_path, preview)
                if [bump_dict(bump) for bump in bumps] != preview["bumps"]:
                    raise ValueError(UV_PREVIEW_STALE)
                apply_uv_bumps(staged_path, bumps)
                edits.append((path, staged_path, result, preview["bumps"]))
            for manifest, preview in upcoming:
                relative = os.path.relpath(manifest.path.resolve(), workspace.resolve())
                staged_path = Path(os.path.normpath(staged / relative))
                # A later project reports its own problems when it is written.
                with contextlib.suppress(OSError, ValueError):
                    if preview["bumps"] and staged_path.is_file():
                        apply_uv_bumps(
                            staged_path, _reviewed_uv_bumps(staged_path, preview)
                        )
            ok, log = uv_lock_refresh(str(staged))
            if not ok:
                raise ValueError(f"uv lock failed:\n{_tail(log)}")
            lock, candidate = workspace / "uv.lock", staged / "uv.lock"
            changes = {
                path: staged_path.read_bytes() for path, staged_path, _, _ in edits
            }
            changes[lock] = candidate.read_bytes()
        snapshots = {
            path: path.read_bytes() if path.is_file() else None for path in changes
        }
        if uv_workspace_fingerprint(workspace, applied) != fingerprint:
            raise ValueError(UV_PREVIEW_STALE)
        for path, content in changes.items():
            original = snapshots[path]
            if content != original:
                originals[str(path)] = (
                    original.decode("utf-8") if original is not None else None
                )
                path.write_bytes(content)
        for path, _, result, bumps in edits:
            changed = [file for file in (path, lock) if str(file) in originals]
            result.update(
                written=len(bumps),
                bumps=bumps,
                changed=changed,
                originals={str(file): originals[str(file)] for file in changed},
            )
        if not edits and originals:
            owners[0][2].update(changed=[lock], originals=originals)
    except (OSError, ValueError) as exc:
        with contextlib.suppress(OSError):
            restore(originals)
        for _, _, result in owners:
            result["error"] = str(exc)
    return results


def write_manifest(
    manifest: Manifest, *, releases: RuntimeReleases | None = None
) -> dict:
    """Write one manifest and refresh its lockfile if needed, without committing."""
    result = _write_result(manifest)
    originals: dict[str, str | None] = {}
    try:
        if manifest.is_runtime:
            bump, result["error"] = _runtime_bump(
                manifest, releases or RuntimeReleases()
            )
            if bump is not None:
                path = manifest.path
                if (
                    path.is_symlink()
                    or path.read_text(encoding="utf-8", newline="") != bump.raw
                ):
                    result["error"] = (
                        "The runtime pin changed during lookup. Scan again."
                    )
                    return result
                originals[str(path)] = bump.raw
                path.write_text(bump.raw_new, encoding="utf-8", newline="")
                result.update(
                    written=1,
                    bumps=[bump_dict(bump)],
                    changed=[path],
                    originals=originals,
                )
            return result
        return _write_manifest(manifest, result, originals)
    except Exception as exc:  # noqa: BLE001 — one bad manifest fails on its own
        with contextlib.suppress(OSError):  # a rollback that cannot write is moot here
            restore(originals)
        result["error"] = f"❌ {exc}"
        return result


def _write_manifest(manifest: Manifest, result: dict, originals: dict) -> dict:
    folder = str(manifest.path.parent)
    if manifest.kind == "uv":
        resolved, err = resolved_versions(folder)
        if err:
            result["error"] = err
            return result
        bumps = compute_uv_bumps(manifest.path, resolved)
    else:
        latest, err = _js_latest(manifest)
        if err:
            result["error"] = err
            return result
        bumps = compute_npm_bumps(manifest.path, latest)
    if not bumps:
        return result

    lock = (
        _pnpm_lock_path(manifest)
        if manifest.kind == "pnpm"
        else _lock_path(folder, _LOCKS[manifest.kind])
    )
    originals.update(
        {
            str(manifest.path): manifest.path.read_text(encoding="utf-8"),
            str(lock): lock.read_text(encoding="utf-8") if lock.is_file() else None,
        }
    )

    if manifest.kind == "uv":
        apply_uv_bumps(manifest.path, bumps)
        ok, log = uv_lock_refresh(folder)
        applied, skipped, err = (
            (bumps, [], None) if ok else ([], [], f"uv lock failed:\n{_tail(log)}")
        )
    elif manifest.kind == "pnpm":
        apply_npm_bumps(manifest.path, bumps)
        ok, log = pnpm_lock_refresh(manifest)
        applied, skipped, err = (
            (bumps, [], None)
            if ok
            else ([], [], f"pnpm lock refresh failed:\n{_tail(log)}")
        )
    else:
        applied, skipped, err = _npm_write_with_retry(
            manifest.path, folder, bumps, originals[str(manifest.path)]
        )
    result["skipped"] = skipped
    if err:
        restore(originals)
        result["error"] = err
        return result
    if not applied:  # every bump was backed out — nothing actually changed
        restore(originals)
        return result

    result["written"] = len(applied)
    result["bumps"] = [bump_dict(b) for b in applied]
    result["changed"] = [manifest.path] + ([lock] if lock.is_file() else [])
    result["originals"] = originals
    return result


def restore(originals: dict[str, str | None]) -> None:
    """Undo writes: restore each captured file, or delete one we newly created."""
    for path_str, text in originals.items():
        path = Path(path_str)
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8", newline="")


def _tail(log: str, lines: int = 15) -> str:
    return "\n".join(log.splitlines()[-lines:])
