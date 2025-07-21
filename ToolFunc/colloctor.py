import os
import threading
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count


def collect_target_files(input_folder, ext, include_subfolder=True):
    # Collect all specified required files
    exts = {e.lower() for e in ext}
    input_folder = os.path.abspath(input_folder)
    collected_files = []
    dirs_line = [input_folder]
    lock = threading.Lock()
    max_cpu = max(2, cpu_count()-1)


    def process_dir(dir_path):
    # Inspect each file if it satisfies the requirement.
        local_files = []
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if entry.name.startswith("~$"):
                        continue
                    if entry.is_file():
                        suffix = os.path.splitext(entry.name)[1].lower()
                        if suffix in exts:
                            local_files.append(entry.path)
                    # Recursive call the function if subfolders are included.
                    elif include_subfolder and entry.is_dir():
                        with lock:
                            dirs_line.append(entry.path)
        except Exception:
            pass

        return local_files


    with ThreadPoolExecutor(max_workers=max_cpu) as executor:
        while dirs_line:
            batch = dirs_line[:]
            dirs_line.clear()
            for files in executor.map(process_dir, batch):
                collected_files.extend(files)

    return collected_files
