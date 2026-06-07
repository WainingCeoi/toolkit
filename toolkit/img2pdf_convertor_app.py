import os

import streamlit as st
from PIL import Image


# Set up the title of your web page
st.title("🖼️ Image to PDF Converter")
st.write("Upload your images, name your file, and convert them instantly over the local network.")

# 1. UI Input: Text box for the PDF name
pdf_name = st.text_input("Enter the output PDF file name:", "PDF Name")

# 2. UI Input: File uploader widget
uploaded_files = st.file_uploader(
    "Select Image(s)", 
    type=["png", "jpg", "jpeg", "heic"], 
    accept_multiple_files=True
)

# 3. Action Button
if st.button("Convert to PDF"):
    if not uploaded_files:
        st.error("❌ Please select at least one image first.")
    elif not pdf_name:
        st.error("❌ Please enter a PDF file name.")
    else:
        with st.spinner("Processing images..."):
            try:
                # Process the images
                images = []
                # Streamlit automatically handles sorting if you upload them together, 
                # or you can sort by the uploaded file's name:
                sorted_files = sorted(uploaded_files, key=lambda x: x.name)

                for uploaded_file in sorted_files:
                    img = Image.open(uploaded_file).convert("RGB")
                    images.append(img)

                # Define local save path
                output_path = os.path.expanduser("~/Desktop")
                
                if not pdf_name.lower().endswith(".pdf"):
                    pdf_name += ".pdf"
                    
                output_pdf = os.path.join(output_path, pdf_name)

                # Save the PDF
                images[0].save(output_pdf, save_all=True, append_images=images[1:])
                
                st.success(f"✅ Done! PDF saved to your Desktop as: {pdf_name}")
                
            except Exception as e:
                st.error(f"An error occurred: {e}")
