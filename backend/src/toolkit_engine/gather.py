"""File Gatherer engine: preset globs, recursive scan, move-with-renumber."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from scandir_rs import Scandir

from .fsutil import natural_sort_key

# File-type presets -> scandir_rs file_include glob patterns
FILE_TYPE_PRESETS = {
    "Video": [
        "*.mkv",
        "*.mp4",
        "*.mov",
        "*.ts",
        "*.flv",
        "*.avi",
        "*.webm",
        "*.m4v",
        "*.wmv",
        "*.mpg",
        "*.mpeg",
    ],
    "Audio": ["*.mp3", "*.flac", "*.aac", "*.wav", "*.m4a", "*.ogg", "*.opus", "*.wma"],
    "Image": [
        "*.jpg",
        "*.jpeg",
        "*.png",
        "*.gif",
        "*.heic",
        "*.webp",
        "*.bmp",
        "*.tiff",
    ],
    "Subtitle": ["*.srt", "*.ass", "*.ssa", "*.sub", "*.vtt"],
    "Document": ["*.pdf", "*.docx", "*.doc", "*.txt", "*.epub", "*.pptx", "*.xlsx"],
    "Archive": ["*.zip", "*.rar", "*.7z", "*.tar", "*.gz"],
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
    """Assemble the dedup-sorted glob list exactly as the page did."""
    patterns = []
    for category in categories:
        patterns.extend(FILE_TYPE_PRESETS[category])
    for token in custom_raw.replace(",", " ").split():
        pattern = normalize_pattern(token)
        if pattern:
            patterns.append(pattern)
    return sorted(set(patterns))


def scan_source(src: Path, patterns: list[str]) -> tuple[list[str], list]:
    """Recursively find matching files under `src`, natural-sorted by name.

    Returns (file paths, scan errors) — an error means part of the tree was
    unreadable, so the gather may be incomplete.
    """
    scanner, errors = Scandir(str(src), file_include=patterns).collect()
    files = [str(src / entry.path) for entry in scanner if entry.is_file]
    files.sort(key=lambda p: natural_sort_key(Path(p).name))
    return files, list(errors) if errors else []


def move_files(
    files: list[str],
    tgt: Path,
    on_progress: Callable[[int, int], bool] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Move files into `tgt`, auto-numbering duplicate names (stem_1, stem_2…).

    Returns (moved names, failed (name, error) pairs). `on_progress(done,
    total)` is called after each file; returning True stops the run early
    (cancellation).
    """
    total = len(files)
    moved, failed = [], []
    for idx, file_path in enumerate(files, start=1):
        file = Path(file_path)
        try:
            target_path = tgt / file.name
            # Handle duplicated files
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
