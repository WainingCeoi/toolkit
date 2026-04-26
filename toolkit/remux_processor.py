import subprocess
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tkinter.filedialog import askopenfilenames as get_files

import ffmpeg
from ffmpeg_progress_yield import FfmpegProgress
from tqdm import tqdm


def run_ffmpeg_task(task_id, input_video, input_subtitle, output_video, track_configs, queue):
    """
    Worker function executed in a separate process.
    Sends progress updates to the main process via a queue instead of printing to stdout.
    """
    try:
        stream_title = Path(input_video).stem
        
        # Build stream
        steam = ffmpeg.input(input_video)
        video = steam[f"v:{track_configs['video']}"]
        audio = steam[f"a:{track_configs['audio']}"]
        
        # Determine subtitle source
        if input_subtitle:
            subtitle = ffmpeg.input(input_subtitle)["s:0"]
        else:
            subtitle = steam[f"s:{track_configs['subtitle']}"]

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
        
        # Report progress to the queue so the listener can update the tqdm bar
        for progress in ff.run_command_with_progress():
            queue.put({"type": "update", "task_id": task_id, "progress": progress})
        
        # Signal that this specific task is finished
        queue.put({"type": "done", "task_id": task_id})
        return f"🟢 Success: {stream_title}"
    except Exception as e:
        queue.put({"type": "done", "task_id": task_id})
        return f"🔴 Failed: {stream_title} | Error: {e}"


def progress_listener(queue, total_tasks, task_titles):
    """
    Background thread function that manages tqdm progress bars.
    It reads from the queue and updates bars based on the task_id.
    """
    # Create tqdm bars. 'position' is critical here to ensure bars stack without overlapping.
    bars = [tqdm(total=100, position=i, desc=f"🟡 {task_titles[i]}", leave=True) for i in range(total_tasks)]
    
    completed = 0
    # Keep listening until all tasks report completion
    while completed < total_tasks:
        msg = queue.get() # Blocking call: waits for messages from worker processes
        if msg["type"] == "update":
            bars[msg["task_id"]].update(msg["progress"] - bars[msg["task_id"]].n)
        elif msg["type"] == "done":
            completed += 1
            
    # Cleanup progress bars once finished
    for bar in bars:
        bar.close()


if __name__ == "__main__":
    # Setup
    out_path = Path("~/Desktop/🎬").expanduser()
    out_path.mkdir(exist_ok=True)
    track_configs = {"video": 0, "audio": 0, "subtitle": 0}
    extra_sub = False

    # Select files
    raw_video_files = get_files(title="Please Select Video(s)")
    raw_subtitle_files = get_files(title="Please Select Subtitle(s)") if extra_sub else None
    tasks_num = len(raw_video_files)
    
    if not raw_video_files:
        print("🔴 No video selected.")
        exit()

    print(f"Starting processing for {tasks_num} files...")
    
    # Manager provides a shared Queue which is a "shared mailbox" allowing all spawned processes "post" info. into it.
    manager = multiprocessing.Manager()
    queue = manager.Queue()
    task_titles = [Path(f).name for f in raw_video_files]
    
    # Initialize and start the background listener thread BEFORE running tasks
    listener = threading.Thread(target=progress_listener, args=(queue, tasks_num, task_titles))
    listener.start()

    tasks = []
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
            "queue": queue
        })


    # ProcessPoolExecutor manages the workers; they receive the queue to send updates back
    with ProcessPoolExecutor() as executor:
        results = list(executor.submit(run_ffmpeg_task, **task) for task in tasks)
    
    # Wait for the listener thread to clean up all bars
    listener.join()
    
    print("\n")
    for r in results:
        print(r.result())

    print("Done!")
    subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
