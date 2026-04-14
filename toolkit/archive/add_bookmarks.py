from toolkit.tool_function.fetchor import add_bookmark
import os


input_folder = r""
output_folder = r""


files = os.scandir(input_folder)
for idx, file in enumerate(files):
    url = input(f"{idx+1}. {file.name}: ")
    input_pdf = file.path
    output_pdf = os.path.join(output_folder, file.name)
    add_bookmark(url, input_pdf, output_pdf)
