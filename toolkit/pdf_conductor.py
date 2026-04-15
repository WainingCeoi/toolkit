import os
from PIL import Image
from tkinter.filedialog import askopenfilenames as get_files


# Config parameter
output_path = os.path.expanduser("~/Desktop")

# Collect images
image_files = sorted(get_files(title="Please select image(s)"))
if image_files:
    print(f"Processing {len(image_files)} Page...")
else:
    print("❌ No image selected.")
    exit()


# Convert to PDF
images = []
for img_path in image_files:
    img = Image.open(img_path).convert("RGB")
    images.append(img)


# Prompt for PDF name
pdf_name = f"{input("Enter the output PDF file name: ")}.pdf"
output_pdf = os.path.join(output_path, pdf_name)

# Save PDF
images[0].save(output_pdf, save_all=True, append_images=images[1:])
print("Done!")
