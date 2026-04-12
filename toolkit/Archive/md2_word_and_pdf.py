from toolkit.ToolFunc.colloctor import collect_target_files
from toolkit.ToolFunc.convertor import convert_md_to_word_and_pdf
from multiprocessing import Pool, cpu_count
import os


# Config input and output
md_folder = r"C:\Users\wei-ning.xu\OneDrive - Arup\Project\深圳魏桥\隔油设备"
sub_folder = True
word_folder = r"C:\Users\wei-ning.xu\Desktop"
pdf_folder = r"C:\Users\wei-ning.xu\Desktop"
extensions = [".md"]
bookmarks = 1


if __name__ == "__main__":
    os.makedirs(word_folder, exist_ok=True)
    os.makedirs(pdf_folder, exist_ok=True)

    # Collected all to be converted markdown files.
    md_files = collect_target_files(md_folder, extensions, sub_folder)
    print(f"Found {len(md_files)} MarkDown Files.")
    # Config task parameters
    chunk_size = 3 # If encounter any unexpected crash event or error, please priority lower this valve.
    max_cpu = max(2, cpu_count()-2)

    # Passing 5 (chunk size) markdown files for simultaneously converting process each time.
    for i in range(0, len(md_files), chunk_size):
        sub_md_files = md_files[i:i+chunk_size]
        args = [(md_file, word_folder, pdf_folder, bookmarks) for md_file in sub_md_files]
        with Pool(processes=max_cpu) as pool:
            pool.starmap(convert_md_to_word_and_pdf, args)

    print(f"\n{len(md_files)} MarkDown have been Converted to Word and PDF!")
