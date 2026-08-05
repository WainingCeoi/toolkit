"""Mask-to-clean-image pipeline, plus the folder batch the CLI runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np

from .detect import DEFAULT_SENSITIVITY, propose_mask
from .imgio import encode_png, load_rgb
from .inpaint import get_inpainter

# What the tool accepts (bare suffixes, lowercase). Deliberately narrower than
# toolkit_engine's IMAGE_EXTENSIONS: these are the formats the whole pipeline
# is exercised against, browser canvas included.
IMAGE_TYPES = ("png", "jpg", "jpeg", "webp")

# The mask is grown a few px before inpainting: a proposal (or a brush stroke)
# that hugs the watermark too tightly leaves a one-pixel ghost outline behind.
DEFAULT_DILATE_PX = 3

# Inpainting runs in tiles of at most this many pixels a side, with the
# surrounding CONTEXT_PX of real image as context. LaMa's memory grows with the
# frame it is handed -- measured on CPU: 0.8 MP peaked at 12 GB and 3.1 MP at
# 25 GB, so a 36 MP phone photo (8064x4536) needs well over 100 GB and takes
# the process down before it ever returns. A tile keeps peak memory flat no
# matter how large the image is, and skipping tiles with nothing masked makes
# the cost track the watermark's area rather than the photo's.
#
# 640 is measured, not guessed: on a 12 MP frame, 640/96 peaked at 12.3 GB in
# 56s where 1024/128 took 29.9 GB in 54s -- 2.4x the memory to save 2 seconds.
# Smaller is not automatically better either; 512/96 was 14.0 GB in 63s, since
# more tiles means paying the model's fixed cost more often.
TILE_PX = 640
CONTEXT_PX = 96


def _inpaint_tiled(
    rgb: np.ndarray,
    mask: np.ndarray,
    inpaint: Callable[[np.ndarray, np.ndarray], np.ndarray],
) -> np.ndarray:
    """Inpaint tile by tile, touching only tiles that contain masked pixels."""
    height, width = mask.shape
    out = rgb.copy()
    for top in range(0, height, TILE_PX):
        for left in range(0, width, TILE_PX):
            bottom, right = min(top + TILE_PX, height), min(left + TILE_PX, width)
            if not mask[top:bottom, left:right].any():
                continue  # nothing to fill here
            # Widen the frame handed to the inpainter so pixels at the tile
            # edge are filled from real surroundings, not from the cut.
            ctop, cleft = max(0, top - CONTEXT_PX), max(0, left - CONTEXT_PX)
            cbottom = min(height, bottom + CONTEXT_PX)
            cright = min(width, right + CONTEXT_PX)
            patch = inpaint(
                np.ascontiguousarray(rgb[ctop:cbottom, cleft:cright]),
                np.ascontiguousarray(mask[ctop:cbottom, cleft:cright]),
            )
            core = mask[top:bottom, left:right] > 0
            out[top:bottom, left:right][core] = patch[
                top - ctop : bottom - ctop, left - cleft : right - cleft
            ][core]
    return out


def remove_watermark(
    rgb: np.ndarray,
    mask: np.ndarray,
    inpaint: Callable[[np.ndarray, np.ndarray], np.ndarray],
    dilate_px: int = DEFAULT_DILATE_PX,
) -> np.ndarray:
    """Inpaint ``mask`` out of ``rgb`` and return the cleaned copy.

    Only masked pixels are ever written. LaMa reconstructs the whole frame it
    is given, so without this it would subtly rewrite untouched parts of the
    photo; compositing keeps the guarantee that what you did not mark is
    bit-identical to what you uploaded.
    """
    if dilate_px > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate_px + 1,) * 2)
        mask = cv2.dilate(mask, kernel)
    if not mask.any():
        return rgb.copy()
    return _inpaint_tiled(rgb, mask, inpaint)


def list_images(folder: Path) -> list[Path]:
    """The images ``clean_folder`` would process, in name order."""
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower().lstrip(".") in IMAGE_TYPES
    )


def _unique_names(paths: list[Path]) -> list[str]:
    """Output names for ``paths``, disambiguating collisions.

    Every output is a PNG, so "photo.jpg" and "photo.png" both want
    "photo.png" — one would silently overwrite the other and still be counted
    as cleaned. The second becomes "photo (2).png", as elsewhere in the app.
    """
    names: list[str] = []
    taken: set[str] = set()
    for path in paths:
        name = f"{path.stem}.png"
        counter = 2
        while name in taken:
            name = f"{path.stem} ({counter}).png"
            counter += 1
        taken.add(name)
        names.append(name)
    return names


def clean_folder(
    in_dir: str | Path,
    out_dir: str | Path,
    inpainter: str = "lama",
    sensitivity: int = DEFAULT_SENSITIVITY,
    dilate_px: int = DEFAULT_DILATE_PX,
    on_progress: Callable[[int, int], bool] | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Headless batch: auto-mask and inpaint every image in ``in_dir``.

    Cleaned images are written to ``out_dir`` as PNGs (same stem — inpainted
    pixels re-encoded as JPEG would pick up fresh artifacts around the fill).
    Returns (cleaned names, failed (name, error) pairs); a bad file never
    aborts the batch. `on_progress(done, total)` is called after each file;
    returning True stops the run early (cancellation).
    """
    src = Path(in_dir).expanduser()
    dst = Path(out_dir).expanduser()
    if not src.is_dir():
        raise ValueError(f"Input folder not found: {src}")
    # Writing into the input folder would overwrite images this run has not
    # read yet, so the later ones would be cleaned twice.
    if dst.exists() and dst.resolve() == src.resolve():
        raise ValueError("Output folder must be different from the input folder.")
    inpaint = get_inpainter(inpainter)
    files = list_images(src)
    dst.mkdir(parents=True, exist_ok=True)

    cleaned: list[str] = []
    failed: list[tuple[str, str]] = []
    for idx, (path, out_name) in enumerate(
        zip(files, _unique_names(files), strict=True)
    ):
        try:
            rgb = load_rgb(path.read_bytes())
            mask = propose_mask(rgb, sensitivity)
            out = remove_watermark(rgb, mask, inpaint, dilate_px)
            (dst / out_name).write_bytes(encode_png(out))
            cleaned.append(out_name)
        except Exception as e:  # noqa: BLE001 — reported per file, batch goes on
            failed.append((path.name, str(e)))
        if on_progress is not None and on_progress(idx + 1, len(files)):
            break
    return cleaned, failed
