"""Temp-file store for job outputs (zips, PDFs) served by /api/artifacts.

Jobs write their output files into one spool directory per process; the
download endpoint streams them back by id. Everything is deleted when the
app shuts down — artifacts are session-scoped, matching the old UI where
results lived in st.session_state.

"Session" is not always short, though: `make host` is meant to be left up for
weeks as a LAN service, and there the store used to only ever grow. Files also
outlive their reachability — the job registry keeps the last 50 jobs, and an
artifact id only ever travels inside its job's snapshot, so an evicted job
leaves a file no client can ask for again. Hence the TTL sweep below, the same
shape WatermarkBatches already uses for its staging directories.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

ARTIFACT_TTL_SECONDS = 6 * 60 * 60


class ArtifactStore:
    def __init__(self, ttl: float = ARTIFACT_TTL_SECONDS):
        self._dir = Path(tempfile.mkdtemp(prefix="toolkit_artifacts_"))
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.ttl = ttl

    def put_bytes(self, filename: str, content: bytes, media_type: str) -> str:
        """Store raw bytes under a fresh id; returns the artifact id."""
        artifact_id = uuid.uuid4().hex[:12]
        path = self._dir / f"{artifact_id}_{filename}"
        path.write_bytes(content)
        with self._lock:
            self._items[artifact_id] = self._record(path, filename, media_type)
            self._sweep()
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
            if item is not None:
                item["used"] = time.monotonic()  # still being written to
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
            self._items[artifact_id] = self._record(path, filename, media_type)
            self._sweep()
        return artifact_id

    def get(self, artifact_id: str) -> dict | None:
        with self._lock:
            item = self._items.get(artifact_id)
            if item is not None:
                # Downloading counts as use: a result someone keeps coming back
                # to should not expire out from under them mid-session.
                item["used"] = time.monotonic()
            self._sweep()
            return item

    @staticmethod
    def _record(path: Path, filename: str, media_type: str) -> dict:
        return {
            "path": path,
            "filename": filename,
            "media_type": media_type,
            "used": time.monotonic(),
        }

    def _sweep(self) -> None:
        """Drop artifacts untouched for longer than the TTL. Holds the lock."""
        cutoff = time.monotonic() - self.ttl
        stale = [
            artifact_id
            for artifact_id, item in self._items.items()
            if item["used"] < cutoff
        ]
        for artifact_id in stale:
            expired = self._items.pop(artifact_id)
            expired["path"].unlink(missing_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
