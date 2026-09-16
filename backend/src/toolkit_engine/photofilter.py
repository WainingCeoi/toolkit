"""Photos Library Filter engine: mirror a macOS Photos library minus its caches."""

from __future__ import annotations

import ctypes
import functools
import os
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from scandir_rs import Scandir

DEFAULT_RULES = """\
# Photos Library Filter rules (rsync-style)
# Everything not listed here is kept. Do NOT exclude resources/renders/: it holds
# the edit recipes (UUID.plist) and rendered edits that the database expects to
# be present.
.DS_Store
# search index (Spotlight + leo.sqlite): rebuilt by Photos
database/search/
# runtime lock / WAL / SHM: the tool snapshots Photos.sqlite with the WAL folded in
database/*.lock
database/*-wal
database/*-shm
# thumbnails and previews: pure cache, rebuilt by Photos (blank thumbs until then,
# or Repair Library)
resources/derivatives/
resources/caches/
# analysis caches (scene/face/knowledge graph): rebuilt by photoanalysisd
private/**/caches/
"""


class PhotoFilterError(ValueError):
    """A run that must not start: SRC is not a library, or DEST is unsafe."""


# ---------------------------------------------------------------- rules


@dataclass(frozen=True)
class Rule:
    pattern: str
    regex: re.Pattern
    dir_only: bool


def compile_rules(lines: str | Iterable[str]) -> list[Rule]:
    """Parse an rsync-style rules file (a string, or its lines)."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    rules = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        dir_only = line.endswith("/")
        pat = line.rstrip("/")
        anchored = pat.startswith("/")
        pat = pat.lstrip("/")
        rx, i = "", 0
        while i < len(pat):
            if pat.startswith("**", i):
                rx, i = rx + ".*", i + 2
            else:
                rx += {"*": "[^/]*", "?": "[^/]"}.get(pat[i], re.escape(pat[i]))
                i += 1
        prefix = "^" if anchored else "(?:^|/)"
        rules.append(Rule(line, re.compile(prefix + rx + "$"), dir_only))
    return rules


def first_match(rules: list[Rule], relpath: str, is_dir: bool = False) -> str | None:
    """Pattern text of the first rule matching this path (or a parent dir)."""
    parts = relpath.split("/")
    ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
    for r in rules:
        candidates = ancestors if r.dir_only and not is_dir else ancestors + [relpath]
        for cand in candidates:
            if r.regex.search(cand):
                return r.pattern
    return None


# ---------------------------------------------------------------- plan


class FileStat(NamedTuple):
    size: int
    mtime_ns: int


@dataclass
class Plan:
    dirs: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    snapshot: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)  # relpath -> rule
    stat: dict[str, FileStat] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _ns(seconds: float) -> int:
    return round(seconds * 1e9)


def _join(rel_root: str, name: str) -> str:
    return name if rel_root == "." else f"{rel_root}/{name}"


def plan(src: Path | str, rules: list[Rule]) -> Plan:
    entries, errors = Scandir(str(src)).collect()
    p = Plan(errors=[f"scan failed: {error}" for error in errors])
    entries.sort(key=lambda entry: entry.path)  # parents before children
    files = {entry.path for entry in entries if not entry.is_dir}
    for entry in entries:
        rel = entry.path
        if entry.is_dir:
            # An excluded dir stays as an empty skeleton; its subdirs do not.
            parent = rel.rpartition("/")[0]
            if not parent or not first_match(rules, parent, is_dir=True):
                p.dirs.append(rel)
            continue
        if rel.endswith(("-wal", "-shm")):
            continue  # folded into the snapshot of the database next to it
        p.stat[rel] = FileStat(entry.st_size, _ns(entry.mtime))
        rule = first_match(rules, rel)
        if rule:
            p.excluded[rel] = rule
        elif rel + "-wal" in files:
            p.snapshot.append(rel)
        else:
            p.keep.append(rel)
    # scandir-rs drops a directory it cannot open without reporting an error.
    for rel in ("", *p.dirs):
        if not os.access(os.path.join(src, rel), os.R_OK | os.X_OK):
            p.errors.append(f"unreadable directory: {rel or '.'}")
    return p


# ---------------------------------------------------------------- execute


def _uri(path: Path | str, query: str) -> str:
    return Path(path).resolve().as_uri() + "?" + query


def snapshot_sqlite(src: Path | str, dest: Path | str) -> None:
    """Consistent copy of a live WAL-mode SQLite db (VACUUM INTO reads the WAL)."""
    tmp = str(dest) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(_uri(src, "mode=ro"), uri=True, timeout=30)
    try:
        conn.execute("VACUUM INTO ?", (tmp,))
    finally:
        conn.close()
    os.replace(tmp, dest)


COPYFILE_ALL = 0xF  # ACL | STAT | XATTR | DATA
COPYFILE_CLONE = 1 << 24  # APFS clone when possible, else a plain copy


@functools.cache
def _copyfile():
    # Resolved lazily: the symbol exists only on macOS and must not break import.
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.copyfile
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    return fn


def copy_file(src: Path | str, dest: Path | str) -> None:
    """Clone (or copy) one file, mtime and xattrs included."""
    if os.path.lexists(dest):
        os.remove(dest)
    flags = COPYFILE_CLONE | COPYFILE_ALL
    if _copyfile()(os.fsencode(src), os.fsencode(dest), None, flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src))


def verify(
    db_path: Path | str, exists: Callable[[str], bool]
) -> tuple[list[str], int, int]:
    """Check that every asset's original (and edit recipe, if edited) exists."""
    problems, n_assets, n_edited = [], 0, 0
    conn = sqlite3.connect(_uri(db_path, "mode=ro"), uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT ZUUID, ZDIRECTORY, ZFILENAME, ZADJUSTMENTTIMESTAMP FROM ZASSET"
        ).fetchall()
    finally:
        conn.close()
    for uuid, directory, filename, adjusted in rows:
        n_assets += 1
        original = f"originals/{directory}/{filename}"
        if not exists(original):
            problems.append(f"missing original for {uuid}: {original}")
        if adjusted is not None:
            n_edited += 1
            recipe = f"resources/renders/{uuid[0]}/{uuid}.plist"
            if not exists(recipe):
                problems.append(f"missing edit recipe for {uuid}: {recipe}")
    return problems, n_assets, n_edited


