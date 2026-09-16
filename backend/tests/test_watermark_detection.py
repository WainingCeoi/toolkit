"""Detection benchmark: recall and false positives per watermark shape."""

from __future__ import annotations

import numpy as np
import pytest
from watermark_fixtures import (
    COARSE_RECTANGULAR,
    RECTANGULAR,
    SHALLOW_OBLIQUE,
    STEEP_OBLIQUE,
    score,
    stacked_batch,
    tiled_pair,
)

from watermark.detect import collect_marks, propose_mask_detailed
from watermark.stacked import recover_stacked, stamp_stacked


def propose(marked, sensitivity=50):
    return propose_mask_detailed(marked, sensitivity, detector="pattern")


# --- What the pattern detector handles ---


def test_a_rectangular_lattice_is_recovered_and_masked_precisely():
    _clean, marked, truth = tiled_pair(basis=RECTANGULAR)
    mask, used = propose(marked)
    assert used == "pattern"
    recall, false_positives = score(mask, truth)
    assert recall > 0.25, f"only {recall:.1%} of the mark was masked"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


def test_a_lattice_too_coarse_to_fold_is_still_recovered_from_one_image():
    _clean, marked, truth = tiled_pair(
        size=(1080, 1922), basis=COARSE_RECTANGULAR, glyph_size=44, alpha=70
    )
    mask, used = propose(marked)
    assert used == "pattern", "the coarse lattice fell through every route"
    recall, false_positives = score(mask, truth)
    assert recall > 0.25, f"only {recall:.1%} of the mark was masked"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


@pytest.mark.parametrize(
    "background", ["sky_grass", "grass", "render_dither", "gradient"]
)
def test_a_clean_frame_of_that_shape_is_never_given_a_coarse_lattice(background):
    clean = tiled_pair(
        size=(1080, 1922),
        basis=COARSE_RECTANGULAR,
        background=background,
        watermarked=False,
    )[0]
    mask, used = propose(clean)
    assert used == "none", f"a clean {background} frame was handed a lattice"
    assert np.count_nonzero(mask) == 0


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
    _clean, marked, truth = tiled_pair(basis=RECTANGULAR, **kwargs)
    mask, used = propose(marked)
    assert used == "pattern"
    recall, false_positives = score(mask, truth)
    assert recall > least, f"only {recall:.1%} of the mark was masked"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


def test_the_primitive_lattice_is_used_not_a_multiple_of_it():
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


# --- False positives ---


@pytest.mark.parametrize(
    "background", ["sky_grass", "render_dither", "gradient", "grass"]
)
@pytest.mark.parametrize(
    "size", [(1200, 800), (800, 600), (1400, 900), (2100, 1100), (1300, 700)]
)
@pytest.mark.parametrize("sensitivity", [0, 50, 100])
def test_a_clean_frame_is_never_pattern_masked(background, size, sensitivity):
    clean, _marked, _truth = tiled_pair(
        watermarked=False, background=background, size=size
    )
    mask, used = propose(clean, sensitivity)
    assert used == "none", "invented a repeating pattern in a clean frame"
    assert np.count_nonzero(mask) == 0


# --- Oblique lattices ---


@pytest.mark.parametrize(
    "basis,angle",
    [(SHALLOW_OBLIQUE, 11.0), (STEEP_OBLIQUE, -14.0)],
    ids=["shallow-11deg", "steep-76deg"],
)
def test_an_oblique_lattice_is_recovered(basis, angle):
    _clean, marked, truth = tiled_pair(basis=basis, angle=angle)
    mask, used = propose(marked)
    assert used == "pattern"
    recall, false_positives = score(mask, truth)
    assert recall > 0.25, f"only {recall:.1%} of the mark was masked"
    assert false_positives < 0.02, f"{false_positives:.1%} of clean pixels masked"


# --- Sharing a mark across a batch ---


