"""Bounded reading of uploaded files.

Starlette spools uploads over 1 MB to disk; ``upload.file.read()`` pulls them
back into RAM. With no ceiling, one oversized upload can OOM the process before
any tool runs. read_uploads() reads each file in chunks against a shared budget
and rejects the batch (413) the moment it exceeds the cap.
"""

from __future__ import annotations

from fastapi import HTTPException, UploadFile

# Generous per-request ceiling for a personal tool (docs/images), but a real
# bound so a giant or malicious upload can't exhaust memory.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_CHUNK = 1024 * 1024


def read_uploads(
    files: list[UploadFile], max_total: int = MAX_UPLOAD_BYTES
) -> list[bytes]:
    """Read every upload fully, capping the combined size at ``max_total``.

    Raises HTTPException(413) as soon as the running total would exceed the cap,
    so an oversized upload is refused before it is read all the way into memory.
    """
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
