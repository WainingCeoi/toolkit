"""Detection benchmark: measured recall and false positives per watermark shape.

This file exists because the first fixtures lied by omission. They all tiled
their watermark axis-aligned, every test passed, and half of a real sample of
eight photos still fell back to the weaker detector — their overlays sit on
OBLIQUE lattices, which the detector had no way to express. Cases are kept here
whether or not they currently pass, so the gap is visible rather than absent.
"""

from __future__ import annotations

import numpy as np
import pytest
from watermark_fixtures import (
    RECTANGULAR,
    SHALLOW_OBLIQUE,
    STEEP_OBLIQUE,
    score,
    tiled_pair,
)

from watermark.detect import collect_marks, propose_mask_detailed


def propose(marked, sensitivity=50):
    return propose_mask_detailed(marked, sensitivity, detector="pattern")


# =========================================================================
# What the pattern detector handles today
# =========================================================================


def test_a_rectangular_lattice_is_recovered_and_masked_precisely():
    _clean, marked, truth = tiled_pair(basis=RECTANGULAR)
    mask, used = propose(marked)
    assert used == "pattern"
    recall, false_positives = score(mask, truth)
    assert recall > 0.25, f"only {recall:.1%} of the mark was masked"
    # Precision is the point of this detector: it stamps instances of the
    # recovered mark, so it should hardly touch anything else.
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


@pytest.mark.parametrize(
    "kwargs,least",
    [
        (dict(background="render_dither"), 0.15),
        (dict(background="gradient"), 0.60),
        (dict(alpha=22), 0.20),
        (dict(color=(20, 20, 20), alpha=55), 0.25),
    ],
    ids=["dither-bg", "gradient-bg", "faint-alpha-22", "dark-mark"],
)
def test_the_mark_is_found_across_opacities_and_backgrounds(kwargs, least):
    # The dual top-hat answers light and dark marks alike, and the fold does not
    # care either — these cases are here because the fixtures used to be unable
    # to express them. Their watermark was composited with paste(im, box, im),
    # which premultiplies twice: a light mark at 16% opacity was silently
    # rendered as a DARK one at 2%, so every case looked the same, and looked
    # nothing like the samples. See tiled_pair.
    _clean, marked, truth = tiled_pair(basis=RECTANGULAR, **kwargs)
    mask, used = propose(marked)
    assert used == "pattern"
    recall, false_positives = score(mask, truth)
    assert recall > least, f"only {recall:.1%} of the mark was masked"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


def test_the_primitive_lattice_is_used_not_a_multiple_of_it():
    # A doubled or tripled vector explains the autocorrelation peaks just as
    # well, but folding on it puts several instances in one tile; the crop then
    # straddles them and the stamps land between marks instead of on them.
    # Measured before this was fixed: only 13% of the stamped area had any
    # evidence under it.
    import cv2

    import watermark.pattern as pattern

    _clean, marked, _truth = tiled_pair(basis=RECTANGULAR)
    gray = cv2.cvtColor(marked, cv2.COLOR_RGB2GRAY)
    hp = pattern._highpass(gray)
    texture = pattern._local_texture(gray)
    quiet = (texture < np.percentile(texture, pattern._QUIET_PCT)).astype(np.float32)
    ac = pattern._masked_autocorrelation(hp, quiet)
    prominence = ac - cv2.GaussianBlur(ac, (0, 0), sigmaX=9)
    basis = pattern._fit_lattice(pattern._peaks(prominence, pattern._PEAK_COUNT))
    assert basis is not None

    lengths = sorted(
        [
            float(np.hypot(basis[0, 0], basis[1, 0])),
            float(np.hypot(basis[0, 1], basis[1, 1])),
        ]
    )
    expected = sorted([56.0, 114.0])  # the fixture's own basis
    assert lengths[0] == pytest.approx(expected[0], abs=4)
    assert lengths[1] == pytest.approx(expected[1], abs=4)


# =========================================================================
# False positives — the constraint that matters most without a brush
# =========================================================================


@pytest.mark.parametrize("background", ["sky_grass", "render_dither", "gradient"])
def test_a_clean_frame_is_never_pattern_masked(background):
    clean, _marked, _truth = tiled_pair(watermarked=False, background=background)
    mask, used = propose(clean)
    assert used == "none", "invented a repeating pattern in a clean frame"
    assert np.count_nonzero(mask) == 0


# =========================================================================
# Oblique lattices — the shape most real overlays actually use
# =========================================================================


@pytest.mark.parametrize(
    "basis,angle",
    [(SHALLOW_OBLIQUE, 11.0), (STEEP_OBLIQUE, -14.0)],
    ids=["shallow-11deg", "steep-76deg"],
)
def test_an_oblique_lattice_is_recovered(basis, angle):
    # These were xfail: the lattice fitter found the right grid, but rectifying
    # a sheared parallelogram onto a rectangle tripled the cell area and blew
    # the frame guard, so every oblique case silently fell back. Reducing the
    # basis and excluding the rectified frame's padding from the fold fixed it.
    _clean, marked, truth = tiled_pair(basis=basis, angle=angle)
    mask, used = propose(marked)
    assert used == "pattern"
    recall, false_positives = score(mask, truth)
    assert recall > 0.25, f"only {recall:.1%} of the mark was masked"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


