"""Recover a once-per-image watermark by stacking the batch."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import cv2
import numpy as np

# Longest working-frame side; the whole stack must share one frame.
_WORK = 700

# Border excluded from seeding: np.roll wraps content across it in the null.
_MARGIN = 24

_SEED_T = 6.0
_ROLLS = 24
_SHARE_FLOOR = 0.0002  # of frame pixels
_RATIO_BAR = 5.0
_CORR_MAX = 0.9
_BODY_CAP = 0.08  # of frame pixels
MIN_STACK = 3  # two frames give no usable variance

# Sensitivity 0..100 -> grow bar, geometric through the default: 4.0, 2.5, ~1.56.
_GROW_DEFAULT = 2.5
_GROW_MAX = 4.0

# 21 is the largest kernel that does not pass the registered edges of like frames.
_SE = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))


@dataclass
class StackedMark:
    """The batch's proven overlay: the |t| field and the seeds inside it."""

    t: np.ndarray  # float32 (H, W) working frame, |t| per pixel
    seeds: np.ndarray  # bool (H, W), |t| >= _SEED_T inside the margin
    keep: np.ndarray  # bool (H, W), the margin mask the gates ran under


def _field(gray: np.ndarray) -> np.ndarray:
    """Signed dual top-hat (a Gaussian high-pass lets unrelated scenes agree)."""
    top = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, _SE)
    black = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, _SE)
    return top - black


def _t_field(stack: np.ndarray) -> np.ndarray:
    """Mean over spread per pixel; +0.5 keeps a flat region from dividing by ~0."""
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
    """The batch's shared overlay, or None; ``load`` is walked once."""
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

    # The null: the same frames stacked out of registration.
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
    """The overlay body for one image of the batch, at its native ``shape``."""
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
