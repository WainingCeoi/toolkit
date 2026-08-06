"""Find a repeating watermark by recovering the pattern itself, then matching it.

The texture detector in detect.py asks "does this pixel stand out from its
surroundings", which is all you can ask of a single pixel — and it is why thin
image detail (tent seams, wires, railings) reads as watermark. On the images
this tool is actually for, the watermark has a much stronger property: it
repeats, identically, on a grid. Nothing in the photograph does.

So instead of judging pixels, recover the mark:

1. High-pass the image, leaving stroke-scale structure only.
2. Find the period it repeats on, from the autocorrelation.
3. Median-fold every smooth tile onto one. The mark sits at the same phase in
   each tile so it survives; the photo differs in each tile so it cancels.
   This is the "raise the contrast" step done statistically -- SNR grows with
   the number of tiles, which is how a mark far too faint to see in any single
   place becomes legible.
4. Cross-correlate that recovered template back over the whole image, snapping
   to each grid site, and mask only where it genuinely matches.

The mask is therefore the union of pattern instances and nothing else, so a
tent seam is never marked no matter how sharp it is.

Returns None whenever the evidence is weak -- no period, too few tiles, a
template no stronger than a deliberately misaligned fold of the same tiles,
too few confident matches -- and the caller falls back to the texture detector.

This runs only when the caller asks for it, because those gates cannot tell a
watermark from any other structure consistent across the frame: a clean test
photo passed them by locking onto its own sky gradient. See detect.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import cv2
import numpy as np

# Longest side the search runs at. The period has to stay comfortably larger
# than the high-pass scale to be findable, and cross-correlation over a full
# 36 MP frame is far slower than the answer is worth.
WORK_MAX = 2000

# Removes illumination and large-scale content, keeps watermark strokes.
HIGHPASS_SIGMA = 18

# Period search bounds, in working pixels.
MIN_PERIOD = 45
# How far off-axis a peak may sit and still count as the horizontal/vertical
# period; the grids in practice are axis-aligned to within a few pixels.
AXIS_TOLERANCE = 8
# Harmonics summed when scoring a candidate pitch.
_HARMONICS = 4
# Autocorrelation peaks considered when fitting the lattice, how far off a whole
# number a peak's lattice coordinates may sit, and how many peaks a candidate
# basis must explain before it is believed.
_PEAK_COUNT = 60
_LATTICE_TOL = 0.18
_MIN_SUPPORT = 5

# --- PATCH: new constants -------------------------------------------------
_REFINE_SCHEDULE = ((2, 10), (3, 7), (6, 5), (8, 3))
_SCORE_ORDER = 3
# Largest lag, as a share of each dimension, where enough of the image still
# overlaps itself for the correlation to carry evidence.
_MAX_LAG_SHARE = 0.35
# The quietest share of the photo, which is where the overlay is measurable.
_QUIET_PCT = 45.0

# Evidence gates. Below any of these, the caller falls back.
#
# MIN_TILES is high because the fold is a median: too few tiles and the
# photograph does not cancel, so the "recovered mark" is just leftover scenery.
MIN_TILES = 9
MIN_NCC = 0.30
MIN_INSTANCES = 6
# The recovered mark must be this much stronger than a deliberately misaligned
# fold of the same tiles. Folding noise always produces *something*; the
# question is whether aligning on the estimated period produced more than
# aligning on nothing, and a clean photo answers no.
MIN_SIGNIFICANCE = 2.0

# Sensitivity maps onto how much of the recovered mark's footprint to take:
# the strongest strokes only, out to its faint lettering. Kept tight because
# every site is stamped, so a loose footprint multiplies across the whole grid.
_FOOTPRINT_MAX_PCT = 97.0
_FOOTPRINT_MIN_PCT = 72.0

# Share of the tile the located crop spans. Wide enough to carry the lettering
# beside the logo — the stamp comes from this crop, so anything outside it is
# never masked — but not the whole tile, which is mostly empty and would give
# cross-correlation far less to lock onto.
_CROP_FRACTION = 0.62

# A location with no prior reason to hold an instance must correlate better
# than one the grid predicts.
_MIN_NCC_UNPROMPTED = 0.45

# A correlation peak is only believed where the image itself has something under
# it, as a share of the template's own variation. Normalised correlation divides
# by the window's standard deviation, so over a FLAT window -- a blown-out sky,
# the white background of a product render -- it divides ~0 by ~0 and OpenCV
# hands back 1.0. Measured on a render sample: every one of its correlation
# peaks scored 1.000 over windows of standard deviation 0.0000, while genuine
# instances elsewhere sat at 2.4-3.9 against a template deviation of 2.07. Those
# perfect scores are arithmetic, not evidence, and without this they anchor the
# grid walk in empty sky.
_MIN_WINDOW_STD_SHARE = 0.25

# A stamped pixel is kept only where the image's response exceeds this multiple
# of its own neighbourhood's response. Deliberately near 1: the stamp already
# asserts the pixel is part of the mark's shape, so this only has to reject
# stamps that landed somewhere genuinely featureless.
_EVIDENCE_RATIO = 1.0
_EVIDENCE_WINDOW = 81
_EVIDENCE_FLOOR = 1.0
# If this little of the stamped area survives that check, the repeat was an
# artefact of the period estimate rather than ink on the photo.
_MIN_EVIDENCE_SHARE = 0.2
# Footprint the evidence share is measured over — the mark's strong core, fixed
# so the gate does not move when sensitivity widens the footprint being masked.
_GATE_PCT = 90.0


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
    """Autocorrelation of ``hp`` measured only where ``weight`` is set.

    Restricting it to the quiet parts of the photo is what makes the period
    findable: over a whole frame, the ground's texture swamps the overlay and
    the estimate comes back wrong. Multiplying by the weight would bias the
    result by the weight's own shape, so it is divided out again — the standard
    masked-correlation normalisation.
    """
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
    """The repeat pitch in a 1-D autocorrelation profile, or None.

    Scored by the mean height at the candidate's own harmonics (d, 2d, 3d…).
    A real pitch peaks at all of them; the steep shoulder near zero lag, which
    otherwise wins on raw height alone, does not. The profile is detrended
    first so that shoulder is not competing in the first place.
    """
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


def _rect_period(hp: np.ndarray, texture: np.ndarray) -> tuple[int, int] | None:
    """The (vertical, horizontal) period the overlay repeats on, or None.

    A rectangular period is enough even for the common half-offset brick
    layout: that grid simply repeats on twice the vertical pitch, and folding
    on the doubled period just puts two instances in the tile.
    """
    quiet = (texture < np.percentile(texture, _QUIET_PCT)).astype(np.float32)
    if quiet.mean() < 0.05:
        return None
    ac = _masked_autocorrelation(hp, quiet)
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
    work = prominence.copy()
    yy, xx = np.mgrid[0:height, 0:width]
    work[(yy - cy) ** 2 + (xx - cx) ** 2 < MIN_PERIOD**2] = -np.inf
    work[:cy, :] = -np.inf  # autocorrelation is symmetric; one half is enough
    # Far lags have too little of the image overlapping itself to mean
    # anything, and the masked estimate is forced to zero out there. Left in,
    # the step down to that zero reads as an enormous ridge once the smooth
    # trend is subtracted, and every "peak" lands on the cliff instead of on
    # the overlay.
    reach_y, reach_x = int(height * _MAX_LAG_SHARE), int(width * _MAX_LAG_SHARE)
    work[cy + reach_y :, :] = -np.inf
    work[:, : cx - reach_x] = -np.inf
    work[:, cx + reach_x :] = -np.inf
    found = []
    for _ in range(count):
        idx = np.unravel_index(int(np.argmax(work)), work.shape)
        if not np.isfinite(work[idx]) or work[idx] <= 0:
            break
        found.append((float(idx[0] - cy), float(idx[1] - cx)))
        y0, x0 = idx
        radius = max(8, MIN_PERIOD // 2)
        work[
            max(0, y0 - radius) : y0 + radius + 1, max(0, x0 - radius) : x0 + radius + 1
        ] = -np.inf
    return found


def _fit_lattice(peaks: list[tuple[float, float]]) -> np.ndarray | None:
    """A 2x2 basis whose integer combinations explain the peaks, or None.

    The overlay's grid is frequently NOT axis-aligned — measured on sample
    photos, the strongest peak sat at 16 degrees in one and 76 in another — so
    the repeat cannot be described by a row pitch and a column pitch. Two
    arbitrary vectors can describe any of them.

    Candidate pairs are scored by how many of the other peaks they explain as
    near-integer combinations, then by being short. Shortness matters as much
    as support: a doubled or tripled vector explains the peaks just as well,
    but folding on it puts several instances in one tile, and the crop taken
    from that tile then straddles them and matches nothing cleanly.
    """
    best_basis, best_key = None, None
    for a_index, first in enumerate(peaks):
        for second in peaks[a_index + 1 :]:
            basis = np.array(
                [[first[1], second[1]], [first[0], second[0]]], np.float64
            )  # columns are the vectors, rows are (x, y)
            area = abs(np.linalg.det(basis))
            if area < MIN_PERIOD**2 * 0.25:  # near-collinear or far too small
                continue
            inverse = np.linalg.inv(basis)
            supported = []
            for peak in peaks:
                coords = inverse @ np.array([peak[1], peak[0]], np.float64)
                if np.all(np.abs(coords - np.round(coords)) <= _LATTICE_TOL):
                    supported.append((np.round(coords), peak))
            length = np.hypot(*first) + np.hypot(*second)
            key = (len(supported), -length)
            if len(supported) >= _MIN_SUPPORT and (best_key is None or key > best_key):
                best_basis, best_key = (basis, supported), key

    if best_basis is None:
        return None
    basis, supported = best_basis
    # Refit from every supported peak at once, which puts the vectors on a
    # sub-pixel footing. Rounding a pitch to whole pixels drifts a little in
    # each tile, and over a dozen tiles that smears the fold.
    integer_coords = np.array([c for c, _p in supported]).T  # 2 x N
    observed = np.array([[p[1], p[0]] for _c, p in supported]).T  # 2 x N
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
    """An affine that makes the lattice axis-aligned, plus the resulting cell.

    Once warped, the overlay repeats on whole rows and columns, so the folding,
    matching and stamping code needs to know nothing about oblique grids — it
    all happens in this rectified space and the mask is warped back at the end.
    """
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
    # A pathological shear can blow the rectified frame up; refuse rather than
    # allocate hundreds of megabytes for a guess.
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
    # Prefer quiet tiles; a photo with no quiet region still gets a chance,
    # since the median over many busy tiles cancels content too, just slower.
    if len(tiles) < MIN_TILES:
        tiles = tiles + busy_tiles
    if len(tiles) < MIN_TILES:
        return None
    stack = np.stack(tiles)
    template = np.median(stack, axis=0).astype(np.float32)

    # Significance: fold the same tiles again, each rolled by a different
    # arbitrary offset, so no real pattern can survive. Whatever strength that
    # leaves is what this many tiles of this photo produce by chance.
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
    """The busiest part of the tile, plus where it sits in the tile.

    Matching wants a distinctive subject, so it gets the crop around the mark's
    strongest feature — a logo, usually. Masking wants the mark's whole extent,
    including the fainter lettering beside that logo, so the caller stamps the
    full tile positioned by this offset. Stamping the crop instead left the
    text behind.
    """
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
    """A recovered watermark, reusable on other images of the same batch.

    Everything needed to mask an instance without recovering it again: the
    lattice it repeats on, the tile-sized template folded out of it, the crop
    that correlation locks onto, and where that crop sits in the tile.

    This exists because recovering a mark and applying one have completely
    different requirements. Recovery needs a correct primitive lattice, at least
    MIN_TILES tiles of the frame, and a quiet enough photograph for a median
    over those tiles to cancel the scenery. Applying one needs none of that --
    only that the mark be present. So an image whose own recovery is refused can
    still be masked precisely from a sibling's mark, which is the common case in
    a batch: one watermarking tool ran over all of them.
    """

    __slots__ = ("basis", "template", "patch", "crop_top", "crop_left", "cell")

    def __init__(self, basis, template, patch, crop_top, crop_left, cell):
        self.basis = basis
        self.template = template
        self.patch = patch
        self.crop_top = crop_top
        self.crop_left = crop_left
        self.cell = cell


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
    """Recover the repeating mark in this image, or None if it cannot be.

    None here does NOT mean "no watermark" -- see Mark. It means this image
    cannot produce a template, most often because the photograph is busy
    everywhere (the fold's median never cancels it) or because too few tiles of
    the lattice fit in the frame.
    """
    work = _work_size(rgb)
    gray = cv2.cvtColor(work, cv2.COLOR_RGB2GRAY)
    hp = _highpass(gray)
    texture = _local_texture(gray)

    # Rectify onto the overlay's own grid first. Everything downstream then
    # works in rows and columns, whatever angle the real lattice sits at.
    basis = None
    quiet = (texture < np.percentile(texture, _QUIET_PCT)).astype(np.float32)
    if quiet.mean() >= 0.05:
        ac = _masked_autocorrelation(hp, quiet).astype(np.float32)
        basis = _fit_rectifying_lattice(ac)
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
        # No usable lattice — either none was fitted, or the one fitted could not
        # be warped onto. Fall back to a plain row/column pitch, which still
        # serves the axis-aligned case. The basis is dropped rather than carried:
        # nothing downstream rectified with it, so keeping it would describe a
        # frame this mark was never measured in.
        basis = None
        period = _rect_period(hp, texture)
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
    """Standard deviation of ``hp`` under every placement of a shape-sized window.

    Laid out to index like a matchTemplate score map, so a peak can be checked
    against the variation actually present beneath it (see _MIN_WINDOW_STD_SHARE).
    """
    high, wide = shape
    mean = cv2.blur(hp, (wide, high))
    mean_square = cv2.blur(hp * hp, (wide, high))
    deviation = np.sqrt(np.maximum(mean_square - mean * mean, 0))
    top, left = high // 2, wide // 2
    return deviation[top : top + hp.shape[0], left : left + hp.shape[1]]


def apply_mark(
    rgb: np.ndarray, mark: Mark, sensitivity: int, own: bool = True
) -> np.ndarray | None:
    """Mask every instance of ``mark`` in this image, or None if there are none.

    ``own`` False means the mark came from a different image, which raises the
    bar: it must correlate confidently in at least MIN_INSTANCES places before
    the grid walk is allowed to extend it anywhere.
    """
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
    # Discard peaks with nothing beneath them before anything reads this map,
    # the anchor included.
    floor = _MIN_WINDOW_STD_SHARE * float(patch.std())
    substance = _window_std(hp, patch.shape)[: score.shape[0], : score.shape[1]]
    score = np.where(substance >= floor, score, -1.0).astype(np.float32)

    least = MIN_NCC if own else _MIN_NCC_UNPROMPTED
    if score.max() < least:
        return None

    # Anchor on the best match, then visit every grid site from there. Sites
    # are snapped to their local correlation peak, which absorbs the drift
    # left by rounding the period to whole pixels.
    _min_v, _max_v, _min_l, max_loc = cv2.minMaxLoc(score)
    anchor_x, anchor_y = max_loc
    snap_y, snap_x = max(1, py // 4), max(1, px // 4)

    footprint_pct = (
        _FOOTPRINT_MAX_PCT
        - (_FOOTPRINT_MAX_PCT - _FOOTPRINT_MIN_PCT)
        * max(0, min(100, sensitivity))
        / 100
    )
    # Blur before thresholding: on a noisy template, judging bare pixels picks
    # specks out of the noise instead of the mark's body, and a speckled stamp
    # inpaints as a rash rather than a removed logo.
    #
    # Thresholded over the WHOLE TILE, not over the crop. The crop exists to
    # give cross-correlation a distinctive subject; it spans _CROP_FRACTION of
    # the tile in each axis, so barely a third of the tile's area, and the mark
    # does not fit inside it — measured on the samples, the lettering beside the
    # logo runs straight out of the crop, so no threshold could ever stamp it
    # and it survived removal in full. Masking wants the mark's whole extent, so
    # the stamp is the whole tile, offset back to where the crop began.
    energy = cv2.GaussianBlur(np.abs(template), (0, 0), sigmaX=2.0)

    def _stamp_at(percentile: float) -> np.ndarray:
        cut = np.percentile(energy, percentile)
        binary = (energy >= cut).astype(np.uint8) * 255
        return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    stamp = _stamp_at(footprint_pct)
    # The evidence-share gate below is judged on this FIXED reference footprint
    # rather than on the stamp actually being returned. The gate asks "is the
    # recovered repeat really ink on the photo"; measured on the stamp itself it
    # instead re-measures the caller's own footprint choice, because a looser
    # footprint necessarily reaches further into the tile's empty background and
    # drives the survival share down. That coupling made a *higher* sensitivity
    # return None — the slider at 100 reported "no watermark" on an image the
    # same code found the mark in at 50, and the pipeline skips a None.
    gate_stamp = _stamp_at(_GATE_PCT)

    mask = np.zeros(gray.shape, np.uint8)
    sites: set[tuple[int, int]] = set()

    # Every place the recovered mark genuinely correlates. This catches the
    # instances directly, and does not care whether the estimated pitch is the
    # true one or a multiple of it.
    neighbourhood = np.ones((max(3, snap_y), max(3, snap_x)), np.float32)
    peaks = (score >= _MIN_NCC_UNPROMPTED) & (score >= cv2.dilate(score, neighbourhood))
    for y, x in zip(*np.nonzero(peaks), strict=True):
        sites.add((int(y), int(x)))

    # A borrowed mark has to prove it belongs here BEFORE the grid walk below
    # extends it, because the walk admits sites on the weaker MIN_NCC bar and so
    # would spread a foreign mark across a frame from one lucky anchor. Measured:
    # walking ungated put a mask on three images carrying a completely different
    # overlay, and on a clean control frame. Counted on confident matches alone
    # the two separate cleanly — 7 and 19 sites where the mark really is present,
    # against 0 to 5 for the foreign overlay and 0 for every clean control — so
    # the existing instance bar is all this needs.
    if not own and len(sites) < MIN_INSTANCES:
        return None

    # Then walk the grid from the strongest match and snap each site to its own
    # local peak, which picks up instances too faint to win a maximum of their
    # own — over a bright tent roof, say — and absorbs the drift left by
    # rounding the pitch to whole pixels.
    for i in range(-(anchor_y // py) - 1, (score.shape[0] - anchor_y) // py + 2):
        for j in range(-(anchor_x // px) - 1, (score.shape[1] - anchor_x) // px + 2):
            top = max(0, anchor_y + i * py - snap_y)
            left = max(0, anchor_x + j * px - snap_x)
            bottom = min(score.shape[0], anchor_y + i * py + snap_y + 1)
            right = min(score.shape[1], anchor_x + j * px + snap_x + 1)
            if top >= bottom or left >= right:
                continue
            window = score[top:bottom, left:right]
            local = np.unravel_index(int(np.argmax(window)), window.shape)
            if window[local] < MIN_NCC:
                continue
            sites.add((top + int(local[0]), left + int(local[1])))

    if len(sites) < MIN_INSTANCES:
        return None

    def _paint(target: np.ndarray, shape: np.ndarray) -> None:
        """Stamp ``shape`` at every site. A site is where the CROP matched, and
        the crop began (crop_top, crop_left) into the tile, so the tile-sized
        stamp starts that far back — which can be off the top or left edge."""
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

    # Confirm each stamped pixel against the image itself. The grid says where
    # instances *should* be; this keeps only the pixels where the photo really
    # does deviate from its surroundings, so a stamp landing on clean sky
    # contributes nothing. It tightens true instances and erases phantom ones.
    # Judged LOCALLY, against the response typical of each pixel's own
    # surroundings. A global cut would repeat the very mistake the texture
    # detector had to fix: the median response over a photo with any texture in
    # it sits far above a faint overlay on smooth sky, so a global threshold
    # deletes exactly the marks this mode exists to find.
    deviation = np.abs(hp)
    baseline = cv2.blur(deviation, (_EVIDENCE_WINDOW, _EVIDENCE_WINDOW))
    supported = deviation >= _EVIDENCE_RATIO * (baseline + _EVIDENCE_FLOOR)

    # If almost nothing of the reference footprint survived, the "pattern" was an
    # artefact of the period estimate rather than something present in the photo.
    gate = np.zeros(gray.shape, np.uint8)
    _paint(gate, gate_stamp)
    reference = gate > 0
    gate_area = int(np.count_nonzero(reference))
    if gate_area == 0:
        return None
    if int(np.count_nonzero(reference & supported)) < _MIN_EVIDENCE_SHARE * gate_area:
        return None

    mask[~supported] = 0
    if not mask.any():
        return None

    if rectify is not None:
        # Back out of the rectified frame. Nearest-neighbour: this is a binary
        # mask, and it gets dilated before inpainting anyway.
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


def propose_pattern_mask(rgb: np.ndarray, sensitivity: int) -> np.ndarray | None:
    """Mask the instances of a repeating watermark, or None if there is no
    convincing repeating pattern to mask."""
    mark = recover_mark(rgb)
    if mark is None:
        return None
    return apply_mark(rgb, mark, sensitivity)


def shareable_marks(images: Iterable[np.ndarray], sensitivity: int = 50) -> list[Mark]:
    """The marks in ``images`` that are fit to be reused on their siblings.

    One watermarking tool usually ran over a whole batch, so an image whose own
    recovery is refused is very often carrying a mark that a sibling recovered
    perfectly well. Recovery is the fragile half — it needs a correct primitive
    lattice, MIN_TILES tiles in frame, and a photograph quiet enough for a median
    to cancel — while applying a mark needs only that the mark be there.

    A mark qualifies only if it masked its OWN image, which is what tells a real
    overlay from a lattice fitted to scenery: offering every recovered mark
    instead put a mask on a clean control frame. It must also carry a lattice
    basis, since one from the axis-aligned fallback has no frame to rectify a
    sibling into.

    Consumes ``images`` one at a time and keeps only the marks — a template and
    a 2x2 basis each — so a batch of 36 MP photos costs one of them in memory,
    not all of them.
    """
    marks: list[Mark] = []
    for rgb in images:
        mark = recover_mark(rgb)
        if mark is None or mark.basis is None:
            continue
        if apply_mark(rgb, mark, sensitivity) is not None:
            marks.append(mark)
    return marks


def propose_pattern_mask_shared(
    rgb: np.ndarray, sensitivity: int, marks: Sequence[Mark]
) -> np.ndarray | None:
    """This image's own mask, or failing that one borrowed from ``marks``.

    Borrowing is deliberately timid. A borrowed mark must clear the higher
    unprompted correlation bar, is trusted only where it actually correlates
    rather than walked across a grid, and still faces every existing gate
    including the per-pixel evidence check. Measured on a sample of eight: two of
    the five images that came back empty carry the same overlay as three that
    succeeded, and both are now masked from a sibling's mark — 4.6% and 4.4% of
    frame against 5.8% for the image the mark came from — while all three clean
    controls refuse every mark offered to them.
    """
    own = propose_pattern_mask(rgb, sensitivity)
    if own is not None:
        return own
    for mark in marks:
        borrowed = apply_mark(rgb, mark, sensitivity, own=False)
        if borrowed is not None:
            return borrowed
    return None
