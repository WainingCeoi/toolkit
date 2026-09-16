"""Recover a repeating watermark by folding its tiles, then match it back."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import cv2
import numpy as np

# Longest side the search runs at; full-frame correlation is far too slow.
WORK_MAX = 2000

# Removes illumination and large-scale content, keeps watermark strokes.
HIGHPASS_SIGMA = 18

# Period search bounds, in working pixels.
MIN_PERIOD = 45
# How far off-axis a peak may sit and still count as the row/column period.
AXIS_TOLERANCE = 8
# Harmonics summed when scoring a candidate pitch.
_HARMONICS = 4
# Peaks considered, off-integer tolerance, and peaks a basis must explain.
_PEAK_COUNT = 60
_LATTICE_TOL = 0.18
_MIN_SUPPORT = 5

_REFINE_SCHEDULE = ((2, 10), (3, 7), (6, 5), (8, 3))
_SCORE_ORDER = 3
# Largest lag, as a share of each dimension, that still carries evidence.
_MAX_LAG_SHARE = 0.35
# The quietest share of the photo, which is where the overlay is measurable.
_QUIET_PCT = 45.0

# Evidence gates; below any of these the route answers None.
# The fold is a median: with too few tiles the scenery does not cancel.
MIN_TILES = 9
MIN_NCC = 0.30
MIN_INSTANCES = 6
# The mark must be this much stronger than a misaligned fold of the same tiles.
MIN_SIGNIFICANCE = 2.0

# Sensitivity -> share of the mark's footprint stamped; every site multiplies it.
_FOOTPRINT_MAX_PCT = 97.0
_FOOTPRINT_MIN_PCT = 72.0

# Share of the tile the matched crop spans; the whole tile is mostly empty.
_CROP_FRACTION = 0.62

# A site the grid did not predict must correlate better than one it did.
_MIN_NCC_UNPROMPTED = 0.45

# Normalised correlation scores 1.0 over a flat window (0/0), so peaks need substance.
_MIN_WINDOW_STD_SHARE = 0.25

# Share of a cell a match may sit off a node, and share of matches that must be
# on one: the only gate that tells an overlay from a lattice fitted to scenery.
_NODE_TOLERANCE = 0.2
_MIN_ON_LATTICE = 0.65

# A stamped pixel is kept only where the image deviates from its own neighbourhood.
_EVIDENCE_RATIO = 1.0
_EVIDENCE_WINDOW = 81
_EVIDENCE_FLOOR = 1.0
# Less of the stamp surviving than this means the repeat was a period artefact.
_MIN_EVIDENCE_SHARE = 0.2
# Percentile the filled shape is cut at: the mark's ink outline.
_FILL_PCT = 90.0
# Filled pieces smaller than this are the fold's residue, not ink.
_FILL_MIN_PIECE = 40
# Pull the fill inside the ink's blurred edge; the removal mask is dilated by more.
_FILL_ERODE_PX = 3
# Fixed footprint the evidence gate is measured over, so sensitivity cannot move it.
_GATE_PCT = 90.0

# --- the sparse route (see pooled_marks) ----------------------------------
# Half-extents of the patch cut around an anchor; it is both matched and stamped.
_ANCHOR_HALF_H = 34
_ANCHOR_HALF_W = 78
# Candidate anchors tried per image, and the response filter that ranks them.
_ANCHOR_COUNT = 12
_ANCHOR_KERNEL = 13
_NORM_WIN = 81
_NORM_FLOOR = 2.0
# Correlation a match needs to join a run, how many matches make one, how far off
# an even spacing a member may sit, and how many matches are considered at all.
_RUN_NCC = 0.35
MIN_RUN = 3
_RUN_TOL = 0.06
_MAX_SITES = 40
# Images that must independently produce a run, and how closely their pitches
# must agree, before the batch is believed to share one overlay.
_MIN_POOL_IMAGES = 3
_PITCH_TOL = 0.04
# Range the pitch across a run is voted over, and the snap around each predicted site.
_CROSS_MAX = 500
_CROSS_SNAP = 6
# Fewest candidates the vote needs, the margin a doubled pitch must beat the voted
# one by (see _true_pitch), and how prominent the winner must be (robust deviations).
_CROSS_MIN_CANDIDATES = 60
_SUBHARMONIC_EDGE = 1.25
_CROSS_PROMINENCE = 4.5
# Ink: a share of the mark's own peak, plus slack; the trimmed mark must leave a
# quarter of the cell empty in both axes (see _fold_on_grid).
_INK_SHARE = 0.25
_INK_MARGIN = 6
_INK_CELL_SHARE = 0.75
# Cells stacked per fold; unbounded stacking made peak memory grow with the batch.
_FOLD_MAX_TILES = 48


TRACE = {}


def _highpass(gray: np.ndarray) -> np.ndarray:
    g = gray.astype(np.float32)
    return g - cv2.GaussianBlur(g, (0, 0), sigmaX=HIGHPASS_SIGMA)


def _local_texture(gray: np.ndarray, win: int = 51) -> np.ndarray:
    """Local standard deviation — how busy the photo is around each pixel."""
    g = gray.astype(np.float32)
    mean = cv2.blur(g, (win, win))
    return np.sqrt(np.maximum(cv2.blur(g * g, (win, win)) - mean * mean, 0))


def _masked_autocorrelation(hp: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Masked autocorrelation of ``hp`` where ``weight`` is set, weight divided out."""
    signal = np.fft.rfft2(hp * weight)
    mask_spectrum = np.fft.rfft2(weight)
    numerator = np.fft.irfft2(signal * np.conj(signal), s=hp.shape)
    denominator = np.fft.irfft2(mask_spectrum * np.conj(mask_spectrum), s=hp.shape)
    # Lags where too little of the mask overlaps itself carry no evidence.
    floor = 0.02 * denominator.flat[0]
    ac = np.where(denominator > floor, numerator / np.maximum(denominator, 1e-9), 0.0)
    if ac.flat[0] > 0:
        ac = ac / ac.flat[0]
    return np.fft.fftshift(ac)


