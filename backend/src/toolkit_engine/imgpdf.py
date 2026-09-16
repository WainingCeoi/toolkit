"""Image to PDF engine: combine uploaded images into one PDF's bytes."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .fsutil import natural_sort_key

register_heif_opener()

# Decompression-bomb guard; 256 MP is well past any real photo or scan.
MAX_PIXELS = 256_000_000


def images_to_pdf_bytes(named_files: list[tuple[str, bytes]]) -> bytes:
    """Combine (filename, bytes) images into a single multi-page PDF's bytes."""
    sorted_files = sorted(named_files, key=lambda x: natural_sort_key(x[0]))
    images = []
    for _, data in sorted_files:
        image = Image.open(BytesIO(data))
        w, h = image.size
        if w * h > MAX_PIXELS:
            raise ValueError(f"Image is too large to process ({w}×{h} pixels).")
        # PDF ignores EXIF; in_place, or Pillow copies even with nothing to rotate.
        ImageOps.exif_transpose(image, in_place=True)
        images.append(image.convert("RGB"))

    buffer = BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()