@pytest.mark.parametrize(
    "background,size",
    [("gradient", (1200, 800)), ("sky_grass", (980, 640))],
    ids=["other-background", "other-size"],
)
def test_a_mark_from_one_image_masks_the_same_overlay_on_another(background, size):
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
    _clean, marked, _truth = tiled_pair(basis=RECTANGULAR)
    marks = collect_marks(lambda: [marked])
    assert marks
    clean, _m, _t = tiled_pair(watermarked=False, background=background)
    mask, used = propose_mask_detailed(clean, 50, detector="pattern", marks=marks)
    assert used == "none", "a borrowed mark was forced onto a clean frame"
    assert np.count_nonzero(mask) == 0


def test_a_mark_that_cannot_mask_its_own_image_is_not_offered_to_the_batch():
    clean, _m, _t = tiled_pair(watermarked=False, background="sky_grass")
    assert collect_marks(lambda: [clean]) == []


@pytest.mark.parametrize("noise", [0.0, 1.0, 2.0], ids=["flat", "quantised", "faint"])
def test_a_featureless_frame_holds_no_instances(noise):
    from watermark import pattern

    _clean, marked, _truth = tiled_pair(basis=RECTANGULAR)
    mark = pattern.recover_mark(marked)
    assert mark is not None

    rng = np.random.default_rng(5)
    level = np.clip(252 + rng.normal(0, noise, (600, 900)), 0, 255).astype(np.uint8)
    frame = np.dstack([level] * 3)
    assert pattern.apply_mark(frame, mark, 50, own=False) is None
    assert pattern.propose_pattern_mask(frame, 50) is None


# --- The sparse route ---


def _sparse_batch(count=3, size=(1300, 800), seed=0):
    """Frames on a 300px cell: about six copies each, under the nine the fold needs."""
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
        # Only the fold must fail here; the tiled route may still mask it.
        assert pattern.recover_mark(marked) is None or (
            pattern._propose_own_folded(marked, 50) is None
        ), "fixture no longer needs the pooled route"

    marks = pattern.pooled_marks([m for m, _t in batch])
    assert marks, "three frames sharing one sparse overlay pooled into nothing"
    assert marks[0].pooled is True


@pytest.mark.parametrize("background", ["sky_grass", "render_dither", "gradient"])
def test_a_clean_batch_never_pools_into_a_mark(background):
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
    from watermark import pattern

    batch = _sparse_batch()
    assert pattern.pooled_marks([batch[0][0]]) == []
    assert pattern.pooled_marks([batch[0][0], batch[1][0]]) == []

    _clean, easy, _truth = tiled_pair(basis=RECTANGULAR)
    calls = []

    def load():
        calls.append(1)
        return iter([easy])

    collect_marks(load)
    assert len(calls) == 1, "pooled pass ran despite the fold covering the batch"


def test_the_batch_votes_for_the_pitch_across_the_run():
    from watermark import pattern

    batch = _sparse_batch()
    marks = pattern.pooled_marks([m for m, _t in batch])
    assert marks and marks[0].grid is not None, "the batch failed to vote a grid"
    across, along = marks[0].grid
    assert abs(across - 300) / 300 <= 0.05, f"pitch across the run was {across}"
    assert abs(along - 300) / 300 <= 0.05, f"pitch along the run was {along}"


@pytest.mark.parametrize("background", ["render_dither", "sky_grass", "gradient"])
def test_a_clean_batch_is_never_handed_a_grid(background):
    from watermark import pattern

    frames = [
        tiled_pair(watermarked=False, background=background, size=(1200, 800))[0]
        for _ in range(3)
    ]
    for mark in pattern.pooled_marks(frames):
        assert mark.grid is None, f"a clean {background} batch was given a grid"


def test_the_grid_masks_the_copies_that_never_correlate():
    from watermark import pattern

    batch = _sparse_batch()
    marks = pattern.pooled_marks([m for m, _t in batch])
    mark = marks[0]
    blind = pattern.Mark(
        None,
        mark.template,
        mark.patch,
        mark.crop_top,
        mark.crop_left,
        mark.cell,
        pooled=True,
        grid=None,
    )

    gained = 0
    for marked, truth in batch:
        spent = pattern.apply_mark(marked, mark, 50, own=False)
        if spent is None:
            continue
        withheld = pattern.apply_mark(marked, blind, 50, own=False)
        before = 0.0 if withheld is None else score(withheld, truth)[0]
        after = score(spent, truth)[0]
        assert after >= before - 1e-9, "spending the grid lost recall"
        gained += after > before
    assert gained, "spending the grid recovered nothing anywhere in the batch"


