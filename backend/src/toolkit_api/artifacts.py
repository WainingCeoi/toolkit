"""Temp-file store for job outputs (zips, PDFs) served by /api/artifacts.

Jobs write their output files into one spool directory per process; the
download endpoint streams them back by id. Everything is deleted when the
app shuts down — artifacts are session-scoped, matching the old UI where
results lived in st.session_state.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import uuid
from pathlib import Path


class ArtifactStore:
    def __init__(self):
        self._dir = Path(tempfile.mkdtemp(prefix="toolkit_artifacts_"))
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put_bytes(self, filename: str, content: bytes, media_type: str) -> str:
        """Store raw bytes under a fresh id; returns the artifact id."""
        artifact_id = uuid.uuid4().hex[:12]
        path = self._dir / f"{artifact_id}_{filename}"
        path.write_bytes(content)
        with self._lock:
            self._items[artifact_id] = {
                "path": path,
                "filename": filename,
                "media_type": media_type,
            }
        return artifact_id

    def replace_bytes(self, artifact_id: str, content: bytes) -> None:
        """Overwrite an artifact's content in place, keeping its id and name.

        For results that grow as a job runs -- a zip republished after every
        finished image so a batch that dies still hands over what it got. The
        write goes to a sibling temp file first and lands with os.replace, so a
        download racing the update streams a complete old zip or a complete
        new one, never a torn file.
        """
        with self._lock:
            item = self._items.get(artifact_id)
        if item is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        staging = item["path"].with_suffix(".staging")
        staging.write_bytes(content)
        staging.replace(item["path"])

    def put_file(self, filename: str, src: Path, media_type: str) -> str:
        """Move an existing file (e.g. from a job's tempdir) into the store."""
        artifact_id = uuid.uuid4().hex[:12]
        path = self._dir / f"{artifact_id}_{filename}"
        shutil.move(str(src), path)
        with self._lock:
            self._items[artifact_id] = {
                "path": path,
                "filename": filename,
                "media_type": media_type,
            }
        return artifact_id

    def get(self, artifact_id: str) -> dict | None:
        with self._lock:
            return self._items.get(artifact_id)

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