def _pitch(profile: np.ndarray) -> int | None:
    """The repeat pitch in a 1-D autocorrelation profile, or None."""
    limit = len(profile) - 1
    if limit < 2 * MIN_PERIOD:
        return None
    window = 2 * MIN_PERIOD + 1
    trend = cv2.blur(profile.reshape(-1, 1).astype(np.float32), (1, window)).ravel()
    detrended = profile - trend

    best, best_score = None, -np.inf
    for d in range(MIN_PERIOD, limit // 2 + 1):
        harmonics = [k * d for k in range(1, _HARMONICS + 1) if k * d <= limit]
        if len(harmonics) < 2:
            continue
        # Must be the local peak, not a point on somebody else's slope.
        near = detrended[max(0, d - 3) : min(limit, d + 4)]
        if detrended[d] < near.max() - 1e-12:
            continue
        score = float(np.mean([detrended[k] for k in harmonics]))
        if score > best_score:
            best, best_score = d, score
    if best is None or best_score <= 0:
        return None
    return best


def _rect_period(ac: np.ndarray | None) -> tuple[int, int] | None:
    """The (vertical, horizontal) period the overlay repeats on, or None."""
    if ac is None:  # no quiet share, so the caller had no autocorrelation to give
        return None
    cy, cx = ac.shape[0] // 2, ac.shape[1] // 2
    vertical = ac[cy:, cx - AXIS_TOLERANCE : cx + AXIS_TOLERANCE + 1].mean(axis=1)
    horizontal = ac[cy - AXIS_TOLERANCE : cy + AXIS_TOLERANCE + 1, cx:].mean(axis=0)
    py, px = _pitch(vertical), _pitch(horizontal)
    if py is None or px is None:
        return None
    return py, px


def _lag_window(shape: tuple[int, int]) -> tuple[int, int, int, int]:
    """Centre and usable lag reach of a shifted autocorrelation."""
    height, width = shape
    return (
        height // 2,
        width // 2,
        int(height * _MAX_LAG_SHARE),
        int(width * _MAX_LAG_SHARE),
    )


def _debias(ac: np.ndarray) -> np.ndarray:
    """Remove additive separable structure -- f(dy) + g(dx) -- from an AC."""
    cy, cx, ry, rx = _lag_window(ac.shape)
    top, bottom = max(0, cy - ry), min(ac.shape[0], cy + ry + 1)
    left, right = max(0, cx - rx), min(ac.shape[1], cx + rx + 1)
    out = ac.copy()
    view = out[top:bottom, left:right]
    view -= np.median(view, axis=1, keepdims=True)
    view -= np.median(view, axis=0, keepdims=True)
    return out


def _peaks(prominence: np.ndarray, count: int) -> list[tuple[float, float]]:
    """The strongest autocorrelation offsets, as (dy, dx) in the half-plane."""
    height, width = prominence.shape
    cy, cx = height // 2, width // 2
    # The AC is symmetric, and past the usable reach it is zero -- a cliff that would
    # win as a peak. Only this window can hold one, so crop to it and search there.
    reach_y, reach_x = int(height * _MAX_LAG_SHARE), int(width * _MAX_LAG_SHARE)
    left = max(0, cx - reach_x)
    work = prominence[cy : cy + reach_y, left : cx + reach_x].copy()
    if work.size == 0:
        return []
    yy, xx = np.mgrid[0 : work.shape[0], left - cx : left - cx + work.shape[1]]
    work[yy * yy + xx * xx < MIN_PERIOD**2] = -np.inf
    found = []
    for _ in range(count):
        idx = np.unravel_index(int(np.argmax(work)), work.shape)
        if not np.isfinite(work[idx]) or work[idx] <= 0:
            break
        found.append((float(idx[0]), float(left + idx[1] - cx)))
        y0, x0 = idx
        radius = max(8, MIN_PERIOD // 2)
        work[
            max(0, y0 - radius) : y0 + radius + 1, max(0, x0 - radius) : x0 + radius + 1
        ] = -np.inf
    return found


def _fit_lattice(peaks: list[tuple[float, float]]) -> np.ndarray | None:
    """A 2x2 basis whose integer combinations explain the peaks, or None."""
    if len(peaks) < 2:
        return None
    points = np.array([(dx, dy) for dy, dx in peaks], np.float64)  # rows are (x, y)
    # triu_indices walks the pairs in the order the nested loops did, for the tie-break.
    left, right = np.triu_indices(len(peaks), 1)
    bases = np.stack([points[left], points[right]], axis=2)  # columns are the vectors
    usable = np.abs(np.linalg.det(bases)) >= MIN_PERIOD**2 * 0.25  # not near-collinear
    left, right, bases = left[usable], right[usable], bases[usable]
    if len(bases) == 0:
        return None

    coords = np.linalg.inv(bases) @ points.T  # every peak's lattice coords, per pair
    on = np.all(np.abs(coords - np.round(coords)) <= _LATTICE_TOL, axis=1)
    support = on.sum(axis=1)
    enough = np.nonzero(support >= _MIN_SUPPORT)[0]
    if len(enough) == 0:
        return None
    # shorter wins; a doubled vector fits too
    length = np.hypot(points[left, 1], points[left, 0]) + np.hypot(
        points[right, 1], points[right, 0]
    )
    best = enough[np.lexsort((enough, length[enough], -support[enough]))[0]]

    basis = bases[best]
    # Refit from every supported peak, putting the vectors on a sub-pixel footing.
    integer_coords = np.round(coords[best][:, on[best]])  # 2 x N
    observed = points[on[best]].T  # 2 x N
    gram = integer_coords @ integer_coords.T
    if abs(np.linalg.det(gram)) < 1e-9:
        return basis
    refined = observed @ integer_coords.T @ np.linalg.inv(gram)
    return refined if abs(np.linalg.det(refined)) > MIN_PERIOD**2 * 0.25 else basis


def _reduce_basis(basis: np.ndarray) -> np.ndarray:
    """The shortest, most nearly orthogonal basis of the same lattice."""
    a = basis[:, 0].astype(np.float64).copy()
    b = basis[:, 1].astype(np.float64).copy()
    for _ in range(64):
        if b @ b < a @ a:
            a, b = b, a
        if a @ a <= 1e-12:
            break
        mu = round(float((b @ a) / (a @ a)))
        if mu == 0:
            break
        b = b - mu * a
    if b @ b < a @ a:
        a, b = b, a
    return np.column_stack([a, b])


def _refine_basis(ac: np.ndarray, basis: np.ndarray) -> np.ndarray:
    """Least-squares fit of the basis to where the AC peaks actually are."""
    cy, cx, ry, rx = _lag_window(ac.shape)
    basis = basis.astype(np.float64).copy()
    for order, half in _REFINE_SCHEDULE:
        coefficients, observed = [], []
        for i in range(-order, order + 1):
            for j in range(-order, order + 1):
                if i == 0 and j == 0:
                    continue
                dx, dy = basis @ np.array([i, j], np.float64)
                if dx * dx + dy * dy < MIN_PERIOD**2:
                    continue
                if abs(dy) > ry - half - 1 or abs(dx) > rx - half - 1:
                    continue
                y0, x0 = int(round(cy + dy)), int(round(cx + dx))
                window = ac[y0 - half : y0 + half + 1, x0 - half : x0 + half + 1]
                if window.size == 0:
                    continue
                weight = np.clip(window - np.median(window), 0, None)
                if weight.sum() <= 1e-12:
                    continue
                gy, gx = np.mgrid[0 : window.shape[0], 0 : window.shape[1]]
                oy = float((gy * weight).sum() / weight.sum()) - half
                ox = float((gx * weight).sum() / weight.sum()) - half
                observed.append([x0 - cx + ox, y0 - cy + oy])
                coefficients.append([i, j])
        if len(coefficients) < 4:
            break
        integer_coords = np.array(coefficients, np.float64).T
        sites = np.array(observed, np.float64).T
        gram = integer_coords @ integer_coords.T
        if abs(np.linalg.det(gram)) < 1e-9:
            break
        candidate = sites @ integer_coords.T @ np.linalg.inv(gram)
        if abs(np.linalg.det(candidate)) < MIN_PERIOD**2 * 0.25:
            break
        basis = candidate
    return basis


def _lattice_score(ac: np.ndarray, basis: np.ndarray) -> float:
    """How much autocorrelation actually sits on this lattice's sites."""
    cy, cx, ry, rx = _lag_window(ac.shape)
    values = []
    for i in range(-_SCORE_ORDER, _SCORE_ORDER + 1):
        for j in range(-_SCORE_ORDER, _SCORE_ORDER + 1):
            if i == 0 and j == 0:
                continue
            dx, dy = basis @ np.array([i, j], np.float64)
            if dx * dx + dy * dy < MIN_PERIOD**2:
                continue
            if abs(dy) > ry - 1 or abs(dx) > rx - 1:
                continue
            values.append(float(ac[int(round(cy + dy)), int(round(cx + dx))]))
    if len(values) < 4:
        return -np.inf
    return float(np.median(values))


def _fit_rectifying_lattice(ac: np.ndarray) -> np.ndarray | None:
    """The best lattice basis the autocorrelation supports, or None."""
    debiased = _debias(ac)
    best, best_score = None, -np.inf
    for source in (ac, debiased):
        prominence = source - cv2.GaussianBlur(source, (0, 0), sigmaX=9)
        rough = _fit_lattice(_peaks(prominence, _PEAK_COUNT))
        if rough is None:
            continue
        basis = _reduce_basis(_refine_basis(debiased, _reduce_basis(rough)))
        if abs(np.linalg.det(basis)) < MIN_PERIOD**2 * 0.25:
            continue
        score = _lattice_score(debiased, basis)
        if score > best_score:
            best, best_score = basis, score
    return best


def _warp_to_lattice(basis: np.ndarray, shape: tuple[int, int]):
    """An affine that makes the lattice axis-aligned, plus the resulting cell."""
    cell_x = int(round(np.hypot(basis[0, 0], basis[1, 0])))
    cell_y = int(round(np.hypot(basis[0, 1], basis[1, 1])))
    if cell_x < MIN_PERIOD or cell_y < MIN_PERIOD:
        return None
    linear = np.diag([cell_x, cell_y]).astype(np.float64) @ np.linalg.inv(basis)

    height, width = shape
    corners = np.array([[0, width, 0, width], [0, 0, height, height]], np.float64)
    mapped = linear @ corners
    offset = -mapped.min(axis=1)
    out_w = int(np.ceil(mapped[0].max() + offset[0]))
    out_h = int(np.ceil(mapped[1].max() + offset[1]))
    # A pathological shear can blow the rectified frame up; refuse it.
    if out_w <= 0 or out_h <= 0 or out_w * out_h > 4 * width * height:
        return None
    forward = np.hstack([linear, offset.reshape(2, 1)])
    return forward, (cell_y, cell_x), (out_h, out_w)


def _fold_template(
    hp: np.ndarray,
    texture: np.ndarray,
    py: int,
    px: int,
    cover: np.ndarray | None = None,
) -> np.ndarray | None:
    """Median of the quietest tiles, per phase — the recovered mark."""
    if cover is None:
        cover = np.ones(hp.shape, np.float32)
    inside = cover > 0.5
    if not inside.any():
        return None
    quiet = np.percentile(texture[inside], 35)
    tiles = []
    busy_tiles = []
    for top in range(0, hp.shape[0] - py + 1, py):
        for left in range(0, hp.shape[1] - px + 1, px):
            if cover[top : top + py, left : left + px].mean() < 0.98:
                continue
            cell = hp[top : top + py, left : left + px]
            if np.mean(texture[top : top + py, left : left + px] < quiet) > 0.85:
                tiles.append(cell)
            else:
                busy_tiles.append(cell)
    # Busy tiles still cancel under the median, just slower.
    if len(tiles) < MIN_TILES:
        tiles = tiles + busy_tiles
    if len(tiles) < MIN_TILES:
        return None
    stack = np.stack(tiles)
    template = np.median(stack, axis=0).astype(np.float32)

    # Null: the same tiles rolled by arbitrary offsets, so no real pattern survives.
    rng = np.random.default_rng(0)  # fixed, so a given image always agrees
    scrambled = np.stack(
        [
            np.roll(
                tile,
                (int(rng.integers(0, py)), int(rng.integers(0, px))),
                axis=(0, 1),
            )
            for tile in stack
        ]
    )
    null = np.median(scrambled, axis=0).astype(np.float32)

    def peak(a):
        return float(cv2.GaussianBlur(np.abs(a), (0, 0), sigmaX=3).max())

    null_peak = peak(null)
    if null_peak <= 1e-6 or peak(template) < MIN_SIGNIFICANCE * null_peak:
        return None
    return template


def _crop_to_mark(template: np.ndarray) -> tuple[np.ndarray, int, int]:
    """The busiest part of the tile, plus where it sits in the tile."""
    energy = cv2.GaussianBlur(np.abs(template), (0, 0), sigmaX=6)
    _min_v, _max_v, _min_l, max_loc = cv2.minMaxLoc(energy)
    cx, cy = max_loc
    half_y = max(
        12, min(template.shape[0], round(template.shape[0] * _CROP_FRACTION)) // 2
    )
    half_x = max(
        12, min(template.shape[1], round(template.shape[1] * _CROP_FRACTION)) // 2
    )
    top = int(np.clip(cy - half_y, 0, max(0, template.shape[0] - 2 * half_y)))
    left = int(np.clip(cx - half_x, 0, max(0, template.shape[1] - 2 * half_x)))
    return template[top : top + 2 * half_y, left : left + 2 * half_x], top, left


class Mark:
    """A recovered watermark, reusable on other images of the same batch."""

    __slots__ = (
        "basis",
        "template",
        "patch",
        "crop_top",
        "crop_left",
        "cell",
        "pooled",
        "grid",
    )

    def __init__(
        self,
        basis,
        template,
        patch,
        crop_top,
        crop_left,
        cell,
        pooled=False,
        grid=None,
    ):
        self.basis = basis
        self.template = template
        self.patch = patch
        self.crop_top = crop_top
        self.crop_left = crop_left
        self.cell = cell  # how far apart two copies can be told apart
        self.pooled = pooled
        self.grid = grid  # lattice stamped at every site; None when unknown


def _work_size(rgb: np.ndarray) -> np.ndarray:
    """The image at the bounded working size the whole detector runs at."""
    height, width = rgb.shape[:2]
    scale = max(height, width) / WORK_MAX
    if scale <= 1:
        return rgb
    return cv2.resize(
        rgb,
        (max(1, round(width / scale)), max(1, round(height / scale))),
        interpolation=cv2.INTER_AREA,
    )


def _rectify(hp: np.ndarray, basis: np.ndarray | None):
    """Warp onto the lattice's own axes, or None if there is no usable lattice."""
    if basis is None:
        return None
    return _warp_to_lattice(basis, hp.shape)


def recover_mark(rgb: np.ndarray) -> Mark | None:
    """Recover the repeating mark in this image, or None if it cannot be."""
    work = _work_size(rgb)
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    hp = _highpass(gray)
    texture = _local_texture(gray)

    # Rectify onto the overlay's own grid first; downstream works in rows and columns.
    basis, ac = None, None
    quiet = (texture < np.percentile(texture, _QUIET_PCT)).astype(np.float32)
    if quiet.mean() >= 0.05:
        ac = _masked_autocorrelation(hp, quiet)
        basis = _fit_rectifying_lattice(ac.astype(np.float32))
    rectify = _rectify(hp, basis)

    cover = None
    if rectify is not None:
        forward, (py, px), (out_h, out_w) = rectify
        cover = cv2.warpAffine(
            np.ones(hp.shape, np.float32),
            forward,
            (out_w, out_h),
            flags=cv2.INTER_NEAREST,
        )
        hp = cv2.warpAffine(hp, forward, (out_w, out_h), flags=cv2.INTER_LINEAR)
        texture = cv2.warpAffine(
            texture, forward, (out_w, out_h), flags=cv2.INTER_LINEAR
        )
    else:
        # No usable lattice: take the row/column pitch off that same AC (hp is unwarped
        # here). The basis is dropped; nothing downstream was rectified with it.
        basis = None
        period = _rect_period(ac)
        if period is None:
            return None
        py, px = period
    if py < MIN_PERIOD or px < MIN_PERIOD:
        return None

    template = _fold_template(hp, texture, py, px, cover)
    if template is None:
        return None
    patch, crop_top, crop_left = _crop_to_mark(template)
    if min(patch.shape) < 12 or patch.std() <= 1e-3:
        return None
    if patch.shape[0] >= hp.shape[0] or patch.shape[1] >= hp.shape[1]:
        return None
    return Mark(basis, template, patch, crop_top, crop_left, (py, px))


def _window_std(hp: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Window std of ``hp`` at every placement, indexed like a matchTemplate map."""
    high, wide = shape
    mean = cv2.blur(hp, (wide, high))
    mean_square = cv2.blur(hp * hp, (wide, high))
    deviation = np.sqrt(np.maximum(mean_square - mean * mean, 0))
    top, left = high // 2, wide // 2
    return deviation[top : top + hp.shape[0], left : left + hp.shape[1]]


def apply_mark(
    rgb: np.ndarray, mark: Mark, sensitivity: int, own: bool = True
) -> np.ndarray | None:
    """Mask every instance of ``mark`` in this image, or None if there are none."""
    height, width = rgb.shape[:2]
    work = _work_size(rgb)
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    hp = _highpass(gray)

    rectify = _rectify(hp, mark.basis)
    if mark.basis is not None and rectify is None:
        return None
    if rectify is not None:
        forward, (py, px), (out_h, out_w) = rectify
        hp = cv2.warpAffine(hp, forward, (out_w, out_h), flags=cv2.INTER_LINEAR)
        gray = cv2.warpAffine(gray, forward, (out_w, out_h), flags=cv2.INTER_LINEAR)
    else:
        py, px = mark.cell

    template, patch = mark.template, mark.patch
    crop_top, crop_left = mark.crop_top, mark.crop_left
    if patch.shape[0] >= hp.shape[0] or patch.shape[1] >= hp.shape[1]:
        return None

    score = cv2.matchTemplate(hp, patch, cv2.TM_CCOEFF_NORMED)
    # Discard peaks with nothing beneath them before anything reads this map.
    floor = _MIN_WINDOW_STD_SHARE * float(patch.std())
    substance = _window_std(hp, patch.shape)[: score.shape[0], : score.shape[1]]
    score = np.where(substance >= floor, score, -1.0).astype(np.float32)

    least = MIN_NCC if own else _MIN_NCC_UNPROMPTED
    if score.max() < least:
        return None

    # Anchor on the best match; snapping absorbs the drift of a whole-pixel period.
    _min_v, _max_v, _min_l, max_loc = cv2.minMaxLoc(score)
    anchor_x, anchor_y = max_loc
    snap_y, snap_x = max(1, py // 4), max(1, px // 4)

    footprint_pct = (
        _FOOTPRINT_MAX_PCT
        - (_FOOTPRINT_MAX_PCT - _FOOTPRINT_MIN_PCT)
        * max(0, min(100, sensitivity))
        / 100
    )
    # Blur before thresholding (bare pixels pick specks), and threshold the WHOLE
    # tile: the mark's lettering runs outside the crop that correlation matched.
    energy = cv2.GaussianBlur(np.abs(template), (0, 0), sigmaX=2.0)

    def _stamp_at(percentile: float) -> np.ndarray:
        cut = np.percentile(energy, percentile)
        binary = (energy >= cut).astype(np.uint8) * 255
        return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    stamp = _stamp_at(footprint_pct)
    # The evidence gate uses this fixed footprint, so sensitivity cannot move the gate.
    gate_stamp = _stamp_at(_GATE_PCT)

    mask = np.zeros(gray.shape, np.uint8)
    sites: set[tuple[int, int]] = set()

    # Every place the mark confidently correlates, whatever the estimated pitch.
    neighbourhood = np.ones((max(3, snap_y), max(3, snap_x)), np.float32)
    peaks = (score >= _MIN_NCC_UNPROMPTED) & (score >= cv2.dilate(score, neighbourhood))
    for y, x in zip(*np.nonzero(peaks), strict=True):
        sites.add((int(y), int(x)))

    # Presence is judged on confident copies alone, before the lattice is walked.
    # A pooled mark was already proven across the batch, so it answers to MIN_RUN.
    least_sites = MIN_RUN if mark.pooled else MIN_INSTANCES
    if len(sites) < least_sites:
        return None

    # A folded mark was rectified onto its grid, so the frame's rows/columns are it.
    grid = mark.grid if mark.pooled else (py, px)

    # The matches must sit ON the lattice: scenery repeats strongly but not arranged.
    if grid is not None:
        rows = np.array([y for y, _x in sites], np.float64)
        columns = np.array([x for _y, x in sites], np.float64)
        off_y = np.abs(((rows - anchor_y) / grid[0] + 0.5) % 1.0 - 0.5)
        off_x = np.abs(((columns - anchor_x) / grid[1] + 0.5) % 1.0 - 0.5)
        on_node = (off_y <= _NODE_TOLERANCE) & (off_x <= _NODE_TOLERANCE)
        if float(np.mean(on_node)) < _MIN_ON_LATTICE:
            return None

    # Stamp every site of the grid: a faint or edge-clipped copy can never prove
    # itself. Sites snap to a local peak where one exists; _paint clips the edges.
    if grid is not None:
        step_y, step_x = grid
        rows = range(-(anchor_y // step_y) - 2, (hp.shape[0] - anchor_y) // step_y + 3)
        columns = range(
            -(anchor_x // step_x) - 2, (hp.shape[1] - anchor_x) // step_x + 3
        )
        reach_y, reach_x = max(1, snap_y // 2), max(1, snap_x // 2)
        for i in rows:
            for j in columns:
                site_y, site_x = anchor_y + i * step_y, anchor_x + j * step_x
                if site_y + patch.shape[0] <= 0 or site_x + patch.shape[1] <= 0:
                    continue
                if site_y >= hp.shape[0] or site_x >= hp.shape[1]:
                    continue
                top, left = max(0, site_y - reach_y), max(0, site_x - reach_x)
                bottom = min(score.shape[0], site_y + reach_y + 1)
                right = min(score.shape[1], site_x + reach_x + 1)
                if bottom > top and right > left:
                    window = score[top:bottom, left:right]
                    local = np.unravel_index(int(np.argmax(window)), window.shape)
                    if window[local] >= MIN_NCC:
                        site_y, site_x = top + int(local[0]), left + int(local[1])
                sites.add((site_y, site_x))

    def _paint(target: np.ndarray, shape: np.ndarray) -> None:
        """Stamp ``shape`` at every site, offset back by where the crop began."""
        for sy, sx in sites:
            top, left = sy - crop_top, sx - crop_left
            src_y, src_x = max(0, -top), max(0, -left)
            dst_y, dst_x = max(0, top), max(0, left)
            high = min(shape.shape[0] - src_y, target.shape[0] - dst_y)
            wide = min(shape.shape[1] - src_x, target.shape[1] - dst_x)
            if high <= 0 or wide <= 0:
                continue
            region = target[dst_y : dst_y + high, dst_x : dst_x + wide]
            region[:] = np.maximum(
                region, shape[src_y : src_y + high, src_x : src_x + wide]
            )

    _paint(mask, stamp)

    # Keep a stamped pixel only where the photo deviates from its own neighbourhood;
    # a global cut would delete exactly the faint marks on smooth sky.
    deviation = np.abs(hp)
    baseline = cv2.blur(deviation, (_EVIDENCE_WINDOW, _EVIDENCE_WINDOW))
    supported = deviation >= _EVIDENCE_RATIO * (baseline + _EVIDENCE_FLOOR)

    gate = np.zeros(gray.shape, np.uint8)
    _paint(gate, gate_stamp)
    reference = gate > 0
    gate_area = int(np.count_nonzero(reference))
    if gate_area == 0:
        return None
    if int(np.count_nonzero(reference & supported)) < _MIN_EVIDENCE_SHARE * gate_area:
        return None

    # The fill is unconditional per site (per-site evidence lost the faintest copies)
    # and cut tighter than the stamp, so the sensitivity halo is not filled whole.
    fill = _stamp_at(max(footprint_pct, _FILL_PCT))
    pieces, labels, stats, _mids = cv2.connectedComponentsWithStats(fill, 8)
    for label in range(1, pieces):
        if stats[label][cv2.CC_STAT_AREA] < _FILL_MIN_PIECE:
            fill[labels == label] = 0
    # Erode the halo ring off, but add back the thin lettering the erosion destroys.
    kernel = np.ones((_FILL_ERODE_PX,) * 2, np.uint8)
    eroded = cv2.erode(fill, kernel)
    thin = cv2.subtract(fill, cv2.dilate(eroded, kernel))
    fill = cv2.max(eroded, thin)

    # Not clipped to the confident matches' span; busy ground is where they are absent.
    trimmed = mask.copy()
    trimmed[~supported] = 0
    for site_y, site_x in sites:
        top, left = site_y - crop_top, site_x - crop_left
        src_y, src_x = max(0, -top), max(0, -left)
        dst_y, dst_x = max(0, top), max(0, left)
        span_y = min(fill.shape[0] - src_y, mask.shape[0] - dst_y)
        span_x = min(fill.shape[1] - src_x, mask.shape[1] - dst_x)
        if span_y <= 0 or span_x <= 0:
            continue
        body = fill[src_y : src_y + span_y, src_x : src_x + span_x] > 0
        if not body.any():
            continue
        region = trimmed[dst_y : dst_y + span_y, dst_x : dst_x + span_x]
        region[:] = np.maximum(region, body.astype(np.uint8) * 255)
    mask = trimmed
    if not mask.any():
        return None

    if rectify is not None:
        # Back out of the rectified frame; nearest-neighbour, it is a binary mask.
        forward = rectify[0]
        mask = cv2.warpAffine(
            mask,
            forward,
            (work.shape[1], work.shape[0]),
            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
        )
    if mask.shape != (height, width):
        mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    return mask


def _propose_own_folded(rgb: np.ndarray, sensitivity: int) -> np.ndarray | None:
    """This image's mask from a mark it recovered by folding its own tiles."""
    mark = recover_mark(rgb)
    if mark is None:
        return None
    return apply_mark(rgb, mark, sensitivity)


def propose_pattern_mask(rgb: np.ndarray, sensitivity: int) -> np.ndarray | None:
    """Mask the instances of a repeating watermark, or None."""
    own = _propose_own_folded(rgb, sensitivity)
    if own is not None:
        return own
    # Last resort: a cell too big to fold. Imported at call time because tiled.py
    # imports this module.
    from .tiled import propose_tiled_mask

    return propose_tiled_mask(rgb, sensitivity)


def shareable_marks(
    load: Callable[[], Iterable[np.ndarray]], sensitivity: int = 50
) -> list[Mark]:
    """Marks this batch can reuse across itself; ``load`` is called once per pass."""
    marks: list[Mark] = []
    unresolved = False
    for rgb in load():
        mark = recover_mark(rgb)
        # A mark must carry a basis and have masked its own image to be offered.
        if mark is not None and mark.basis is not None:
            if apply_mark(rgb, mark, sensitivity) is not None:
                marks.append(mark)
                continue
        unresolved = True
    if unresolved:
        marks.extend(pooled_marks(load()))
    return marks


def _anchor_candidates(gray: np.ndarray) -> list[tuple[int, int]]:
    """Where an instance plausibly sits: strong local response, quiet surroundings."""
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_ANCHOR_KERNEL,) * 2)
    response = cv2.max(
        cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel),
        cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel),
    ).astype(np.float32)
    normalised = response / (cv2.blur(response, (_NORM_WIN,) * 2) + _NORM_FLOOR)
    texture = _local_texture(gray)
    quiet = texture < np.percentile(texture, _QUIET_PCT)
    field = np.where(quiet, cv2.GaussianBlur(normalised, (0, 0), sigmaX=9), 0)

    found: list[tuple[int, int]] = []
    work = field.copy()
    for _ in range(_ANCHOR_COUNT):
        cy, cx = np.unravel_index(int(np.argmax(work)), work.shape)
        if work[cy, cx] <= 0:
            break
        found.append((int(cy), int(cx)))
        work[
            max(0, cy - 2 * _ANCHOR_HALF_H) : cy + 2 * _ANCHOR_HALF_H,
            max(0, cx - 2 * _ANCHOR_HALF_W) : cx + 2 * _ANCHOR_HALF_W,
        ] = 0
    return found


