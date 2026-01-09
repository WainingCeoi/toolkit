import ffmpeg
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import os


def run_ffmpeg_task(video_in, sub_in, video_out, track_idx):
    # A pure worker function. It doesn't care about season or episode numbers, it just takes paths and executes them.
    try:
        steam = ffmpeg.input(video_in)
        video = steam[f"v:{track_idx["video"]}"]
        audio = steam[f"a:{track_idx["audio"]}"]
        in_title = Path(video_in).stem
        out_title = Path(video_out).stem

        # Determine subtitle source
        if sub_in:
            subtitle = ffmpeg.input(sub_in).subtitle
        else:
            subtitle = steam[f"s:{track_idx["subtitle"]}"]

        streams = [video, audio, subtitle]

        # Use the unique key strategy
        out_config = {
            "c": "copy",
            "metadata:s:s:0": "language=chi",
            "metadata:s:s:0": "title=简英",
            "disposition:s:0": "default",
            "metadata:g": f"title={out_title}"
        }

        (
            ffmpeg
            .output(*streams, video_out, **out_config)
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )

        return f"✅ Success: {out_title}"
    except Exception as e:
        return f"❌ Failed: {in_title} | Error: {e}"


if __name__ == "__main__":
    # --- 1. Configuration ---
    se = 1
    ep_num = 10
    in_path = "/Users/"
    out_path = "/Users/"
    track_settings = {"video": 0, "audio": 0, "subtitle": 0}
    extra_sub = False
    max_worker = 5
    # --- 2. Build the Task List ---
    # This is where you handle the varying filenames by manually defining different patterns for different episodes
    tasks = []
    for ep in range(ep_num):
        ep += 1
        in_v_name = f"Samples.S{se:02}.E{ep:02}.mkv"
        in_s_name = f""
        out_file_name = f"Samples.se{se}.ep{ep}.mkv"
        video_in = os.path.join(in_path, in_v_name)
        sub_in = os.path.join(in_path, in_s_name)
        video_out = os.path.join(out_path, out_file_name)

        if os.path.exists(video_in):
            tasks.append({
                "video_in": in_path + in_v_name,
                "sub_in": sub_in if extra_sub else None,
                "video_out": video_out,
                "track_idx": track_settings
            })
        else:
            print(f"❌{in_v_name} doesn't exist")


    # --- 3. Execute Simultaneously ---
    print(f"Starting parallel processing for {len(tasks)} files...")

    with ProcessPoolExecutor(max_workers=max_worker) as executor:
        # We use a dictionary unpacking (**) to pass the task info to the function
        futures = [executor.submit(run_ffmpeg_task, **task) for task in tasks]

        for future in futures:
            print(future.result())
