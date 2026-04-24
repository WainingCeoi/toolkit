import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tkinter.filedialog import askopenfilenames as get_files

import ffmpeg
from ffmpeg_progress_yield import FfmpegProgress
from tqdm import tqdm


def run_ffmpeg_task(task_id, input_video, input_subtitle, output_video, track_configs):
    try:
        stream_title = Path(input_video).stem
        
        # Build stream
        steam = ffmpeg.input(input_video)
        video = steam[f"v:{track_configs["video"]}"]
        audio = steam[f"a:{track_configs["audio"]}"]
        
        # Determine subtitle source
        if input_subtitle:
            subtitle = ffmpeg.input(input_subtitle)["s:0"]
        else:
            subtitle = steam[f"s:{track_configs["subtitle"]}"]

        # Config processing parameters
        streams = [video, audio, subtitle]
        out_config = {
            "c": "copy",
            "metadata:s:s:0": "language=chi",
            "metadata:s:s:0": "title=简英",
            "disposition:s:0": "default",
            "metadata:g": f"title={stream_title}"
        }
        stream = ffmpeg.output(*streams, output_video, **out_config).overwrite_output()
        
        cmd = ["ffmpeg"] + stream.get_args()
        ff = FfmpegProgress(cmd)

            
        # 4. Track progress
        # Using tqdm to keep the bars organized if you run this in a terminal
        with tqdm(total=100, position=task_id, leave=True, desc=f"🟡 Working on: {stream_title}") as pbar:
            for progress in ff.run_command_with_progress():
                pbar.update(progress - pbar.n)
                
                
        return f"🟢 Success: {stream_title}"
    except Exception as e:
        return f"🔴 Failed: {stream_title} | Error: {e}"


if __name__ == "__main__":
    # Setup
    out_path = Path("~/Desktop/🎬").expanduser()
    out_path.mkdir(exist_ok=True)
    track_configs = {"video": 0, "audio": 0, "subtitle": 0}
    extra_sub = False

    # Select files
    tasks = []
    raw_video_files = get_files(title="Please Select Video(s)")
    raw_subtitle_files = get_files(title="Please Select Subtitle(s)") if extra_sub else None
    tasks_num = len(raw_video_files)
    
    # Build task configs
    if not raw_video_files:
        print("🔴 No video selected.")
        exit()

    for idx in range(tasks_num):
        in_video = raw_video_files[idx]
        in_subtitle = raw_subtitle_files[idx] if extra_sub else None
        stream_title = Path(in_video).name
        out_video = str(out_path / stream_title)

        tasks.append({
            "task_id": idx,
            "input_video": in_video,
            "input_subtitle": in_subtitle,
            "output_video": out_video,
            "track_configs": track_configs,
        })


    # Execute Simultaneously
    print(f"Starting processing for {tasks_num} files...")

    with ProcessPoolExecutor() as executor:
        # We use a dictionary unpacking (**) to pass the task info to the function
        results = list(executor.submit(run_ffmpeg_task, **task) for task in tasks)
    
    print("\n")
    for r in results:
        print(r.result())

    print("Done!")
    subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