@dataclass
class Result:
    plan: Plan
    copied: int = 0
    skipped: int = 0
    snapshotted: int = 0
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # False until verify ran; a run stopped early was never checked.
    verified: bool = False
    problems: list[str] = field(default_factory=list)
    assets: int = 0
    edited: int = 0


def _same_dir(a: Path, b: Path) -> bool:
    # APFS ignores case and Unicode normalisation, so only the inode identifies a dir.
    try:
        return os.path.samestat(os.stat(a), os.stat(b))
    except OSError:
        return False


def check_paths(src: Path | str, dest: Path | str) -> tuple[Path, Path]:
    """The guards every run starts with. Returns the resolved pair."""
    src, dest = Path(src).resolve(), Path(dest).resolve()
    if not (src / "database" / "Photos.sqlite").is_file():
        raise PhotoFilterError(
            f"{src} is not a Photos library (no database/Photos.sqlite)"
        )
    if dest.suffix != ".photoslibrary":
        raise PhotoFilterError(f"DEST must be a *.photoslibrary path, got {dest}")
    if (
        _same_dir(src, dest)
        or any(_same_dir(src, up) for up in dest.parents)
        or any(_same_dir(dest, up) for up in src.parents)
    ):
        raise PhotoFilterError("SRC and DEST overlap")
    return src, dest


def _delete(remove: Callable[[str], None], path: str, rel: str, r: Result) -> None:
    try:
        remove(path)
    except OSError as e:
        r.errors.append(f"delete failed for {rel}: {e}")
    else:
        r.deleted.append(rel)


# (phase, done, total) -> True to stop; total is 0 when unknown up front.
Progress = Callable[[str, int, int], bool]

# Scanner mtimes are doubles (about 0.5 us of precision); wider gaps are real changes.
_TIME_WINDOW_NS = 2_000


