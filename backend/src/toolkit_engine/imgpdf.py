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

from .fsutil import natural_sort_key

# Let Pillow open HEIC/HEIF files (e.g. iPhone photos)
register_heif_opener()

# Refuse absurdly large images: a decompression bomb (tiny file, enormous pixel
# dimensions) would otherwise allocate gigabytes on decode. 256 MP covers any
# real photo/scan with wide margin.
MAX_PIXELS = 256_000_000


def images_to_pdf_bytes(named_files: list[tuple[str, bytes]]) -> bytes:
    """Combine (filename, bytes) images into a single multi-page PDF's bytes."""
    # Natural sort so numbered pages order as a human expects (2 before 10).
    sorted_files = sorted(named_files, key=lambda x: natural_sort_key(x[0]))
    images = []
    for _, data in sorted_files:
        image = Image.open(BytesIO(data))
        w, h = image.size
        if w * h > MAX_PIXELS:
            raise ValueError(
                f"Image is too large to process ({w}×{h} pixels)."
            )
        images.append(image.convert("RGB"))

    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()
