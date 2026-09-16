"""Remux Processor engine: parallel, lossless ffmpeg stream-copy remuxing."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ffmpeg
from ffmpeg_progress_yield import FfmpegProgress

from toolkit_engine.filetypes import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS
from toolkit_engine.fsutil import natural_sort_key

# Re-exported: callers and tests import these from toolkit_engine.remux.
__all__ = ["SUBTITLE_EXTENSIONS", "VIDEO_EXTENSIONS"]


# --- CORE REMUX LOGIC ---
def build_ffmpeg_cmd(
    input_video, input_subtitle, output_video, track_configs, sub_lang
):
    """Build the ffmpeg command (lossless stream-copy remux) for one file."""
    stream_title = Path(input_video).stem

    source = ffmpeg.input(input_video)
    streams = []

    if track_configs["video"] is not None:
        streams.append(source[f"v:{track_configs['video']}"])

    for a_idx in track_configs["audio"]:
        streams.append(source[f"a:{a_idx}"])

    has_subtitle = False
    if input_subtitle:
        streams.append(ffmpeg.input(input_subtitle)["s:0"])
        has_subtitle = True
    elif track_configs["subtitle"] is not None:
        streams.append(source[f"s:{track_configs['subtitle']}"])
        has_subtitle = True

    out_config = {"c": "copy", "metadata:g": f"title={stream_title}"}
    if has_subtitle:
        out_config["metadata:s:s:0"] = f"language={sub_lang}"
        out_config["disposition:s:0"] = "default"

    stream = ffmpeg.output(*streams, output_video, **out_config).overwrite_output()
    return ["ffmpeg"] + stream.get_args()


def run_remux_task(task, progress_state, lock, ff_registry=None):
    """Thread worker: run one ffmpeg remux, reporting progress into the shared dict."""
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
        if ff_registry is not None:
            with lock:
                ff_registry[task_id] = ff

        for progress in ff.run_command_with_progress():
            with lock:
                progress_state[task_id] = progress
        with lock:
            progress_state[task_id] = 100.0

        return {"task_id": task_id, "title": title, "success": True, "error": None}
    except Exception as e:
        return {"task_id": task_id, "title": title, "success": False, "error": str(e)}
    finally:
        if ff_registry is not None:
            with lock:
                ff_registry.pop(task_id, None)


# --- FOLDER SCANNING & SUBTITLE MATCHING ---
def list_videos(folder: str) -> tuple[list[str], str | None]:
    """List video files in `folder`, natural-sorted, as absolute path strings."""
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
            return [], f"❌ Cannot read the source folder: {e}"
        return [str(p) for p in video_files], None
    return [], "❌ Folder not found — use an absolute path (e.g. ~/Movies)."


def match_subtitles(
    sub_folder: str, selected: list[str]
) -> tuple[dict[str, str | None], str | None]:
    """Match each selected video to a subtitle sharing the same filename stem."""
    sub_folder_path = Path(sub_folder).expanduser()
    # Require an absolute path: a relative one would list the app's CWD.
    if sub_folder_path.is_absolute() and sub_folder_path.is_dir():
        try:
            subs_by_stem = {
                p.stem: str(p)
                for p in sub_folder_path.iterdir()
                if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
            }
        except OSError as e:
            return {}, f"❌ Cannot read the subtitle folder: {e}"
        external_sub_map = {s: subs_by_stem.get(Path(s).stem) for s in selected}
        return external_sub_map, None
    return {}, "❌ Subtitle folder not found — use an absolute path."


# --- BATCH EXECUTION ---
def run_remux_batch(tasks: list[dict], max_workers: int, job) -> list[dict]:
    """Run the remux tasks in a thread pool, mirroring progress into `job`."""
    progress_state = {t["task_id"]: 0.0 for t in tasks}
    ff_registry: dict = {}  # task_id -> live FfmpegProgress (for cancel-kill)
    lock = threading.Lock()
    killed: set = set()

    for i in range(len(tasks)):
        job.update_item(i, state="running")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(run_remux_task, t, progress_state, lock, ff_registry)
            for t in tasks
        ]
        while not all(f.done() for f in futures):
            if job.cancelled:
                # Kill live ffmpeg too: a hung one would keep the job alive forever.
                for f in futures:
                    f.cancel()
                with lock:
                    live = list(ff_registry.items())
                for task_id, ff in live:
                    try:
                        ff.quit()
                        killed.add(task_id)
                    except Exception:
                        pass  # already exited between snapshot and kill
            with lock:
                snapshot = dict(progress_state)
            for i, t in enumerate(tasks):
                pct = max(0, min(100, int(snapshot.get(t["task_id"], 0))))
                job.update_item(i, pct=pct)
            time.sleep(0.2)
        results = [f.result() for f in futures if not f.cancelled()]

    results_by_id = {r["task_id"]: r for r in results}
    for i, t in enumerate(tasks):
        res = results_by_id.get(t["task_id"])
        if res is not None and not res["success"] and t["task_id"] in killed:
            # SIGKILL surfaces inside ffmpeg_progress_yield as an AttributeError.
            res["cancelled"] = True
            res["error"] = "Cancelled"
            Path(t["output_video"]).unlink(missing_ok=True)
        if res is None or res.get("cancelled"):  # never started, or killed mid-file
            job.update_item(i, pct=0, state="pending")
        elif res["success"]:
            job.update_item(i, pct=100, state="done")
        else:
            job.update_item(i, pct=0, state="failed", error=res["error"])
    return results