def run(
    src: Path | str,
    dest: Path | str,
    rules: list[Rule],
    dry_run: bool = False,
    on_progress: Progress | None = None,
) -> Result:
    """Mirror SRC into DEST (or, dry_run, plan and verify without writing)."""
    src, dest = check_paths(src, dest)

    def stop(phase: str, done: int = 0, total: int = 0) -> bool:
        return on_progress is not None and on_progress(phase, done, total)

    if stop("plan"):
        return Result(plan=Plan())
    p = plan(src, rules)
    r = Result(plan=p, errors=list(p.errors))
    db = "database/Photos.sqlite"
    if dry_run:
        planned = set(p.keep) | set(p.snapshot)
        if stop("verify"):
            return r
        r.problems, r.assets, r.edited = verify(src / db, planned.__contains__)
        r.verified = True
        return r

    dest.mkdir(parents=True, exist_ok=True)
    for d in p.dirs:
        (dest / d).mkdir(exist_ok=True)

    for i, rel in enumerate(p.keep):
        if stop("copy", i, len(p.keep)):
            return r
        st, target = p.stat[rel], dest / rel
        try:
            ds = os.stat(target)
            if (
                ds.st_size == st.size
                and abs(ds.st_mtime_ns - st.mtime_ns) <= _TIME_WINDOW_NS
            ):
                # Stat SRC: an xattr-only edit (a favourite) bumps ctime, not mtime.
                # The scanner's "ctime" is the birth time, so it cannot be used here.
                ss = os.stat(src / rel)
                if (
                    ss.st_mtime_ns == ds.st_mtime_ns
                    and ss.st_ctime_ns <= ds.st_ctime_ns
                ):
                    r.skipped += 1
                    continue
        except FileNotFoundError:
            pass
        try:
            copy_file(src / rel, target)
        except OSError as e:
            r.errors.append(f"copy failed for {rel}: {e}")
        else:
            r.copied += 1

    # Always re-snapshotted; comparing mtimes would only save the VACUUM's seconds.
    for i, rel in enumerate(p.snapshot):
        if stop("snapshot", i, len(p.snapshot)):
            return r
        try:
            snapshot_sqlite(src / rel, dest / rel)
            r.snapshotted += 1
        except sqlite3.Error as e:
            r.errors.append(f"snapshot failed for {rel}: {e}")

    wanted_files, wanted_dirs = set(p.keep) | set(p.snapshot), set(p.dirs)
    if p.errors:
        # An incomplete scan must never prune the mirror down to what it did see.
        r.errors.append("delete skipped: the scan of SRC was incomplete")
    else:
        for root, dnames, fnames in os.walk(dest, topdown=False):
            if stop("delete", len(r.deleted)):
                r.deleted.sort()
                return r
            rel_root = os.path.relpath(root, dest)
            for f in fnames:
                rel = _join(rel_root, f)
                if rel not in wanted_files:
                    _delete(os.remove, os.path.join(root, f), rel, r)
            for d in dnames:
                rel, path = _join(rel_root, d), os.path.join(root, d)
                if os.path.islink(path):
                    # os.walk files a symlink to a dir here, and rmtree refuses one.
                    if rel not in wanted_files:
                        _delete(os.remove, path, rel, r)
                elif rel not in wanted_dirs:
                    _delete(shutil.rmtree, path, rel + "/", r)
        r.deleted.sort()

    if stop("verify"):
        return r
    r.problems, r.assets, r.edited = verify(
        dest / db, lambda rel: (dest / rel).exists()
    )
    r.verified = True
    return r


# ---------------------------------------------------------------- report


def summary(result: Result, rules: list[Rule]) -> dict:
    """The run as JSON-safe data: sizes per rule, counts, the verify outcome."""
    p = result.plan

    def size(rels: Iterable[str]) -> int:
        return sum(p.stat[rel].size for rel in rels)

    by_rule: dict[str, list[str]] = {r.pattern: [] for r in rules}
    for rel, pattern in p.excluded.items():
        by_rule.setdefault(pattern, []).append(rel)
    rows = [
        {"rule": pattern, "files": len(rels), "bytes": size(rels)}
        for pattern, rels in by_rule.items()
    ]
    rows.sort(key=lambda row: -row["bytes"])
    return {
        "kept": {"files": len(p.keep), "bytes": size(p.keep)},
        "excluded": {"files": len(p.excluded), "bytes": size(p.excluded)},
        "rules": rows,
        "snapshots": list(p.snapshot),
        "copied": result.copied,
        "skipped": result.skipped,
        "snapshotted": result.snapshotted,
        "deleted": list(result.deleted),
        "errors": list(result.errors),
        "verify": {
            "ran": result.verified,
            "assets": result.assets,
            "edited": result.edited,
            "problems": list(result.problems),
        },
    }
