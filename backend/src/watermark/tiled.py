"""The tiled route: a repeating overlay whose cell is too big for the fold.

pattern.py recovers a repeat by folding its tiles together, which needs
MIN_TILES=9 cells in frame. A 572x268 cell fits a 1080x1922 photograph SEVEN
times, in one column, so the fold never gets there.

Worse, that period is UNREACHABLE rather than merely unproven, and no constant
moves it. After fftshift the autocorrelation of a 1080-wide frame spans lags
-540..+539, so a 572px lag is not a value the data can hold at any threshold.
Measured on such a photograph: _rect_period answers (406, 255) confidently,
_fit_rectifying_lattice answers a 38x66 basis fitted to the photograph's own
texture, and the fold that follows is judged against that wrong pitch and
refused by MIN_SIGNIFICANCE at a ratio of 1.007. The refusal is correct -- what
it was shown was not the mark. Nothing downstream can recover from a period
that was never representable, which is why this SEARCHES the period directly
instead of inferring it from an autocorrelation peak.

What one image can prove about such a mark is the question pooled_marks
answers "not much, use the batch" -- it needs _MIN_POOL_IMAGES=3 because, on
the sparse marks it was built for, a clean control frame beat three genuinely
marked photographs on both gates available at the time. This route is only
allowed to exist because it asks a different question. Not "is this repeat
STRONG", which a photograph answers for free, but "is it ARRANGED": see
_accept, where the gate and the populations it separates are documented.

Every route in pattern.py runs first. This one is reached only after
recover_mark, apply_mark and every borrowed mark have already returned None,
so no image that gets a mask today can get a different one.
"""

from __future__ import annotations

from collections import Counter

import cv2
import numpy as np

from .pattern import (
    _ANCHOR_HALF_H,
    _ANCHOR_HALF_W,
    _FILL_MIN_PIECE,
    _INK_CELL_SHARE,
    _INK_MARGIN,
    _INK_SHARE,
    _MIN_WINDOW_STD_SHARE,
    _anchor_candidates,
    _window_std,
    _work_size,
)

# --- new constants --------------------------------------------------------
# The window the residual's local energy is measured over, and the two
# background estimates the residual itself is taken against. See whitened().
_WHITEN_ENERGY = 31
_MATCH_BG = 31
_INK_BG = 91
# Period search, in working pixels: from a cell that clearly holds a mark of
# anchor size, out to one that only fits a couple of times in the frame.
_TILED_MIN_PERIOD = 150
_TILED_MAX_SHARE = 0.75
_TILED_STEP = 2
# A predicted cell is read only where this much of the patch is inside the
# frame; below that its score is mostly zero-pad arithmetic.
_MIN_VISIBLE = 0.5
# Cells a lattice must predict (not counting the seed) before its mean means
# anything.
_MIN_CELLS = 4
# Anchors enumerated from the whitened field, in addition to pattern.py's own.
_TILED_ANCHOR_COUNT = 10


def whitened(gray: np.ndarray, background: int = _MATCH_BG) -> np.ndarray:
    """Residual against a median background, divided by its own local energy.

    Dividing by local energy is why this exists. The plain Gaussian high-pass
    the rest of pattern.py matches on leaves the score field's noise dominated
    by whichever part of the photograph is busiest, so a copy stamped on smooth
    canvas and one stamped on grass are not comparable. Dividing makes them so:
    measured on the sample, score-field noise 0.0607 -> 0.0320 and the best
    genuine instance 0.279 -> 0.442.

    ``background`` is called at TWO widths and the pair is load-bearing.

    _MATCH_BG (31) is what everything that MATCHES uses -- the lattice search,
    the per-cell scores, the gate. It has to be tight, because a wider residual
    keeps more of the photograph's own low-frequency structure and the search
    then locks onto that: measured, running the search at 91 moved the real
    photo's answer from its true 268x572 to 150x156, which the emptiness test
    then threw away. The mark was lost.

    _INK_BG (91) is used for NOTHING but the shape of the stamp, inside a box
    the 31 fold has already drawn and already gated. A background estimate
    narrower than a solid feature ABSORBS it: inside this mark's 62px logo disc
    the 31 estimate becomes the disc, the residual falls to zero, and the disc
    folds as a hollow ring. Measured over the disc's middle third, mean |field|
    is 0.338 at 31 against 1.010 at 91; the stamp cut from the 31 fold covers
    the disc as a broken crescent, which encloses nothing so hole-filling
    cannot close it, and the logo survives inpainting. 91 costs 10ms on a 2 MP
    frame.
    """
    g8 = gray if gray.dtype == np.uint8 else np.clip(gray, 0, 255).astype(np.uint8)
    res = g8.astype(np.float32) - cv2.medianBlur(g8, background).astype(np.float32)
    energy = cv2.boxFilter(res * res, -1, (_WHITEN_ENERGY, _WHITEN_ENERGY))
    return res / (np.sqrt(energy) + 1.0)


