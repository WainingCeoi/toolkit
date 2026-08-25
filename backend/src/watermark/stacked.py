"""Recover a once-per-image watermark by stacking the batch.

A supplier's watermark tool stamps the same mark at the same place on every
photo it processes. One such image is unremovable here — a mark that never
repeats gives the single-image routes nothing to hold: no lattice to fold
(pattern.py), no evenly spaced run to pool, and a texture filter cannot tell a
translucent stroke from a chair leg. But the BATCH knows what no single frame
does: the overlay is the one thing every frame agrees on. Stack the frames'
high-pass fields and score each pixel by how consistently the frames deviate
there — mean over spread, a t statistic — and the mark stands while the
scenes cancel.

Every constant below is measured, not styled (445 synthetic batches: 75
product-scene marked + realistic grainy re-fixtures + 286 clean/stress
negatives; the decision configuration detected 70/75 marked with 0/286 false
fires):

- The field is a SIGNED dual top-hat (top-hat minus black-hat, 21px ellipse)
  rather than a Gaussian high-pass. Gaussian sigma-18 was the difference
  between leaking and not: its smooth response let independent product shots
  agree by coincidence (1-4 of 25 clean batches came back marked), while the
  top-hat answers only for detail narrower than its kernel, where honest
  scenes disagree — 0 clean seed pixels in all 25, and the glyph body it
  keeps lifts median recall from 0.17-0.29 to ~0.70. Kernel 21 is the largest
  safe size: 27 and 31 raise recall further but start passing the registered
  edges of a same-product-different-colour batch.
- Proof is |t| >= SEED_T with three gates. The seed share must clear
  SHARE_FLOOR (0.02% of pixels — set by the worst realistic negative, a
  fixed-camera batch jittered 1px, at 0.0151%); it must exceed ROLLS=24
  out-of-registration re-stacks of the same frames by RATIO_BAR (share/null
  >= 5 — realistic clean batches reach 0.28% share but never ratio 1.9, so
  this is the gate that actually holds that line; 6 rolls was a lottery,
  single-roll nulls varied 400x within one batch); and the frames must not be
  near-duplicates (median pairwise correlation of the fields <= CORR_MAX —
  genuine marked batches measure at most 0.681 while the same scene re-shot
  measures 0.998-1.000, and for those "everything agrees" is the scene, not
  a mark).
- The mask grows the proven seeds through the looser |t| >= grow-bar field by
  connected component, so a glyph counts whole rather than only at its
  strongest pixels. A body over BODY_CAP of the frame is refused — nothing
  genuine measured over 5.91%, and what exceeds it is a flood.

The honest limits, also measured: a lone pair of frames proves nothing
(variance from two samples; never separable, MIN_STACK=3), three grainy
frames rarely enough (every realistic n=3 batch fell under RATIO_BAR), and a
batch of near-identical frames is refused rather than guessed at. More frames
only help.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import cv2
import numpy as np

# Longest working-frame side. The stack must share one frame; 700 keeps 20
# fields under ~30 MB while a 6px glyph stroke at 1600px is still ~3px here.
_WORK = 700

# Border band excluded from seeding: np.roll wraps content across it when the
# null is built, so evidence there cannot be told from its own null.
_MARGIN = 24

_SEED_T = 6.0
_ROLLS = 24
_SHARE_FLOOR = 0.0002  # of frame pixels
_RATIO_BAR = 5.0
_CORR_MAX = 0.9
_BODY_CAP = 0.08  # of frame pixels
MIN_STACK = 3

# Sensitivity 0..100 -> grow bar, geometric through the calibrated default:
# 4.0 at 0, 2.5 at 50 (the measured configuration), ~1.56 at 100. Lower bar
# marks a superset, so the slider stays monotone (see stamp_stacked for the
# one exception, the flood fallback).
_GROW_DEFAULT = 2.5
_GROW_MAX = 4.0

_SE = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))


@dataclass
class StackedMark:
    """The batch's proven overlay: the |t| field and the seeds inside it.

    The seeds are the proof and never move; the body is cut from ``t`` at
    stamp time, so the sensitivity slider re-cuts without re-reading the
    batch.
    """

    t: np.ndarray  # float32 (H, W) working frame, |t| per pixel
    seeds: np.ndarray  # bool (H, W), |t| >= _SEED_T inside the margin
    keep: np.ndarray  # bool (H, W), the margin mask the gates ran under


def _field(gray: np.ndarray) -> np.ndarray:
    """Signed dual top-hat: bright fine detail positive, dark negative."""
    top = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, _SE)
    black = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, _SE)
    return top - black


def _t_field(stack: np.ndarray) -> np.ndarray:
    """Per-pixel consistency of the frames' deviations: mean over spread.

    +0.5 in the denominator keeps a flat, noiseless region from dividing by
    ~0 and promoting nothing into proof.
    """
    n = len(stack)
    return stack.mean(axis=0) / (stack.std(axis=0, ddof=1) / np.sqrt(n) + 0.5)


def _work_shape(sizes: list[tuple[int, int]]) -> tuple[int, int]:
    height = min(h for h, _ in sizes)
    width = min(w for _, w in sizes)
    scale = _WORK / max(height, width)
    if scale < 1:
        return max(1, round(height * scale)), max(1, round(width * scale))
    return height, width


def _grow(seeds: np.ndarray, loose: np.ndarray) -> np.ndarray:
    """Every loose-field connected piece that contains a proven seed, whole."""
    count, labels = cv2.connectedComponents(loose.astype(np.uint8), 8)
    if count <= 1:
        return seeds
    live = set(np.unique(labels[seeds])) - {0}
    return np.isin(labels, list(live)) if live else seeds


def _body(
    mark_t: np.ndarray, seeds: np.ndarray, keep: np.ndarray, bar: float
) -> np.ndarray:
    body = _grow(seeds, (mark_t >= bar) & keep)
    return cv2.morphologyEx(
        body.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8)
    ).astype(bool)


def recover_stacked(
    load: Callable[[], Iterable[np.ndarray]],
) -> StackedMark | None:
    """The batch's shared overlay, or None when none can be proven.

    ``load`` yields the batch's RGB frames and is walked ONCE — the pass is
    paid for in full-size decodes, so each frame is dropped to its own
    working-size grayscale as it streams past, and the common frame is only
    settled once every size has been seen. A uniform batch (the ordinary
    case) needs no second resample at all; a mixed one costs its smaller
    frames one extra INTER_AREA hop, which the t statistic never notices.
    """
    sizes: list[tuple[int, int]] = []
    grays: list[np.ndarray] = []
    for frame in load():
        height, width = frame.shape[:2]
        sizes.append((height, width))
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(np.float32)
        own = _work_shape([(height, width)])
        if own != (height, width):
            gray = cv2.resize(gray, (own[1], own[0]), interpolation=cv2.INTER_AREA)
        grays.append(gray)
    if len(sizes) < MIN_STACK:
        return None
    shape = _work_shape(sizes)
    if shape[0] <= 2 * _MARGIN or shape[1] <= 2 * _MARGIN:
        return None  # margin would swallow the whole frame
    fields = [
        _field(
            gray
            if gray.shape == shape
            else cv2.resize(gray, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        )
        for gray in grays
    ]
    stack = np.stack(fields)

    t = np.abs(_t_field(stack))
    keep = np.zeros(shape, bool)
    keep[_MARGIN:-_MARGIN, _MARGIN:-_MARGIN] = True
    seeds = (t >= _SEED_T) & keep
    share = float(seeds.mean())
    if share < _SHARE_FLOOR:
        return None

    # The null: the same frames stacked out of registration. Whatever level
    # of agreement THAT produces is what coincidence alone is worth here.
    rng = np.random.default_rng(7)
    nulls = []
    for _ in range(_ROLLS):
        rolled = np.stack(
            [
                np.roll(
                    field,
                    (
                        int(rng.integers(40, shape[0] - 40)),
                        int(rng.integers(40, shape[1] - 40)),
                    ),
                    (0, 1),
                )
                for field in stack
            ]
        )
        nulls.append(float(((np.abs(_t_field(rolled)) >= _SEED_T) & keep).mean()))
    if share / max(float(np.mean(nulls)), 1e-6) < _RATIO_BAR:
        return None

    # Near-duplicate frames agree everywhere — on the scene, not a mark.
    flat = stack[:, keep]
    centred = flat - flat.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(centred, axis=1)
    corr = (centred @ centred.T) / np.maximum(np.outer(norms, norms), 1e-9)
    if float(np.median(corr[np.triu_indices(len(stack), 1)])) > _CORR_MAX:
        return None

    # A default-bar body over the cap is a flood, not a watermark.
    if float(_body(t, seeds, keep, _GROW_DEFAULT).mean()) > _BODY_CAP:
        return None
    return StackedMark(t=t.astype(np.float32), seeds=seeds, keep=keep)


def stamp_stacked(
    mark: StackedMark, shape: tuple[int, int], sensitivity: int
) -> np.ndarray:
    """The overlay body for one image of the batch, at its native ``shape``.

    Cut from the shared |t| field at the sensitivity's grow bar. A widened
    body that floods past the cap falls back to the calibrated default-bar
    body (proven under the cap at recovery) — the one deliberate break in the
    slider's monotonicity, taken over handing a flood to the inpainter.
    """
    sensitivity = max(0, min(100, sensitivity))
    bar = _GROW_DEFAULT * (_GROW_DEFAULT / _GROW_MAX) ** ((sensitivity - 50) / 50)
    body = _body(mark.t, mark.seeds, mark.keep, bar)
    if float(body.mean()) > _BODY_CAP:
        body = _body(mark.t, mark.seeds, mark.keep, _GROW_DEFAULT)
    return cv2.resize(
        body.astype(np.uint8) * 255,
        (shape[1], shape[0]),
        interpolation=cv2.INTER_NEAREST,
    )
