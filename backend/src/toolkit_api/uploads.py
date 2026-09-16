"""Bounded upload reading; an uncapped ``upload.file.read()`` can OOM the process."""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_CHUNK = 1024 * 1024


def read_uploads(
    files: list[UploadFile], max_total: int = MAX_UPLOAD_BYTES
) -> list[bytes]:
    """Read every upload fully; 413 as soon as the total exceeds ``max_total``."""
    contents: list[bytes] = []
    remaining = max_total
    for upload in files:
        chunks: list[bytes] = []
        while True:
            chunk = upload.file.read(_CHUNK)
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "❌ Upload too large "
                        f"(limit {max_total // (1024 * 1024)} MB total)."
                    ),
                )
            chunks.append(chunk)
        contents.append(b"".join(chunks))
    return contents
