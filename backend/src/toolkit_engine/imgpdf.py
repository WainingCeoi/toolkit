"""Image to PDF engine: combine uploaded images into one PDF's bytes."""

from __future__ import annotations

from contextlib import ExitStack, closing
from io import BytesIO

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from .fsutil import natural_sort_key

register_heif_opener()

# Decompression-bomb guard; 256 MP is well past any real photo or scan.
MAX_PIXELS = 256_000_000
# Keep decoded batches small; one larger page is still allowed up to MAX_PIXELS.
MAX_BATCH_PIXELS = 16_000_000


def images_to_pdf_bytes(named_files: list[tuple[str, bytes]]) -> bytes:
    """Combine (filename, bytes) images into a single multi-page PDF's bytes."""
    if not named_files:
        raise ValueError("Select at least one image to create a PDF.")
    sorted_files = sorted(named_files, key=lambda x: natural_sort_key(x[0]))
    images = []
    pixels = 0
    with BytesIO() as buffer, ExitStack() as decoded:

        def flush():
            if not images:
                return
            # ponytail: append reparses the PDF; use a streaming writer if very
            # large batch counts become a measured bottleneck.
            images[0].save(
                buffer,
                format="PDF",
                save_all=True,
                append_images=images[1:],
                append=buffer.tell() > 0,
            )
            decoded.close()
            images.clear()

        for _, data in sorted_files:
            with closing(Image.open(BytesIO(data))) as image:
                w, h = image.size
                if w * h > MAX_PIXELS:
                    raise ValueError(f"Image is too large to process ({w}×{h} pixels).")
                if images and pixels + w * h > MAX_BATCH_PIXELS:
                    flush()
                    pixels = 0
                # PDF ignores EXIF; avoid another pixel copy when nothing rotates.
                ImageOps.exif_transpose(image, in_place=True)
                rgb = image.convert("RGB")
                decoded.callback(rgb.close)
                images.append(rgb)
                pixels += w * h
        flush()
        return buffer.getvalue()
