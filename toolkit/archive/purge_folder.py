import os
from concurrent.futures import ThreadPoolExecutor
from toolkit.ToolFunc.colloctor import collect_target_files


# Config parameters
folder_to_purge = r""
extensions = [".dwl", ".dwl2", ".bak", ".log", ".db", ".tmp", ".err"]
min_workers = 200


def delete_file(files):
    try:
        for file in files:
            os.remove(file)
    except Exception as e:
        print(e)


if __name__ == "__main__":
    # get the target to delete files
    files_to_delete = collect_target_files(input_folder=folder_to_purge, file_types=extensions, include_subfolder=True)
    total_files = len(files_to_delete)
    print(f"Found {total_files} files.")


    dynamic_workers = max(min_workers, total_files)
    with ThreadPoolExecutor(max_workers=dynamic_workers) as executor:
        executor.map(delete_file, (files_to_delete[idx::dynamic_workers] for idx in range(dynamic_workers)))

    print("Purge Done!")
