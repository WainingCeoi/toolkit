"""Image bytes <-> arrays, normalized once so every layer sees the same pixels."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageOps

# Decompression-bomb guard: refuse before decode allocates gigabytes.
MAX_PIXELS = 256_000_000


def load_rgb(data: bytes) -> np.ndarray:
    """Decode to EXIF-upright RGB (H, W, 3) uint8; browsers rotate, OpenCV does not."""
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
    """Decode mask PNG bytes to a binary (H, W) uint8 array of {0, 255}."""
    mask = np.asarray(Image.open(io.BytesIO(data)).convert("L"))
    if mask.shape != shape:
        raise ValueError(
            f"Mask is {mask.shape[1]}×{mask.shape[0]} but the image is "
            f"{shape[1]}×{shape[0]}."
        )
    return np.where(mask > 127, 255, 0).astype(np.uint8)
