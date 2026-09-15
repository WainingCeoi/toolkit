"""Photos Library Filter engine: mirror a macOS Photos library minus its caches.

SRC.photoslibrary -> DEST.photoslibrary, safe to take while Photos is running:

1. plan     -- walk SRC and classify every file with rsync-style rules (see
               compile_rules). Every directory is mirrored; an excluded
               directory stays as an empty skeleton so the bundle keeps its
               shape and Photos can still open the mirror.
2. snapshot -- any file with a "<name>-wal" sibling is a live WAL-mode SQLite
               database (database/Photos.sqlite, the analysis databases). It is
               written with VACUUM INTO from a read-only connection, so the copy
               is complete even mid-write; -wal/-shm files are never copied.
3. copy     -- copyfile(3) with COPYFILE_CLONE: an APFS clone (instant, no
               extra space on the same volume) that keeps mtime and the
               com.apple.assetsd.* extended attributes Photos stores on
               originals. Unchanged files are skipped.
4. delete   -- anything in DEST that is not in the plan is removed.
5. verify   -- DEST's Photos.sqlite is opened read-only and every asset's
               original (and edit recipe, if it was edited) is checked for.

Standard library only; macOS only (copyfile). Nothing here ever writes into
SRC: it is read, walked and stat'ed, and its database is read through a
read-only connection.
"""

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

# The shipped rules. Everything not listed is kept. Never exclude
# resources/renders/: it is not a cache -- it holds the edit recipes
# (UUID.plist) and rendered edits that the database expects to be present.
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
    """Parse an rsync-style rules file (a string, or its lines).

    Blank lines and '#' comments are ignored; a trailing '/' matches
    directories (and everything inside); a leading '/' anchors to the library
    root, otherwise the pattern matches whole path components at any depth;
    '*' and '?' stay inside one component, '**' crosses components.
    """
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


@dataclass
class Plan:
    dirs: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    snapshot: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)  # relpath -> rule
    stat: dict[str, os.stat_result] = field(default_factory=dict)


def _join(rel_root: str, name: str) -> str:
    return name if rel_root == "." else f"{rel_root}/{name}"


def plan(src: Path | str, rules: list[Rule]) -> Plan:
    p = Plan()
    for root, dnames, fnames in os.walk(src):
        dnames.sort()
        rel_root = os.path.relpath(root, src)
        if rel_root == "." or not first_match(rules, rel_root, is_dir=True):
            # An excluded dir stays as an empty skeleton; its subdirs do not.
            p.dirs += [_join(rel_root, d) for d in dnames]
        for f in sorted(fnames):
            if f.endswith(("-wal", "-shm")):
                continue  # folded into the snapshot of the database next to it
            rel = _join(rel_root, f)
            p.stat[rel] = os.stat(os.path.join(root, f))
            rule = first_match(rules, rel)
            if rule:
                p.excluded[rel] = rule
            elif os.path.exists(os.path.join(root, f + "-wal")):
                p.snapshot.append(rel)
            else:
                p.keep.append(rel)
    return p


# ---------------------------------------------------------------- execute


def _uri(path: Path | str, query: str) -> str:
    return Path(path).resolve().as_uri() + "?" + query


def snapshot_sqlite(src: Path | str, dest: Path | str) -> None:
    """Consistent copy of a (possibly live, WAL-mode) SQLite database.

    VACUUM INTO reads through the WAL, so rows committed but not yet
    checkpointed land in the copy -- the thing a raw file copy without -wal
    loses. Written to a .tmp sibling first, so a failure never leaves a
    half-written database under the real name.
    """
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
    # Resolved on first use, not at import: the symbol only exists on macOS,
    # and an import-time lookup would take the whole API down elsewhere.
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.copyfile
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint32]
    fn.restype = ctypes.c_int
    return fn


def copy_file(src: Path | str, dest: Path | str, st: os.stat_result) -> None:
    if os.path.lexists(dest):
        os.remove(dest)
    flags = COPYFILE_CLONE | COPYFILE_ALL
    if _copyfile()(os.fsencode(src), os.fsencode(dest), None, flags) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src))
    # Exact mtime, so the next run can recognise the copy and skip it.
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))


def verify(
    db_path: Path | str, exists: Callable[[str], bool]
) -> tuple[list[str], int, int]:
    """Check that every asset's original (and edit recipe, if edited) exists.

    Returns (problems, asset count, edited count). `exists` answers for a
    library-relative path, so the same check runs against DEST on disk and
    against a plan that was never written.
    """
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
    # False until the verify step ran -- a run stopped early has not been
    # checked, which is not the same as having been checked and found clean.
    verified: bool = False
    problems: list[str] = field(default_factory=list)
    assets: int = 0
    edited: int = 0


