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