# =========================================================================
# Sharing a mark across a batch
# =========================================================================


@pytest.mark.parametrize(
    "background,size",
    [("gradient", (1200, 800)), ("sky_grass", (980, 640))],
    ids=["other-background", "other-size"],
)
def test_a_mark_from_one_image_masks_the_same_overlay_on_another(background, size):
    """The transfer itself: a mark recovered on one frame masks a different one.

    What makes this worth having is that recovery and application need completely
    different things — see watermark.pattern.Mark. On the real sample this is why
    two more of eight images are masked at all: they carry the overlay three
    siblings recovered, but their own folds are refused, one for want of a quiet
    region and one for a lattice fitted to the furniture instead of the mark.

    The frames here recover their own marks too, so what is checked is that a
    FOREIGN mark lands accurately on a different photograph — a mark placed by a
    stale offset would fail the false-positive bound, not the recall one.

    Transfer is not unconditional, and the bound is the target's own texture
    rather than anything about the donor: measured with this donor, the same
    overlay correlates at 0.998 over sky and 0.997 over a gradient, but only 0.29
    once the background is noise of several times the mark's amplitude, well
    under the bar to be believed. The real samples that this rescues sit at
    0.87-0.93. An overlay drowned in the photograph's own texture is not
    recoverable from a sibling either.
    """
    from watermark import pattern

    _clean, donor, _truth = tiled_pair(basis=RECTANGULAR)
    marks = collect_marks([donor])
    assert marks, "the donor frame should offer its mark to the batch"

    _c, other, truth = tiled_pair(basis=RECTANGULAR, background=background, size=size)
    mask = pattern.apply_mark(other, marks[0], 50, own=False)
    assert mask is not None, "a foreign mark found none of an overlay it matches"
    recall, false_positives = score(mask, truth)
    assert recall > 0.15, f"borrowed mark masked only {recall:.1%} of the mark"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


def test_an_image_that_finds_its_own_mark_does_not_use_a_borrowed_one():
    # Sharing must only ever add. An image that recovers its own mark knows more
    # about itself than any sibling does, so the borrowed path must not preempt
    # it — only answer when it comes back empty.
    from watermark import pattern

    _clean, donor, _t = tiled_pair(basis=RECTANGULAR, background="render_dither")
    marks = collect_marks([donor])
    _c, own_frame, _t2 = tiled_pair(basis=RECTANGULAR)
    alone = pattern.propose_pattern_mask(own_frame, 50)
    assert alone is not None
    shared = pattern.propose_pattern_mask_shared(own_frame, 50, marks)
    assert np.array_equal(shared, alone)


@pytest.mark.parametrize("background", ["sky_grass", "render_dither", "gradient"])
def test_a_borrowed_mark_is_never_forced_onto_a_clean_frame(background):
    # The whole risk of sharing. A mark that masked its own image is real, but
    # that says nothing about the next image, and an over-eager match would
    # inpaint a photograph that has no watermark in it at all.
    _clean, marked, _truth = tiled_pair(basis=RECTANGULAR)
    marks = collect_marks([marked])
    assert marks
    clean, _m, _t = tiled_pair(watermarked=False, background=background)
    mask, used = propose_mask_detailed(clean, 50, detector="pattern", marks=marks)
    assert used == "none", "a borrowed mark was forced onto a clean frame"
    assert np.count_nonzero(mask) == 0


def test_a_mark_that_cannot_mask_its_own_image_is_not_offered_to_the_batch():
    # Corroboration is what keeps a lattice fitted to scenery from travelling.
    # Offering every recovered mark instead of only the ones that masked their
    # own image put a mask on a clean control frame.
    clean, _m, _t = tiled_pair(watermarked=False, background="sky_grass")
    assert collect_marks([clean]) == []


@pytest.mark.parametrize("noise", [0.0, 1.0, 2.0], ids=["flat", "quantised", "faint"])
def test_a_featureless_frame_holds_no_instances(noise):
    # Normalised correlation divides by the window's own deviation, so where
    # there is nothing under it — the white backdrop of a product render, a
    # blown-out sky — it divides ~0 by ~0. On a real render sample every
    # correlation peak scored a perfect 1.000 over windows of deviation 0.0000,
    # and those anchored the grid walk in empty sky. See _MIN_WINDOW_STD_SHARE.
    from watermark import pattern

    _clean, marked, _truth = tiled_pair(basis=RECTANGULAR)
    mark = pattern.recover_mark(marked)
    assert mark is not None

    rng = np.random.default_rng(5)
    level = np.clip(252 + rng.normal(0, noise, (600, 900)), 0, 255).astype(np.uint8)
    frame = np.dstack([level] * 3)
    assert pattern.apply_mark(frame, mark, 50, own=False) is None
    assert pattern.propose_pattern_mask(frame, 50) is None
