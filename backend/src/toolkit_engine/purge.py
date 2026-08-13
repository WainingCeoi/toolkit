"""Cache Purge engine: safe pattern parsing, scan with sizes, parallel delete."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scandir_rs import Scandir

from .fsutil import natural_sort_key

DEFAULT_CACHE_TYPES = ["*.dwl", "*.dwl2", "*.bak", "*.log", "*.db", "*.tmp", "*.err"]

# Every character that starts a glob construct rather than matching itself.
_GLOB_CHARS = frozenset("*?[]{}")
# A bracket expression: [abc], [!0-9], [a-z], and []abc] where a leading ] is
# literal. Matched as a whole because the whole thing selects one arbitrary
# character -- `[!/]` excludes only a separator, so `*[!/]*` takes everything.
_BRACKET = re.compile(r"\[!?\]?[^]]*\]")


def normalize_pattern(token):
    """Turn a user token into a glob: 'bak'/'.bak' -> '*.bak'; keep real globs.

    Catch-all patterns that would match every file are rejected (return None)
    so a stray '*' can't wipe out the whole folder.

    The test is what a name would still have to CONTAIN once every construct
    that matches arbitrary text is taken away. Nothing left means the pattern
    selects the entire tree, whatever spelling was used to get there.

    This has been got wrong twice, in the same direction both times. First an
    enumerated deny-list, which missed '?*' and '*?' and '*.???'. Then stripping
    only '*', '?' and '.', which missed bracket expressions -- '*[!/]*' reads
    like a constraint and matches every file on the disk. Hence a rule about
    what survives rather than a list of what to catch.
    """
    token = token.strip()
    if not token:
        return None
    if not _GLOB_CHARS.intersection(token):
        return f"*.{token.lstrip('.')}"  # a bare extension
    residue = _BRACKET.sub("", token).strip("*?.{},")
    return token if residue else None


def delete_file(file_path):
    """Delete one file; return (path, None) on success or (path, error)."""
    try:
        Path(file_path).unlink()
        return (file_path, None)
    except Exception as e:
        return (file_path, str(e))


def parse_patterns(raw: str) -> tuple[list[str], list[str]]:
    """Split raw user input into (dedup-sorted globs, rejected catch-alls)."""
    patterns = []
    rejected = []
    for token in raw.replace(",", " ").split():
        pattern = normalize_pattern(token)
        if pattern:
            patterns.append(pattern)
        else:
            rejected.append(token)
    return sorted(set(patterns)), rejected


def scan_folder(src: Path, patterns: list[str]) -> tuple[list[str], list, int]:
    """Recursively find matching files under `src`, natural-sorted by name.

    Returns (file paths, scan errors, total size in bytes). Files whose size
    can't be read still count as matches; they just add 0 bytes.
    """
    scanner, errors = Scandir(str(src), file_include=patterns).collect()
    # Take sizes from the walk's own metadata instead of re-stat()ing every
    # match — one pass over the tree instead of two.
    entries = [
        (str(src / entry.path), entry.st_size) for entry in scanner if entry.is_file
    ]
    entries.sort(key=lambda item: natural_sort_key(Path(item[0]).name))
    found = [path for path, _ in entries]
    total_size = sum(size for _, size in entries)
    return found, list(errors) if errors else [], total_size


def delete_files(
    paths: list[str],
    on_progress: Callable[[int, int], bool] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Delete files in a thread pool; deletion is permanent.

    Returns (deleted paths, failed (path, error) pairs). `on_progress(done,
    total)` is called after each result; returning True stops collecting
    early (cancellation is best-effort — already-submitted deletes finish).
    """
    deleted, failed = [], []
    total = len(paths)
    with ThreadPoolExecutor() as executor:
        for idx, (path, error) in enumerate(executor.map(delete_file, paths), start=1):
            if error is None:
                deleted.append(path)
            else:
                failed.append((path, error))
            if on_progress is not None and on_progress(idx, total):
                break
    return deleted, failed
