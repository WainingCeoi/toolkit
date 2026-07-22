"""Upgrade a project's dependencies across ecosystems, then commit.

Point it at a folder; it walks the tree (skipping node_modules/.venv/.git/…) for
every ``pyproject.toml`` (uv) and ``package.json`` (npm), and for each:

- **uv**: runs ``uv sync -U``, reads the resolved versions from ``uv.lock``, and
  raises the lagging ``>=`` floors (leaving ==, ~=, ranges, markered deps).
- **npm**: runs ``npm install`` + ``npm outdated``, and bumps each dependency's
  range to the latest published version, preserving its ^/~ operator.

Rewrites are surgical text edits (never a re-serialize) so comments and
formatting survive. After each rewrite the lockfile is re-resolved (``uv lock`` /
``npm install --package-lock-only``) so the manifest and its lock always land in
the same commit agreeing with each other. Every changed file across every
manifest is committed together. Pure logic, no FastAPI; the router feeds
``on_message``/``is_cancelled`` in from a Job so sync progress streams and can be
cancelled.
"""

from __future__ import annotations

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

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# Directories never worth descending into: dependency stores, build output,
# caches, VCS. Anything starting with "." is also pruned.
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
_MAX_MANIFESTS = 40  # a sane ceiling; a bigger monorepo gets a truncation note


@dataclass(frozen=True)
class Bump:
    """One dependency to raise: ``name old → new`` in ``table``."""

    name: str  # display name, e.g. "mineru" or "eslint"
    table: str  # e.g. "project.dependencies" or "devDependencies"
    old: str  # the old spec/range, e.g. ">=3.4.0" or "^9.15.0"
    new: str  # the new spec/range, e.g. ">=6.14.2" or "^10.7.0"
    major: bool  # True when the major version changed
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
    """A dependency manifest found under the scanned root."""

    path: Path  # absolute path to the pyproject.toml / package.json
    kind: str  # "uv" | "npm"
    rel: str  # display path relative to the root, e.g. "backend/pyproject.toml"


# --------------------------------------------------------------------------- #
# Discovery                                                                    #
# --------------------------------------------------------------------------- #


def _validate_folder(folder: str) -> tuple[Path | None, str | None]:
    """A folder must be a real, absolute directory. Empty/relative input is
    rejected so a stray ``""`` or ``"."`` can't resolve to the server's CWD."""
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
    return (
        "project" in data
        or "dependency-groups" in data
        or (isinstance(tool, dict) and "uv" in tool)
    )


def _has_npm_deps(path: Path) -> bool:
    """True if a package.json declares any bump-able dependency table."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return False
    return any(isinstance(data.get(t), dict) and data.get(t) for t in _NPM_TABLES)


def find_manifests(folder: str) -> tuple[list[Manifest], str | None]:
    """Every uv/npm manifest under ``folder`` (heavy/hidden dirs pruned)."""
    base, err = _validate_folder(folder)
    if err:
        return [], err
    found: list[Manifest] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        here = Path(dirpath)
        if "pyproject.toml" in filenames and _is_uv_project(here / "pyproject.toml"):
            p = here / "pyproject.toml"
            found.append(Manifest(p, "uv", str(p.relative_to(base))))
        if "package.json" in filenames and _has_npm_deps(here / "package.json"):
            p = here / "package.json"
            found.append(Manifest(p, "npm", str(p.relative_to(base))))
    found.sort(key=lambda m: m.rel)
    return found[:_MAX_MANIFESTS], None


# --------------------------------------------------------------------------- #
# Subprocess streaming (uv sync / npm install)                                #
# --------------------------------------------------------------------------- #


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
) -> tuple[bool, str]:
    """Run ``cmd`` in ``folder``, streaming output lines to ``on_message``.

    A reader thread drains stdout so the loop can poll ``is_cancelled`` on a
    fixed cadence and kill the process promptly (downloads can stall output for
    seconds). Returns (ok, combined_output).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=folder,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)
        q.put(None)  # EOF sentinel

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
    """Run a lockfile-only resolve in ``folder``. Returns (ok, output).

    A timeout is reported as a failure rather than raised: apply rolls back on a
    False, whereas an escaping TimeoutExpired would abort the request with the
    manifest already rewritten and nothing restored.
    """
    exe = shutil.which(cmd[0])
    if exe is None:
        return False, f"❌ {tool} is not installed or not on PATH."
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=_LOCK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_LOCK_TIMEOUT}s"
    return proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