def _matched_sites(hp: np.ndarray, template: np.ndarray, least: float) -> np.ndarray:
    """Every well-separated place ``template`` correlates at ``least``, best first."""
    if template.std() < 1e-3:
        return np.empty((0, 2), int)
    score = cv2.matchTemplate(hp, template, cv2.TM_CCOEFF_NORMED)
    floor = _MIN_WINDOW_STD_SHARE * float(template.std())
    substance = _window_std(hp, template.shape)[: score.shape[0], : score.shape[1]]
    guarded = np.where(substance >= floor, score, -1.0).astype(np.float32)
    spread = np.ones((template.shape[0] + 1, template.shape[1] + 1), np.float32)
    ys, xs = np.nonzero((guarded >= least) & (guarded >= cv2.dilate(guarded, spread)))
    if len(ys) == 0:
        return np.empty((0, 2), int)
    order = np.argsort(-guarded[ys, xs])[:_MAX_SITES]
    return np.column_stack([ys[order], xs[order]])


def _best_run(points: np.ndarray) -> tuple[list[tuple[int, int]], float, np.ndarray]:
    """Largest evenly spaced collinear run in ``points``, plus its pitch and step."""
    best: list[int] = []
    pitch = 0.0
    stride = np.zeros(2, np.float64)
    for i in range(len(points)):
        for j in range(len(points)):
            if i == j:
                continue
            step = points[j] - points[i]
            length = float(np.hypot(*step))
            if length < MIN_PERIOD:
                continue
            tolerance = max(6.0, _RUN_TOL * length)
            run, k = [i], 1
            while True:
                target = points[i] + k * step
                distance = np.abs(points - target).max(axis=1)
                nearest = int(np.argmin(distance))
                if distance[nearest] > tolerance:
                    break
                run.append(nearest)
                k += 1
            if len(run) > len(best):
                best, pitch, stride = run, length, step
    return (
        [(int(points[i][0]), int(points[i][1])) for i in best],
        pitch,
        np.asarray(stride, np.float64),
    )


