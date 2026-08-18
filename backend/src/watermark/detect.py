"""Watermark mask proposal.

Two detectors:

- ``pattern`` (pattern.py, the default) recovers a repeating watermark by
  folding its tiles together and matching the recovered mark back. It marks
  pattern instances and nothing else, so thin image detail survives — but it
  needs the watermark to actually repeat on a grid.
- ``texture`` (below) judges each pixel against its own neighbourhood. It
  applies to any watermark, including a single corner logo, at the cost of also
  flagging thin image detail: tent seams, wires, railings.

``pattern`` leads because when it applies it is dramatically more precise, and
because it now knows when it does not apply. Every mask it proposes is checked
against the image — a stamped pixel survives only where the photo really does
deviate there — and if little of the stamp survives, the "repeat" was an
artefact of the period estimate and None comes back. Measured over sample
photos that check separates cleanly: 0.53-0.57 of the stamp survived where the
mark was genuinely recovered, against 0.00-0.20 where the period estimate had
locked onto scenery, including 0.00 on a watermark-free photo.

That check is what makes leading with ``pattern`` safe. Without it, a fold
"finding something" does not distinguish a watermark from any other structure
consistent across the frame: a CLEAN photo scored 4.7 on fold significance
alone, having locked onto its own sky gradient, while genuinely watermarked
photos scored 0.9-1.1.

Falling back is per image, so a batch can mix the two, and the caller is always
told which one ran.

The texture detector: a dual top-hat filter for semi-transparent text.

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

The raw response cannot be thresholded globally, though. A faint grey mark on
smooth sky may respond at 8, while grass or gravel responds at 40 without any
watermark in it — so the threshold that catches the mark also selects half the
photo. The fix is to divide the response by the response typical of each
pixel's own neighbourhood: what matters is standing out LOCALLY, not
absolutely. Measured against a synthetic hard case (smooth sky + textured
grass, faint tiled text over both), at 60% recall this cut false positives
from 55.1% of the image to 13.3% — 4.1x fewer.

Over-detection used to be fine by design, because a human corrected the proposal
with a brush before anything was inpainted. The brush exists again — it came
back for the watermark that never repeats, which no detector here can find —
but a default cannot lean on it: most proposals ship exactly as shown, so a
mask nobody is going to correct has to be right, and this one marks thin image
detail along with the mark. That is why this detector runs only when asked for
by name, or under AUTO's evidence-and-worth gate (see propose_mask_detailed).
A watermark faint enough to hide inside the scene's own texture does not
separate at any sensitivity, and the honest answer for those is the empty mask
``pattern`` returns, which the caller skips.
"""

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

DEFAULT_SENSITIVITY = 50

# Which detector to use, and which produced a mask. AUTO is the mode callers
# should not have to think about: pattern where a repeat is recoverable, and
# texture only for an image that demonstrably carries a repeating mark the
# pattern routes could not turn into a mask (see propose_mask_detailed).
AUTO = "auto"
PATTERN = "pattern"
TEXTURE = "texture"
DETECTORS = (AUTO, PATTERN, TEXTURE)
DEFAULT_DETECTOR = AUTO

# Reported instead of a detector name when no watermark could be found, with an
# empty mask. NOT a failure to handle quietly: it means "leave this image
# alone", and the caller must skip it rather than inpaint anything.
NONE = "none"

# Longest image side the filter actually looks at (see module docstring).
_DETECT_MAX = 1600

# Structuring element diameter. Wider than a watermark text stroke at the
# working size (so strokes survive into the response) but narrower than real
# image features.
_KERNEL_SIZE = 13

# Neighbourhood the local response is measured against. Wide enough to span
# several watermark strokes (so the mark itself does not dominate its own
# baseline) but well inside a photo's regions, so sky and grass get their own.
_NORM_WINDOW = 81

# Floor under the local baseline, in gray levels. Without it a perfectly flat
# region — a blown-out sky, the white background of a product render — divides
# by ~0 and amplifies sensor noise into a full-frame mask.
_NORM_FLOOR = 2.0

# Sensitivity 0..100 maps onto a threshold over the NORMALISED response, which
# is a ratio: 1.0 means "as strong as this neighbourhood's usual texture".
#
# The mapping is geometric, not linear, because the interesting behaviour is
# all at the low end. Linearly, everything below ~1.5 crammed into the last few
# steps and 100 fell off a cliff (50% of the image marked); geometrically the
# same span spreads across the whole slider and 100 lands at 8.7%.
_THRESHOLD_MAX = 5.0
_THRESHOLD_MIN = 1.45


def propose_mask(
    rgb: np.ndarray,
    sensitivity: int = DEFAULT_SENSITIVITY,
    detector: str = DEFAULT_DETECTOR,
    marks: Sequence[Mark] = (),
) -> np.ndarray:
    """Propose a binary watermark mask (H, W) uint8 of {0, 255}."""
    return propose_mask_detailed(rgb, sensitivity, detector, marks)[0]


def collect_marks(
    load: Callable[[], Iterable[np.ndarray]],
    sensitivity: int = DEFAULT_SENSITIVITY,
) -> list[Mark]:
    """Marks from a batch that can be reused on the rest of it (see pattern.py).

    Run this over the batch before masking any of it, and pass the result to
    ``propose_mask``. It only ever helps: an image that finds its own mark is
    unaffected, and one that does not gets the chance to be masked from a
    sibling's instead of skipped.

    ``load`` yields the batch and may be called more than once — the sparse pass
    needs a second look at the images — so hand over a function that re-reads
    them, not an iterator that can only be walked once.
    """
    return shareable_marks(load, sensitivity)


