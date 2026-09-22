"""Reproducible watermark quality/timing benchmark; see the adjacent README."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))
from watermark import imgio  # noqa: E402
from watermark.detect import _propose_with_evidence, collect_marks  # noqa: E402
from watermark.inpaint import MODEL_ENV, get_inpainter  # noqa: E402
from watermark.pipeline import (  # noqa: E402
    DEFAULT_DILATE_PX,
    probe_removal,
    remove_watermark,
)

CASES = ("repeating", "repeating-6mp", "single-batch", "clean", "rgba")


def synthetic_case(name: str, directory: Path) -> dict:
    # Deliberately share the deterministic quality fixtures with the test suite.
    sys.path.insert(0, str(BACKEND / "tests"))
    from watermark_fixtures import stacked_batch, tiled_pair

    if name == "single-batch":
        marked, truth = stacked_batch()
        originals, _ = stacked_batch(watermarked=False)
        pairs = list(zip(originals, marked, [truth] * len(marked), strict=True))
    else:
        original, marked, truth = tiled_pair(watermarked=name != "clean")
        if name == "repeating-6mp":
            original = cv2.resize(
                original, (3000, 2000), interpolation=cv2.INTER_LINEAR
            )
            marked = cv2.resize(marked, (3000, 2000), interpolation=cv2.INTER_LINEAR)
            truth = cv2.resize(truth, (3000, 2000), interpolation=cv2.INTER_NEAREST)
        if name == "rgba":
            alpha = np.full(marked.shape[:2], 255, np.uint8)
            alpha[:80, :160] = 0
            alpha[80:160, :160] = 128
            original, marked = (
                imgio.with_alpha(rgb, alpha) for rgb in (original, marked)
            )
        pairs = [(original, marked, truth)]
    images = []
    for index, (original, marked, truth) in enumerate(pairs):
        record = {}
        for key, array in (("original", original), ("marked", marked), ("mask", truth)):
            path = directory / f"{name}-{index}-{key}.png"
            path.write_bytes(imgio.encode_png(array))
            record[key] = str(path)
        images.append(record)
    return {"name": name, "images": images}


def read_manifest(path: Path) -> list[dict]:
    cases = json.loads(path.read_text())["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("Manifest must contain a nonempty 'cases' list.")
    for case in cases:
        if not isinstance(case.get("name"), str) or not case.get("images"):
            raise ValueError("Every case needs a name and a nonempty images list.")
        for image in case["images"]:
            if "marked" not in image:
                raise ValueError("Every image needs a marked path.")
            for key in ("marked", "original", "mask"):
                if key in image:
                    image[key] = str((path.parent / image[key]).resolve())
                    if not Path(image[key]).is_file():
                        raise ValueError(f"Missing {key} image: {image[key]}")
    return cases


def peak_rss() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def quality(rgb, output, original, truth, mask, alpha, encoded) -> dict:
    # Bound metric scratch space too: the benchmark must not dominate its own RSS.
    hits = positives = false_hits = negatives = before = after = 0
    untouched = True
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * DEFAULT_DILATE_PX + 1,) * 2
    )
    effective = cv2.dilate(mask, kernel)
    for top in range(0, len(rgb), 128):
        rows = slice(top, top + 128)
        changed = np.any(rgb[rows] != output[rows], axis=2)
        untouched = untouched and not np.any(changed & (effective[rows] == 0))
        if truth is not None:
            selected = mask[rows] > 0
            watermark, clean = truth[rows] == 255, truth[rows] == 0
            hits += int(np.count_nonzero(selected & watermark))
            positives += int(np.count_nonzero(watermark))
            false_hits += int(np.count_nonzero(selected & clean))
            negatives += int(np.count_nonzero(clean))
        if original is not None:
            before += int(cv2.absdiff(rgb[rows], original[rows]).sum())
            after += int(cv2.absdiff(output[rows], original[rows]).sum())
    _, saved_alpha = imgio.load_rgba(encoded)
    return {
        "mask_pixels": int(np.count_nonzero(mask)),
        "mask_fraction": float(np.count_nonzero(mask) / mask.size),
        "recall": hits / positives if positives else None,
        "false_positive_rate": false_hits / negatives if negatives else None,
        "mae_before": before / rgb.size if original is not None else None,
        "mae_after": after / rgb.size if original is not None else None,
        "unmasked_pixels_unchanged": bool(untouched),
        "alpha_preserved": bool(
            (alpha is None and saved_alpha is None)
            or (alpha is not None and np.array_equal(alpha, saved_alpha))
        ),
        "mask_sha256": hashlib.sha256(mask.tobytes()).hexdigest(),
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def run_case(spec: dict) -> dict:
    cv2.setNumThreads(spec["threads"])
    if spec.get("model"):
        os.environ[MODEL_ENV] = spec["model"]
    case = spec["case"]
    start = time.perf_counter()
    frames = [
        imgio.load_rgba(Path(item["marked"]).read_bytes()) for item in case["images"]
    ]
    decoded = time.perf_counter()
    marks = collect_marks(lambda: (rgb for rgb, _ in frames), spec["sensitivity"])
    collected = time.perf_counter()
    inpaint = None
    images = []
    for item, (rgb, alpha) in zip(case["images"], frames, strict=True):
        detected = time.perf_counter()
        mask, detector, evidence, probe = _propose_with_evidence(
            rgb, spec["sensitivity"], marks=marks
        )
        detection_seconds = time.perf_counter() - detected
        removed = time.perf_counter()
        output = rgb
        if not mask.any():
            outcome = "protected" if evidence else "skipped"
        else:
            destroys = False
            if probe is None:
                probe, destroys = probe_removal(rgb, mask, DEFAULT_DILATE_PX)
            if destroys:
                outcome = "protected"
            else:
                outcome = "cleaned"
                if spec["inpainter"] == "cv2":
                    output = probe
                else:
                    if inpaint is None:
                        inpaint = get_inpainter(spec["inpainter"])
                    output = remove_watermark(rgb, mask, inpaint)
        removal_seconds = time.perf_counter() - removed
        encoding = time.perf_counter()
        encoded = imgio.encode_png(imgio.with_alpha(output, alpha))
        encoding_seconds = time.perf_counter() - encoding
        original = (
            imgio.load_rgb(Path(item["original"]).read_bytes())
            if "original" in item
            else None
        )
        if original is not None and original.shape != rgb.shape:
            raise ValueError(f"Original dimensions do not match {item['marked']}")
        truth, truth_source = None, None
        if "mask" in item:
            with Image.open(item["mask"]) as image:
                truth = np.asarray(image.convert("L"))
            if truth.shape != mask.shape:
                raise ValueError(f"Truth mask dimensions do not match {item['marked']}")
            truth_source = "provided_mask"
        elif original is not None:
            truth = np.where(cv2.absdiff(rgb, original).max(axis=2) > 2, 255, 0).astype(
                np.uint8
            )
            truth_source = "original_difference_gt_2"
        images.append(
            {
                "name": Path(item["marked"]).name,
                "dimensions": [rgb.shape[1], rgb.shape[0]],
                "detector": detector,
                "outcome": outcome,
                "truth_source": truth_source,
                "detection_seconds": detection_seconds,
                "removal_seconds": removal_seconds,
                "encoding_seconds": encoding_seconds,
                **quality(rgb, output, original, truth, mask, alpha, encoded),
            }
        )
    return {
        "case": case["name"],
        "wall_seconds": time.perf_counter() - start,
        "decode_seconds": decoded - start,
        "collect_seconds": collected - decoded,
        "peak_rss_bytes": peak_rss(),
        "images": images,
    }


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def metadata(args, cases: list[dict], model: Path | None) -> dict:
    versions = {}
    for name in ("numpy", "opencv-python", "pillow", "torch"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    digest = hashlib.sha256()
    for path in sorted((BACKEND / "src" / "watermark").glob("*.py")):
        digest.update(path.name.encode() + path.read_bytes())
    device = "cpu"
    if args.inpainter == "lama":
        from watermark.inpaint import resolve_device

        device = resolve_device()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "versions": versions,
        "engine_sha256": digest.hexdigest(),
        "harness_sha256": file_hash(Path(__file__)),
        "fixture_sha256": file_hash(BACKEND / "tests" / "watermark_fixtures.py"),
        "input_sha256": [
            {
                "case": case["name"],
                "images": [
                    {
                        key: file_hash(Path(item[key]))
                        for key in ("marked", "original", "mask")
                        if key in item
                    }
                    for item in case["images"]
                ],
            }
            for case in cases
        ],
        "model_sha256": file_hash(model) if args.inpainter == "lama" else None,
        "inpainter": args.inpainter,
        "device": device,
        "opencv_threads": args.threads,
        "sensitivity": args.sensitivity,
        "peak_rss_scope": (
            "whole isolated child, including decoding and bounded quality metrics"
        ),
        "wall_scope": (
            "decode through quality metrics; "
            "excludes child startup and fixture generation"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, action="append", dest="cases")
    parser.add_argument(
        "--manifest", type=Path, help="JSON cases with local image paths"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    parser.add_argument("--threads", type=int, default=1, help="OpenCV worker threads")
    parser.add_argument("--sensitivity", type=int, default=50)
    parser.add_argument("--inpainter", choices=("cv2", "lama"), default="cv2")
    parser.add_argument(
        "--model", type=Path, help="existing local LaMa checkpoint; never downloaded"
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            print(json.dumps(run_case(json.load(sys.stdin))))
            return 0
        if args.repeat < 1 or args.threads < 1 or not 0 <= args.sensitivity <= 100:
            raise ValueError(
                "Repeat/threads must be positive and sensitivity between 0 and 100."
            )
        if args.manifest and args.cases:
            raise ValueError("Choose either --manifest or built-in --case values.")
        model = args.model or (
            Path(os.environ[MODEL_ENV]) if os.getenv(MODEL_ENV) else None
        )
        if args.inpainter == "lama" and (model is None or not model.is_file()):
            raise ValueError(
                "LaMa requires --model pointing at an existing checkpoint; "
                "downloads are disabled."
            )
        with tempfile.TemporaryDirectory(prefix="watermark_benchmark_") as temporary:
            cases = (
                read_manifest(args.manifest)
                if args.manifest
                else [
                    synthetic_case(name, Path(temporary))
                    for name in args.cases or CASES
                ]
            )
            runs = []
            for case in cases:
                for repeat in range(args.repeat):
                    spec = {
                        "case": case,
                        "threads": args.threads,
                        "sensitivity": args.sensitivity,
                        "inpainter": args.inpainter,
                        "model": str(model.resolve()) if model else None,
                    }
                    child = subprocess.run(
                        [sys.executable, str(Path(__file__).resolve()), "--worker"],
                        input=json.dumps(spec),
                        capture_output=True,
                        text=True,
                        env={
                            **os.environ,
                            "OPENBLAS_NUM_THREADS": "1",
                            "OMP_NUM_THREADS": "1",
                        },
                    )
                    if child.returncode:
                        raise ValueError(
                            f"Case {case['name']} failed: {child.stderr.strip()}"
                        )
                    runs.append({"repeat": repeat + 1, **json.loads(child.stdout)})
            report = json.dumps(
                {"metadata": metadata(args, cases, model), "runs": runs}, indent=2
            )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report + "\n")
        else:
            print(report)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
