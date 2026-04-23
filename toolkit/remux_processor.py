import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tkinter.filedialog import askopenfilenames as get_files

import ffmpeg


def run_ffmpeg_task(input_video, input_subtitle, output_video, track_idx):
    # A pure worker function. It doesn't care about season or episode numbers, it just takes paths and executes them.
    try:
        steam = ffmpeg.input(input_video)
        video = steam[f"v:{track_idx["video"]}"]
        audio = steam[f"a:{track_idx["audio"]}"]
        stream_title = Path(input_video).stem

        # Determine subtitle source
        if input_subtitle:
            subtitle = ffmpeg.input(input_subtitle)["s:0"]
        else:
            subtitle = steam[f"s:{track_idx["subtitle"]}"]

        streams = [video, audio, subtitle]

        # Use the unique key strategy
        out_config = {
            "c": "copy",
            "metadata:s:s:0": "language=chi",
            "metadata:s:s:0": "title=简英",
            "disposition:s:0": "default",
            "metadata:g": f"title={stream_title}"
        }

        (
            ffmpeg
            .output(*streams, output_video, **out_config)
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

        return f"✅ Success: {stream_title}"
    except Exception as e:
        return f"❌ Failed: {stream_title} | Error: {e}"


if __name__ == "__main__":
    # --- 1. Configuration ---
    out_path = os.path.expanduser("~/Desktop/🎬")
    os.makedirs(out_path, exist_ok=True)
    track_settings = {"video": 0, "audio": 0, "subtitle": 0}
    extra_sub = False


    # --- 2. Build the Task List ---
    # This is where you handle the varying filenames by manually defining different patterns for different episodes
    tasks = []
    raw_video_files = get_files(title="Please Select Video(s)")
    raw_subtitle_files = get_files(title="Please Select Subtitle(s)") if extra_sub else None
    tasks_num = len(raw_video_files)

    if not raw_video_files:
        print("❌ No video selected.")
        exit()

    for idx in range(tasks_num):
        in_video = raw_video_files[idx]
        in_subtitle = raw_subtitle_files[idx] if extra_sub else None
        streams_title = os.path.basename(in_video)
        out_video = os.path.join(out_path, streams_title)

        tasks.append({
            "input_video": in_video,
            "input_subtitle": in_subtitle,
            "output_video": out_video,
            "track_idx": track_settings,
        })


    # --- 3. Execute Simultaneously ---
    print(f"Starting processing for {tasks_num} files...")

    with ProcessPoolExecutor() as executor:
        # We use a dictionary unpacking (**) to pass the task info to the function
        results = list(executor.submit(run_ffmpeg_task, **task) for task in tasks)

        for r in results:
            print(r.result())

    print("Done!")
    subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
