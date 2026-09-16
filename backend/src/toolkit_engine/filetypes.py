"""Extension -> category sets shared across tools."""

from __future__ import annotations

from pathlib import Path

VIDEO_EXTENSIONS = frozenset(
    {
        ".mkv",
        ".mp4",
        ".mov",
        ".avi",
        ".ts",
        ".m2ts",
        ".webm",
        ".flv",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".m4v",
    }
)
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".flac", ".aac", ".wav", ".m4a", ".ogg", ".opus", ".wma"}
)
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp", ".bmp", ".tiff"}
)
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".sub", ".vtt"})
DOCUMENT_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".doc", ".txt", ".epub", ".pptx", ".xlsx", ".nfo"}
)
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"})

CATEGORY_EXTENSIONS: dict[str, frozenset[str]] = {
    "video": VIDEO_EXTENSIONS,
    "audio": AUDIO_EXTENSIONS,
    "image": IMAGE_EXTENSIONS,
    "subtitle": SUBTITLE_EXTENSIONS,
    "document": DOCUMENT_EXTENSIONS,
    "archive": ARCHIVE_EXTENSIONS,
}

# Only these get the minimum-size filter; a floor would drop every subtitle/.nfo.
SIZED_CATEGORIES = frozenset({"video", "audio"})

CATEGORY_NAMES = (*CATEGORY_EXTENSIONS, "other")


def categorize(path: str) -> str:
    """Category name for a path, or "other" when no set claims its suffix."""
    suffix = Path(path).suffix.lower()
    for name, extensions in CATEGORY_EXTENSIONS.items():
        if suffix in extensions:
            return name
    return "other"