# --------------------------------------------------------------------------- #
# uv (pyproject.toml + uv.lock)                                                #
# --------------------------------------------------------------------------- #


def uv_available() -> bool:
    return shutil.which("uv") is not None


def run_uv_sync(folder, on_message=None, is_cancelled=None) -> tuple[bool, str]:
    uv = shutil.which("uv")
    if uv is None:
        return False, "❌ uv is not installed or not on PATH."
    return _stream([uv, "sync", "-U"], folder, on_message, is_cancelled)


def uv_lock_refresh(folder: str) -> tuple[bool, str]:
    """Re-resolve uv.lock against the just-rewritten pyproject.toml.

    Required, not cosmetic: uv.lock records the *declared* specifiers under
    ``[package.metadata] requires-dist``, so raising a floor makes the lock
    stale even when every resolved version stays identical — and when the
    resolution is unchanged the file is byte-identical, so a commit would carry
    the manifest alone and leave the lock behind. ``uv lock`` rewrites that
    metadata without touching the virtualenv.
    """
    return _lock_refresh(["uv", "lock"], folder, "uv")


def resolved_versions(folder: str) -> tuple[dict[str, str], str | None]:
    """Canonical-name → version, read from the folder's ``uv.lock``."""
    lock = Path(folder).expanduser() / "uv.lock"
    if not lock.is_file():
        return {}, f"❌ No uv.lock in {folder} (run the scan first)."
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, f"❌ Could not read uv.lock: {exc}"
    # A forked resolution can list the same package at several versions, each
    # under its own resolution-markers. We can't tell which one this machine
    # installed, so drop any package that resolved to more than one — leaving
    # those floors alone rather than bumping to an arbitrary fork.
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
    """A Bump for one requirement, or None if it should be left alone.

    Only a single-clause ``>=`` with a resolved version strictly greater than the
    floor qualifies. Markered deps are skipped (their version is env-specific).
    """
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
    # A local build tag (e.g. torch "2.1.0+cpu") is machine-specific and makes an
    # INVALID ">=" specifier; bump to the public version only, never on the local
    # segment alone (a "+cpu" build is not an upgrade over 2.1.0).
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


# The tables compute_uv_bumps scans; apply only ever edits lines inside one of
# these, so an identical "pkg>=x" in [build-system], [tool.uv], or a comment is
# never touched.
_SCANNED_SECTIONS = {
    "project",
    "project.optional-dependencies",
    "dependency-groups",
}


def _section_header(stripped: str) -> str | None:
    """The table name if the line is a ``[section]`` header, else None."""
    if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
        return stripped.strip("[]").strip()
    return None


def apply_uv_bumps(pyproject_path: Path, bumps: list[Bump]) -> None:
    """Rewrite the lagging floors in place, scoped to the three dependency
    tables. Only the exact quoted requirement strings change — comments, other
    tables, alignment, and every other byte stay as-is."""
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
            continue  # never touch comments or unscanned tables
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


# --------------------------------------------------------------------------- #
# npm (package.json + package-lock.json)                                       #
# --------------------------------------------------------------------------- #

_NPM_TABLES = ("dependencies", "devDependencies", "optionalDependencies")
# Ranges we know how to bump: a bare ^, ~, or >= (or none) in front of an
# x.y.z version. Anything fancier (1.x, *, ">=1 <2", "workspace:*", git/url,
# dist-tags) is left alone.
_NPM_RANGE = re.compile(r"^([~^]|>=)?\s*(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)$")


def npm_available() -> bool:
    return shutil.which("npm") is not None


