import subprocess
from scandir_rs import Scandir
from pathlib import Path
import shutil


# Configuration
source_folder = Path("~/Desktop/1").expanduser()
target_folder = Path("~/Desktop/2").expanduser()
v_types = ["*.mkv", "*.mp4", "*.mov", "*.ts", "*.flv", "*.avi"]

# Collect targeted files path
target_folder.mkdir(parents=True, exist_ok=True)
scaner, errors = Scandir(str(source_folder), file_include=v_types).collect()
files = []
for file in scaner:
    if file.is_file:
        files.append(source_folder / file.path)

if errors:
    for error in errors:
        print(error)

print(f"Found {len(files)} files.")


# Move files
for file in files:
    try:
        target_path = target_folder / file.name

        # Handle duplicated files
        counter = 1
        while target_path.exists():
            new_file_name = f"{file.stem}_{counter}{file.suffix}"
            target_path = target_folder / new_file_name
            counter += 1

        shutil.move(str(file), str(target_path))
    except Exception as e:
        print(f"Error moving {file}: {e}")

print("Done!")
subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
