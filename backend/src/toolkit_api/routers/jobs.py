"""Job status, SSE progress streams, cancellation, and artifact downloads."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from ..deps import ArtifactsDep, JobsDep
from ..jobs import FINISHED_STATES
from ..schemas import JobOut

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
def job_status(job_id: str, jobs: JobsDep) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id.")
    return job.snapshot()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, jobs: JobsDep) -> EventSourceResponse:
    """SSE progress stream: `progress` frames until the job finishes, then a
    terminal `done` frame carrying the final snapshot."""
    if jobs.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Unknown job id.")

    async def gen():
        while True:
            job = jobs.get(job_id)
            if job is None:  # evicted mid-stream — tell the client to stop
                yield {
                    "event": "done",
                    "data": json.dumps(
                        {
                            "state": "failed",
                            "items": [],
                            "message": "",
                            "error": "This job is no longer available.",
                        }
                    ),
                }
                return
            snap = job.snapshot()
            if snap["state"] in FINISHED_STATES:
                yield {"event": "done", "data": json.dumps(snap)}
                return
            yield {"event": "progress", "data": json.dumps(snap)}
            await asyncio.sleep(0.3)

    return EventSourceResponse(gen())


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, jobs: JobsDep) -> dict:
    """Best-effort: the worker stops at the next between-items check."""
    if jobs.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Unknown job id.")
    return {"cancelling": jobs.cancel(job_id)}


@router.get("/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, artifacts: ArtifactsDep) -> FileResponse:
    meta = artifacts.get(artifact_id)
    if meta is None or not meta["path"].is_file():
        raise HTTPException(status_code=404, detail="Unknown or expired artifact.")
    return FileResponse(
        meta["path"], filename=meta["filename"], media_type=meta["media_type"]
    )
