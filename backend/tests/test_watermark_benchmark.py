"""The benchmark is runnable from a checkout and measures input/output quality."""

import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

import numpy as np

from watermark import imgio

SCRIPT = Path(__file__).parents[1] / "benchmarks" / "watermark_benchmark.py"


def test_benchmark_sensitivity_also_applies_to_shared_marks(tmp_path, monkeypatch):
    path = tmp_path / "image.png"
    path.write_bytes(imgio.encode_png(np.full((30, 40, 3), 128, np.uint8)))
    runner = runpy.run_path(str(SCRIPT))["run_case"]
    sensitivities = []

    def collect(load, sensitivity=50):
        sensitivities.append(sensitivity)
        return []

    monkeypatch.setitem(runner.__globals__, "collect_marks", collect)
    cv2 = runner.__globals__["cv2"]
    previous_threads = cv2.getNumThreads()
    try:
        runner(
            {
                "case": {"name": "custom", "images": [{"marked": str(path)}]},
                "threads": 1,
                "sensitivity": 73,
                "inpainter": "cv2",
            }
        )
    finally:
        cv2.setNumThreads(previous_threads)
    assert sensitivities == [73]


def test_benchmark_manifest_reports_quality_and_preserves_transparency(tmp_path):
    rgba = np.full((30, 40, 4), 128, np.uint8)
    rgba[:, :, 3] = 255
    rgba[:5, :5, 3] = 0
    (tmp_path / "image.png").write_bytes(imgio.encode_png(rgba))
    truth = tmp_path / "truth.png"
    truth.write_bytes(imgio.encode_png(np.zeros((30, 40), np.uint8)))
    manifest = tmp_path / "cases.json"
    manifest.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "clean-rgba",
                        "images": [
                            {
                                "marked": "image.png",
                                "original": "image.png",
                                "mask": "truth.png",
                            }
                        ],
                    }
                ]
            }
        )
    )
    output = tmp_path / "result.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(output.read_text())
    assert report["metadata"]["inpainter"] == "cv2"
    assert report["metadata"]["device"] == "cpu"
    assert (
        report["metadata"]["harness_sha256"]
        == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    )
    fixtures = Path(__file__).with_name("watermark_fixtures.py")
    assert (
        report["metadata"]["fixture_sha256"]
        == hashlib.sha256(fixtures.read_bytes()).hexdigest()
    )
    hashes = report["metadata"]["input_sha256"][0]["images"][0]
    assert (
        hashes["marked"]
        == hashes["original"]
        == hashlib.sha256((tmp_path / "image.png").read_bytes()).hexdigest()
    )
    assert hashes["mask"] == hashlib.sha256(truth.read_bytes()).hexdigest()
    assert report["metadata"]["model_sha256"] is None
    result = report["runs"][0]
    assert result["case"] == "clean-rgba"
    assert result["peak_rss_bytes"] > 0
    image = result["images"][0]
    assert image["dimensions"] == [40, 30]
    assert image["outcome"] == "skipped"
    assert image["false_positive_rate"] == 0
    assert image["mae_before"] == image["mae_after"] == 0
    assert image["alpha_preserved"] is True
    assert image["unmasked_pixels_unchanged"] is True


def test_builtin_clean_benchmark_is_reproducible():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--case", "clean", "--repeat", "2"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    first, second = json.loads(proc.stdout)["runs"]
    assert first["images"][0]["output_sha256"] == second["images"][0]["output_sha256"]
    assert first["images"][0]["mask_sha256"] == second["images"][0]["mask_sha256"]
    assert first["images"][0]["false_positive_rate"] == 0