def _anchored_run(rgb: np.ndarray) -> dict | None:
    """Instances of a sparse mark in one image, found without folding."""
    work = _work_size(rgb)
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    hp = _highpass(gray)
    best: dict | None = None
    for cy, cx in _anchor_candidates(gray):
        top = int(np.clip(cy - _ANCHOR_HALF_H, 0, hp.shape[0] - 2 * _ANCHOR_HALF_H))
        left = int(np.clip(cx - _ANCHOR_HALF_W, 0, hp.shape[1] - 2 * _ANCHOR_HALF_W))
        patch = hp[top : top + 2 * _ANCHOR_HALF_H, left : left + 2 * _ANCHOR_HALF_W]
        points = _matched_sites(hp, patch, _RUN_NCC)
        if len(points) < MIN_RUN:
            continue
        run, pitch, step = _best_run(points)
        if len(run) >= MIN_RUN and (best is None or len(run) > len(best["sites"])):
            best = {"hp": hp, "sites": run, "pitch": pitch, "step": step}
    return best


def pooled_marks(images: Iterable[np.ndarray]) -> list[Mark]:
    """One mark pooled from sparse instances across the whole batch, or none."""
    runs = [run for run in (_anchored_run(rgb) for rgb in images) if run is not None]
    if len(runs) < _MIN_POOL_IMAGES:
        return []
    # Grouped by pitch: a batch can carry more than one overlay.
    # Known risk: scenery that recurs across the batch at one pitch would pass here.
    return [
        mark
        for group in _pitch_groups(runs)
        if (mark := _pool_group(group)) is not None
    ]


