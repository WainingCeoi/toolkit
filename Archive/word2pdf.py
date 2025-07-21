from multiprocessing import Pool, cpu_count
from ToolFunc.colloctor import collect_target_files
from ToolFunc.convertor import convert_word_to_pdf
import os

# Config Input and Output Path, and specify file type
word_folder = r"C:\Users\wei-ning.xu\OneDrive - Arup\Project\深圳魏桥\隔油设备"
sub_folder = False
pdf_folder = r"C:\Users\wei-ning.xu\Desktop\PDF"
extensions = [".docx", ".doc"]
bookmarks = 0


if __name__ == "__main__":
    os.makedirs(pdf_folder, exist_ok=True)
    # Collect all Word file
    word_files = collect_target_files(word_folder, extensions, sub_folder)
    print(f"Found {len(word_files)} Word Files.")
    '''
    Due to MS API and memery limitation, we can't process files simultaneously on different thread.
    If we load too much conversion task on thread, some file will crush and conversion will fail.
    Thus, for each single task, we convert 5 Word files simultaneously.
    Loop above task until all Word files are converted.
    '''
    # Config task parameters
    chunk_size = 5 # If encounter any unexpected crash event or error, please priority lower this valve.
    max_cpu = max(2, cpu_count() - 2)
    for i in range(0, len(word_files), chunk_size):
        # Create single task
        sub_word_files = word_files[i: i+chunk_size]
        # Load task on different thread to convert Word file simultaneously.
        args = [(word_file, pdf_folder, bookmarks) for word_file in sub_word_files] # Config parameters
        with Pool(processes=max_cpu) as pool:
            pool.starmap(convert_word_to_pdf, args, chunksize=None)

    print(f"\n{len(word_files)} Files have been Converted to PDF!")
