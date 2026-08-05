"""Watermark mask proposal: a dual top-hat filter for semi-transparent text.

Tiled text is a thin structure, locally brighter (light text) or darker (dark
text) than its surroundings. The white top-hat (image minus its opening)
responds to the bright case, the black top-hat (closing minus image) to the
dark case; the pixel-wise max answers both at once. Anything wider than the
structuring element — faces, sky, gradients — opens or closes onto itself and
cancels out of the response.

Detection runs at a bounded working size, not native resolution: a text
stroke that is 6 px wide at 1600 px is 30 px wide at 8000 px — wider than any
fixed structuring element, so at full resolution the filter would go blind on
exactly the images where it is slowest. The mask is scaled back up at the
end (nearest-neighbour; it is dilated before inpainting anyway).

Over-detection is fine by design, and the proposal is only a proposal: it is
reviewed by a human with a brush and an eraser before anything is inpainted.
A watermark faint enough to hide inside the scene's own texture (heavily
compressed screenshots, ~5% opacity marks) will not separate at any
sensitivity — that is what the brush is for.
"""

from __future__ import annotations

import cv2
import numpy as np

DEFAULT_SENSITIVITY = 50

# Longest image side the filter actually looks at (see module docstring).
_DETECT_MAX = 1600

# Structuring element diameter. Wider than a watermark text stroke at the
# working size (so strokes survive into the response) but narrower than real
# image features.
_KERNEL_SIZE = 13

# Sensitivity 0..100 maps linearly onto a threshold over the top-hat response:
# 0 keeps only screaming-bright overlays, 100 keeps nearly everything the
# filter noticed. A ~35%-opaque white watermark over a mid-gray background
# leaves a response of roughly 40 — the default of 50 sits just under that.
_THRESHOLD_MAX = 70
_THRESHOLD_MIN = 6


def propose_mask(rgb: np.ndarray, sensitivity: int = DEFAULT_SENSITIVITY) -> np.ndarray:
    """Propose a binary watermark mask (H, W) uint8 of {0, 255}.

    Higher ``sensitivity`` always marks a superset of what a lower one marks
    (the threshold moves; the morphology is monotone), so the slider behaves
    predictably in the UI.
    """
    sensitivity = max(0, min(100, sensitivity))
    threshold = round(
        _THRESHOLD_MAX - (_THRESHOLD_MAX - _THRESHOLD_MIN) * sensitivity / 100
    )

    height, width = rgb.shape[:2]
    scale = max(height, width) / _DETECT_MAX
    if scale > 1:
        # max(1, …): an extreme panorama's short side would otherwise round to
        # zero, and cv2.resize rejects an empty destination.
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
    response = cv2.max(bright, dark)

    mask = np.where(response >= threshold, 255, 0).astype(np.uint8)
    # Close small gaps so letter strokes merge into solid patches, then drop
    # single-pixel speckle. Both operators are increasing, which is what keeps
    # the sensitivity slider monotone end to end.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if scale > 1:
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    return mask