def _pitch_groups(runs: list[dict]) -> list[list[dict]]:
    """Runs split into groups that agree on their pitch."""
    groups: list[list[dict]] = []
    for run in sorted(runs, key=lambda r: r["pitch"]):
        if groups:
            current = groups[-1]
            pitches = [member["pitch"] for member in current] + [run["pitch"]]
            spread = (max(pitches) - min(pitches)) / max(float(np.mean(pitches)), 1e-6)
            if spread <= _PITCH_TOL:
                current.append(run)
                continue
        groups.append([run])
    return [group for group in groups if len(group) >= _MIN_POOL_IMAGES]


def _cross_pitch(runs: list[dict], template: np.ndarray) -> tuple[int, int] | None:
    """The lattice step ACROSS the runs, voted on by the whole group, or None."""
    steps = np.array([run["step"] for run in runs], np.float64)
    horizontal = bool(np.all(np.abs(steps[:, 0]) <= AXIS_TOLERANCE))
    vertical = bool(np.all(np.abs(steps[:, 1]) <= AXIS_TOLERANCE))
    if horizontal == vertical:  # neither axis, or a degenerate zero step
        return None

    candidates = np.arange(MIN_PERIOD, _CROSS_MAX)
    curves = []
    surfaces = []
    for run in runs:
        # matchTemplate raises on a frame smaller than the template; skip such runs.
        if (
            template.shape[0] > run["hp"].shape[0]
            or template.shape[1] > run["hp"].shape[1]
        ):
            continue
        score = cv2.matchTemplate(run["hp"], template, cv2.TM_CCOEFF_NORMED)
        # Transposed so the run lies along the rows either way.
        surface = score if horizontal else score.T
        sites = [(y, x) if horizontal else (x, y) for y, x in run["sites"]]
        along = int(round(run["pitch"]))
        if along < MIN_PERIOD:
            return None
        start = min(x for _y, x in sites) % along
        columns = np.arange(start, surface.shape[1], along)
        anchor = min(y for y, _x in sites)
        if len(columns) == 0:
            return None
        curves.append(_cross_votes(surface, candidates, anchor, columns))
        surfaces.append((surface, anchor, columns))

    # The vote needs as many images as the pitch agreement did.
    if len(curves) < _MIN_POOL_IMAGES:
        return None

    vote = np.mean(curves, axis=0)
    # Untested candidates are NaN and stay out of the spread, or it collapses to 0.
    real = np.isfinite(vote)
    if int(np.count_nonzero(real)) < _CROSS_MIN_CANDIDATES:
        return None
    middle = float(np.median(vote[real]))
    spread = float(np.median(np.abs(vote[real] - middle))) * 1.4826
    if spread <= 1e-9:
        return None
    winner = int(np.nanargmax(np.where(real, vote, -np.inf)))
    if (vote[winner] - middle) < _CROSS_PROMINENCE * spread:
        return None

    cross = _true_pitch(surfaces, int(candidates[winner]))
    along = int(round(float(np.mean([run["pitch"] for run in runs]))))
    return (cross, along) if horizontal else (along, cross)