def test_the_grid_folds_the_mark_out_at_its_own_size():
    from watermark import pattern

    batch = _sparse_batch()
    mark = pattern.pooled_marks([m for m, _t in batch])[0]
    assert mark.grid is not None
    assert mark.template.shape != mark.patch.shape, "the fold never ran"
    for axis in (0, 1):
        assert mark.template.shape[axis] < mark.grid[axis], (
            f"the stamp is the whole cell on axis {axis}: "
            f"{mark.template.shape} against a cell of {mark.grid}"
        )


def test_a_frame_too_short_for_the_anchor_window_cannot_sink_the_batch():
    # 56 rows is under the 68-row anchor window; cv2.matchTemplate raises on that.
    from PIL import Image, ImageDraw

    from watermark import pattern

    height, width, pitch = 56, 1300, 300
    strip = Image.new("RGB", (width, height), (225, 225, 228))
    draw = ImageDraw.Draw(strip)
    for x in range(60, width, pitch):
        draw.ellipse([x, height // 2 - 9, x + 26, height // 2 + 9], fill=(60, 60, 60))
        draw.rectangle(
            [x + 30, height // 2 - 5, x + 52, height // 2 + 5], fill=(90, 90, 90)
        )
    noise = np.random.default_rng(3).integers(-4, 5, (height, width, 3))
    odd = np.clip(np.asarray(strip) + noise, 0, 255).astype(np.uint8)
    assert pattern._anchored_run(odd) is not None, "fixture no longer forms a run"

    batch = [marked for marked, _truth in _sparse_batch()] + [odd]
    marks = collect_marks(lambda: iter(batch))
    assert marks, "one short frame cost the whole batch its mark"


def test_a_cell_narrower_than_the_anchor_window_never_hurts():
    from watermark import pattern

    batch = [
        tiled_pair(
            basis=((300, 0), (0, 114)),
            size=(1300, 800),
            background=background,
            glyph_size=18,
            alpha=70,
        )
        for background in ("gradient", "sky_grass", "render_dither")
    ]
    marks = pattern.pooled_marks([marked for _clean, marked, _truth in batch])
    if not marks:
        return  # nothing pooled at all -- nothing to get wrong
    mark = marks[0]
    plain = pattern.Mark(
        None, mark.patch, mark.patch, 0, 0, mark.patch.shape, pooled=True
    )
    for _clean, marked, truth in batch:
        folded = pattern.apply_mark(marked, mark, 50, own=False)
        unfolded = pattern.apply_mark(marked, plain, 50, own=False)
        before = 0.0 if unfolded is None else score(unfolded, truth)[0]
        after = 0.0 if folded is None else score(folded, truth)[0]
        assert after >= before - 0.05, (
            f"the folded mark lost recall against the plain template: "
            f"{after:.3f} < {before:.3f}"
        )


def test_a_lattice_coarser_than_the_search_is_not_halved():
    from watermark import pattern

    frames = [
        tiled_pair(
            basis=((600, 0), (0, 240)),
            size=(1400, 1300),
            background=background,
            glyph_size=30,
            alpha=70,
        )[1]
        for background in ("gradient", "sky_grass", "render_dither")
    ]
    marks = pattern.pooled_marks(frames)
    assert marks and marks[0].grid is not None, "the batch stopped voting a grid"
    assert abs(marks[0].grid[0] - 600) <= 30, (
        f"a 600px lattice was voted as {marks[0].grid[0]}"
    )


def test_a_short_clean_frame_batch_is_still_refused_a_grid():
    from watermark import pattern

    frames = [
        tiled_pair(watermarked=False, background="render_dither", size=(1300, 220))[0]
        for _ in range(3)
    ]
    for mark in pattern.pooled_marks(frames):
        assert mark.grid is None, f"a clean strip batch was handed {mark.grid}"


def test_a_copy_over_busy_ground_is_masked_whole_not_in_fragments():
    from watermark import pattern

    _clean, marked, truth = tiled_pair(basis=RECTANGULAR)
    half = truth.shape[0] // 2
    core = truth[half:] == 255

    mask, used = propose(marked)
    assert used == "pattern"
    filled = np.count_nonzero((mask[half:] > 0) & core) / max(core.sum(), 1)

    before = pattern._FILL_MIN_PIECE
    pattern._FILL_MIN_PIECE = 10**9  # no piece survives, so the fill never fires
    try:
        trimmed_mask, _ = propose(marked)
    finally:
        pattern._FILL_MIN_PIECE = before
    trimmed = np.count_nonzero((trimmed_mask[half:] > 0) & core) / max(core.sum(), 1)

    assert filled > trimmed + 0.10, (
        f"the fill recovered nothing over grass: {filled:.3f} vs {trimmed:.3f}"
    )
    assert filled > 0.45, f"grass-half recall is still only {filled:.3f}"


def test_an_unmaskable_repeat_is_protected_not_reported_clean(tmp_path):
    # Steep oblique: the axis-aligned tiled route would mask a rectangular cell.
    from PIL import Image

    from watermark.pipeline import clean_folder

    src = tmp_path / "in"
    src.mkdir()
    sparse = tiled_pair(basis=STEEP_OBLIQUE, size=(1300, 800), glyph_size=30, alpha=70)[
        1
    ]
    Image.fromarray(sparse).save(src / "screenshot.png")
    clean = tiled_pair(watermarked=False, background="sky_grass")[0]
    Image.fromarray(clean).save(src / "holiday.png")

    cleaned, skipped, protected, failed = clean_folder(
        src, tmp_path / "out", inpainter="cv2", detector="pattern"
    )
    assert failed == []
    assert protected == ["screenshot.png"], "the visible repeat went unreported"
    assert skipped == ["holiday.png"], "a clean photo must stay plainly skipped"


def test_auto_rescues_a_repeat_the_pattern_routes_cannot_mask(tmp_path):
    # Steep oblique: the axis-aligned tiled route would mask a rectangular cell.
    from PIL import Image

    from watermark.detect import propose_mask_detailed
    from watermark.pipeline import clean_folder

    sparse = tiled_pair(basis=STEEP_OBLIQUE, size=(1300, 800), glyph_size=30, alpha=70)[
        1
    ]
    mask, used = propose_mask_detailed(sparse, 50, "auto")
    assert used == "texture", "auto never reached the fallback"
    assert np.count_nonzero(mask) > 0

    src = tmp_path / "in"
    src.mkdir()
    Image.fromarray(sparse).save(src / "screenshot.png")
    clean = tiled_pair(watermarked=False, background="sky_grass")[0]
    Image.fromarray(clean).save(src / "holiday.png")

    cleaned, skipped, protected, failed = clean_folder(
        src, tmp_path / "out", inpainter="cv2"
    )
    assert failed == []
    assert cleaned == ["screenshot.png"], "auto did not rescue the sparse mark"
    assert skipped == ["holiday.png"], "a clean photo must stay plainly skipped"


# --- Spending the lattice ---


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
        # Phase shifted so copies straddle the frame edge.
        (dict(basis=RECTANGULAR, offset=(-28, -17)), 0.95),
        (dict(basis=SHALLOW_OBLIQUE, angle=11.0), 0.95),
        (dict(basis=RECTANGULAR, background="render_dither"), 0.85),
    ],
    ids=["rect", "straddling-the-edge", "oblique", "dither-bg"],
)
def test_every_copy_the_lattice_predicts_is_masked(kwargs, least):
    covered, total = _site_coverage(tiled_pair(**kwargs)[1])
    assert total > 20, f"fixture predicted only {total} copies"
    assert covered / total > least, f"masked {covered}/{total} predicted copies"


# --- Refusing destructive removal ---


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
    from watermark.pipeline import destruction, would_destroy_content

    page = _mark_onto(_document())
    mask, used = propose_mask_detailed(page, 50, detector="pattern")
    assert used == "pattern", "fixture no longer carries a findable mark"
    assert mask.any()
    assert would_destroy_content(page, mask, 3), (
        f"a document scored only {destruction(page, mask, 3):.0f}"
    )


def test_auto_withholds_a_fallback_mask_it_would_not_be_allowed_to_use():
    from watermark import detect
    from watermark.pipeline import destruction

    page = _document()
    fallback = detect.propose_texture_mask(page, 50)
    assert fallback.any(), "fixture no longer makes the texture detector fire"
    assert destruction(page, fallback, 3) > 88, "fixture is no longer destructive"

    mask, used = propose_mask_detailed(page, 50, detector="auto")
    assert used == "none", f"auto offered a {used} mask over a page of text"
    assert np.count_nonzero(mask) == 0

    real = detect._worth_removing
    detect._worth_removing = lambda rgb, m: True
    try:
        ungated, kind = propose_mask_detailed(page, 50, detector="auto")
    finally:
        detect._worth_removing = real
    assert kind == "texture"
    assert np.count_nonzero(ungated) == np.count_nonzero(fallback)


@pytest.mark.parametrize("background", ["sky_grass", "render_dither", "gradient"])
def test_an_ordinary_photograph_is_not_refused(background):
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
    mask, used = propose(scene, sensitivity)
    assert used == "none", f"masked {100 * np.count_nonzero(mask) / mask.size:.1f}%"


@pytest.mark.parametrize("sensitivity", [0, 50, 100])
def test_a_window_facade_is_refused_before_anything_is_inpainted(sensitivity):
    from watermark.pipeline import would_destroy_content

    scene = _facade(1200, 800, 70, 90)
    mask, used = propose(scene, sensitivity)
    if used == "none":
        return  # refused outright, which is a stronger answer still
    assert would_destroy_content(scene, mask, 3), "a facade would have been inpainted"


# --- The stacked route ---


def _reloadable(frames):
    # recover_stacked reads the batch twice, so the loader must be re-callable.
    return lambda: iter(frames)


def test_a_batchwide_banner_is_recovered_from_the_stack():
    frames, truth = stacked_batch(n=5)
    marks = collect_marks(_reloadable(frames))
    assert any(not hasattr(m, "template") for m in marks), "no stacked mark"
    for frame in frames:
        mask, used = propose_mask_detailed(frame, 50, "auto", marks)
        assert used == "stacked"
        recall, false_positives = score(mask, truth)
        assert recall > 0.25, f"recall {recall:.2f}"
        assert false_positives < 0.02, f"fp {false_positives:.3f}"


def test_a_clean_batch_recovers_no_stacked_mark():
    frames, _truth = stacked_batch(n=5, watermarked=False)
    assert recover_stacked(_reloadable(frames)) is None
    marks = collect_marks(_reloadable(frames))
    for frame in frames:
        mask, used = propose_mask_detailed(frame, 50, "auto", marks)
        assert used == "none"
        assert not mask.any()


def test_near_duplicate_frames_are_refused_not_read_as_one_big_mark():
    marked, _ = stacked_batch(n=5, duplicates=True)
    assert recover_stacked(_reloadable(marked)) is None
    clean, _ = stacked_batch(n=5, duplicates=True, watermarked=False)
    assert recover_stacked(_reloadable(clean)) is None


def test_a_stack_of_two_proves_nothing():
    frames, _truth = stacked_batch(n=2)
    assert recover_stacked(_reloadable(frames)) is None


def test_stacked_sensitivity_marks_monotonically_more_pixels():
    frames, _truth = stacked_batch(n=5)
    mark = recover_stacked(_reloadable(frames))
    assert mark is not None
    shape = frames[0].shape[:2]
    previous = 0
    for sensitivity in (0, 25, 50, 75, 100):
        count = int((stamp_stacked(mark, shape, sensitivity) > 0).sum())
        assert count >= previous, f"shrank at sensitivity {sensitivity}"
        previous = count


def test_a_lone_image_gets_no_stacked_mask():
    frames, _truth = stacked_batch(n=1)
    assert recover_stacked(_reloadable(frames)) is None
    mask, used = propose_mask_detailed(frames[0], 50, "auto", [])
    assert used == "none"
    assert not mask.any()