def check_paths(src: Path | str, dest: Path | str) -> tuple[Path, Path]:
    """The guards every run starts with. Returns the resolved pair."""
    src, dest = Path(src).resolve(), Path(dest).resolve()
    if not (src / "database" / "Photos.sqlite").is_file():
        raise PhotoFilterError(
            f"{src} is not a Photos library (no database/Photos.sqlite)"
        )
    if dest.suffix != ".photoslibrary":
        raise PhotoFilterError(f"DEST must be a *.photoslibrary path, got {dest}")
    if src == dest or src in dest.parents or dest in src.parents:
        raise PhotoFilterError("SRC and DEST overlap")
    return src, dest


# (phase, done, total) -> True to stop as soon as it is safe. total is 0 when
# it is not known up front (deleting walks DEST as it goes).
Progress = Callable[[str, int, int], bool]


def run(
    src: Path | str,
    dest: Path | str,
    rules: list[Rule],
    dry_run: bool = False,
    on_progress: Progress | None = None,
) -> Result:
    """Mirror SRC into DEST (or, dry_run, plan and verify without writing).

    A stop requested through `on_progress` returns the partial Result with
    `verified` False. A file that fails to copy is recorded in `errors` and
    the run carries on -- a photo deleted in Photos mid-run must not abandon
    the other hundred thousand -- and verify then reports it as missing.
    """
    src, dest = check_paths(src, dest)

    def stop(phase: str, done: int = 0, total: int = 0) -> bool:
        return on_progress is not None and on_progress(phase, done, total)

    stop("plan")
    p = plan(src, rules)
    r = Result(plan=p)
    db = "database/Photos.sqlite"
    if dry_run:
        planned = set(p.keep) | set(p.snapshot)
        stop("verify")
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
            # Same size and mtime, and the source inode untouched since the copy
            # was written: Photos rewrites xattrs such as assetsd.favorite
            # without changing mtime, and that bumps ctime, so mtime alone
            # would keep a stale favourite flag forever.
            if (
                ds.st_size == st.st_size
                and ds.st_mtime_ns == st.st_mtime_ns
                and st.st_ctime_ns <= ds.st_ctime_ns
            ):
                r.skipped += 1
                continue
        except FileNotFoundError:
            pass
        try:
            copy_file(src / rel, target, st)
        except OSError as e:
            r.errors.append(f"copy failed for {rel}: {e}")
        else:
            r.copied += 1

    # Always re-snapshotted: a database is one file, and comparing its mtime
    # against the WAL's would only save the seconds VACUUM INTO takes.
    for i, rel in enumerate(p.snapshot):
        if stop("snapshot", i, len(p.snapshot)):
            return r
        try:
            snapshot_sqlite(src / rel, dest / rel)
            r.snapshotted += 1
        except sqlite3.Error as e:
            r.errors.append(f"snapshot failed for {rel}: {e}")

    wanted_files, wanted_dirs = set(p.keep) | set(p.snapshot), set(p.dirs)
    for root, dnames, fnames in os.walk(dest, topdown=False):
        if stop("delete", len(r.deleted)):
            r.deleted.sort()
            return r
        rel_root = os.path.relpath(root, dest)
        for f in fnames:
            rel = _join(rel_root, f)
            if rel not in wanted_files:
                os.remove(os.path.join(root, f))
                r.deleted.append(rel)
        for d in dnames:
            rel = _join(rel_root, d)
            if rel not in wanted_dirs:
                shutil.rmtree(os.path.join(root, d))
                r.deleted.append(rel + "/")
    r.deleted.sort()

    stop("verify")
    r.problems, r.assets, r.edited = verify(
        dest / db, lambda rel: (dest / rel).exists()
    )
    r.verified = True
    return r


# ---------------------------------------------------------------- report


def summary(result: Result, rules: list[Rule]) -> dict:
    """The run as JSON-safe data: sizes per rule, counts, the verify outcome.

    Every rule is listed, including one that matched nothing -- a rule that
    saves no bytes is worth seeing -- ordered by bytes saved, ties (the zeros)
    in file order.
    """
    p = result.plan

    def size(rels: Iterable[str]) -> int:
        return sum(p.stat[rel].st_size for rel in rels)

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
