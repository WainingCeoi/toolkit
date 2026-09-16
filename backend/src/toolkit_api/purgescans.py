"""Server-side record of each Cache Purge scan; delete takes a scan id, not paths."""

from __future__ import annotations

import threading
import time
import uuid

SCAN_TTL_SECONDS = 30 * 60
MAX_SCANS = 32


class PurgeScans:
    def __init__(self, ttl: float = SCAN_TTL_SECONDS, max_scans: int = MAX_SCANS):
        self._scans: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.ttl = ttl
        self.max_scans = max_scans

    def put(self, folder: str, files: list[str]) -> str:
        scan_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._sweep()
            self._scans[scan_id] = {
                "folder": folder,
                "files": list(files),
                "created": time.monotonic(),
            }
            while len(self._scans) > self.max_scans:
                self._scans.pop(next(iter(self._scans)))
        return scan_id

    def take(self, scan_id: str) -> dict | None:
        """Consume a scan (single-use); None if unknown or expired."""
        with self._lock:
            self._sweep()
            return self._scans.pop(scan_id, None)

    def _sweep(self) -> None:
        """Drop scans past the TTL. Caller holds the lock."""
        cutoff = time.monotonic() - self.ttl
        for scan_id in [k for k, v in self._scans.items() if v["created"] < cutoff]:
            del self._scans[scan_id]
