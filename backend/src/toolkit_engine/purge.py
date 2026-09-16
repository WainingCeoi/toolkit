"""Cache Purge engine: safe pattern parsing, scan with sizes, parallel delete."""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scandir_rs import Scandir

from .fsutil import natural_sort_key

DEFAULT_CACHE_TYPES = ["*.dwl", "*.dwl2", "*.bak", "*.log", "*.db", "*.tmp", "*.err"]

# Deletes are submitted a chunk at a time; a cancel can only land between chunks.
_DELETE_CHUNK = 16

_GLOB_CHARS = frozenset("*?[]{}")
# Whole bracket expression: it matches one arbitrary char, so `*[!/]*` is a catch-all.
_BRACKET = re.compile(r"\[!?\]?[^]]*\]")


def normalize_pattern(token):
    """User token -> glob ('bak' -> '*.bak'); None for a catch-all pattern."""
    token = token.strip()
    if not token:
        return None
    if not _GLOB_CHARS.intersection(token):
        return f"*.{token.lstrip('.')}"
    # Reject by what survives, not by a deny-list of spellings ('?*', '*[!/]*', ...).
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
    """Recursively find matching files under `src`, natural-sorted by name."""
    scanner, errors = Scandir(str(src), file_include=patterns).collect()
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
    """Delete files in a thread pool; deletion is permanent."""
    deleted, failed = [], []
    total = len(paths)
    done = 0
    stop = False
    with ThreadPoolExecutor() as executor:
        for start in range(0, total, _DELETE_CHUNK):
            chunk = paths[start : start + _DELETE_CHUNK]
            for path, error in executor.map(delete_file, chunk):
                done += 1
                if error is None:
                    deleted.append(path)
                else:
                    failed.append((path, error))
                if on_progress is not None and on_progress(done, total):
                    stop = True
            if stop:
                break
    return deleted, failed
