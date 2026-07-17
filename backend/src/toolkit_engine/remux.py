"""Remux Processor engine: parallel, lossless ffmpeg stream-copy remuxing.

Pure logic lifted verbatim from the Streamlit page
(src/pages/remux_processor.py). The page's executor + 0.2s polling loop is
re-expressed in run_remux_batch to report into a Job instead of Streamlit
progress bars; the folder validations keep the page's exact error strings.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ffmpeg
from ffmpeg_progress_yield import FfmpegProgress

from toolkit_engine.fsutil import natural_sort_key

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".mov",
    ".avi",
    ".ts",
    ".m2ts",
    ".webm",
    ".flv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".m4v",
}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}


# =======================================================
# CORE REMUX LOGIC
# =======================================================
def build_ffmpeg_cmd(
    input_video, input_subtitle, output_video, track_configs, sub_lang
):
    """Build the ffmpeg command (lossless stream-copy remux) for one file."""
    stream_title = Path(input_video).stem

    # Build stream
    source = ffmpeg.input(input_video)
    streams = []

    # Video process session
    if track_configs["video"] is not None:
        streams.append(source[f"v:{track_configs['video']}"])

    # Audio process session
    for a_idx in track_configs["audio"]:
        streams.append(source[f"a:{a_idx}"])

    # Subtitle process session (external file takes priority over embedded)
    has_subtitle = False
    if input_subtitle:
        streams.append(ffmpeg.input(input_subtitle)["s:0"])
        has_subtitle = True
    elif track_configs["subtitle"] is not None:
        streams.append(source[f"s:{track_configs['subtitle']}"])
        has_subtitle = True

    # Config processing parameters: copy every stream, tag the global title.
    # Subtitle language / default disposition only apply when a subtitle exists.
    out_config = {"c": "copy", "metadata:g": f"title={stream_title}"}
    if has_subtitle:
        out_config["metadata:s:s:0"] = f"language={sub_lang}"
        out_config["disposition:s:0"] = "default"

    stream = ffmpeg.output(*streams, output_video, **out_config).overwrite_output()
    return ["ffmpeg"] + stream.get_args()


def run_remux_task(task, progress_state, lock):
    """
    Worker executed in a thread. The heavy lifting happens in the external
    ffmpeg process, so threads avoid the GIL while leaving Streamlit's widget
    updates on the main thread. Progress is reported into a shared dict.
    """
    task_id = task["task_id"]
    title = Path(task["input_video"]).name
    try:
        cmd = build_ffmpeg_cmd(
            task["input_video"],
            task["input_subtitle"],
            task["output_video"],
            task["track_configs"],
            task["sub_lang"],
        )
        ff = FfmpegProgress(cmd)

        # Report progress to the shared dict so the main thread can update bars
        for progress in ff.run_command_with_progress():
            with lock:
                progress_state[task_id] = progress
        with lock:
            progress_state[task_id] = 100.0

        return {"task_id": task_id, "title": title, "success": True, "error": None}
    except Exception as e:
        return {"task_id": task_id, "title": title, "success": False, "error": str(e)}


# =======================================================
# FOLDER SCANNING & SUBTITLE MATCHING (page semantics)
# =======================================================
def list_videos(folder: str) -> tuple[list[str], str | None]:
    """List video files in `folder`, natural-sorted, as absolute path strings.

    Returns (files, error): `error` carries the page's exact message when the
    folder is empty/relative/missing or unreadable, else None.
    """
    folder_path = Path(folder).expanduser()
    # Require an absolute path: a relative one would list the app's CWD.
    if folder and folder_path.is_absolute() and folder_path.is_dir():
        try:
            video_files = sorted(
                (
                    p
                    for p in folder_path.iterdir()
                    if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
                ),
                key=lambda p: natural_sort_key(p.name),
            )
        except OSError as e:
            # e.g. a typed folder that stats fine but isn't readable
            return [], f"❌ Cannot read the source folder: {e}"
        return [str(p) for p in video_files], None
    return [], "❌ Folder not found — use an absolute path (e.g. ~/Movies)."


def match_subtitles(
    sub_folder: str, selected: list[str]
) -> tuple[dict[str, str | None], str | None]:
    """Match each selected video to a subtitle sharing the same filename stem.

    Returns (mapping, error): `error` carries the page's exact message when
    the subtitle folder is relative/missing or unreadable, else None.
    """
    sub_folder_path = Path(sub_folder).expanduser()
    # Require an absolute path: a relative one would list the app's CWD.
    if sub_folder_path.is_absolute() and sub_folder_path.is_dir():
        # Match each selected video to a subtitle sharing the same filename stem
        try:
            subs_by_stem = {
                p.stem: str(p)
                for p in sub_folder_path.iterdir()
                if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
            }
        except OSError as e:
            # e.g. a typed folder that stats fine but isn't readable
            return {}, f"❌ Cannot read the subtitle folder: {e}"
        external_sub_map = {s: subs_by_stem.get(Path(s).stem) for s in selected}
        return external_sub_map, None
    return {}, "❌ Subtitle folder not found — use an absolute path."


# =======================================================
# BATCH EXECUTION (the page's executor + polling loop)
# =======================================================
def run_remux_batch(tasks: list[dict], max_workers: int, job) -> list[dict]:
    """Run the remux tasks in a thread pool, mirroring progress into `job`.

    Re-expression of the page's ThreadPoolExecutor + 0.2s polling loop:
    workers report into a shared progress dict under a lock; the poll loop
    mirrors each task's pct into job.update_item until every future is done,
    then stamps the final per-file done/failed states. Returns the results
    list (one dict per finished task).
    """
    # Shared progress dict updated by worker threads, polled by this thread
    progress_state = {t["task_id"]: 0.0 for t in tasks}
    lock = threading.Lock()

    for i in range(len(tasks)):
        job.update_item(i, state="running")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(run_remux_task, t, progress_state, lock) for t in tasks
        ]
        # Poll the shared dict and refresh the items until every task is done
        while not all(f.done() for f in futures):
            if job.cancelled:
                # Best-effort: drop tasks that have not started yet; running
                # ffmpeg processes finish their current file.
                for f in futures:
                    f.cancel()
            with lock:
                snapshot = dict(progress_state)
            for i, t in enumerate(tasks):
                pct = max(0, min(100, int(snapshot.get(t["task_id"], 0))))
                job.update_item(i, pct=pct)
            time.sleep(0.2)
        results = [f.result() for f in futures if not f.cancelled()]

    # Final per-file item states
    results_by_id = {r["task_id"]: r for r in results}
    for i, t in enumerate(tasks):
        res = results_by_id.get(t["task_id"])
        if res is None:  # cancelled before it started
            continue
        if res["success"]:
            job.update_item(i, pct=100, state="done")
        else:
            job.update_item(i, pct=0, state="failed", error=res["error"])
    return results
