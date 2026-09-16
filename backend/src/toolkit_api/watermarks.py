"""On-disk staging for Watermark Remover batches, with TTL cleanup."""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

BATCH_TTL_SECONDS = 6 * 60 * 60

# Only directories matching this are ever deleted; root may be a user folder.
_BATCH_ID = re.compile(r"^[0-9a-f]{12}$")


class WatermarkBatches:
    def __init__(self, root: Path, ttl: float = BATCH_TTL_SECONDS) -> None:
        self.root = Path(root)
        self.ttl = ttl
        self._batches: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        # Leftovers from a previous process; their index died with it.
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
                # Expiry counts from last use.
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

    def marks(self, batch_id: str, collect: Callable[[list[Path]], list]) -> list:
        """The batch's watermark marks, collected once then cached."""
        batch = self.get(batch_id)
        if batch is None:
            return []
        # Outside the lock: collection takes seconds, and a double compute is harmless.
        if batch.get("marks") is None:
            marks = collect([entry["path"] for entry in batch["images"]])
            batch["marks"] = marks
        return batch["marks"]

    @contextmanager
    def pin(self, batch_id: str):
        """Hold a batch on disk for the length of a run so a sweep cannot take it."""
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
