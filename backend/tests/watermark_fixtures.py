"""Synthetic watermarked images with exact truth masks, for the detection tests."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Lattices seen in real samples, in working-size pixels.
RECTANGULAR = ((56, 0), (0, 114))
SHALLOW_OBLIQUE = ((16, 84), (-84, 16))  # ~11 degrees
STEEP_OBLIQUE = ((90, 22), (-22, 90))  # ~76 degrees
# Too coarse to fold in a 1080x1922 frame; only the tiled route reaches it.
COARSE_RECTANGULAR = ((268, 0), (0, 572))


def _background(kind: str, w: int, h: int) -> np.ndarray:
    rng = np.random.default_rng(11)
    if kind == "gradient":
        ramp = np.linspace(60, 200, w).astype(np.float32)
        return np.dstack([np.tile(ramp, (h, 1))] * 3).astype(np.uint8)

    if kind == "sky_grass":
        img = np.zeros((h, w, 3), np.float32)
        sky_h = int(h * 0.45)
        ramp = np.linspace(210, 170, sky_h)[:, None]
        img[:sky_h, :, 0] = ramp * 0.75
        img[:sky_h, :, 1] = ramp * 0.88
        img[:sky_h, :, 2] = ramp
        grass = rng.normal(120, 34, (h - sky_h, w, 3))
        grass[:, :, 0] *= 0.85
        grass[:, :, 2] *= 0.55
        grass += rng.normal(0, 26, (h - sky_h, w))[:, :, None]
        img[sky_h:] = grass
        return np.clip(img, 0, 255).astype(np.uint8)

    if kind == "grass":
        # Busy over the whole frame: no quiet tile anywhere for the fold.
        img = rng.normal(128, 30, (h, w, 3))
        img[:, :, 0] *= 0.9
        img[:, :, 2] *= 0.6
        img += rng.normal(0, 22, (h, w))[:, :, None]
        rows = np.linspace(0, 3 * np.pi, h)[:, None]
        cols = np.linspace(0, 2 * np.pi, w)[None, :]
        img += (18 * np.sin(rows) * np.cos(cols))[:, :, None]
        return np.clip(img, 0, 255).astype(np.uint8)

    if kind == "render_dither":
        img = np.full((h, w, 3), 252, np.float32)
        ground = slice(int(h * 0.45), h)
        speckle = rng.normal(0, 26, (h - int(h * 0.45), w))
        img[ground, :, 0] = np.clip(150 + speckle, 0, 255)
        img[ground, :, 1] = np.clip(70 + speckle, 0, 255)
        img[ground, :, 2] = np.clip(60 + speckle, 0, 255)
        return img.astype(np.uint8)

    raise ValueError(f"unknown background {kind!r}")


def _stamp(text: str, size: int, angle: float, color, alpha: int) -> Image.Image:
    """One watermark instance: text, optionally rotated, on transparency."""
    font = ImageFont.load_default(size=size)
    pad = size * 4
    tile = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.text((pad // 4, pad // 3), text, font=font, fill=(*color, alpha))
    # A ring gives the correlator a shape beyond a line of text.
    draw.ellipse(
        (pad // 4 - size, pad // 3 - size // 3, pad // 4 - size // 6, pad // 3 + size),
        outline=(*color, alpha),
        width=max(2, size // 8),
    )
    return tile.rotate(angle, resample=Image.BICUBIC, expand=False)


def tiled_pair(
    size=(1200, 800),
    text="LOGO",
    basis=RECTANGULAR,
    angle=0.0,
    alpha=40,
    glyph_size=26,
    background="sky_grass",
    color=(235, 235, 235),
    watermarked=True,
    offset=(0, 0),
):
    """(clean, marked, truth) for a mark tiled on ``basis``: two (dy, dx) vectors."""
    w, h = size
    clean = _background(background, w, h)
    if watermarked:
        stamp = _stamp(text, glyph_size, angle, color, alpha)
        pad_x, pad_y = stamp.width, stamp.height
        canvas = Image.new("RGBA", (w + 2 * pad_x, h + 2 * pad_y), (0, 0, 0, 0))
        (v1y, v1x), (v2y, v2x) = basis
        # Enough integer combinations to cover the frame whatever the basis.
        reach = int(2 * (w + h) / max(1, min(abs(v1y) + abs(v1x), abs(v2y) + abs(v2x))))
        off_y, off_x = offset
        for i in range(-reach, reach + 1):
            for j in range(-reach, reach + 1):
                y = i * v1y + j * v2y + off_y
                x = i * v1x + j * v2x + off_x
                if -pad_y < y < h and -pad_x < x < w:
                    # Not paste(mask=stamp): that premultiplies alpha twice.
                    canvas.alpha_composite(stamp, (int(x) + pad_x, int(y) + pad_y))
        overlay = canvas.crop((pad_x, pad_y, pad_x + w, pad_y + h))
    else:
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    marked = Image.alpha_composite(Image.fromarray(clean).convert("RGBA"), overlay)

    # The antialiased fringe is a don't-care band that score() leaves out.
    ink = np.asarray(overlay)[:, :, 3]
    core = ink >= max(1, round(0.2 * int(ink.max())))
    truth = np.where(core & (ink > 0), TRUTH_MARK, np.where(ink > 0, TRUTH_FRINGE, 0))
    # Enclosed gaps (letter counters) are don't-care too.
    body = cv2.morphologyEx(
        (ink > 0).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)
    )
    truth = np.where((body > 0) & (truth == 0), TRUTH_FRINGE, truth)
    return clean, np.asarray(marked.convert("RGB")), truth.astype(np.uint8)


TRUTH_MARK = 255
TRUTH_FRINGE = 128


def score(proposed: np.ndarray, truth: np.ndarray) -> tuple[float, float]:
    """(recall over the mark core, false-positive rate over clean pixels)."""
    hit = proposed > 0
    wm = truth == TRUTH_MARK
    clean = truth == 0
    recall = (hit & wm).sum() / max(wm.sum(), 1)
    false_positives = (hit & clean).sum() / max(clean.sum(), 1)
    return float(recall), float(false_positives)


def _studio_scene(w: int, h: int, seed: int) -> np.ndarray:
    """A studio product shot; without the grain a stack's variance collapses."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 208, np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img += (rng.uniform(-14, 14) * xx / w + rng.uniform(-14, 14) * yy / h)[..., None]
    for _ in range(rng.integers(2, 4)):
        cx, cy = rng.integers(0, w), rng.integers(0, h)
        ax, ay = rng.integers(w // 8, w // 3), rng.integers(h // 12, h // 5)
        shade = float(rng.integers(120, 245))
        blob = np.zeros((h, w), np.float32)
        cv2.ellipse(
            blob,
            (int(cx), int(cy)),
            (int(ax), int(ay)),
            float(rng.integers(0, 180)),
            0,
            360,
            1.0,
            -1,
        )
        fill = (
            shade
            + 26 * ((xx - cx) / max(ax, 1)) * rng.uniform(-1, 1)
            + 26 * ((yy - cy) / max(ay, 1)) * rng.uniform(-1, 1)
        )
        img = img * (1 - blob[..., None]) + fill[..., None] * blob[..., None]
    img = cv2.GaussianBlur(img, (0, 0), 2)
    img += rng.normal(0, 2.0, (h, w, 1))
    return np.clip(img, 0, 255).astype(np.uint8)


def stacked_batch(
    n=5,
    size=(700, 520),
    text="TAIZHOU QIJIA NEW MATERIALS CO",
    alpha=48,
    watermarked=True,
    duplicates=False,
    seed0=100,
):
    """(frames, truth): one banner at the same place on n different scenes."""
    w, h = size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if watermarked:
        draw = ImageDraw.Draw(overlay)
        glyph_px = 26
        for candidate in range(8, 80):
            font = ImageFont.load_default(size=candidate)
            if draw.textlength(text, font=font) > 0.85 * w:
                break
            glyph_px = candidate
        font = ImageFont.load_default(size=glyph_px)
        draw.text(
            (int(w * 0.06), int(h * 0.52)),
            text,
            font=font,
            fill=(240, 240, 240, alpha),
        )
    ink = np.asarray(overlay)[:, :, 3]
    truth = (ink >= max(1, round(0.2 * max(1, int(ink.max()))))).astype(np.uint8) * 255
    frames = [
        np.asarray(
            Image.alpha_composite(
                Image.fromarray(
                    _studio_scene(w, h, seed0 if duplicates else seed0 + i)
                ).convert("RGBA"),
                overlay,
            ).convert("RGB")
        )
        for i in range(n)
    ]
    return frames, truth
