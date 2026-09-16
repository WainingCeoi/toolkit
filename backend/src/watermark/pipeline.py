"""Mask-to-clean-image pipeline, plus the folder batch the CLI runs."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import cv2
import numpy as np

from .detect import (
    AUTO,
    DEFAULT_DETECTOR,
    DEFAULT_SENSITIVITY,
    PATTERN,
    collect_marks,
    propose_mask,
    repeating_evidence,
)
from .imgio import encode_png, load_rgb
from .inpaint import get_inpainter

# Only the formats the whole pipeline, browser canvas included, is exercised on.
IMAGE_TYPES = ("png", "jpg", "jpeg", "webp")

# A mask hugging the watermark too tightly leaves a one-pixel ghost outline.
DEFAULT_DILATE_PX = 3

# LaMa's peak memory grows with the frame handed to it; tiles keep it flat.
# 640/96 is tuned: larger tiles cost far more memory, smaller ones more time.
TILE_PX = 640
CONTEXT_PX = 96

# Rewriting above this is refused; the value sits between documents and photographs.
MAX_DESTRUCTION = 88.0


class CancelledError(Exception):
    """Raised out of a tiled inpaint when ``should_stop`` asked it to stop."""


def _inpaint_tiled(
    rgb: np.ndarray,
    mask: np.ndarray,
    inpaint: Callable[[np.ndarray, np.ndarray], np.ndarray],
    should_stop: Callable[[], bool] | None = None,
) -> np.ndarray:
    """Inpaint tile by tile, touching only tiles that contain masked pixels."""
    height, width = mask.shape
    out = rgb.copy()
    for top in range(0, height, TILE_PX):
        for left in range(0, width, TILE_PX):
            bottom, right = min(top + TILE_PX, height), min(left + TILE_PX, width)
            if not mask[top:bottom, left:right].any():
                continue
            # A big LaMa image is minutes of tiles; cancel cannot wait for the image.
            if should_stop is not None and should_stop():
                raise CancelledError
            # Context so tile-edge pixels are filled from real surroundings.
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
    should_stop: Callable[[], bool] | None = None,
) -> np.ndarray:
    """Inpaint ``mask`` out of ``rgb``; only masked pixels are ever written."""
    if dilate_px > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate_px + 1,) * 2)
        mask = cv2.dilate(mask, kernel)
    if not mask.any():
        return rgb.copy()
    return _inpaint_tiled(rgb, mask, inpaint, should_stop)


def destruction(rgb: np.ndarray, mask: np.ndarray, dilate_px: int) -> float:
    """How violently removing ``mask`` rewrites the image, in grey levels."""
    # Always probed with cv2 so the reading is comparable across inpainters.
    probe = remove_watermark(rgb, mask, get_inpainter("cv2"), dilate_px)
    moved = np.abs(rgb.astype(np.int16) - probe.astype(np.int16)).max(axis=2)
    changed = moved > 2
    if not changed.any():
        return 0.0
    return float(np.percentile(moved[changed], 90))


def would_destroy_content(rgb: np.ndarray, mask: np.ndarray, dilate_px: int) -> bool:
    """Whether removing this mask would cost more than the watermark is worth."""
    return destruction(rgb, mask, dilate_px) > MAX_DESTRUCTION


def list_images(folder: Path) -> list[Path]:
    """The images ``clean_folder`` would process, in name order."""
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower().lstrip(".") in IMAGE_TYPES
    )


def _unique_names(paths: list[Path]) -> list[str]:
    """PNG output names for ``paths``, disambiguating stem collisions."""
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


def _read_each(files: list[Path]) -> Iterator[np.ndarray]:
    """Every readable image in turn, skipping any that will fail later anyway."""
    for path in files:
        try:
            yield load_rgb(path.read_bytes())
        except Exception:  # noqa: BLE001,S112 — reported per file by the caller
            continue


def clean_folder(
    in_dir: str | Path,
    out_dir: str | Path,
    inpainter: str = "lama",
    sensitivity: int = DEFAULT_SENSITIVITY,
    dilate_px: int = DEFAULT_DILATE_PX,
    detector: str = DEFAULT_DETECTOR,
    on_progress: Callable[[int, int], bool] | None = None,
) -> tuple[list[str], list[str], list[str], list[tuple[str, str]]]:
    """Auto-mask and inpaint every image in ``in_dir`` into ``out_dir`` as PNGs."""
    src = Path(in_dir).expanduser()
    dst = Path(out_dir).expanduser()
    if not src.is_dir():
        raise ValueError(f"Input folder not found: {src}")
    # Same folder would overwrite images this run has not read yet.
    if dst.exists() and dst.resolve() == src.resolve():
        raise ValueError("Output folder must be different from the input folder.")
    inpaint = get_inpainter(inpainter)
    files = list_images(src)
    dst.mkdir(parents=True, exist_ok=True)

    # Batch marks first, so an image that cannot recover its own can borrow one.
    marks = []
    if detector in (PATTERN, AUTO):
        marks = collect_marks(lambda: _read_each(files), sensitivity)

    cleaned: list[str] = []
    skipped: list[str] = []
    protected: list[str] = []
    failed: list[tuple[str, str]] = []
    for idx, (path, out_name) in enumerate(
        zip(files, _unique_names(files), strict=True)
    ):
        try:
            rgb = load_rgb(path.read_bytes())
            mask = propose_mask(rgb, sensitivity, detector, marks)
            if not mask.any():
                # A mark is demonstrably there but could not be isolated: protected.
                if detector in (PATTERN, AUTO) and repeating_evidence(rgb):
                    protected.append(path.name)
                else:
                    skipped.append(path.name)
                if on_progress is not None and on_progress(idx + 1, len(files)):
                    break
                continue
            if would_destroy_content(rgb, mask, dilate_px):
                protected.append(path.name)
                if on_progress is not None and on_progress(idx + 1, len(files)):
                    break
                continue
            out = remove_watermark(rgb, mask, inpaint, dilate_px)
            (dst / out_name).write_bytes(encode_png(out))
            cleaned.append(out_name)
        except Exception as e:  # noqa: BLE001 — reported per file, batch goes on
            failed.append((path.name, str(e)))
        if on_progress is not None and on_progress(idx + 1, len(files)):
            break
    return cleaned, skipped, protected, failed
