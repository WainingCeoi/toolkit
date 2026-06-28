import os

import streamlit as st
from PIL import Image
from pillow_heif import register_heif_opener

# Let Pillow open HEIC/HEIF files (e.g. iPhone photos)
register_heif_opener()

st.title("🖼️ Image to PDF")
st.write("Combine selected images into a single PDF saved to your Desktop.")

# 1. UI Input: text box for the PDF name (placeholder leaves the value empty so
#    the blank-name check below can fire)
pdf_name = st.text_input(
    "Enter the output PDF file name:", placeholder="e.g. scanned_docs"
)

# 2. UI Input: file uploader widget
uploaded_files = st.file_uploader(
    "Select Image(s)",
    type=["png", "jpg", "jpeg", "heic"],
    accept_multiple_files=True,
)

# 3. Action Button
if st.button("Convert to PDF"):
    if not uploaded_files:
        st.error("❌ Please select at least one image first.")
    elif not pdf_name.strip():
        st.error("❌ Please enter a PDF file name.")
    else:
        with st.spinner("Processing images..."):
            try:
                # Sort by filename so page order is predictable
                sorted_files = sorted(uploaded_files, key=lambda x: x.name)
                images = [Image.open(f).convert("RGB") for f in sorted_files]

                # Define local save path
                output_path = os.path.expanduser("~/Desktop")
                name = pdf_name.strip()
                if not name.lower().endswith(".pdf"):
                    name += ".pdf"
                output_pdf = os.path.join(output_path, name)

                # Save the PDF
                images[0].save(output_pdf, save_all=True, append_images=images[1:])

                st.success(f"✅ Done! PDF saved to your Desktop as: {name}")
                st.toast(f"Image to PDF: saved {name}.", icon="🖼️")

            except Exception as e:
                st.error(f"❌ An error occurred: {e}")
