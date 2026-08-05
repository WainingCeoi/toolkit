"""Watermark engine: synthetic watermarks in, measured quality out.

No real watermarked images anywhere: every fixture is generated — a gradient
background with shapes, tiled with semi-transparent text the way stock-photo
watermarks are. Generating the watermark yields its ground-truth mask for
free, so detection recall and inpainting improvement are measured, not
eyeballed. Everything here runs on the cv2 inpainter; LaMa (torch + a ~200 MB
checkpoint) lives behind the slow marker in test_watermark_lama.py.
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from watermark import imgio
from watermark.__main__ import main
from watermark.detect import propose_mask
from watermark.inpaint import get_inpainter, inpaint_cv2
from watermark.pipeline import remove_watermark


def synthetic_pair(
    size=(320, 240),
    text="SAMPLE",
    color=(255, 255, 255),
    alpha=110,
    bg_range=(40, 160),
    font_size=22,
):
    """A clean image, the same image watermarked, and the true mask.

    The background is a gradient with two filled shapes (so it is not
    trivially flat), tiled with ``text`` at ``alpha`` — the classic
    semi-transparent stock-photo watermark. Tile spacing scales with the font
    so a high-resolution variant is the same watermark, larger.
    """
    w, h = size
    lo, hi = bg_range
    xs = np.linspace(lo, hi, w).astype(np.uint8)
    base = Image.fromarray(np.dstack([np.tile(xs, (h, 1))] * 3))
    draw = ImageDraw.Draw(base)
    draw.ellipse(
        (w * 0.10, h * 0.20, w * 0.45, h * 0.70),
        fill=(max(lo, 70), max(lo, 90), max(lo, 120)),
    )
    draw.rectangle((w * 0.55, h * 0.50, w * 0.90, h * 0.85), fill=(hi, 140, 90))
    clean = np.asarray(base)

    overlay = Image.new("RGBA", size, (0, 0, 0, 0))
    tile = ImageDraw.Draw(overlay)
    font = ImageFont.load_default(size=font_size)
    step_y = round(70 * font_size / 22)
    step_x = round(130 * font_size / 22)
    for row, y in enumerate(range(6, h - font_size, step_y)):
        for x in range(6 + (row % 2) * step_x // 2, w - font_size, step_x):
            tile.text((x, y), text, font=font, fill=(*color, alpha))
    marked = Image.alpha_composite(base.convert("RGBA"), overlay)
    true_mask = (np.asarray(overlay)[:, :, 3] > 0).astype(np.uint8) * 255
    return clean, np.asarray(marked.convert("RGB")), true_mask


def recall(proposed: np.ndarray, true_mask: np.ndarray) -> float:
    hits = np.count_nonzero((proposed > 0) & (true_mask > 0))
    return hits / np.count_nonzero(true_mask)


# =========================================================================
# Detection
# =========================================================================


def test_default_sensitivity_finds_most_of_a_light_text_watermark():
    _clean, marked, true_mask = synthetic_pair()
    proposed = propose_mask(marked)
    assert recall(proposed, true_mask) >= 0.6
    # Over-detection is acceptable, blanketing the image is not: a mask that
    # marks half the picture would make the review step useless.
    assert np.count_nonzero(proposed) / proposed.size < 0.5


def test_dark_text_is_caught_by_the_black_tophat_half():
    _clean, marked, true_mask = synthetic_pair(
        color=(10, 10, 10), alpha=140, bg_range=(100, 200)
    )
    assert recall(propose_mask(marked), true_mask) >= 0.6


def test_a_high_resolution_watermark_is_still_found():
    # Same watermark at phone-camera resolution: the strokes are now far wider
    # than the structuring element, so at native resolution the top-hat is
    # completely blind to them (recall 0.00, measured). This passes only
    # because detection runs at a bounded working size and scales the mask
    # back up.
    _clean, marked, true_mask = synthetic_pair(size=(7200, 4800), font_size=260)
    proposed = propose_mask(marked)
    assert proposed.shape == true_mask.shape
    assert recall(proposed, true_mask) >= 0.6
    assert np.count_nonzero(proposed) / proposed.size < 0.5


def test_sensitivity_marks_monotonically_more_pixels():
    _clean, marked, _true = synthetic_pair()
    counts = [np.count_nonzero(propose_mask(marked, s)) for s in (10, 50, 90)]
    assert counts[0] <= counts[1] <= counts[2]
    # ...and the slider's ends actually differ, or it is decoration.
    assert counts[0] < counts[2]


# =========================================================================
# Inpainting
# =========================================================================


def test_cv2_inpaint_moves_pixels_toward_the_unwatermarked_original():
    clean, marked, true_mask = synthetic_pair()
    cleaned = remove_watermark(marked, true_mask, inpaint_cv2)
    region = true_mask > 0
    err_before = np.abs(marked[region].astype(int) - clean[region]).mean()
    err_after = np.abs(cleaned[region].astype(int) - clean[region]).mean()
    assert err_after < err_before * 0.5


def test_inpainting_is_tiled_so_memory_does_not_track_image_size():
    # LaMa's memory grows with the frame it is handed: 0.8 MP peaked at 12 GB
    # and 3.1 MP at 25 GB on CPU, so a 36 MP photo took the whole process down.
    # Tiling is what keeps the frame — and therefore the peak — bounded.
    from watermark.pipeline import CONTEXT_PX, TILE_PX

    seen = []

    def recording_inpaint(rgb, mask):
        seen.append(rgb.shape[:2])
        return inpaint_cv2(rgb, mask)

    big = np.full((2400, 3000, 3), 130, np.uint8)
    mask = np.zeros((2400, 3000), np.uint8)
    mask[::600, ::700] = 255  # spread out, so several tiles have work
    remove_watermark(big, mask, recording_inpaint)

    ceiling = TILE_PX + 2 * CONTEXT_PX
    assert seen, "the inpainter was never called"
    assert all(h <= ceiling and w <= ceiling for h, w in seen), (
        f"a frame exceeded {ceiling}px: {sorted(set(seen))}"
    )


def test_tiles_with_nothing_masked_are_skipped_entirely():
    # Cost should track the watermark's area, not the photo's.
    calls = []

    def counting_inpaint(rgb, mask):
        calls.append(1)
        return inpaint_cv2(rgb, mask)

    big = np.full((2400, 3000, 3), 130, np.uint8)
    mask = np.zeros((2400, 3000), np.uint8)
    mask[10:30, 10:30] = 255  # one corner only
    remove_watermark(big, mask, counting_inpaint)
    assert len(calls) == 1, f"expected one tile of work, ran {len(calls)}"


def test_only_masked_pixels_are_ever_rewritten():
    # LaMa reconstructs the whole frame it is handed; compositing keeps
    # everything the user did not mark bit-identical to the upload.
    rgb = np.dstack([np.tile(np.arange(200, dtype=np.uint8), (150, 1))] * 3)
    mask = np.zeros((150, 200), np.uint8)
    mask[40:60, 40:60] = 255

    def destructive(_rgb, _mask):
        return np.zeros_like(_rgb)  # a "model" that rewrites everything

    out = remove_watermark(rgb, mask, destructive, dilate_px=0)
    assert (out[mask > 0] == 0).all()  # masked pixels came from the model
    assert np.array_equal(out[mask == 0], rgb[mask == 0])  # the rest untouched


def test_an_empty_mask_returns_the_image_unchanged():
    rgb = np.full((40, 60, 3), 200, np.uint8)
    out = remove_watermark(rgb, np.zeros((40, 60), np.uint8), inpaint_cv2)
    assert np.array_equal(out, rgb)


def test_get_inpainter_rejects_unknown_names():
    with pytest.raises(ValueError, match="Unknown inpainter 'photoshop'"):
        get_inpainter("photoshop")


# =========================================================================
# Image IO
# =========================================================================


def test_mask_roundtrips_through_png():
    _clean, marked, _true = synthetic_pair()
    mask = propose_mask(marked)
    again = imgio.load_mask(imgio.encode_png(mask), mask.shape)
    assert np.array_equal(again, mask)


def test_a_mask_of_the_wrong_size_is_refused_not_resized():
    mask = np.zeros((10, 20), np.uint8)
    with pytest.raises(ValueError, match="Mask is 20×10 but the image is 40×30"):
        imgio.load_mask(imgio.encode_png(mask), (30, 40))


def test_exif_rotation_is_normalized_at_decode():
    # A 60×30 JPEG tagged orientation 6 (rotate 90 CW to display) must decode
    # to upright 30×60 pixels — cv2 would ignore the tag and the browser would
    # honour it, and the mask would land on a rotated copy of the image.
    import io

    image = Image.new("RGB", (60, 30), "white")
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    assert imgio.load_rgb(buffer.getvalue()).shape == (60, 30, 3)


# =========================================================================
# CLI
# =========================================================================


def write_marked_folder(folder, count=2):
    folder.mkdir()
    for i in range(count):
        _clean, marked, _true = synthetic_pair(size=(120, 90))
        suffix = "png" if i % 2 == 0 else "webp"
        Image.fromarray(marked).save(folder / f"photo_{i}.{suffix}")
    (folder / "notes.txt").write_text("not an image")
    return folder


def test_cli_cleans_a_folder_with_cv2(tmp_path, capsys):
    src = write_marked_folder(tmp_path / "in")
    out = tmp_path / "out"
    assert main(["clean", str(src), str(out), "--inpainter", "cv2"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["photo_0.png", "photo_1.png"]
    assert "[2/2]" in capsys.readouterr().out


def test_two_inputs_with_the_same_stem_both_survive(tmp_path, capsys):
    # Every output is a PNG, so photo.jpg and photo.png both want photo.png —
    # one would silently overwrite the other and still be counted as cleaned.
    src = tmp_path / "in"
    src.mkdir()
    _clean, marked, _true = synthetic_pair(size=(120, 90))
    Image.fromarray(marked).save(src / "photo.png")
    Image.fromarray(marked).save(src / "photo.jpg")
    out = tmp_path / "out"
    assert main(["clean", str(src), str(out), "--inpainter", "cv2"]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["photo (2).png", "photo.png"]


def test_cli_refuses_to_write_into_its_own_input_folder(tmp_path, capsys):
    # Writing into the input folder would overwrite images the run has not
    # read yet, so the later ones would be cleaned twice.
    src = write_marked_folder(tmp_path / "in")
    code = main(["clean", str(src), str(src), "--inpainter", "cv2"])
    assert code == 2
    assert "must be different from the input folder" in capsys.readouterr().err


def test_cli_reports_a_missing_input_folder(tmp_path, capsys):
    args = ["clean", str(tmp_path / "nope"), str(tmp_path / "out")]
    code = main([*args, "--inpainter", "cv2"])
    assert code == 2
    assert "Input folder not found" in capsys.readouterr().err


def test_cli_reports_an_empty_input_folder(tmp_path, capsys):
    (tmp_path / "in").mkdir()
    args = ["clean", str(tmp_path / "in"), str(tmp_path / "out")]
    code = main([*args, "--inpainter", "cv2"])
    assert code == 2
    assert "no png/jpg/webp images" in capsys.readouterr().err


def test_cli_refuses_lama_without_torch(tmp_path, capsys, monkeypatch):
    import watermark.__main__ as cli

    monkeypatch.setattr(cli, "lama_available", lambda: False)
    with pytest.raises(SystemExit) as excinfo:
        main(["clean", str(tmp_path), str(tmp_path / "out")])
    assert excinfo.value.code == 2
    assert "--inpainter cv2" in capsys.readouterr().err


def test_cli_module_entrypoint_is_wired(tmp_path):
    # `python -m watermark` must work headless, straight from the venv.
    src = write_marked_folder(tmp_path / "in", count=1)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "watermark",
            "clean",
            str(src),
            str(tmp_path / "out"),
            "--inpainter",
            "cv2",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "photo_0.png").is_file()


def test_importing_the_engine_never_imports_torch():
    # The lazy-ML contract: torch loads on first LaMa *use*, not on import.
    code = (
        "import sys; import watermark.pipeline, watermark.inpaint, "
        "watermark.detect, watermark.__main__; "
        "sys.exit(1 if 'torch' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
