import os
import subprocess
from scandir_rs import Scandir


# Configuration
source_folder = os.path.expanduser("~/Desktop/1")
target_folder = os.path.expanduser("~/Desktop/2")
v_types = ["*.mkv", "*.mp4", "*.mov", "*.ts", "*.flv", "*.avi"]

# Collect targeted files path
os.makedirs(target_folder, exist_ok=True)
scaner, errors = Scandir(source_folder, file_include=v_types).collect()
files = []
for file in scaner:
    if file.is_file:
        files.append(os.path.join(source_folder, file.path))

if errors:
    for error in errors:
        print(error)

print(f"Found {len(files)} files.")


# Move files
for file in files:
    try:
        file_name = os.path.basename(file)
        name, ext = os.path.splitext(file_name)
        target_path = os.path.join(target_folder, file_name)

        # Module use for handling duplicated files.
        counter = 1
        while os.path.exists(target_path):
            new_file_name = f"{name}_{counter}{ext}"
            target_path = os.path.join(target_folder, new_file_name)
            counter += 1

        os.rename(file, target_path)
    except Exception as e:
        print(f"Error moving {file}: {e}")

print("Done!")
subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
