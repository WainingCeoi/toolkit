"""File Gatherer engine: preset globs, recursive scan, move-with-renumber."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from scandir_rs import Scandir

from .filetypes import CATEGORY_EXTENSIONS
from .fsutil import natural_sort_key

# Derived from the shared table so the presets cannot drift from it.
FILE_TYPE_PRESETS = {
    name.capitalize(): sorted(f"*{ext}" for ext in extensions)
    for name, extensions in CATEGORY_EXTENSIONS.items()
}


def normalize_pattern(token):
    """Turn a user token into a glob: 'srt'/'.srt' -> '*.srt'; keep real globs."""
    token = token.strip()
    if not token:
        return None
    if "*" in token or "?" in token:
        return token
    return f"*.{token.lstrip('.')}"


def build_patterns(categories: list[str], custom_raw: str) -> list[str]:
    """Assemble the dedup-sorted glob list from presets and custom tokens."""
    patterns = []
    for category in categories:
        if category not in FILE_TYPE_PRESETS:
            raise ValueError(f"❌ Unknown file type: {category}")
        patterns.extend(FILE_TYPE_PRESETS[category])
    for token in custom_raw.replace(",", " ").split():
        pattern = normalize_pattern(token)
        if pattern:
            patterns.append(pattern)
    return sorted(set(patterns))


def scan_source(src: Path, patterns: list[str]) -> tuple[list[str], list]:
    """Recursively find matching files under `src`, natural-sorted by name."""
    scanner, errors = Scandir(str(src), file_include=patterns).collect()
    files = [str(src / entry.path) for entry in scanner if entry.is_file]
    files.sort(key=lambda p: natural_sort_key(Path(p).name))
    return files, list(errors) if errors else []


def move_files(
    files: list[str],
    tgt: Path,
    on_progress: Callable[[int, int], bool] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Move files into `tgt`, auto-numbering duplicate names (stem_1, stem_2…)."""
    total = len(files)
    moved, failed = [], []
    for idx, file_path in enumerate(files, start=1):
        file = Path(file_path)
        try:
            target_path = tgt / file.name
            counter = 1
            while target_path.exists():
                target_path = tgt / f"{file.stem}_{counter}{file.suffix}"
                counter += 1
            shutil.move(str(file), str(target_path))
            moved.append(file.name)
        except Exception as e:
            failed.append((file.name, str(e)))
        if on_progress is not None and on_progress(idx, total):
            break
    return moved, failed
