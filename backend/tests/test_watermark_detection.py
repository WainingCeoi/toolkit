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

from watermark.detect import propose_mask_detailed


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
    assert recall > 0.05, "nothing of the mark was masked"
    # Precision is the point of this detector: it stamps instances of the
    # recovered mark, so it should hardly touch anything else.
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
    recall, _fp = score(mask, truth)
    assert recall > 0.05