def _true_pitch(surfaces: list, cross: int) -> int:
    """``cross``, or the multiple of it the copies really sit on."""

    def scored(step: int) -> float:
        one = np.array([step])
        return float(np.mean([_cross_votes(s, one, a, c)[0] for s, a, c in surfaces]))

    best = cross
    for multiple in (2, 3):
        step = cross * multiple
        if all(step >= surface.shape[0] for surface, _a, _c in surfaces):
            break
        here, there = scored(best), scored(step)
        if not (np.isfinite(here) and np.isfinite(there)):
            break
        if there > here * _SUBHARMONIC_EDGE:
            best = step
    return best


def _cross_votes(
    surface: np.ndarray, candidates: np.ndarray, anchor: int, columns: np.ndarray
) -> np.ndarray:
    """For each candidate step, how well the rows it ADDS to ``anchor`` correlate."""
    votes = np.empty(len(candidates), np.float64)
    for index, step in enumerate(candidates):
        offsets = np.concatenate(
            [
                np.arange(anchor + step, surface.shape[0], step),
                np.arange(anchor - step, -1, -step),
            ]
        )
        if len(offsets) == 0:
            votes[index] = np.nan
            continue
        reads = []
        for row in offsets:
            top = max(0, row - _CROSS_SNAP)
            bottom = min(surface.shape[0], row + _CROSS_SNAP + 1)
            for column in columns:
                left = max(0, column - _CROSS_SNAP)
                right = min(surface.shape[1], column + _CROSS_SNAP + 1)
                if bottom > top and right > left:
                    reads.append(surface[top:bottom, left:right].max())
        # NaN, not a bad score: an untested step stays out of the statistic.
        votes[index] = float(np.mean(reads)) if reads else np.nan
    return votes


