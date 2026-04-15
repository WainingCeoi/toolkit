import os
from concurrent.futures import ThreadPoolExecutor
from scandir_rs import Scandir


# Config parameters
folder_to_purge = r""
cache_types = ["*.dwl", "*.dwl2", "*.bak", "*.log", "*.db", "*.tmp", "*.err"]


def delete_file(file_path):
    try:
        os.remove(file_path)
    except Exception as e:
        print(e)


if __name__ == "__main__":
    # get the target to delete files
    scaner, errors = Scandir(folder_to_purge, file_include=cache_types).collect()
    files_to_delete = []
    for file in scaner:
        if file.is_file:
            files_to_delete.append(os.path.join(folder_to_purge, file.path))
    if errors:
        for error in errors:
            print(error)

    total_files = len(files_to_delete)
    print(f"Found {total_files} files.")


    with ThreadPoolExecutor() as executor:
        executor.map(delete_file, files_to_delete)

    print("Purge Done!")