def run_npm_install(folder, on_message=None, is_cancelled=None) -> tuple[bool, str]:
    npm = shutil.which("npm")
    if npm is None:
        return False, "❌ npm is not installed or not on PATH."
    return _stream([npm, "install"], folder, on_message, is_cancelled)


def npm_outdated(folder: str) -> tuple[dict, str | None]:
    """Parse ``npm outdated --json`` → {name: {current, wanted, latest}}.

    npm exits 1 when packages are outdated — that is expected, not a failure, so
    the output is parsed regardless of the return code.
    """
    npm = shutil.which("npm")
    if npm is None:
        return {}, "❌ npm is not installed or not on PATH."
    proc = subprocess.run(
        [npm, "outdated", "--json"],
        cwd=folder,
        capture_output=True,
        text=True,
    )
    out = proc.stdout.strip()
    if not out:
        return {}, None  # nothing outdated
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return {}, f"❌ Could not parse npm outdated: {exc}"


def npm_installed(folder: str) -> dict[str, str]:
    """Direct-dependency name → installed version, read from package-lock.json.

    This is the latest version already permitted by each declared range (npm
    install resolves to it), so it catches floors that lag even when npm
    outdated stays silent because the range already allows the newest release.
    """
    lock = Path(folder) / "package-lock.json"
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
    """name → latest publishable version, merging ``npm outdated`` (which knows
    the true latest, even beyond the current range) over the installed versions
    (the latest already inside each range)."""
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
    """Resolve package-lock.json for the current package.json without installing
    node_modules — fast, and all apply needs to commit a consistent lock."""
    return _lock_refresh(["npm", "install", "--package-lock-only"], folder, "npm")


