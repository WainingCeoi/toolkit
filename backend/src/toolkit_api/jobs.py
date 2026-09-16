"""In-process job registry for long-running batch work."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

FINISHED_STATES = frozenset({"done", "failed", "cancelled"})


class Job:
    """One batch run; all mutation happens under the job's own lock."""

    def __init__(self, tool: str, item_names: list[str]):
        self.id = uuid.uuid4().hex[:12]
        self.tool = tool
        self.state = "running"
        self.message = ""
        self.items = [
            {"name": name, "pct": 0, "state": "pending", "error": None}
            for name in item_names
        ]
        self.result: dict | None = None
        self.error: str | None = None
        self.created_at = datetime.now(UTC).isoformat()
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def set_message(self, message: str) -> None:
        with self._lock:
            self.message = message

    def update_item(
        self,
        index: int,
        *,
        pct: int | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            item = self.items[index]
            if pct is not None:
                item["pct"] = max(0, min(100, int(pct)))
            if state is not None:
                item["state"] = state
            if error is not None:
                item["error"] = error

    def set_result(self, result: dict | None) -> None:
        """Publish results so far without finishing the job."""
        with self._lock:
            self.result = result

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "tool": self.tool,
                "state": self.state,
                "message": self.message,
                "items": [dict(item) for item in self.items],
                "result": self.result,
                "error": self.error,
                "created_at": self.created_at,
            }

    def _finish(self, result: dict | None) -> None:
        with self._lock:
            self.state = "cancelled" if self._cancel.is_set() else "done"
            self.result = result

    def _fail(self, error: str) -> None:
        with self._lock:
            self.state = "failed"
            self.error = error


class JobRegistry:
    """Creates jobs, runs their workers on a bounded daemon pool, keeps the last N."""

    def __init__(self, max_jobs: int = 50, max_workers: int = 8):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._max_workers = max_workers
        # A queued item is a (job, worker) pair; None is the retire signal.
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._workers: list[threading.Thread] = []

    def submit(
        self,
        tool: str,
        item_names: list[str],
        worker: Callable[[Job], dict | None],
    ) -> Job:
        """Create a job and queue ``worker(job)``; the worker polls job.cancelled."""
        job = Job(tool, item_names)
        with self._lock:
            self._jobs[job.id] = job
            self._evict_finished()
            self._grow_pool()
        self._queue.put((job, worker))
        return job

    def _grow_pool(self) -> None:
        """Add a worker thread if the pool is below its cap. Holds the lock."""
        if len(self._workers) >= self._max_workers:
            return
        thread = threading.Thread(
            target=self._serve, name=f"job-worker-{len(self._workers)}", daemon=True
        )
        # Appended only after start(), so a failed start never holds a slot.
        thread.start()
        self._workers.append(thread)

    def _run_one(self, job: Job, worker: Callable[[Job], dict | None]) -> None:
        # Honour a cancel from the queue: purge deletes before its own check.
        if job.cancelled:
            job._finish(None)
            return
        try:
            result = worker(job)
        except Exception as exc:  # noqa: BLE001 — surfaced to the client
            job._fail(str(exc))
        except BaseException as exc:  # noqa: BLE001 — thread is dying
            job._fail(f"The worker stopped unexpectedly: {exc!r}")
            raise
        else:
            job._finish(result)

    def _serve(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                # Nested frame + del so upload bytes free before blocking on get().
                self._run_one(*item)
                del item
        finally:
            # Give the slot back however this thread ends; the next submit refills it.
            with self._lock:
                here = threading.current_thread()
                self._workers = [t for t in self._workers if t is not here]

    def shutdown(self, timeout: float = 3.0) -> None:
        """Cancel in-flight jobs and briefly join the workers (teardown)."""
        with self._lock:
            jobs = list(self._jobs.values())
            workers = list(self._workers)
        for job in jobs:
            if job.state not in FINISHED_STATES:
                job._cancel.set()
        for _ in workers:
            self._queue.put(None)
        deadline = time.monotonic() + timeout
        for thread in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        """Best-effort: sets the cancel flag the worker checks between items."""
        job = self.get(job_id)
        if job is None or job.state in FINISHED_STATES:
            return False
        job._cancel.set()
        return True

    def _evict_finished(self) -> None:
        # Caller holds the lock.
        while len(self._jobs) > self._max_jobs:
            for job_id, job in self._jobs.items():
                if job.state in FINISHED_STATES:
                    del self._jobs[job_id]
                    break
            else:
                return
