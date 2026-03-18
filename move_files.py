from ToolFunc.colloctor import collect_target_files
import os


source_folder = os.path.expanduser("~Desktop/1")
target_folder = os.path.expanduser("~Desktop/2")

v_types = [".mp4", ".mov", ".ts", ".flv"]
files = collect_target_files(input_folder=source_folder, file_types=v_types, include_subfolder=True)


for file in files:
    file_name = os.path.basename(file)
    target_path = os.path.join(target_folder, file_name)
    os.rename(file, target_path)