def _fold_on_grid(
    runs: list[dict], template: np.ndarray, grid: tuple[int, int]
) -> tuple[np.ndarray, int, int] | None:
    """The mark's whole cell, median-folded over every copy in the batch."""
    cell_y, cell_x = grid
    high, wide = template.shape
    offset_y, offset_x = cell_y // 2 - high // 2, cell_x // 2 - wide // 2

    # Bounded cells per run, keeping memory flat and cross-image variety in the median.
    share = max(2, _FOLD_MAX_TILES // max(1, len(runs)))
    tiles: list[np.ndarray] = []
    for run in runs:
        mine: list[np.ndarray] = []
        hp = run["hp"]
        # Anchor on the pooled template's best match; runs' own sites differ in offset.
        if template.shape[0] > hp.shape[0] or template.shape[1] > hp.shape[1]:
            continue
        agreement = cv2.matchTemplate(hp, template, cv2.TM_CCOEFF_NORMED)
        _lo, _hi, _at, best = cv2.minMaxLoc(agreement)
        base_y, base_x = best[1] % cell_y, best[0] % cell_x
        for site_y in range(base_y, hp.shape[0], cell_y):
            for site_x in range(base_x, hp.shape[1], cell_x):
                top, left = site_y - offset_y, site_x - offset_x
                read_top, read_left = max(0, top), max(0, left)
                read_bottom = min(hp.shape[0], top + cell_y)
                read_right = min(hp.shape[1], left + cell_x)
                if read_bottom <= read_top or read_right <= read_left:
                    continue
                tile = np.full((cell_y, cell_x), np.nan, np.float32)
                tile[
                    read_top - top : read_bottom - top,
                    read_left - left : read_right - left,
                ] = hp[read_top:read_bottom, read_left:read_right]
                mine.append(tile)
                if len(mine) >= share:
                    break
            if len(mine) >= share:
                break
        tiles.extend(mine)

    if len(tiles) < MIN_TILES:
        return None
    stack = np.stack(tiles)
    covered = np.count_nonzero(~np.isnan(stack), axis=0)
    if not (covered >= MIN_TILES).any():
        return None
    # nanmedian warns on an all-NaN column, and warnings are errors here.
    stack[0][covered == 0] = 0.0
    folded = np.nanmedian(stack, axis=0).astype(np.float32)
    # Where too few copies overlapped, the median is noise rather than a mark.
    folded[covered < MIN_TILES] = 0.0
    if folded.std() <= 1e-3:
        return None

    # Trim the cell to the ink touching the matched window. Ink is a share of the
    # mark's own peak: a floor-relative cut bridges faint streaks to the cell edge.
    energy = cv2.GaussianBlur(np.abs(folded), (0, 0), sigmaX=3)
    ink = (energy >= _INK_SHARE * float(energy.max())).astype(np.uint8)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, 8)
    # Clipped, not indexed raw: a negative offset would read from the far edge.
    window = labels[
        max(0, offset_y) : max(0, offset_y + high),
        max(0, offset_x) : max(0, offset_x + wide),
    ]
    touching = {int(v) for v in np.unique(window) if v > 0}
    if not touching:
        return None
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
    if bottom - top < 12 or right - left < 12:
        return None

    # The mark must be smaller than its cell; on a false grid the ink fills the tile.
    if (bottom - top) > _INK_CELL_SHARE * cell_y:
        return None
    if (right - left) > _INK_CELL_SHARE * cell_x:
        return None

    folded = folded[top:bottom, left:right]
    offset_y, offset_x = offset_y - top, offset_x - left

    # The coarse patch stays the match subject; a crop of the fold matches far worse.
    return folded, offset_y, offset_x


