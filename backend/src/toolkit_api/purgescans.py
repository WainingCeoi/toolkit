"""Server-side record of what a Cache Purge scan actually found.

Delete used to take the folder *and* the file list from the client and confine
one against the other. Both came from the same request, so the confinement was
satisfied by construction — `folder: "/"` let every absolute path through, and
nothing required the paths to have come from a scan at all. The extension
filtering only ever existed in /scan.

So the list stops being something the client can state. A scan records its
result here and hands back an opaque id; delete takes the id and removes
exactly the recorded paths. The client chooses *which scan* to act on, never
*which files*.

Entries expire: a preview that has been sitting around is no longer a
description of the disk, and deleting against it would destroy contents nobody
reviewed. Re-scanning is cheap, so an expired id asks for one rather than
guessing.
"""

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
            # Oldest-first bound, so a client that scans in a loop can't grow
            # this without limit.
            while len(self._scans) > self.max_scans:
                self._scans.pop(next(iter(self._scans)))
        return scan_id

    def take(self, scan_id: str) -> dict | None:
        """Consume a scan. Returns None if it is unknown or expired.

        Single-use on purpose: once its files are deleted the record describes
        a state of the disk that no longer exists, so a repeat delete has
        nothing to say. A retry after a partial failure re-scans.
        """
        with self._lock:
            self._sweep()
            return self._scans.pop(scan_id, None)

    def _sweep(self) -> None:
        """Drop scans past the TTL. Caller holds the lock."""
        cutoff = time.monotonic() - self.ttl
        for scan_id in [k for k, v in self._scans.items() if v["created"] < cutoff]:
            del self._scans[scan_id]
