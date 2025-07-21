import os
from PIL import Image
from ToolFunc.colloctor import collect_target_files

# Config parameters
input_path = r"C:\Users\wei-ning.xu\Desktop\1"
output_path = r"C:\Users\wei-ning.xu\Desktop"
extention = [".jpg", ".jpeg", ".png"]

# Collect all image files
image_files = collect_target_files(input_path, ext=extention, include_subfolder=False)


if image_files:
    print(f"Found {len(image_files)} Page.")
else:
    print("No images found in this folder.")
    exit()

# Rename files
renamed_files = []

for old_file_path in image_files:
    file_name = os.path.basename(old_file_path)
    new_name = file_name.replace("bg", "")
    base_name, file_extension = os.path.splitext(new_name)
    decimal_value = int(base_name, 16)

    new_file_name = f"Page {decimal_value}{file_extension}"
    dir_path = os.path.dirname(old_file_path)
    new_file_path = os.path.join(dir_path, new_file_name)
    os.rename(old_file_path, new_file_path)

    renamed_files.append(new_file_path)

# Sort image files by name
renamed_files.sort(key=lambda x: int(''.join(filter(str.isdigit, os.path.basename(x)))))

# Convert to PDF
images = []
for img_path in renamed_files:
    img = Image.open(img_path).convert('RGB')
    images.append(img)


# Prompt user for output PDF file name
pdf_name = f"{input("Enter the output PDF file name: ")}.pdf"
output_pdf = os.path.join(output_path, pdf_name)

# Save PDF
images[0].save(output_pdf, save_all=True, append_images=images[1:])
print("Done!")