def _pool_group(runs: list[dict]) -> Mark | None:
    """One mark from a group of runs that agree on their pitch, or None."""
    patches = []
    for run in runs:
        hp = run["hp"]
        for y, x in run["sites"]:
            if (
                y + 2 * _ANCHOR_HALF_H <= hp.shape[0]
                and x + 2 * _ANCHOR_HALF_W <= hp.shape[1]
            ):
                patches.append(
                    hp[y : y + 2 * _ANCHOR_HALF_H, x : x + 2 * _ANCHOR_HALF_W]
                )
    if len(patches) < MIN_TILES:
        return None

    template = np.stack(patches).mean(axis=0).astype(np.float32)
    if template.std() <= 1e-3:
        return None
    # No significance test: rolling a patch moves the mark rather than removing it.
    # A grid is kept only if folding on it produces a mark; otherwise it is dropped.
    grid = _cross_pitch(runs, template)
    folded = None if grid is None else _fold_on_grid(runs, template, grid)
    if folded is None:
        return Mark(None, template, template, 0, 0, template.shape, pooled=True)
    whole, crop_top, crop_left = folded
    return Mark(
        None,
        whole,
        template,
        crop_top,
        crop_left,
        template.shape,
        pooled=True,
        grid=grid,
    )


def propose_pattern_mask_shared(
    rgb: np.ndarray, sensitivity: int, marks: Sequence[Mark]
) -> np.ndarray | None:
    """This image's own mask, or failing that one borrowed from ``marks``."""
    # Own fold, borrowed marks, then tiled: a batch-proven mark outranks one image's.
    own = _propose_own_folded(rgb, sensitivity)
    if own is not None:
        return own
    for mark in marks:
        borrowed = apply_mark(rgb, mark, sensitivity, own=False)
        if borrowed is not None:
            return borrowed
    from .tiled import propose_tiled_mask  # deferred: tiled.py imports this module

    return propose_tiled_mask(rgb, sensitivity)
