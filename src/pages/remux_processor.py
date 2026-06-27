import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ffmpeg
import streamlit as st
from ffmpeg_progress_yield import FfmpegProgress

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
COMPLETION_SOUND = "/System/Library/Sounds/Hero.aiff"


def natural_sort_key(name):
    """
    Human-friendly sort key: split a name into text/number chunks so digit
    runs compare numerically (ep2 < ep10) and text compares case-insensitively.
    """
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", name)
    ]


# =======================================================
# FOLDER PICKER
# =======================================================
def _applescript_str(value):
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def pick_folder(start_dir=None):
    """
    Open the native macOS folder chooser and return the selected path.

    Uses AppleScript (`osascript`) rather than tkinter, which isn't bundled
    with every Python build (e.g. Homebrew's). The dialog opens at start_dir
    when given. Returns "" if the user cancels.
    """
    prompt = "Select a folder"
    start = Path(start_dir).expanduser() if start_dir else None
    if start and start.is_dir():
        script = (
            f'POSIX path of (choose folder with prompt "{prompt}" '
            f"default location (POSIX file {_applescript_str(str(start))}))"
        )
    else:
        script = f'POSIX path of (choose folder with prompt "{prompt}")'

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    path = result.stdout.strip()
    return path.rstrip("/") if len(path) > 1 else path


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
# STREAMLIT UI SETUP
# =======================================================
st.title("🎬 Remux Processor")
st.write("Parallel, lossless remuxing (re-multiplexing) of videos with FFmpeg.")

# --- 1. SELECT SOURCE FOLDER & FILES ---
st.write("## 1. Select Videos")

if "source_folder" not in st.session_state:
    st.session_state.source_folder = str(Path("~/Desktop").expanduser())

# Browse updates the folder, then st.rerun() refreshes the field + file list.
folder = st.session_state.source_folder
st.text_input(
    "Source folder", value=folder, disabled=True, label_visibility="collapsed"
)
if st.button("📂 Browse…", key="browse_src"):
    picked = pick_folder(st.session_state.source_folder)
    if picked:
        st.session_state.source_folder = picked
        st.rerun()

