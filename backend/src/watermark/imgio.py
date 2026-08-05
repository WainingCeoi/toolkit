"""Image bytes <-> arrays, normalized once so every layer sees the same pixels.

The auto-mask, the browser canvas, the inpainter and the before/after view
must agree pixel-for-pixel. Browsers apply EXIF rotation when they decode;
OpenCV ignores it — a phone photo would get a mask drawn on a rotated copy of
itself. So images are normalized at the door (EXIF-transposed, forced to RGB)
and only the normalized pixels ever leave this module.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageOps

# Same guard as Image to PDF: a decompression bomb (tiny file, enormous pixel
# dimensions) would otherwise allocate gigabytes on decode. 256 MP covers any
# real photo/scan with wide margin.
MAX_PIXELS = 256_000_000


def load_rgb(data: bytes) -> np.ndarray:
    """Decode image bytes to an EXIF-upright RGB array (H, W, 3) uint8."""
    image = Image.open(io.BytesIO(data))
    w, h = image.size
    if w * h > MAX_PIXELS:
        raise ValueError(f"Image is too large to process ({w}×{h} pixels).")
    upright = ImageOps.exif_transpose(image)
    return np.asarray(upright.convert("RGB"))


def encode_png(rgb: np.ndarray) -> bytes:
    """Encode an (H, W, 3) RGB or (H, W) grayscale array as PNG bytes."""
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="PNG")
    return buffer.getvalue()


def load_mask(data: bytes, shape: tuple[int, int]) -> np.ndarray:
    """Decode mask PNG bytes to a binary (H, W) uint8 array of {0, 255}.

    ``shape`` is the (height, width) of the image the mask belongs to; a mask
    of any other size is drawn on different pixels than it will be applied to,
    so it is refused rather than resized.
    """
    mask = np.asarray(Image.open(io.BytesIO(data)).convert("L"))
    if mask.shape != shape:
        raise ValueError(
            f"Mask is {mask.shape[1]}×{mask.shape[0]} but the image is "
            f"{shape[1]}×{shape[0]}."
        )
    return np.where(mask > 127, 255, 0).astype(np.uint8)
