"""Watermark mask proposal: detector selection and the texture detector."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import cv2
import numpy as np

from .pattern import (
    Mark,
    _anchored_run,
    propose_pattern_mask,
    propose_pattern_mask_shared,
    shareable_marks,
)
from .stacked import MIN_STACK, StackedMark, recover_stacked, stamp_stacked

DEFAULT_SENSITIVITY = 50

# AUTO: pattern where a repeat is recoverable, texture only under the evidence gate.
AUTO = "auto"
PATTERN = "pattern"
TEXTURE = "texture"
DETECTORS = (AUTO, PATTERN, TEXTURE)
DEFAULT_DETECTOR = AUTO

# Reported with an empty mask when no watermark was found; the caller skips it.
NONE = "none"

# Reported, never chosen: the mask came from the batch-stacking route (stacked.py).
STACKED = "stacked"

# Longest side the filter runs at; at full resolution strokes outgrow the kernel.
_DETECT_MAX = 1600

# Wider than a text stroke at the working size, narrower than image features.
_KERNEL_SIZE = 13

# Baseline neighbourhood: spans several strokes, stays inside one region of the photo.
_NORM_WINDOW = 81

# Floor under the local baseline, so a flat region does not divide by ~0.
_NORM_FLOOR = 2.0

# Sensitivity 0..100 -> threshold over the normalised ratio; geometric, not linear.
_THRESHOLD_MAX = 5.0
_THRESHOLD_MIN = 1.45


def propose_mask(
    rgb: np.ndarray,
    sensitivity: int = DEFAULT_SENSITIVITY,
    detector: str = DEFAULT_DETECTOR,
    marks: Sequence[Mark | StackedMark] = (),
) -> np.ndarray:
    """Propose a binary watermark mask (H, W) uint8 of {0, 255}."""
    return propose_mask_detailed(rgb, sensitivity, detector, marks)[0]


def collect_marks(
    load: Callable[[], Iterable[np.ndarray]],
    sensitivity: int = DEFAULT_SENSITIVITY,
) -> list[Mark | StackedMark]:
    """Marks reusable across the batch; ``load`` is called more than once."""
    # Counts frames so the stacked pass, a second full read, runs only when it can.
    walked: list[int] = []

    def counting_load():
        def walk():
            count = 0
            for frame in load():
                count += 1
                yield frame
            walked.append(count)

        return walk()

    marks: list[Mark | StackedMark] = shareable_marks(counting_load, sensitivity)
    if walked and max(walked) >= MIN_STACK:
        stacked = recover_stacked(load)
        if stacked is not None:
            marks = [*marks, stacked]
    return marks


def propose_mask_detailed(
    rgb: np.ndarray,
    sensitivity: int = DEFAULT_SENSITIVITY,
    detector: str = DEFAULT_DETECTOR,
    marks: Sequence[Mark | StackedMark] = (),
) -> tuple[np.ndarray, str]:
    """The mask plus which detector produced it, or NONE and an empty mask."""
    mask, used, _evidence = _propose_with_evidence(rgb, sensitivity, detector, marks)
    return mask, used


def _propose_with_evidence(
    rgb: np.ndarray,
    sensitivity: int = DEFAULT_SENSITIVITY,
    detector: str = DEFAULT_DETECTOR,
    marks: Sequence[Mark | StackedMark] = (),
) -> tuple[np.ndarray, str, bool | None]:
    """The mask, the detector, and the repeat evidence if this route needed it."""
    if detector not in DETECTORS:
        raise ValueError(
            f"Unknown detector {detector!r} (choose from: {', '.join(DETECTORS)})."
        )
    if detector == TEXTURE:
        return propose_texture_mask(rgb, sensitivity), TEXTURE, None
    pattern_marks = [m for m in marks if isinstance(m, Mark)]
    stacked = next((m for m in marks if isinstance(m, StackedMark)), None)
    if pattern_marks:
        pattern = propose_pattern_mask_shared(rgb, sensitivity, pattern_marks)
    else:
        pattern = propose_pattern_mask(rgb, sensitivity)
    if pattern is not None:
        return pattern, PATTERN, None
    # Stacked runs after pattern, which masks actual copies rather than a consensus.
    if stacked is not None:
        mask = stamp_stacked(stacked, rgb.shape[:2], sensitivity)
        if mask.any():
            return mask, STACKED, None
    evidence = None
    if detector == AUTO:
        evidence = repeating_evidence(rgb)
        if evidence:
            texture = propose_texture_mask(rgb, sensitivity)
            if texture.any() and _worth_removing(rgb, texture):
                return texture, TEXTURE, evidence
    return np.zeros(rgb.shape[:2], np.uint8), NONE, evidence


def _worth_removing(rgb: np.ndarray, mask: np.ndarray) -> bool:
    # Imported at call time: pipeline imports this module.
    from .pipeline import DEFAULT_DILATE_PX, would_destroy_content

    return not would_destroy_content(rgb, mask, DEFAULT_DILATE_PX)


def repeating_evidence(rgb: np.ndarray) -> bool:
    """Whether a repeating mark is demonstrably present (used for wording only)."""
    return _anchored_run(rgb) is not None


def propose_texture_mask(
    rgb: np.ndarray, sensitivity: int = DEFAULT_SENSITIVITY
) -> np.ndarray:
    """Mask pixels that stand out from their neighbourhood; monotone in sensitivity."""
    sensitivity = max(0, min(100, sensitivity))
    threshold = _THRESHOLD_MAX * (_THRESHOLD_MIN / _THRESHOLD_MAX) ** (
        sensitivity / 100
    )

    height, width = rgb.shape[:2]
    scale = max(height, width) / _DETECT_MAX
    if scale > 1:
        # max(1, …): a panorama's short side would otherwise round to zero.
        work = cv2.resize(
            rgb,
            (max(1, round(width / scale)), max(1, round(height / scale))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        work = rgb

    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_KERNEL_SIZE,) * 2)
    bright = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    dark = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)
    response = cv2.max(bright, dark).astype(np.float32)

    # Ratio to the local baseline; a global threshold cannot span sky and grass.
    baseline = cv2.blur(response, (_NORM_WINDOW, _NORM_WINDOW))
    normalised = response / (baseline + _NORM_FLOOR)

    mask = np.where(normalised >= threshold, 255, 0).astype(np.uint8)
    # Both operators are increasing, which keeps the sensitivity slider monotone.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if scale > 1:
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    return mask
