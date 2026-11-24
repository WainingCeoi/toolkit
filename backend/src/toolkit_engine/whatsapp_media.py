"""Copy message attachments referenced by the native WhatsApp store."""

from __future__ import annotations

import os
import re
import shutil
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, TypedDict
from zipfile import ZipFile

_MEDIA_DIRECTORIES = (Path("Message/Media"), Path("Media"), Path("stickers"))
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW


class MediaExport(TypedDict, total=False):
    attachment_path: str
    attachment_name: str
    attachment_status: str
    thumbnail_path: str
    thumbnail_status: str


def safe_export_name(name: str, suffix: str = "") -> str:
    """Use the name as one filesystem component, keeping any supplied suffix."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name)
    budget = 240 - len(suffix.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").strip(" .")
    return f"{stem or 'WhatsApp Chat'}{suffix}"


def _media_paths(root: Path, raw: str) -> tuple[Path, ...]:
    if not isinstance(raw, str) or "\0" in raw or "\\" in raw:
        return ()
    path = Path(raw)
    if not path.parts or ".." in path.parts:
        return ()
    if path.is_absolute():
        if not path.is_relative_to(root):
            return ()
        candidates = (path.relative_to(root),)
    elif path.parts[0] == "Media":
        # Native ZMEDIALOCALPATH values start with Media/, relative to Message/.
        candidates = (Path("Message") / path, path)
    else:
        candidates = (path,)
    return tuple(
        candidate
        for candidate in candidates
        if any(candidate.is_relative_to(directory) for directory in _MEDIA_DIRECTORIES)
    )


def _open_regular(root: Path, relative: Path) -> BinaryIO | None:
    # Open each component relative to a held descriptor. A symlink swap cannot
    # redirect the read outside the allowed directory while WhatsApp is open.
    with ExitStack() as stack:
        directory = os.open(root, _DIRECTORY_FLAGS)
        stack.callback(os.close, directory)
        for component in relative.parts[:-1]:
            directory = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)

        fd = os.open(relative.name, _FILE_FLAGS, dir_fd=directory)
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                stream = os.fdopen(fd, "rb")
                fd = -1  # The stream now owns the descriptor.
                return stream
        finally:
            if fd >= 0:
                os.close(fd)
    return None


def _open_media(root: Path, raw: str | None) -> BinaryIO | None:
    if not raw:
        return None
    resolved_root = root.resolve()
    candidates = _media_paths(root, raw) or _media_paths(resolved_root, raw)
    for relative in candidates:
        try:
            if stream := _open_regular(resolved_root, relative):
                return stream
        except OSError:
            continue
    return None


def _local_name(raw: str | None) -> str | None:
    return Path(raw.replace("\\", "/")).name if isinstance(raw, str) else None


def _attachment_name(local_path: str | None, title: str | None) -> str | None:
    source = _local_name(local_path)
    if not title or re.search(r"[/\\\x00-\x1f\x7f]", title):
        return source
    extension = Path(source).suffix if source else ""
    if extension and Path(title).suffix.casefold() != extension.casefold():
        return f"{Path(title).stem}{extension}"
    return title


def _member_name(name: str, row_id: int) -> str:
    path = Path(name)
    extension = path.suffix
    if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", extension):
        return safe_export_name(f"{row_id}-{path.stem}", extension)
    return safe_export_name(f"{row_id}-{name}")


def _thumbnail_extension(source: BinaryIO, thumbnail_path: str) -> str:
    header = source.read(12)
    source.seek(0)
    if header.startswith(b"\xff\xd8"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    extension = Path(thumbnail_path).suffix
    return extension if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", extension) else ".bin"


def _copy_member(
    archive: ZipFile, chat_folder: str, filename: str, source: BinaryIO
) -> str:
    relative = f"media/{filename}"
    with archive.open(f"{chat_folder}/{relative}", "w", force_zip64=True) as member:
        shutil.copyfileobj(source, member, length=1024 * 1024)
    return relative


def archive_media(
    archive: ZipFile | None,
    *,
    db_path: Path,
    chat_folder: str,
    row_id: int,
    local_path: str | None,
    title: str | None,
    expected: bool,
    thumbnail_path: str | None = None,
) -> MediaExport:
    """Return record fields and link to attachment files written into the ZIP."""
    root = db_path.expanduser().absolute().parent
    fields: MediaExport = {}
    if expected:
        name = _attachment_name(local_path, title)
        if name:
            fields["attachment_name"] = name
        fields["attachment_status"] = "missing_local"
        if source := _open_media(root, local_path):
            with source:
                fields["attachment_status"] = "available_locally"
                if archive is not None:
                    filename = _member_name(name or "attachment", row_id)
                    fields["attachment_path"] = _copy_member(
                        archive, chat_folder, filename, source
                    )
                    fields["attachment_status"] = "exported"

    if thumbnail_path:
        fields["thumbnail_status"] = "missing_local"
        if source := _open_media(root, thumbnail_path):
            with source:
                fields["thumbnail_status"] = "available_locally"
                if archive is not None:
                    extension = _thumbnail_extension(source, thumbnail_path)
                    filename = f"thumbnail-{row_id}{extension}"
                    fields["thumbnail_path"] = _copy_member(
                        archive, chat_folder, filename, source
                    )
                    fields["thumbnail_status"] = "exported"
    return fields
