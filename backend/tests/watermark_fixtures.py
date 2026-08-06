"""Synthetic watermarked images that look like the ones this tool meets.

Shared by the detection tests. Nothing here is a real watermarked photo — the
watermark is generated, which is what makes its ground-truth mask exact.

The important lesson encoded here: the first round of fixtures all tiled their
watermark axis-aligned, so every test passed while half of a real sample fell
back to the weaker detector. Real overlays are frequently laid on an OBLIQUE
lattice — measured on sample photos, the strongest autocorrelation peak was at
16 degrees in one and 76 degrees in another — so ``basis`` here is a pair of
arbitrary vectors, not a row/column pitch.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Lattices seen in real samples, in working-size pixels. The oblique ones are
# what the axis-aligned period search structurally could not find.
RECTANGULAR = ((56, 0), (0, 114))
SHALLOW_OBLIQUE = ((16, 84), (-84, 16))  # ~11 degrees, like the render samples
STEEP_OBLIQUE = ((90, 22), (-22, 90))  # ~76 degrees


def _background(kind: str, w: int, h: int) -> np.ndarray:
    rng = np.random.default_rng(11)
    if kind == "gradient":
        ramp = np.linspace(60, 200, w).astype(np.float32)
        return np.dstack([np.tile(ramp, (h, 1))] * 3).astype(np.uint8)

    if kind == "sky_grass":
        # Two very different textures in one frame: the case a single global
        # threshold cannot serve.
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
        # Busy over the WHOLE frame, with no quiet corner anywhere. This is what
        # defeats the fold rather than the mark being faint: its median needs
        # tiles the photograph does not fill, and a sample hillside offered one
        # quiet tile out of 43. The mark stays perfectly legible locally, so a
        # sibling's mark still matches it — that is the case batch sharing is for.
        img = rng.normal(128, 30, (h, w, 3))
        img[:, :, 0] *= 0.9
        img[:, :, 2] *= 0.6
        img += rng.normal(0, 22, (h, w))[:, :, None]
        # Some large-scale structure too, so it reads as terrain and not static.
        rows = np.linspace(0, 3 * np.pi, h)[:, None]
        cols = np.linspace(0, 2 * np.pi, w)[None, :]
        img += (18 * np.sin(rows) * np.cos(cols))[:, :, None]
        return np.clip(img, 0, 255).astype(np.uint8)

    if kind == "render_dither":
        # A 3D render: flat white paper plus a finely dithered ground texture.
        # The dither is the distractor that stole the period estimate on the
        # real render samples.
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
    # A ring, so the mark has a shape a correlator can lock onto rather than
    # only a line of text — like the logo-plus-lettering marks in the samples.
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
):
    """(clean, marked, truth_mask) for a watermark tiled on ``basis``.

    ``basis`` is two (dy, dx) vectors; instances land at every integer
    combination of them that falls inside the frame, so an oblique pair
    produces the diagonal tiling real overlays actually use. ``watermarked``
    False returns a clean frame with an all-zero truth mask, for false-positive
    control.
    """
    w, h = size
    clean = _background(background, w, h)
    if watermarked:
        stamp = _stamp(text, glyph_size, angle, color, alpha)
        # Padded, so an instance hanging off the top or left edge still
        # composites in bounds; cropped back to the frame afterwards.
        pad_x, pad_y = stamp.width, stamp.height
        canvas = Image.new("RGBA", (w + 2 * pad_x, h + 2 * pad_y), (0, 0, 0, 0))
        (v1y, v1x), (v2y, v2x) = basis
        # Enough integer combinations to cover the frame whatever the basis.
        reach = int(2 * (w + h) / max(1, min(abs(v1y) + abs(v1x), abs(v2y) + abs(v2x))))
        for i in range(-reach, reach + 1):
            for j in range(-reach, reach + 1):
                y = i * v1y + j * v2y
                x = i * v1x + j * v2x
                if -pad_y < y < h and -pad_x < x < w:
                    # alpha_composite, NOT paste(…, mask=stamp): paste with an
                    # RGBA image as its own mask premultiplies a second time,
                    # which turned alpha 40 into 6 and colour 235 into 37 — a
                    # mark that was meant to be light at 16% opacity became DARK
                    # at 2%. Every measurement taken against those fixtures was
                    # against a target far fainter, and of the wrong sign, than
                    # the watermarks this tool is for.
                    canvas.alpha_composite(stamp, (int(x) + pad_x, int(y) + pad_y))
        overlay = canvas.crop((pad_x, pad_y, pad_x + w, pad_y + h))
    else:
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    marked = Image.alpha_composite(Image.fromarray(clean).convert("RGBA"), overlay)

    # Three-level truth. Glyph edges are antialiased down to an alpha of 1 or 2,
    # which is neither visible nor recoverable; counting those as mark caps
    # recall at whatever share of the stamp happens to be fringe, and counting a
    # detector that does cover them as wrong is equally meaningless. So the
    # fringe is a DON'T-CARE band that score() leaves out of both figures.
    ink = np.asarray(overlay)[:, :, 3]
    core = ink >= max(1, round(0.2 * int(ink.max())))
    truth = np.where(core & (ink > 0), TRUTH_MARK, np.where(ink > 0, TRUTH_FRINGE, 0))
    return clean, np.asarray(marked.convert("RGB")), truth.astype(np.uint8)


TRUTH_MARK = 255
TRUTH_FRINGE = 128


def score(proposed: np.ndarray, truth: np.ndarray) -> tuple[float, float]:
    """(recall, false-positive rate) of a proposed mask against ground truth.

    Recall is over the mark's core and false positives over clean pixels only;
    the antialiasing fringe between them counts for neither (see tiled_pair).
    """
    hit = proposed > 0
    wm = truth == TRUTH_MARK
    clean = truth == 0
    recall = (hit & wm).sum() / max(wm.sum(), 1)
    false_positives = (hit & clean).sum() / max(clean.sum(), 1)
    return float(recall), float(false_positives)
