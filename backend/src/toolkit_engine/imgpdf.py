"""Image to PDF engine — lifted from the Streamlit page img_to_pdf.py.

The page wrote the combined PDF to ~/Desktop; here the same images are
combined into PDF bytes returned to the caller so the router can serve
them as a download (deliberate redesign — the filename sorting and the
RGB conversion are the page's behavior, unchanged).
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image
from pillow_heif import register_heif_opener

# Let Pillow open HEIC/HEIF files (e.g. iPhone photos)
register_heif_opener()


def images_to_pdf_bytes(named_files: list[tuple[str, bytes]]) -> bytes:
    """Combine (filename, bytes) images into a single multi-page PDF's bytes."""
    # Sort by filename so page order is predictable
    sorted_files = sorted(named_files, key=lambda x: x[0])
    images = [Image.open(BytesIO(data)).convert("RGB") for _, data in sorted_files]

    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()
