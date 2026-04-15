from toolkit.tool_function.colloctor import collect_target_files
import os
import subprocess


source_folder = os.path.expanduser("~/Desktop/1")
target_folder = os.path.expanduser("~/Desktop/2")
os.makedirs(target_folder, exist_ok=True)

v_types = [".mp4", ".mov", ".ts", ".flv", ".avi"]
files = collect_target_files(input_folder=source_folder, file_types=v_types, include_subfolder=True)
print(f"Found {len(files)} files.")


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

subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