def _score_field(field: np.ndarray, patch: np.ndarray, pad: int) -> np.ndarray:
    """Correlation of ``patch`` over a zero-padded ``field``.

    Padded so a copy hanging off the frame still scores on the part that is
    present -- matchTemplate otherwise evaluates nothing at all there.

    Guarded exactly the way _matched_sites guards its own map, and for the same
    measured reason: normalised correlation over a window with no variation
    divides ~0 by ~0 and OpenCV hands back a perfect score. Without this the
    zero pad itself, and the flat white of a product render, read as evidence.
    """
    padded = cv2.copyMakeBorder(field, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    score = cv2.matchTemplate(padded, patch, cv2.TM_CCOEFF_NORMED)
    floor = _MIN_WINDOW_STD_SHARE * float(patch.std())
    substance = _window_std(padded, patch.shape)[: score.shape[0], : score.shape[1]]
    return np.where(substance >= floor, score, 0.0).astype(np.float32)


def _positions(seed: int, period: int, span: int, extent: int) -> list[int]:
    """Every lattice position along one axis whose patch is visibly in frame."""
    start = seed % period
    while start - period + extent * _MIN_VISIBLE > 0:
        start -= period
    out = []
    p = start
    while p < span - extent * _MIN_VISIBLE + 1:
        if min(p + extent, span) - max(p, 0) >= _MIN_VISIBLE * extent:
            out.append(p)
        p += period
    return out


def _scan(score, pad, seed_y, seed_x, span_y, span_x, high, wide):
    """The best lattice through the seed, by collective evidence.

    Every candidate is judged on the MEAN score over the cells it predicts,
    times the square root of how many it predicts. That product is what makes
    the true period win: a half-period predicts twice as many cells but half of
    them land on nothing, so its mean halves while its count only doubles; a
    double period predicts only live cells but too few of them.

    The seed's own cell is excluded. It is a self-match, scores 1.000 by
    construction, and on a lattice predicting four cells that is a quarter of
    the statistic handed over for free.
    """
    ys = np.arange(_TILED_MIN_PERIOD, int(_TILED_MAX_SHARE * span_y) + 1, _TILED_STEP)
    xs = np.arange(_TILED_MIN_PERIOD, int(_TILED_MAX_SHARE * span_x) + 1, _TILED_STEP)
    if len(ys) == 0 or len(xs) == 0:
        return None

    # Column weights, built once: row j selects the columns period xs[j]
    # predicts, so ONE matrix multiply scores every lattice against the folded
    # row profiles instead of a Python loop per lattice.
    width = score.shape[1]
    weights = np.zeros((len(xs), width), np.float32)
    counts_x = np.zeros(len(xs), np.int32)
    for j, px in enumerate(xs):
        cols = [x + pad for x in _positions(seed_x, int(px), span_x, wide)]
        cols = [c for c in cols if 0 <= c < width]
        if cols:
            weights[j, cols] = 1.0 / len(cols)
            counts_x[j] = len(cols)

    # Magnitude, not sign. One overlay is DARKER than blown-out sky and
    # BRIGHTER than shadow in the same frame, so its copies correlate with
    # opposite signs -- measured on the render fixture, +1.00 above the horizon
    # and -0.42 below it. Read signed, the true period scored -0.070 there and
    # lost to a wrong one at +0.417; read as magnitude it wins, 12 anchors to 3.
    score = np.abs(score)
    height = score.shape[0]
    profiles = np.zeros((len(ys), width), np.float32)
    counts_y = np.zeros(len(ys), np.int32)
    for i, py in enumerate(ys):
        rows = [y + pad for y in _positions(seed_y, int(py), span_y, high)]
        rows = [r for r in rows if 0 <= r < height]
        if rows:
            profiles[i] = score[rows].mean(axis=0)
            counts_y[i] = len(rows)

    means = weights @ profiles.T  # (px, py)
    cells = counts_x[:, None] * counts_y[None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        rest = (means * cells - 1.0) / np.maximum(cells - 1, 1)
    stat = rest * np.sqrt(np.maximum(cells - 1, 0))
    # A lattice that predicts a single ROW of cells, or a single column, is a
    # line and not a grid -- and a line is exactly what a photograph offers for
    # free. Measured: every clean sky_grass and render_dither control's best
    # "lattice" was one row of cells lying along the horizon or the edge of the
    # dithered ground, where the whole band responds and any column pitch fits.
    grid = (counts_x[:, None] >= 2) & (counts_y[None, :] >= 2)
    stat = np.where(grid & (cells - 1 >= _MIN_CELLS), stat, -np.inf)
    if not np.isfinite(stat).any():
        return None
    j, i = np.unravel_index(int(np.argmax(stat)), stat.shape)
    return {
        "stat": float(stat[j, i]),
        "py": int(ys[i]),
        "px": int(xs[j]),
        "mean": float(rest[j, i]),
        "rows": int(counts_y[i]),
        "cols": int(counts_x[j]),
        "cells": int(cells[j, i]),
        "seed": (int(seed_y), int(seed_x)),
    }


def _tiled_anchors(field: np.ndarray, high: int, wide: int) -> list[tuple[int, int]]:
    """Patch-sized windows carrying the most whitened energy, well separated.

    pattern.py's own _anchor_candidates is used as well, but it cannot serve
    alone here: it keeps only responses inside the QUIETEST 45% of the frame,
    and on a smooth gradient the only thing with any local texture at all is
    the mark itself -- so the mark is never quiet, the field it ranks is
    identically zero, and it returns nothing. Measured: 0 anchors on both the
    marked and the clean gradient frames.
    """
    energy = cv2.boxFilter(field * field, -1, (wide, high))
    work = energy.copy()
    found: list[tuple[int, int]] = []
    for _ in range(_TILED_ANCHOR_COUNT):
        cy, cx = np.unravel_index(int(np.argmax(work)), work.shape)
        if work[cy, cx] <= 0:
            break
        found.append((int(cy), int(cx)))
        work[max(0, cy - high) : cy + high, max(0, cx - wide) : cx + wide] = 0
    return found


# --- the gate -------------------------------------------------------------
# A predicted cell counts as holding a copy above this correlation. Measured on
# the cells of accepted lattices: cells confirmed empty by eye score 0.00-0.03,
# the weakest genuine copy 0.10.
_CELL_LIVE = 0.06
# Share of predicted cells that must hold a copy, and the shape they must make.
# THIS IS THE GATE. See _accept.
_MIN_LIVE_SHARE = 0.65
_MIN_LIVE_LINES = 2
# Anchors that must independently converge on the same lattice.
_MIN_TILED_VOTES = 2

# The ink cut, as a fraction of the seed copy's own peak energy, from
# sensitivity 0 to 100. A FRACTION OF PEAK, not the percentile of the box that
# apply_mark spends: a percentile assumes it is looking at a box the mark
# fills, and the box this route draws around a lockup is mostly empty (measured
# on the sample photo, 87x236 of a 268x572 cell, of which the mark is under a
# third). Measured there, the percentile route spent its whole allowance on the
# logo disc and left every copy's lettering on the picture at sensitivity 0, 50
# AND 100 -- the fill fraction it assumes simply is not this shape's.
#
# Against the seed copy's own peak the same allowance lands on the mark:
# measured coverage and destruction at 0.60/0.45/0.30 of peak are 2.9%/4.2%/5.5%
# of frame at destruction 62/66/68, against the pipeline's gate of 88 -- so no
# setting on this slider can push a photograph into PROTECTED.
_TILED_INK_MAX = 0.60
_TILED_INK_MIN = 0.30


def _cells(score, pad, seed, period, span, extent):
    """(rows, cols, signed score at every predicted cell)."""
    rows = _positions(seed[0], period[0], span[0], extent[0])
    cols = _positions(seed[1], period[1], span[1], extent[1])
    vals = np.zeros((len(rows), len(cols)), np.float32)
    for r, y in enumerate(rows):
        for c, x in enumerate(cols):
            gy, gx = y + pad, x + pad
            if 0 <= gy < score.shape[0] and 0 <= gx < score.shape[1]:
                vals[r, c] = float(score[gy, gx])
    return rows, cols, vals


def _accept(rows, cols, vals, seed) -> dict | None:
    """THE GATE: are the copies ARRANGED, or merely present?

    Every other statistic available here asks whether the repeat is STRONG, and
    a photograph offers strong repeats for free. What it does not offer is a
    repeat laid out on a GRID. Measured over eight clean controls, every one of
    them put the live cells of its best lattice in a single LINE -- along the
    horizon of a sky-over-grass frame, or along the edge of a render's dithered
    ground, where the whole band responds and any pitch across it fits. The
    second row is dead: 0.00-0.03 against the live row's 0.12-0.18.

    Two numbers, which are two views of that one fact:
      live share  0.80-1.00 where a mark is present, 0.00-0.47 on a clean frame
      live lines  min(live rows, live cols): 2-3 against 0-1

    _MIN_LIVE_SHARE sits at 0.65 between those populations, which is where
    _MIN_ON_LATTICE sits between its own, and for the same reason.
    """
    live = np.abs(vals) >= _CELL_LIVE
    si = rows.index(seed[0]) if seed[0] in rows else -1
    sj = cols.index(seed[1]) if seed[1] in cols else -1
    if si >= 0 and sj >= 0:
        live[si, sj] = False  # a self-match is not evidence
        total = live.size - 1
    else:
        total = live.size
    if total < _MIN_CELLS:
        return None
    share = float(live.sum()) / total
    lines = min(int(live.any(axis=1).sum()), int(live.any(axis=0).sum()))
    if share < _MIN_LIVE_SHARE or lines < _MIN_LIVE_LINES:
        return None
    if si >= 0 and sj >= 0:
        live[si, sj] = True  # the seed cell does hold a copy; it just did not vote
    return {"live": live, "share": share, "lines": lines}


def _fold(field, rows, cols, live, vals, cell, offset):
    """The whole cell, median-folded over the live copies.

    Each copy is folded in its OWN polarity. A light overlay is darker than
    blown-out sky and brighter than shadow in one frame -- measured on the
    render fixture, the same mark correlates at +1.00 above the horizon and
    -0.42 below it -- and folding those together cancels the mark instead of
    the scenery.

    Partial cells donate what they have, per pixel, exactly as _fold_on_grid
    does: a copy half off the frame edge is still half a copy.
    """
    cell_y, cell_x = cell
    off_y, off_x = offset
    tiles = []
    for r, y in enumerate(rows):
        for c, x in enumerate(cols):
            if not live[r, c]:
                continue
            top, left = y - off_y, x - off_x
            rt, rl = max(0, top), max(0, left)
            rb = min(field.shape[0], top + cell_y)
            rr = min(field.shape[1], left + cell_x)
            if rb <= rt or rr <= rl:
                continue
            tile = np.full((cell_y, cell_x), np.nan, np.float32)
            sign = 1.0 if vals[r, c] >= 0 else -1.0
            tile[rt - top : rb - top, rl - left : rr - left] = (
                sign * field[rt:rb, rl:rr]
            )
            tiles.append(tile)
    if len(tiles) < _MIN_CELLS:
        return None
    stack = np.stack(tiles)
    covered = np.count_nonzero(~np.isnan(stack), axis=0)
    stack[0][covered == 0] = 0.0
    folded = np.nanmedian(stack, axis=0).astype(np.float32)
    folded[covered < 2] = 0.0
    return folded if folded.std() > 1e-3 else None


def _seed_ink(field, seed, cell, offset):
    """The seed copy's own cell window, aligned to the fold's grid.

    The SHAPE of the stamp comes from one copy, not from the fold, and the two
    jobs are why. The fold's job is to decide WHERE the mark is, and folding is
    what cancels the photograph out of that decision. Cutting the stamp needs
    something else: a picture of the mark clean enough that its faintest stroke
    outranks whatever scenery is left over.

    A fold of a dozen copies is not that picture. Cancellation needs numbers --
    it is the same shortfall MIN_TILES=9 exists to refuse -- and measured on the
    sample photo the residue left in a 13-copy fold is as strong as the mark's
    thinnest lettering, so an ink cut taken on it spends its allowance on
    leftover photograph. Every cut tried on that fold left "MUYE OUTDOOR"
    unmasked at sensitivity 0, 50 and 100.

    The seed needs no cancelling instead of poor cancelling. It is an ANCHOR:
    _anchor_candidates ranks only responses inside the quietest 45% of the
    frame, so the seed copy sits on the smoothest ground any copy sits on, and
    its residual is close to the mark alone. Measured, the same allowance then
    covers the disc, every glyph and the full lettering -- 4.2% of frame at
    destruction 66, against 1.9% and lettering left behind from the fold.
    """
    cell_y, cell_x = cell
    top, left = seed[0] - offset[0], seed[1] - offset[1]
    tile = np.zeros((cell_y, cell_x), np.float32)
    rt, rl = max(0, top), max(0, left)
    rb, rr = min(field.shape[0], top + cell_y), min(field.shape[1], left + cell_x)
    if rb <= rt or rr <= rl:
        return None
    tile[rt - top : rb - top, rl - left : rr - left] = field[rt:rb, rl:rr]
    return tile if tile.std() > 1e-3 else None


def _shape(folded, ink, offset, patch_shape, sensitivity):
    """(stamp, how much of the cell the ink fills).

    The emptiness test is _fold_on_grid's own second, independent check, reused
    here verbatim including its constant: a watermark cell is mostly empty by
    construction, and that emptiness is what makes it a watermark rather than a
    texture. Fold on a grid that is not there and nothing coheres, so the ink
    cut spreads over the whole tile.
    """
    cell_y, cell_x = folded.shape
    off_y, off_x = offset
    high, wide = patch_shape
    energy = cv2.GaussianBlur(np.abs(folded), (0, 0), sigmaX=3)
    peak = float(energy.max())
    core = (energy >= _INK_SHARE * peak).astype(np.uint8)
    core = cv2.morphologyEx(core, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    _count, labels, stats, _mid = cv2.connectedComponentsWithStats(core, 8)
    window = labels[
        max(0, off_y) : max(0, off_y + high), max(0, off_x) : max(0, off_x + wide)
    ]
    touching = {int(v) for v in np.unique(window) if v > 0}
    if not touching:
        return None, None
    boxes = [stats[label] for label in touching]
    top = max(0, min(b[cv2.CC_STAT_TOP] for b in boxes) - _INK_MARGIN)
    left = max(0, min(b[cv2.CC_STAT_LEFT] for b in boxes) - _INK_MARGIN)
    bottom = min(
        cell_y,
        max(b[cv2.CC_STAT_TOP] + b[cv2.CC_STAT_HEIGHT] for b in boxes) + _INK_MARGIN,
    )
    right = min(
        cell_x,
        max(b[cv2.CC_STAT_LEFT] + b[cv2.CC_STAT_WIDTH] for b in boxes) + _INK_MARGIN,
    )
    fills = ((bottom - top) / cell_y, (right - left) / cell_x)
    if bottom - top < 12 or right - left < 12:
        return None, fills
    if fills[0] > _INK_CELL_SHARE or fills[1] > _INK_CELL_SHARE:
        return None, fills

    # Cut on the INK image inside the box the fold already drew and already
    # gated: see whitened() for why its background estimate is the wider one.
    # Both are cell-sized and share the fold's offset, so they are pixel-aligned
    # by construction.
    fraction = _TILED_INK_MAX - (_TILED_INK_MAX - _TILED_INK_MIN) * (
        max(0, min(100, sensitivity)) / 100
    )
    region = cv2.GaussianBlur(np.abs(ink), (0, 0), sigmaX=3)[top:bottom, left:right]
    body = np.zeros(folded.shape, np.uint8)
    body[top:bottom, left:right] = (region >= fraction * float(region.max())) * 255
    body = cv2.morphologyEx(
        body, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    )
    pieces, labels, stats, _m = cv2.connectedComponentsWithStats(body, 8)
    for label in range(1, pieces):
        if stats[label][cv2.CC_STAT_AREA] < _FILL_MIN_PIECE:
            body[labels == label] = 0
    # Fill enclosed holes. The mark's largest solid feature is a filled disc,
    # and a residual taken against a background estimate NARROWER than that disc
    # reads its interior as empty -- so the disc masks as a ring, and inpainting
    # leaves a circular ghost exactly where the logo was. Filling what the ink
    # encloses recovers the solid shape without needing the background estimate
    # to be wider than every feature the mark might have. Enclosed gaps are
    # don't-care ground truth anyway: a few pixels across, and they refill from
    # their surroundings.
    holes = body.copy()
    edged = cv2.copyMakeBorder(holes, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    flood = np.zeros((edged.shape[0] + 2, edged.shape[1] + 2), np.uint8)
    cv2.floodFill(edged, flood, (0, 0), 255)
    body = cv2.bitwise_or(body, cv2.bitwise_not(edged[1:-1, 1:-1]))
    return (body if body.any() else None), fills


def propose_tiled_mask(rgb, sensitivity, trace=None):
    """Mask a tiled overlay whose cell is too big for the fold, or None.

    The last thing tried, after recover_mark, apply_mark and every borrowed
    mark have all declined. See the module docstring.

    ``trace``, when given a dict, records the gate's own numbers (anchors,
    lattice, votes, cells, share, lines, fills, stamps). Tests pin those rather
    than only the outcome, so a refactor that moves the bar fails loudly
    instead of quietly masking a different population.
    """
    work = _work_size(rgb)
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    field = whitened(gray)
    span = field.shape
    extent = (2 * _ANCHOR_HALF_H, 2 * _ANCHOR_HALF_W)
    pad = max(extent)

    seen, reports = set(), []
    for cy, cx in list(_anchor_candidates(gray)) + _tiled_anchors(field, *extent):
        top = int(np.clip(cy - _ANCHOR_HALF_H, 0, span[0] - extent[0]))
        left = int(np.clip(cx - _ANCHOR_HALF_W, 0, span[1] - extent[1]))
        if (top, left) in seen:
            continue
        seen.add((top, left))
        patch = field[top : top + extent[0], left : left + extent[1]]
        if patch.std() < 1e-3:
            continue
        score = _score_field(field, patch, pad)
        found = _scan(score, pad, top, left, span[0], span[1], *extent)
        if found is not None:
            reports.append(found)
    if trace is not None:
        trace["anchors"] = len(reports)
    if not reports:
        return None

    # A lattice more than one anchor found. Anchors are cut at different
    # features of the mark and at different copies of it, so agreeing on a
    # pitch is agreement about the OVERLAY rather than about one patch of
    # scenery -- the single-image form of the pitch agreement pooled_marks
    # takes across a batch.
    tally = Counter((r["py"], r["px"]) for r in reports)
    (py, px), votes = tally.most_common(1)[0]
    if trace is not None:
        trace.update(lattice=(py, px), votes=votes)
    if votes < _MIN_TILED_VOTES:
        return None
    winner = max(
        (r for r in reports if (r["py"], r["px"]) == (py, px)), key=lambda r: r["mean"]
    )
    seed = winner["seed"]

    patch = field[seed[0] : seed[0] + extent[0], seed[1] : seed[1] + extent[1]]
    score = _score_field(field, patch, pad)
    rows, cols, vals = _cells(score, pad, seed, (py, px), span, extent)
    if trace is not None:
        probe = np.abs(vals) >= _CELL_LIVE
        si = rows.index(seed[0]) if seed[0] in rows else -1
        sj = cols.index(seed[1]) if seed[1] in cols else -1
        if si >= 0 and sj >= 0:
            probe[si, sj] = False
        trace.update(
            cells=(len(rows), len(cols)),
            share=round(float(probe.sum()) / max(1, probe.size - 1), 3),
            lines=min(int(probe.any(axis=1).sum()), int(probe.any(axis=0).sum())),
        )
    verdict = _accept(rows, cols, vals, seed)
    if verdict is None:
        return None

    offset = (py // 2 - extent[0] // 2, px // 2 - extent[1] // 2)
    folded = _fold(field, rows, cols, verdict["live"], vals, (py, px), offset)
    if folded is None:
        return None
    ink = _seed_ink(whitened(gray, _INK_BG), seed, (py, px), offset)
    if ink is None:
        return None
    body, fills = _shape(folded, ink, offset, extent, sensitivity)
    if trace is not None:
        trace["fills"] = (
            None if fills is None else (round(fills[0], 3), round(fills[1], 3))
        )
    if body is None:
        return None

    mask = np.zeros(gray.shape, np.uint8)
    stamps = 0
    for r, y in enumerate(rows):
        for c, x in enumerate(cols):
            if not verdict["live"][r, c]:
                continue
            top, left = y - offset[0], x - offset[1]
            sy, sx = max(0, -top), max(0, -left)
            dy, dx = max(0, top), max(0, left)
            hi = min(body.shape[0] - sy, mask.shape[0] - dy)
            wi = min(body.shape[1] - sx, mask.shape[1] - dx)
            if hi <= 0 or wi <= 0:
                continue
            region = mask[dy : dy + hi, dx : dx + wi]
            region[:] = np.maximum(region, body[sy : sy + hi, sx : sx + wi])
            stamps += 1
    if trace is not None:
        trace["stamps"] = stamps
    if not mask.any():
        return None
    if mask.shape != (height, width):
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    return mask
