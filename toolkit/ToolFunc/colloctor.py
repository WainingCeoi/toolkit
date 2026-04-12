import os
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count


def collect_target_files(input_folder, file_types, include_subfolder=True):
    # Collect files with given extensions from a folder.
    file_types = {e.lower() for e in file_types}
    input_folder = os.path.abspath(input_folder)
    max_workers = max(2, cpu_count()-1)


    def filter_files(input_files):
        filtered = []
        for filtering_file in input_files:
            name = os.path.basename(filtering_file)
            file_type = os.path.splitext(name)[1].lower()
            if not os.path.basename(filtering_file).startswith("~$") and file_type in file_types:
                filtered.append(filtering_file)
        return filtered


    # Walk directory once and build file list
    all_files = []
    for root, dirs, files in os.walk(input_folder):
        for file in files:
            all_files.append(os.path.join(root, file))
        if not include_subfolder:
            break


    # Threaded Filtering
    collected_files = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(filter_files, (all_files[idx::max_workers] for idx in range(max_workers)))
        for chunk_files in results:
            collected_files.extend(chunk_files)

    return collected_files
