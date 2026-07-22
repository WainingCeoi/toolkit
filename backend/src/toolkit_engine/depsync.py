"""Bump a uv project's declared ``>=`` floors up to the versions uv resolved.

Point it at a folder holding a ``pyproject.toml``; it runs ``uv sync -U``, reads
the resolved versions out of ``uv.lock``, and rewrites only the *lagging* ``>=``
floors — leaving ``==``, ``~=``, ranges, markered deps, comments, and alignment
untouched. The rewrite is a surgical text edit (not a TOML re-serialize) so the
file's comments and formatting survive byte-for-byte.

Pure logic, no FastAPI. The router feeds ``on_message``/``is_cancelled`` in from
a Job so ``uv sync`` progress streams and the run can be cancelled.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class Bump:
    """One declared floor to raise: ``name old → new`` in ``table``."""

    name: str  # display name as parsed, e.g. "mineru"
    table: str  # e.g. "project.dependencies" or "dependency-groups.dev"
    old: str  # the old specifier clause, e.g. ">=3.4.0"
    new: str  # the new specifier clause, e.g. ">=6.14.2"
    major: bool  # True when the major version changed (a >=4 → >=6 jump)
    raw: str  # the requirement string verbatim, e.g. "mineru[core]>=3.4.0"
    raw_new: str  # the same string with only the version swapped


def bump_dict(bump: Bump) -> dict:
    """The wire shape the router returns (raw/raw_new stay server-side)."""
    return {
        "name": bump.name,
        "table": bump.table,
        "old": bump.old,
        "new": bump.new,
        "major": bump.major,
    }


def find_pyproject(folder: str) -> tuple[Path | None, str | None]:
    """Locate the ``pyproject.toml`` at the folder root. Returns (path, error)."""
    base = Path(folder).expanduser()
    if not base.is_dir():
        return None, f"❌ Not a folder: {folder}"
    path = base / "pyproject.toml"
    if not path.is_file():
        return None, f"❌ No pyproject.toml found in {folder}"
    return path, None


def uv_available() -> bool:
    return shutil.which("uv") is not None


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_uv_sync(
    folder: str,
    on_message=None,
    is_cancelled=None,
    poll: float = 0.2,
) -> tuple[bool, str]:
    """Run ``uv sync -U`` in ``folder``, streaming output lines to ``on_message``.

    A reader thread drains stdout so the loop can poll ``is_cancelled`` on a
    fixed cadence and kill the process promptly (uv's own downloads can stall
    output for seconds). Returns (ok, combined_output).
    """
    uv = shutil.which("uv")
    if uv is None:
        return False, "❌ uv is not installed or not on PATH."

    proc = subprocess.Popen(
        [uv, "sync", "-U"],
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


def resolved_versions(folder: str) -> tuple[dict[str, str], str | None]:
    """Canonical-name → version, read from the folder's ``uv.lock``.

    The lock is the authoritative resolution ``uv sync -U`` just produced, so we
    read it rather than the venv's installed metadata.
    """
    lock = Path(folder).expanduser() / "uv.lock"
    if not lock.is_file():
        return {}, f"❌ No uv.lock in {folder} (run the scan first)."
    try:
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, f"❌ Could not read uv.lock: {exc}"
    versions: dict[str, str] = {}
    for pkg in data.get("package", []):
        name, version = pkg.get("name"), pkg.get("version")
        if name and version:
            versions[canonicalize_name(name)] = version
    return versions, None


def _declared_entries(data: dict) -> list[tuple[str, str]]:
    """Every declared requirement string across the three tables we track."""
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


def _bump_for(table: str, req_str: str, resolved: dict[str, str]) -> Bump | None:
    """A Bump for one requirement, or None if it should be left alone.

    Only a single-clause ``>=`` with a resolved version strictly greater than
    the floor qualifies. Markered deps are skipped: the resolved version is
    environment-specific, so bumping their floor could be wrong off this box.
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


def compute_bumps(pyproject_path: Path, resolved: dict[str, str]) -> list[Bump]:
    """The lagging ``>=`` floors across all three dependency tables."""
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    bumps = []
    for table, req_str in _declared_entries(data):
        bump = _bump_for(table, req_str, resolved)
        if bump is not None:
            bumps.append(bump)
    return bumps


def apply_bumps(pyproject_path: Path, bumps: list[Bump]) -> None:
    """Rewrite the floors in place by swapping the exact quoted requirement
    strings — comments, alignment, and every other line stay untouched."""
    text = pyproject_path.read_text(encoding="utf-8")
    # Dedup by raw: the same requirement can appear in two tables and would
    # otherwise be looked up again after the first replace already changed it.
    replacements = {b.raw: b.raw_new for b in bumps}
    for raw, raw_new in replacements.items():
        for quote in ('"', "'"):
            needle = f"{quote}{raw}{quote}"
            if needle in text:
                text = text.replace(needle, f"{quote}{raw_new}{quote}")
                break
        else:
            raise ValueError(f"could not locate {raw!r} in {pyproject_path.name}")
    pyproject_path.write_text(text, encoding="utf-8")


def build_commit_message(bumps: list[Bump]) -> str:
    """A trailer-free ``chore(deps)`` message listing each floor moved."""
    n = len(bumps)
    plural = "" if n == 1 else "s"
    header = f"chore(deps): bump {n} dependency floor{plural} to installed versions"
    width = max((len(b.name) for b in bumps), default=0)
    body = "\n".join(
        f"- {b.name.ljust(width)}  {b.old} → {b.new}{'  (major)' if b.major else ''}"
        for b in bumps
    )
    return f"{header}\n\n{body}"


def is_git_repo(folder: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", folder, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def commit_bumps(folder: str, bumps: list[Bump]) -> tuple[str | None, str | None]:
    """Commit only ``pyproject.toml`` + ``uv.lock``. Returns (short_sha, error).

    A path-limited commit takes just those two files' working-tree content, so
    any other staged or unstaged work in the repo is neither committed nor
    touched.
    """
    base = Path(folder)
    files = [f for f in ("pyproject.toml", "uv.lock") if (base / f).is_file()]
    message = build_commit_message(bumps)
    commit = subprocess.run(
        ["git", "-C", folder, "commit", "-m", message, "--", *files],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        blob = f"{commit.stdout}\n{commit.stderr}".lower()
        if "nothing to commit" in blob or "no changes added" in blob:
            return (
                None,
                "❌ Nothing to commit — pyproject.toml and uv.lock are unchanged.",
            )
        return (
            None,
            f"❌ git commit failed: {commit.stderr.strip() or commit.stdout.strip()}",
        )
    sha = subprocess.run(
        ["git", "-C", folder, "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return sha.stdout.strip() or None, None
