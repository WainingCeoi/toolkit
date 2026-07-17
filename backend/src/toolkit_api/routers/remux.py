"""Remux Processor: scan videos, match external subtitles, start remux jobs.

Thin over toolkit_engine.remux — every validation and its exact error
message (including the ❌ prefix) carries over from the Streamlit page.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from toolkit_engine.remux import list_videos, match_subtitles, run_remux_batch

from ..deps import JobsDep
from ..schemas import JobStartedOut

router = APIRouter(prefix="/remux", tags=["remux"])


class ScanIn(BaseModel):
    folder: str


class VideoOut(BaseModel):
    path: str
    name: str


class ScanOut(BaseModel):
    videos: list[VideoOut]


class SubtitlesIn(BaseModel):
    sub_folder: str
    selected: list[str]


class SubtitleMatchOut(BaseModel):
    video: str
    subtitle: str | None = None


class SubtitlesOut(BaseModel):
    matches: list[SubtitleMatchOut]


class StartIn(BaseModel):
    selected: list[str]
    include_video: bool = True
    video_index: int = 0
    multi_audio: bool = False
    audio_value: str = "0"
    include_subtitle: bool = True
    subtitle_index: int = 0
    sub_lang: str = "chi"
    use_external_sub: bool = False
    external_sub_map: dict[str, str | None] = Field(default_factory=dict)
    out_folder: str
    max_workers: int = Field(default=4, ge=1, le=8)


@router.post("/scan", response_model=ScanOut)
def scan(req: ScanIn) -> ScanOut:
    files, error = list_videos(req.folder)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    return ScanOut(videos=[VideoOut(path=f, name=Path(f).name) for f in files])


@router.post("/subtitles", response_model=SubtitlesOut)
def subtitles(req: SubtitlesIn) -> SubtitlesOut:
    matches, error = match_subtitles(req.sub_folder, req.selected)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    return SubtitlesOut(
        matches=[
            SubtitleMatchOut(video=video, subtitle=subtitle)
            for video, subtitle in matches.items()
        ]
    )


@router.post("/start", response_model=JobStartedOut)
def start(req: StartIn, jobs: JobsDep) -> JobStartedOut:
    if not req.selected:
        raise HTTPException(
            status_code=400, detail="❌ Please select at least one video."
        )
    if not Path(req.out_folder).expanduser().is_absolute():
        # A relative (or empty) output path would land in the app's CWD.
        raise HTTPException(
            status_code=400, detail="❌ Use an absolute output folder path."
        )
    if shutil.which("ffmpeg") is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "❌ ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`)."
            ),
        )

    # Single picker -> one track; multi mode -> parse comma-separated list
    try:
        if req.multi_audio:
            audio_indices = [
                int(x) for x in req.audio_value.replace(" ", "").split(",") if x != ""
            ]
        else:
            audio_indices = [int(req.audio_value)]
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="❌ Audio track indices must be integers, e.g. 0,1",
        ) from None

    track_configs = {
        "video": int(req.video_index) if req.include_video else None,
        "audio": audio_indices,
        "subtitle": int(req.subtitle_index) if req.include_subtitle else None,
    }

    # Refuse an all-empty stream map (ffmpeg would reject it with no -map)
    if (
        track_configs["video"] is None
        and not track_configs["audio"]
        and track_configs["subtitle"] is None
        and not req.use_external_sub
    ):
        raise HTTPException(
            status_code=400,
            detail="❌ Select at least one video, audio, or subtitle track.",
        )

    out_path = Path(req.out_folder).expanduser()
    try:
        out_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # e.g. the typed path (or one of its parents) is a file
        raise HTTPException(
            status_code=400, detail=f"❌ Cannot create the output folder: {e}"
        ) from e

    # Build the task list
    tasks = []
    for idx, video in enumerate(req.selected):
        ext_sub = req.external_sub_map.get(video) if req.use_external_sub else None
        tasks.append(
            {
                "task_id": idx,
                "input_video": video,
                "input_subtitle": ext_sub,
                "output_video": str(out_path / Path(video).name),
                "track_configs": track_configs,
                "sub_lang": req.sub_lang,
            }
        )

    max_workers = req.max_workers

    def worker(job):
        results = run_remux_batch(tasks, max_workers, job)
        if job.cancelled:
            return None
        successful = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]
        return {
            "total": len(results),
            "successful": len(successful),
            "failed": [{"title": r["title"], "error": r["error"]} for r in failed],
            "out_folder": str(out_path),
        }

    item_names = [Path(v).name for v in req.selected]
    job = jobs.submit("remux", item_names, worker)
    return JobStartedOut(job_id=job.id)
