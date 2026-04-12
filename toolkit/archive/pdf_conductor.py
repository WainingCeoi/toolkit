import os
from PIL import Image
from toolkit.ToolFunc.colloctor import collect_target_files

# Config parameters
input_path = os.path.expanduser("~/Desktop/raw")
output_path = os.path.expanduser("~/Desktop")
extension = [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff"]

# Collect all image files
image_files = collect_target_files(input_path, ext=extension, include_subfolder=False)


if image_files:
    print(f"Found {len(image_files)} Page.")
else:
    print("No images found in this folder.")
    exit()

# Sort image files by name
image_files.sort()

# Convert to PDF
images = []
for img_path in image_files:
    img = Image.open(img_path).convert('RGB')
    images.append(img)


# Prompt user for output PDF file name
# pdf_name = f"{input("Enter the output PDF file name: ")}.pdf"
pdf_name = r".pdf"
output_pdf = os.path.join(output_path, pdf_name)

# Save PDF
images[0].save(output_pdf, save_all=True, append_images=images[1:])
print("Done!")
