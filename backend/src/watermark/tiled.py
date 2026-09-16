"""The tiled route: a repeating overlay whose cell is too big for the fold."""

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

# --- constants ---
# Local-energy window, and the two background widths (see whitened).
_WHITEN_ENERGY = 31
_MATCH_BG = 31  # wider and the search locks onto the photo's own structure
_INK_BG = 91  # narrower and a solid feature is absorbed, folding as a ring
# Period search, in working pixels.
_TILED_MIN_PERIOD = 150
_TILED_MAX_SHARE = 0.75
_TILED_STEP = 2
# Share of a predicted cell that must be inside the frame to be read.
_MIN_VISIBLE = 0.5
# Cells a lattice must predict (not counting the seed).
_MIN_CELLS = 4
# Anchors enumerated from the whitened field, in addition to pattern.py's own.
_TILED_ANCHOR_COUNT = 10


def whitened(gray: np.ndarray, background: int = _MATCH_BG) -> np.ndarray:
    """Residual against a ``background``-wide median, over its own local energy."""
    g8 = gray if gray.dtype == np.uint8 else np.clip(gray, 0, 255).astype(np.uint8)
    res = g8.astype(np.float32) - cv2.medianBlur(g8, background).astype(np.float32)
    energy = cv2.boxFilter(res * res, -1, (_WHITEN_ENERGY, _WHITEN_ENERGY))
    return res / (np.sqrt(energy) + 1.0)


def _score_field(
    padded: np.ndarray, patch: np.ndarray, substance: np.ndarray
) -> np.ndarray:
    """Correlation of ``patch`` over the zero-padded field, given its window std."""
    score = cv2.matchTemplate(padded, patch, cv2.TM_CCOEFF_NORMED)
    # TM_CCOEFF_NORMED returns 1.0 over a flat window (0/0), so those are zeroed.
    floor = _MIN_WINDOW_STD_SHARE * float(patch.std())
    gate = substance[: score.shape[0], : score.shape[1]]
    return np.where(gate >= floor, score, 0.0).astype(np.float32)


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
    """The best lattice through the seed, by mean score x sqrt(cells it predicts)."""
    ys = np.arange(_TILED_MIN_PERIOD, int(_TILED_MAX_SHARE * span_y) + 1, _TILED_STEP)
    xs = np.arange(_TILED_MIN_PERIOD, int(_TILED_MAX_SHARE * span_x) + 1, _TILED_STEP)
    if len(ys) == 0 or len(xs) == 0:
        return None

    # One matrix multiply scores every lattice against the folded row profiles.
    width = score.shape[1]
    weights = np.zeros((len(xs), width), np.float32)
    counts_x = np.zeros(len(xs), np.int32)
    for j, px in enumerate(xs):
        cols = [x + pad for x in _positions(seed_x, int(px), span_x, wide)]
        cols = [c for c in cols if 0 <= c < width]
        if cols:
            weights[j, cols] = 1.0 / len(cols)
            counts_x[j] = len(cols)

    # Magnitude: the same overlay correlates with opposite signs over sky and shadow.
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
        # Minus the seed's own cell, a self-match scoring 1.0.
        rest = (means * cells - 1.0) / np.maximum(cells - 1, 1)
    stat = rest * np.sqrt(np.maximum(cells - 1, 0))
    # A single row or column of cells is a line, which photographs offer for free.
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
    """Patch-sized windows carrying the most whitened energy, well separated."""
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
# Correlation above which a predicted cell counts as holding a copy.
_CELL_LIVE = 0.06
# Share of predicted cells that must hold a copy, and the shape they must make.
_MIN_LIVE_SHARE = 0.65
_MIN_LIVE_LINES = 2
# Anchors that must independently converge on the same lattice.
_MIN_TILED_VOTES = 2

# Ink cut, sensitivity 0..100, as a fraction of the seed copy's own peak energy.
# A percentile would assume the box is full; here the mark is under a third of it.
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
    """The gate: are the copies arranged on a grid, or merely present?"""
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
    """The cell median-folded over the live copies, each in its own polarity."""
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
    """The seed copy's cell on the fold's grid: cleaner to cut than a small fold."""
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
    """(stamp, how much of the cell the ink fills)."""
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

    # Cut on the ink image (wider background) inside the box the fold drew.
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
    # Fill enclosed holes: a solid disc folds as a ring against a narrow background.
    holes = body.copy()
    edged = cv2.copyMakeBorder(holes, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    flood = np.zeros((edged.shape[0] + 2, edged.shape[1] + 2), np.uint8)
    cv2.floodFill(edged, flood, (0, 0), 255)
    body = cv2.bitwise_or(body, cv2.bitwise_not(edged[1:-1, 1:-1]))
    return (body if body.any() else None), fills


def propose_tiled_mask(rgb, sensitivity, trace=None):
    """Last route tried: a tiled overlay whose cell is too big to fold, or None."""
    work = _work_size(rgb)
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    field = whitened(gray)
    span = field.shape
    extent = (2 * _ANCHOR_HALF_H, 2 * _ANCHOR_HALF_W)
    pad = max(extent)

    padded = cv2.copyMakeBorder(field, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    # The padded field never changes, so its window std depends only on the patch shape.
    stds: dict[tuple[int, int], np.ndarray] = {}

    def substance(shape: tuple[int, int]) -> np.ndarray:
        if shape not in stds:
            stds[shape] = _window_std(padded, shape)
        return stds[shape]

    seen, reports = set(), []
    # Both: on a smooth gradient the mark is the only texture, so it is never "quiet".
    for cy, cx in list(_anchor_candidates(gray)) + _tiled_anchors(field, *extent):
        top = int(np.clip(cy - _ANCHOR_HALF_H, 0, span[0] - extent[0]))
        left = int(np.clip(cx - _ANCHOR_HALF_W, 0, span[1] - extent[1]))
        if (top, left) in seen:
            continue
        seen.add((top, left))
        patch = field[top : top + extent[0], left : left + extent[1]]
        if patch.std() < 1e-3:
            continue
        score = _score_field(padded, patch, substance(patch.shape))
        found = _scan(score, pad, top, left, span[0], span[1], *extent)
        if found is not None:
            reports.append(found)
    if trace is not None:
        trace["anchors"] = len(reports)
    if not reports:
        return None

    # Different anchors agreeing on a pitch is agreement about the overlay, not scenery.
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
    score = _score_field(padded, patch, substance(patch.shape))
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