video_files = []
folder_path = Path(folder).expanduser()
if folder and folder_path.is_dir():
    video_files = sorted(
        (
            p
            for p in folder_path.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda p: natural_sort_key(p.name),
    )
    if not video_files:
        st.info("No video files found in this folder.")
elif folder:
    st.error("❌ Folder not found.")

options = [str(p) for p in video_files]
selected = st.multiselect(
    "Videos to remux",
    options=options,
    format_func=lambda s: Path(s).name,
)

# --- 2. TRACK CONFIGURATION ---
st.write("## 2. Track Configuration")
col1, col2, col3 = st.columns(3)

with col1:
    include_video = st.checkbox("Include video", value=True)
    video_idx = 0
    if include_video:
        video_idx = st.number_input("Video track index", min_value=0, value=0, step=1)
with col2:
    multi_audio = st.checkbox("Multiple audio tracks", value=False)
    if multi_audio:
        audio_value = st.text_input(
            "Audio track index(es)",
            value="0",
            help="Comma-separated, e.g. 0,1. Leave empty for no audio.",
        )
    else:
        audio_value = st.number_input(
            "Audio track index",
            min_value=0,
            value=0,
            step=1,
        )
with col3:
    include_subtitle = st.checkbox("Include embedded subtitle", value=True)
    subtitle_idx = 0
    if include_subtitle:
        subtitle_idx = st.number_input(
            "Subtitle track index", min_value=0, value=0, step=1
        )

sub_lang = st.text_input("Subtitle language tag", value="chi")

# --- 3. EXTERNAL SUBTITLES (OPTIONAL) ---
st.write("## 3. External Subtitles (Optional)")
use_external_sub = st.checkbox("Attach external subtitle files")
external_sub_map = {}
if use_external_sub:
    # Default to the source folder until a subtitle folder is explicitly chosen
    if "sub_folder" not in st.session_state:
        st.session_state.sub_folder = None

    sub_folder = st.session_state.sub_folder or folder
    st.text_input(
        "Subtitle folder",
        value=sub_folder,
        disabled=True,
        label_visibility="collapsed",
    )
    if st.button("📂 Browse…", key="browse_sub"):
        picked = pick_folder(st.session_state.sub_folder or folder)
        if picked:
            st.session_state.sub_folder = picked
            st.rerun()

    sub_folder_path = Path(sub_folder).expanduser()
    if sub_folder_path.is_dir():
        # Match each selected video to a subtitle sharing the same filename stem
        subs_by_stem = {
            p.stem: str(p)
            for p in sub_folder_path.iterdir()
            if p.is_file() and p.suffix.lower() in SUBTITLE_EXTENSIONS
        }
        for s in selected:
            external_sub_map[s] = subs_by_stem.get(Path(s).stem)
        if selected:
            st.caption("Matched by filename (external takes priority over embedded):")
            st.dataframe(
                [
                    {
                        "Video": Path(s).name,
                        "Subtitle": (
                            Path(external_sub_map[s]).name
                            if external_sub_map[s]
                            else "— none —"
                        ),
                    }
                    for s in selected
                ],
                hide_index=True,
            )
    else:
        st.error("❌ Subtitle folder not found.")

# --- 4. OUTPUT & RUN CONFIG ---
st.write("## 4. Output")

if "out_folder" not in st.session_state:
    st.session_state.out_folder = str(Path("~/Desktop/🎬").expanduser())

out_folder = st.session_state.out_folder
st.text_input(
    "Output folder", value=out_folder, disabled=True, label_visibility="collapsed"
)
if st.button("📂 Browse…", key="browse_out"):
    picked = pick_folder(st.session_state.out_folder)
    if picked:
        st.session_state.out_folder = picked
        st.rerun()

max_workers = st.slider("Parallel workers", min_value=1, max_value=8, value=4)


# =======================================================
# EXECUTION & RESULTS DISPLAY
# =======================================================
if st.button("🚀 Start Remuxing", type="primary"):
    if not selected:
        st.error("❌ Please select at least one video.")
    elif shutil.which("ffmpeg") is None:
        st.error(
            "❌ ffmpeg not found on PATH. Install it (e.g. `brew install ffmpeg`)."
        )
    else:
        # Single picker -> one track; multi mode -> parse comma-separated list
        try:
            if multi_audio:
                audio_indices = [
                    int(x) for x in audio_value.replace(" ", "").split(",") if x != ""
                ]
            else:
                audio_indices = [int(audio_value)]
        except ValueError:
            st.error("❌ Audio track indices must be integers, e.g. 0,1")
            st.stop()

        track_configs = {
            "video": int(video_idx) if include_video else None,
            "audio": audio_indices,
            "subtitle": int(subtitle_idx) if include_subtitle else None,
        }

        # Refuse an all-empty stream map (ffmpeg would reject it with no -map)
        if (
            track_configs["video"] is None
            and not track_configs["audio"]
            and track_configs["subtitle"] is None
            and not use_external_sub
        ):
            st.error("❌ Select at least one video, audio, or subtitle track.")
            st.stop()

        out_path = Path(out_folder).expanduser()
        out_path.mkdir(parents=True, exist_ok=True)

        # Build the task list
        tasks = []
        for idx, video in enumerate(selected):
            ext_sub = external_sub_map.get(video) if use_external_sub else None
            tasks.append(
                {
                    "task_id": idx,
                    "input_video": video,
                    "input_subtitle": ext_sub,
                    "output_video": str(out_path / Path(video).name),
                    "track_configs": track_configs,
                    "sub_lang": sub_lang,
                }
            )

        # One progress bar per file
        st.write("## ⏳ Progress")
        st.caption("The page stays busy until all files finish.")
        bars = [st.progress(0, text=f"🟡 {Path(t['input_video']).name}") for t in tasks]

        # Shared progress dict updated by worker threads, polled by main thread
        progress_state = {t["task_id"]: 0.0 for t in tasks}
        lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(run_remux_task, t, progress_state, lock) for t in tasks
            ]
            # Poll the shared dict and refresh the bars until every task is done
            while not all(f.done() for f in futures):
                with lock:
                    snapshot = dict(progress_state)
                for i, t in enumerate(tasks):
                    pct = max(0, min(100, int(snapshot.get(t["task_id"], 0))))
                    bars[i].progress(
                        pct, text=f"🟡 {Path(t['input_video']).name} — {pct}%"
                    )
                time.sleep(0.2)
            results = [f.result() for f in futures]

        # Final per-file bar states
        results_by_id = {r["task_id"]: r for r in results}
        for i, t in enumerate(tasks):
            res = results_by_id[t["task_id"]]
            if res["success"]:
                bars[i].progress(100, text=f"🟢 {res['title']} — done")
            else:
                bars[i].progress(0, text=f"🔴 {res['title']} — failed")

        # Summary metrics
        successful = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]

        st.write("## 📊 Summary")
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", len(results))
        c2.metric("Success ✅", len(successful))
        c3.metric("Failed ❌", len(failed))

        if failed:
            st.write("### ⚠️ Failures")
            for r in failed:
                st.error(f"🔴 {r['title']}: {r['error']}")

        st.success(f"Done! Output saved to: {out_path}")

        # Play notification sound
        subprocess.run(["afplay", COMPLETION_SOUND])
