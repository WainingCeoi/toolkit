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


@pytest.mark.parametrize(
    "background", ["sky_grass", "render_dither", "gradient", "grass"]
)
@pytest.mark.parametrize(
    "size", [(1200, 800), (800, 600), (1400, 900), (2100, 1100), (1300, 700)]
)
@pytest.mark.parametrize("sensitivity", [0, 50, 100])
def test_a_clean_frame_is_never_pattern_masked(background, size, sensitivity):
    """The bar that matters most, now swept rather than sampled.

    This used to run three backgrounds at tiled_pair's default 1200x800 alone --
    and that size happens to be one that does NOT leak, so it reported a clean
    bill while the detector was masking up to 8.44% of watermark-free frames at
    other sizes. Nine of 96 swept frames leaked before the on-lattice gate in
    apply_mark; none of 144 do now.
    """
    clean, _marked, _truth = tiled_pair(
        watermarked=False, background=background, size=size
    )
    mask, used = propose(clean, sensitivity)
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
    marks = collect_marks(lambda: [donor])
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
    marks = collect_marks(lambda: [donor])
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
    marks = collect_marks(lambda: [marked])
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
    assert collect_marks(lambda: [clean]) == []


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


# =========================================================================
# The sparse route: marks too large for any single frame to fold
# =========================================================================


def _sparse_batch(count=3, size=(1300, 800), seed=0):
    """Frames carrying a mark on a cell so large only a few copies fit.

    A 300px cell puts about six instances in frame where the fold needs nine, so
    no image here can recover its own mark however clear the mark is. Backgrounds
    differ per frame, which is what lets pooling cancel the scenery.
    """
    out = []
    for index in range(count):
        _clean, marked, truth = tiled_pair(
            basis=((0, 300), (300, 0)),
            size=size,
            background=["gradient", "sky_grass", "render_dither"][index % 3],
            glyph_size=30,
            alpha=70,
        )
        out.append((marked, truth))
    return out


def test_a_mark_too_sparse_to_fold_is_pooled_across_the_batch():
    from watermark import pattern

    batch = _sparse_batch()
    for marked, _truth in batch:
        assert pattern.recover_mark(marked) is None or (
            pattern.propose_pattern_mask(marked, 50) is None
        ), "fixture no longer needs the pooled route"

    marks = pattern.pooled_marks([m for m, _t in batch])
    assert marks, "three frames sharing one sparse overlay pooled into nothing"
    assert marks[0].pooled is True


@pytest.mark.parametrize("background", ["sky_grass", "render_dither", "gradient"])
def test_a_clean_batch_never_pools_into_a_mark(background):
    # The guard that makes the sparse route safe. Three collinear evenly spaced
    # matches is a weak claim and clean frames do supply them — measured pitches
    # of 321/236/173 on dithered frames and 311/306/213 on gradients. What they
    # cannot do is AGREE: a real overlay repeats at the same pitch in every image
    # it was stamped on (measured 294.0, 294.0, 294.0), and coincidence does not.
    from watermark import pattern

    frames = [
        tiled_pair(watermarked=False, background=background, size=size)[0]
        for size in ((1200, 800), (1100, 740), (1000, 680))
    ]
    assert pattern.pooled_marks(frames) == []
    marks = collect_marks(lambda: iter(frames))
    assert marks == []
    for frame in frames:
        mask, used = propose_mask_detailed(frame, 50, detector="pattern", marks=marks)
        assert used == "none"
        assert np.count_nonzero(mask) == 0


def test_pooling_needs_several_images_and_is_skipped_when_folding_worked():
    # Per image the sparse route cannot be trusted at all: on real photos a clean
    # control frame beat all three of them on both available gates (0.972 against
    # 0.41-0.46 on evidence share, 2.29-3.23 against 1.34-2.97 on significance).
    # Only agreement between images carries it, so one image can never pool.
    from watermark import pattern

    batch = _sparse_batch()
    assert pattern.pooled_marks([batch[0][0]]) == []
    assert pattern.pooled_marks([batch[0][0], batch[1][0]]) == []

    # And when every image folded on its own there is nothing to rescue, so the
    # pass is not paid for.
    _clean, easy, _truth = tiled_pair(basis=RECTANGULAR)
    calls = []

    def load():
        calls.append(1)
        return iter([easy])

    collect_marks(load)
    assert len(calls) == 1, "pooled pass ran despite the fold covering the batch"


# =========================================================================
# Spending the lattice: every copy's position is known once the grid is
# =========================================================================