def propose_mask_detailed(
    rgb: np.ndarray,
    sensitivity: int = DEFAULT_SENSITIVITY,
    detector: str = DEFAULT_DETECTOR,
    marks: Sequence[Mark] = (),
) -> tuple[np.ndarray, str]:
    """The mask plus which detector produced it, or NONE and an empty mask.

    PATTERN does NOT fall back to TEXTURE. It used to, unconditionally, and
    that was actively harmful: on a sample of eight photos, six had no
    recoverable repeat, so six got a texture mask instead — which marks thin
    image detail like tent seams and railings, not the watermark. Inpainting
    that damaged the photo AND left the watermark in place, which is worse
    than doing nothing. Asked for by name, PATTERN answers NONE for those.

    AUTO is the difference between that lesson and abandoning the images it
    was learned on. It leads with PATTERN, and falls back to TEXTURE only when
    two things hold at once: the image DEMONSTRABLY carries a repeating mark
    (repeating_evidence), and removing the texture mask would not cost more
    than the mark is worth (_worth_removing). The first condition is what the
    blanket fallback got wrong in reverse — the six damaged photos had no such
    evidence reading. The second is what stops the fallback offering a mask of
    the picture: on a product sheet whose watermark is a large faint per-panel
    logo, texture marks the mullions and the body text instead, and that offer
    is now withheld rather than shown and then vetoed downstream.

    An image with neither a recoverable repeat nor evidence of one stays NONE,
    so a clean photograph in an AUTO batch is never handed a texture mask of
    its own seams.

    TEXTURE still runs when it is asked for explicitly.
    """
    if detector not in DETECTORS:
        raise ValueError(
            f"Unknown detector {detector!r} (choose from: {', '.join(DETECTORS)})."
        )
    if detector == TEXTURE:
        return propose_texture_mask(rgb, sensitivity), TEXTURE
    if marks:
        pattern = propose_pattern_mask_shared(rgb, sensitivity, marks)
    else:
        pattern = propose_pattern_mask(rgb, sensitivity)
    if pattern is not None:
        return pattern, PATTERN
    if detector == AUTO and repeating_evidence(rgb):
        texture = propose_texture_mask(rgb, sensitivity)
        if texture.any() and _worth_removing(rgb, texture):
            return texture, TEXTURE
    return np.zeros(rgb.shape[:2], np.uint8), NONE


def _worth_removing(rgb: np.ndarray, mask: np.ndarray) -> bool:
    """Whether removing ``mask`` would cost less than the watermark is worth.

    The fallback is only allowed to OFFER a mask it would actually be allowed
    to use. Without this the auto route proposed a texture mask on a product
    sheet whose real watermark is a large faint logo, one per photo panel at a
    different size — a shape the texture filter cannot see. What it marked
    instead was the content: window mullions, furniture edges, body text. The
    run's own guard then refused that mask (measured: a destruction of 206
    against a bar of 88) so nothing was ever damaged, but the review panel had
    already shown a page covered in red over the picture rather than the mark,
    and the reason given for skipping the image was wrong.

    Asking the same question at proposal time makes the offer honest: the
    fallback appears only where it is real, and where it is not, the image is
    reported as carrying a watermark that could not be isolated. Only the
    fallback pays for this — the pattern routes mask their own recovered mark
    and answer for themselves.
    """
    # Imported at call time, not module scope: pipeline imports this module, so
    # the dependency can only close once something actually asks the question.
    from .pipeline import DEFAULT_DILATE_PX, would_destroy_content

    return not would_destroy_content(rgb, mask, DEFAULT_DILATE_PX)


def repeating_evidence(rgb: np.ndarray) -> bool:
    """Whether a repeating mark is demonstrably present, maskable or not.

    True when the image contains an evenly spaced run of mutually correlating
    patches -- the signature of a tiled overlay -- regardless of whether any
    route could turn that into a mask. The one image of the sample of nine that
    comes back with nothing is exactly this case: a chat-app screenshot whose
    logos repeat at a pitch nothing else in the batch shares, over a document
    that no mask could be cut for without destroying it (texture's mask on it
    measures a destruction of 206 against the bar of 88).

    The caller uses this to tell "no watermark found" from "a watermark is
    visible but cannot be removed safely" -- opposite messages to put on an
    image that comes back unmasked. It decides WORDS, never pixels: nothing is
    ever inpainted on its say-so, which is why an occasional false positive on
    coincidentally regular scenery costs a mislabel and not a photograph.
    """
    return _anchored_run(rgb) is not None


def propose_texture_mask(
    rgb: np.ndarray, sensitivity: int = DEFAULT_SENSITIVITY
) -> np.ndarray:
    """Mask pixels that stand out from their own neighbourhood.

    Higher ``sensitivity`` always marks a superset of what a lower one marks
    (the threshold moves; the morphology is monotone), so the slider behaves
    predictably in the UI.
    """
    sensitivity = max(0, min(100, sensitivity))
    threshold = _THRESHOLD_MAX * (_THRESHOLD_MIN / _THRESHOLD_MAX) ** (
        sensitivity / 100
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
    response = cv2.max(bright, dark).astype(np.float32)

    # Local contrast normalisation — the whole reason this generalises across
    # a photo. cv2.blur is a box mean, so this is "how many times the local
    # baseline is this pixel", and the comparison is scale-free.
    baseline = cv2.blur(response, (_NORM_WINDOW, _NORM_WINDOW))
    normalised = response / (baseline + _NORM_FLOOR)

    mask = np.where(normalised >= threshold, 255, 0).astype(np.uint8)
    # Close small gaps so letter strokes merge into solid patches, then drop
    # single-pixel speckle. Both operators are increasing, which is what keeps
    # the sensitivity slider monotone end to end.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    if scale > 1:
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    return mask
