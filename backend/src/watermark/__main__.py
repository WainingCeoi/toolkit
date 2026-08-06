"""Headless CLI — clean a folder of images without the web app:

    python -m watermark clean IN_DIR OUT_DIR --inpainter lama|cv2

Only edit images you own or are licensed to edit.
"""

from __future__ import annotations

import argparse
import sys

from .detect import DEFAULT_DETECTOR, DEFAULT_SENSITIVITY, DETECTORS
from .inpaint import INPAINTERS, lama_available
from .pipeline import DEFAULT_DILATE_PX, clean_folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m watermark",
        description="Detect and inpaint watermarks out of images.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    clean = commands.add_parser(
        "clean", help="auto-mask and inpaint every image in a folder"
    )
    clean.add_argument("in_dir", help="folder of png/jpg/webp images to clean")
    clean.add_argument("out_dir", help="folder the cleaned PNGs are written to")
    clean.add_argument(
        "--inpainter",
        choices=INPAINTERS,
        default="lama",
        help="lama: best quality, needs torch + a one-time ~200 MB model "
        "download; cv2: instant, no extras (default: lama)",
    )
    clean.add_argument(
        "--sensitivity",
        type=int,
        default=DEFAULT_SENSITIVITY,
        metavar="0-100",
        help=f"how eagerly to mask (default: {DEFAULT_SENSITIVITY})",
    )
    clean.add_argument(
        "--detector",
        choices=DETECTORS,
        default=DEFAULT_DETECTOR,
        help="texture: judge each pixel against its neighbourhood, works on "
        "anything; pattern: recover a repeating watermark and mask only its "
        "instances — far more precise when the mark really is tiled, and it "
        f"falls back to texture per image when it is not (default: {DEFAULT_DETECTOR})",
    )
    clean.add_argument(
        "--dilate",
        type=int,
        default=DEFAULT_DILATE_PX,
        metavar="PX",
        help=f"grow the mask before inpainting (default: {DEFAULT_DILATE_PX})",
    )
    args = parser.parse_args(argv)

    if args.inpainter == "lama" and not lama_available():
        parser.error(
            "the lama inpainter needs torch — install the backend's "
            "`watermark` extra (uv sync --extra watermark), or pass "
            "--inpainter cv2"
        )

    def on_progress(done: int, total: int) -> bool:
        print(f"[{done}/{total}] inpainted")
        return False

    try:
        cleaned, skipped, failed = clean_folder(
            args.in_dir,
            args.out_dir,
            inpainter=args.inpainter,
            sensitivity=args.sensitivity,
            dilate_px=args.dilate,
            detector=args.detector,
            on_progress=on_progress,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not cleaned and not failed and not skipped:
        print(f"error: no png/jpg/webp images in {args.in_dir}", file=sys.stderr)
        return 2
    print(f"cleaned {len(cleaned)} image(s) -> {args.out_dir}")
    if skipped:
        print(f"skipped {len(skipped)} image(s) with no watermark found:")
        for name in skipped:
            print(f"  {name}")
    for name, error in failed:
        print(f"failed: {name}: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
