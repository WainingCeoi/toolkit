"""In-process job registry for long-running batch work.

Every long-running tool (remux, gather, purge, conversions, scraping) follows
one shape: submit a batch -> a worker thread reports per-item progress ->
done/failed lists plus an optional artifact. The registry holds one Job per
run; the SSE endpoint in routers/jobs.py streams each job's snapshot.

Single-process by design: the registry lives on app.state, so the server must
run exactly one worker (see host.py).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime

# Terminal job states — anything else means "still running".
FINISHED_STATES = frozenset({"done", "failed", "cancelled"})


class Job:
    """One batch run. Worker threads mutate it via the helpers; readers take
    snapshot(). All mutation happens under the job's own lock."""

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

    # --- worker-side API -------------------------------------------------
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

    # --- registry-side API ------------------------------------------------
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
    """Creates jobs, runs their workers in daemon threads, keeps the last N.

    Workers run on daemon threads so a long child process (a 30-min MinerU run)
    never blocks process exit, but concurrency is capped by a semaphore so a
    burst of submits can't spawn threads without bound. shutdown() cancels
    in-flight jobs on teardown so their children (ffmpeg, …) get cleaned up.
    """

    def __init__(self, max_jobs: int = 50, max_workers: int = 8):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._slots = threading.BoundedSemaphore(max_workers)
        self._threads: set[threading.Thread] = set()

    def submit(
        self,
        tool: str,
        item_names: list[str],
        worker: Callable[[Job], dict | None],
    ) -> Job:
        """Create a job and run `worker(job)` in a daemon thread.

        The worker reports progress via job.update_item()/set_message(),
        checks job.cancelled between items, and returns the result dict.
        """
        job = Job(tool, item_names)
        with self._lock:
            self._jobs[job.id] = job
            self._evict_finished()

        def run() -> None:
            # Cap concurrent execution; a queued job stays 'running' until a slot
            # frees (a single user never hits the bound in practice).
            self._slots.acquire()
            try:
                result = worker(job)
            except Exception as exc:  # noqa: BLE001 — surfaced to the client
                job._fail(str(exc))
            else:
                job._finish(result)
            finally:
                self._slots.release()
                with self._lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, name=f"job-{tool}", daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return job

    def shutdown(self, timeout: float = 3.0) -> None:
        """Cancel in-flight jobs and briefly join their workers (teardown).

        Setting the cancel flag lets cooperative workers stop and clean up their
        children (e.g. remux kills its ffmpeg processes); the bounded join gives
        them a moment before the daemon threads die with the process.
        """
        with self._lock:
            jobs = list(self._jobs.values())
            threads = list(self._threads)
        for job in jobs:
            if job.state not in FINISHED_STATES:
                job._cancel.set()
        deadline = time.monotonic() + timeout
        for thread in threads:
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
        # Called under self._lock. Drop oldest finished jobs beyond the cap.
        while len(self._jobs) > self._max_jobs:
            for job_id, job in self._jobs.items():
                if job.state in FINISHED_STATES:
                    del self._jobs[job_id]
                    break
            else:
                return
