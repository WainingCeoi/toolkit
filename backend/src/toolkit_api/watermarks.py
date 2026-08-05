"""On-disk staging for Watermark Remover batches, with TTL cleanup.

A batch is the working set between "upload" and "run": the normalized PNG
copies that the canvas editor, the mask endpoint and the inpainting job all
read. Files live under data/watermark/<batch_id>/ next to an in-memory index
(single-user local app; nothing here needs to survive a restart).

Two rules keep the folder honest without ever costing a user their data:

- Expiry is measured from LAST USE, not from creation, and a batch is pinned
  for the duration of a run. A correction session can take as long as it
  takes, and a long LaMa run cannot have its own inputs deleted underneath it.
- Only directories this class could itself have created are ever deleted:
  the root is derived from SUB_DB_PATH, which the user can point anywhere, so
  a blanket rmtree of it could take a real folder with it. Batch ids are
  12 hex characters and nothing else here is swept.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

BATCH_TTL_SECONDS = 6 * 60 * 60

# What create() names a batch directory — and so the only thing this class is
# ever willing to delete.
_BATCH_ID = re.compile(r"^[0-9a-f]{12}$")


class WatermarkBatches:
    def __init__(self, root: Path, ttl: float = BATCH_TTL_SECONDS) -> None:
        self.root = Path(root)
        self.ttl = ttl
        self._batches: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        # Anything batch-shaped on disk now belongs to a previous process,
        # whose index died with it — no one can reach these files again.
        for leftover in self._own_dirs():
            shutil.rmtree(leftover, ignore_errors=True)

    def _own_dirs(self) -> list[Path]:
        return [
            child
            for child in self.root.iterdir()
            if child.is_dir() and _BATCH_ID.match(child.name)
        ]

    def create(self, images: list[tuple[str, bytes, int, int]]) -> dict:
        """Store (name, png_bytes, width, height) working copies as one batch."""
        batch_id = uuid.uuid4().hex[:12]
        batch_dir = self.root / batch_id
        batch_dir.mkdir()
        entries = []
        for name, png, width, height in images:
            image_id = uuid.uuid4().hex[:8]
            path = batch_dir / f"{image_id}.png"
            path.write_bytes(png)
            entries.append(
                {
                    "id": image_id,
                    "name": name,
                    "width": width,
                    "height": height,
                    "path": path,
                }
            )
        batch = {
            "id": batch_id,
            "dir": batch_dir,
            "used": time.monotonic(),
            "pins": 0,
            "images": entries,
        }
        with self._lock:
            self._sweep()
            self._batches[batch_id] = batch
        return batch

    def get(self, batch_id: str) -> dict | None:
        with self._lock:
            self._sweep()
            batch = self._batches.get(batch_id)
            if batch is not None:
                # Expiry is measured from last use: a batch someone is still
                # working with is not idle, however long the session runs.
                batch["used"] = time.monotonic()
            return batch

    def image(self, batch_id: str, image_id: str) -> dict | None:
        batch = self.get(batch_id)
        if batch is None:
            return None
        for entry in batch["images"]:
            if entry["id"] == image_id:
                return entry
        return None

    @contextmanager
    def pin(self, batch_id: str):
        """Hold a batch on disk for the length of a run.

        The inpainting job reads each image lazily on a worker thread, so
        without this a batch could expire — and be swept by any concurrent
        request — between two images of its own run.
        """
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is not None:
                batch["pins"] += 1
        try:
            yield
        finally:
            with self._lock:
                if batch is not None:
                    batch["pins"] -= 1
                    batch["used"] = time.monotonic()

    def _sweep(self) -> None:
        # Caller holds the lock.
        cutoff = time.monotonic() - self.ttl
        stale = [
            batch_id
            for batch_id, rec in self._batches.items()
            if rec["used"] < cutoff and rec["pins"] == 0
        ]
        for batch_id in stale:
            expired = self._batches.pop(batch_id)
            shutil.rmtree(expired["dir"], ignore_errors=True)