def _semver_release(version: str) -> tuple[int, int, int]:
    """(major, minor, patch) from a semver string, ignoring pre-release/build."""
    core = version.split("+", 1)[0].split("-", 1)[0]
    nums = [int(p) if p.isdigit() else 0 for p in core.split(".")[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _npm_bump(table: str, name: str, declared, latest: str) -> Bump | None:
    """Bump ``name``'s range to ``latest`` (preserving its ^/~/>= operator), or
    None if the range is fancier than we safely rewrite or already current."""
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


def apply_npm_bumps(package_json_path: Path, bumps: list[Bump]) -> None:
    """Rewrite each `"name": "range"` value in place, tolerant of the JSON
    spacing. Only the matched key's version range changes."""
    text = package_json_path.read_text(encoding="utf-8")
    for bump in bumps:
        pattern = re.compile(
            r'("' + re.escape(bump.name) + r'"\s*:\s*)"' + re.escape(bump.old) + r'"'
        )
        text, n = pattern.subn(
            lambda m, new=bump.new: m.group(1) + f'"{new}"', text, count=1
        )
        if n == 0:
            raise ValueError(
                f'could not locate "{bump.name}": "{bump.old}" '
                f"in {package_json_path.name}"
            )
    package_json_path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# git                                                                          #
# --------------------------------------------------------------------------- #


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
    """Commit exactly ``paths``, together, in one commit. (sha, rels, error).

    Each path is staged individually — so a freshly-created (untracked) lock is
    included and a gitignored one is skipped rather than aborting. The commit is
    path-limited to those staged files, so any other staged or unstaged work in
    the repo is neither committed nor touched.
    """
    rels: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            continue  # outside this repo
        added = subprocess.run(
            ["git", "-C", repo_root, "add", "--", rel],
            capture_output=True,
            text=True,
        )
        if added.returncode == 0:  # a gitignored file fails here and is skipped
            rels.append(rel)

    nothing = "❌ Nothing to commit — the manifests and lockfiles are unchanged."
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
        return (
            None,
            [],
            f"❌ git commit failed: {commit.stderr.strip() or commit.stdout.strip()}",
        )
    sha = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return sha.stdout.strip() or None, rels, None


# --------------------------------------------------------------------------- #
# Per-manifest orchestration (used by the router)                             #
# --------------------------------------------------------------------------- #

_LOCKS = {"uv": "uv.lock", "npm": "package-lock.json"}
_NPM_RETRY_ROUNDS = 3


def scan_manifest(manifest: Manifest, on_message=None, is_cancelled=None) -> dict:
    """Sync one manifest and compute its bumps. Returns a wire-ready dict with
    ``rel``, ``kind``, ``bumps`` (list of dicts), and ``error`` (str|None)."""
    folder = str(manifest.path.parent)
    out = {"rel": manifest.rel, "kind": manifest.kind, "bumps": [], "error": None}
    if manifest.kind == "uv":
        ok, log = run_uv_sync(folder, on_message, is_cancelled)
        if is_cancelled is not None and is_cancelled():
            return out
        if not ok:
            out["error"] = f"uv sync -U failed:\n{_tail(log)}"
            return out
        resolved, err = resolved_versions(folder)
        if err:
            out["error"] = err
            return out
        bumps = compute_uv_bumps(manifest.path, resolved)
    else:
        ok, log = run_npm_install(folder, on_message, is_cancelled)
        if is_cancelled is not None and is_cancelled():
            return out
        if not ok:
            out["error"] = f"npm install failed:\n{_tail(log)}"
            return out
        latest, err = npm_latest(folder)
        if err:
            out["error"] = err
            return out
        bumps = compute_npm_bumps(manifest.path, latest)
    out["bumps"] = [bump_dict(b) for b in bumps]
    return out


def _eresolve_culprits(log: str, candidates: set[str]) -> set[str]:
    """Which of the packages we bumped does npm name in its ERESOLVE report."""
    found = set()
    for name in candidates:
        # npm writes each package as "<name>@<spec>"; anchoring on the preceding
        # character keeps "eslint" from matching "eslint-plugin-react@...".
        if re.search(r'(?:^|[\s/"])' + re.escape(name) + r"@", log):
            found.add(name)
    return found


def _npm_write_with_retry(
    package_json_path: Path, folder: str, bumps: list[Bump], original: str
) -> tuple[list[Bump], list[dict], str | None]:
    """Write the npm bumps and refresh the lock, backing out any package npm
    reports as an unresolvable peer conflict and retrying with the rest.

    Upgrading everything to latest can produce a graph npm rightly refuses — a
    new major of a tool whose plugins still pin the old one (eslint 10 vs
    plugins on eslint 9). Rather than failing the whole manifest, drop the
    conflicting packages and upgrade the remainder. (applied, skipped, error).
    """
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


def write_manifest(manifest: Manifest) -> dict:
    """Recompute and write one manifest, then re-resolve its lockfile. No commit
    — the caller commits every changed file from every manifest together."""
    folder = str(manifest.path.parent)
    result = {
        "rel": manifest.rel,
        "kind": manifest.kind,
        "written": 0,
        "bumps": [],
        "skipped": [],
        "error": None,
        "changed": [],
        "originals": {},
    }

    if manifest.kind == "uv":
        resolved, err = resolved_versions(folder)
        if err:
            result["error"] = err
            return result
        bumps = compute_uv_bumps(manifest.path, resolved)
    else:
        latest, err = npm_latest(folder)
        if err:
            result["error"] = err
            return result
        bumps = compute_npm_bumps(manifest.path, latest)
    if not bumps:
        return result

    lock = Path(folder) / _LOCKS[manifest.kind]
    originals: dict[str, str | None] = {
        str(manifest.path): manifest.path.read_text(encoding="utf-8"),
        str(lock): lock.read_text(encoding="utf-8") if lock.is_file() else None,
    }

    if manifest.kind == "uv":
        apply_uv_bumps(manifest.path, bumps)
        ok, log = uv_lock_refresh(folder)
        applied, skipped, err = (
            (bumps, [], None) if ok else ([], [], f"uv lock failed:\n{_tail(log)}")
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
            path.write_text(text, encoding="utf-8")


def _tail(log: str, lines: int = 15) -> str:
    return "\n".join(log.splitlines()[-lines:])