def _site_coverage(marked):
    """(copies whose position the mask covers, copies the lattice predicts)."""
    import cv2

    from watermark import pattern

    mask, _kind = propose_mask_detailed(marked, 50, detector="pattern")
    mark = pattern.recover_mark(marked)
    assert mark is not None and mark.basis is not None
    work = pattern._work_size(marked)
    hp = pattern._highpass(cv2.cvtColor(work, cv2.COLOR_RGB2GRAY))
    forward, (py, px), (out_h, out_w) = pattern._rectify(hp, mark.basis)
    rect_hp = cv2.warpAffine(hp, forward, (out_w, out_h), flags=cv2.INTER_LINEAR)
    inside = cv2.warpAffine(
        np.ones(hp.shape, np.float32), forward, (out_w, out_h), flags=cv2.INTER_NEAREST
    )
    small = cv2.resize(
        mask, (work.shape[1], work.shape[0]), interpolation=cv2.INTER_NEAREST
    )
    rect_mask = cv2.warpAffine(small, forward, (out_w, out_h), flags=cv2.INTER_NEAREST)
    high, wide = mark.patch.shape
    score = cv2.matchTemplate(rect_hp, mark.patch, cv2.TM_CCOEFF_NORMED)
    _a, _b, _c, loc = cv2.minMaxLoc(score)
    anchor_x, anchor_y = loc
    covered = total = 0
    for i in range(-(anchor_y // py) - 2, (rect_hp.shape[0] - anchor_y) // py + 3):
        for j in range(-(anchor_x // px) - 2, (rect_hp.shape[1] - anchor_x) // px + 3):
            cy, cx = anchor_y + i * py + high // 2, anchor_x + j * px + wide // 2
            if not (0 <= cy < rect_hp.shape[0] and 0 <= cx < rect_hp.shape[1]):
                continue
            if inside[cy, cx] < 0.5:  # rectification padding, not photograph
                continue
            core = rect_mask[
                max(0, cy - high // 3) : cy + high // 3,
                max(0, cx - wide // 3) : cx + wide // 3,
            ]
            total += 1
            covered += int(core.size > 0 and float((core > 0).mean()) > 0.05)
    return covered, total


@pytest.mark.parametrize(
    "kwargs,least",
    [
        (dict(basis=RECTANGULAR), 0.95),
        # Lattice phase shifted so copies STRADDLE the frame edge, which is
        # structurally different from merely being faint: cv2.matchTemplate only
        # scores where the whole template fits, so a straddling copy has no score
        # at all and no threshold can reach it.
        (dict(basis=RECTANGULAR, offset=(-28, -17)), 0.95),
        (dict(basis=SHALLOW_OBLIQUE, angle=11.0), 0.95),
        (dict(basis=RECTANGULAR, background="render_dither"), 0.85),
    ],
    ids=["rect", "straddling-the-edge", "oblique", "dither-bg"],
)
def test_every_copy_the_lattice_predicts_is_masked(kwargs, least):
    """The grid is the answer, not a hint.

    The overlay is laid on a regular lattice, so once that lattice is established
    every copy's position is known and there is nothing left for an individual
    copy to prove. Requiring each one to clear a correlation bar of its own threw
    most of the watermark away: measured on this fixture it covered 39.0% of the
    predicted copies, and on real photographs 41.9% of interior copies and 1.2%
    of the ones clipped by the frame edge.

    The copies this hits hardest are exactly the ones that cannot answer for
    themselves — an overlay fainter than the photograph's own grain (4-8 grey
    levels against a median high-pass of 4-9) correlates at 0.13-0.25 where a
    colour boundary runs under it, against 0.39 on smooth sky.

    Correlation still decides whether the mark is present at all, on confidently
    matching copies alone, before any of this runs; and the per-pixel evidence
    check still trims every stamp. Only the per-copy veto is gone.
    """
    covered, total = _site_coverage(tiled_pair(**kwargs)[1])
    assert total > 20, f"fixture predicted only {total} copies"
    assert covered / total > least, f"masked {covered}/{total} predicted copies"


# =========================================================================
# Refusing when removal would cost more than the watermark
# =========================================================================


def _document(width=1200, height=800, ground=246, ink=25):
    """A spec-sheet-like frame: body text and rules on a light ground."""
    from PIL import Image, ImageDraw, ImageFont

    lines = [
        "Projected area (m2)  Floor dimension (m)  Indoor area (m2)  Remark",
        "75.69         6.7x6.7          32       Customizable size",
        "Main framework    80x80x2.0mm galvanized steel pipe",
        "Outer layer material  1050g/m2 PVDF tensioned membrane",
        "Inner layer material  850gsm block out PVC",
    ]
    page = Image.new("RGB", (width, height), (ground,) * 3)
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=19)
    y = 60
    for _block in range(4):
        for line in lines:
            draw.text((40, y), line, font=font, fill=(ink,) * 3)
            y += 34
        y += 18
    for k in range(6):
        rule = 50 + k * 130
        draw.line((40, rule, width - 40, rule), fill=(150,) * 3, width=2)
    return np.asarray(page)


def _mark_onto(base):
    """Tile the fixture's own overlay across an arbitrary background."""
    height, width = base.shape[:2]
    _c, marked, _t = tiled_pair(size=(width, height), basis=RECTANGULAR)
    _c2, clean, _t2 = tiled_pair(size=(width, height), watermarked=False)
    overlay = marked.astype(np.int16) - clean.astype(np.int16)
    return np.clip(base.astype(np.int16) + overlay, 0, 255).astype(np.uint8)


def test_a_watermarked_document_is_left_alone_rather_than_wrecked():
    """The mark is found, and removing it anyway would be the wrong answer.

    A watermark tiled over a DOCUMENT is genuinely present and genuinely masked,
    but the strokes underneath carry the meaning and inpainting discards whatever
    a mask covers. On the real spec sheet that prompted this it turned "Projected
    area (m2)" into "Projected a m2", moving 13.35% of the frame by a mean of 39
    grey levels. No sensitivity escapes it: at 0 the damage falls but residual
    mark correlation is 0.412 against 0.441 untouched, so it removes nothing.
    """
    from watermark.pipeline import destruction, would_destroy_content

    page = _mark_onto(_document())
    mask, used = propose_mask_detailed(page, 50, detector="pattern")
    assert used == "pattern", "fixture no longer carries a findable mark"
    assert mask.any()
    assert would_destroy_content(page, mask, 3), (
        f"a document scored only {destruction(page, mask, 3):.0f}"
    )


@pytest.mark.parametrize("background", ["sky_grass", "render_dither", "gradient"])
def test_an_ordinary_photograph_is_not_refused(background):
    # The guard must not cost a single ordinary image. Measured over five
    # documents and eight photographs the two populations do not overlap:
    # documents scored 97-150 and photographs 40-79, so MAX_DESTRUCTION sits
    # between them rather than being tuned against either.
    from watermark.pipeline import destruction, would_destroy_content

    _clean, marked, _truth = tiled_pair(basis=RECTANGULAR, background=background)
    mask, used = propose_mask_detailed(marked, 50, detector="pattern")
    assert used == "pattern"
    assert not would_destroy_content(marked, mask, 3), (
        f"refused a photograph, which scored {destruction(marked, mask, 3):.0f}"
    )


def _facade(width, height, cell_w, cell_h, gap=14, ink=60, ground=200):
    """Regular architecture: a grid of dark openings on a light wall."""
    img = np.full((height, width, 3), ground, np.uint8)
    for y in range(gap, height - cell_h, cell_h + gap):
        for x in range(gap, width - cell_w, cell_w + gap):
            img[y : y + cell_h, x : x + cell_w] = ink
    return img


def _brick(width, height, brick_w=90, brick_h=34, mortar=210, face=140):
    """Running bond: a repeat whose rows are offset by half a brick."""
    img = np.full((height, width, 3), mortar, np.uint8)
    for row, y in enumerate(range(0, height - brick_h, brick_h + 5)):
        offset = (brick_w // 2) if row % 2 else 0
        for x in range(-offset, width, brick_w + 6):
            img[y : y + brick_h, max(0, x) : x + brick_w] = face
    return img


def _railings(width, height, step=26):
    """A one-dimensional repeat: evenly spaced vertical bars."""
    img = np.full((height, width, 3), 190, np.uint8)
    for x in range(0, width, step):
        img[:, x : x + 5] = 70
    return img


@pytest.mark.parametrize("sensitivity", [0, 50, 100])
@pytest.mark.parametrize(
    "scene",
    [
        _brick(1200, 800),
        _brick(1500, 950, 110, 42),
        _railings(1200, 800),
        _railings(1600, 1000, 34),
    ],
    ids=["brick", "brick-wide", "railings", "railings-coarse"],
)
def test_repeating_architecture_is_not_mistaken_for_a_watermark(scene, sensitivity):
    """Scenery repeats too, and it is not a watermark.

    A brick wall and a rank of railings are strong periodic structure, and every
    gate before the on-lattice one asks only whether a repeat is STRONG. What
    separates them is that their matches are not ARRANGED on the fitted lattice:
    measured 0.333-0.538 of matches on a node here against 0.769-1.000 where a
    real overlay is present.
    """
    mask, used = propose(scene, sensitivity)
    assert used == "none", f"masked {100 * np.count_nonzero(mask) / mask.size:.1f}%"


@pytest.mark.parametrize("sensitivity", [0, 50, 100])
def test_a_window_facade_is_refused_before_anything_is_inpainted(sensitivity):
    """The case the on-lattice gate CANNOT catch, caught by the next one.

    A facade is a genuine two-dimensional lattice, so its matches really do sit
    on the grid and the arrangement test passes — a watermark and a repeating
    scene element are geometrically the same thing. What stops it is the cost of
    removal: erasing the windows out of a wall rewrites it beyond recognition
    (destruction 130-140 against a bar of 88), so the image is left alone.

    Known gap, deliberately not asserted here: a LOW-CONTRAST regular pattern —
    pale tiling, ink 170 on ground 205 — passes both gates, because filling it
    changes little (destruction 35) and so is judged harmless.
    """
    from watermark.pipeline import would_destroy_content

    scene = _facade(1200, 800, 70, 90)
    mask, used = propose(scene, sensitivity)
    if used == "none":
        return  # refused outright, which is a stronger answer still
    assert would_destroy_content(scene, mask, 3), "a facade would have been inpainted"
