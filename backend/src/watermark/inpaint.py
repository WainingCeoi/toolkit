"""Inpainters: fill the masked pixels from their surroundings.

Two engines behind one call shape, ``inpaint(rgb, mask) -> rgb``:

- ``cv2`` — cv2.inpaint (Telea). Instant and always available; fine on thin
  strokes, smeary on large regions.
- ``lama`` — the big-lama TorchScript checkpoint, the same weights the
  simple-lama-inpainting package wraps. That package itself pins numpy<2 and
  pillow<10, neither of which exists for Python 3.14, so the few lines of
  pre/post-processing live here instead and only torch is required (the
  optional ``watermark`` extra). The checkpoint (~200 MB) downloads into
  torch.hub's cache on first use.

torch is imported inside LamaInpainter, never at module level: the web app
must boot — and every other tool must work — without the extra installed.
"""

from __future__ import annotations

import importlib.util
import os
import warnings

import cv2
import numpy as np

INPAINTERS = ("lama", "cv2")

LAMA_URL = (
    "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/"
    "v0.1.0/big-lama.pt"
)

# Where LaMa runs: cpu (default), mps, cuda. cpu is the safe default — big-lama
# is fast enough per image, and mps support varies across torch releases.
DEVICE_ENV = "WATERMARK_DEVICE"
# Points at an already-downloaded big-lama.pt, for offline machines.
MODEL_ENV = "WATERMARK_LAMA_MODEL"

_CV2_RADIUS = 3


def lama_available() -> bool:
    """Whether the lama inpainter could run (torch installed), without importing it."""
    return importlib.util.find_spec("torch") is not None


def inpaint_cv2(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Telea inpainting: fast, dependency-free, good enough for thin strokes."""
    return cv2.inpaint(rgb, mask, _CV2_RADIUS, cv2.INPAINT_TELEA)


class LamaInpainter:
    """Callable like inpaint_cv2; loads the model on the first call."""

    def __init__(self, device: str | None = None) -> None:
        self.device = device or os.environ.get(DEVICE_ENV, "cpu")
        self._model = None

    def __call__(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import torch  # deferred: see module docstring

        if self._model is None:
            self._model = self._load(torch)

        # big-lama wants dimensions in multiples of 8; pad symmetrically and
        # crop the result back. Inputs are float [0, 1], mask strictly binary.
        h, w = rgb.shape[:2]
        pad_h, pad_w = (-h) % 8, (-w) % 8
        padded_rgb = np.pad(rgb, ((0, pad_h), (0, pad_w), (0, 0)), mode="symmetric")
        padded_mask = np.pad(mask, ((0, pad_h), (0, pad_w)), mode="symmetric")

        image = torch.from_numpy(padded_rgb).permute(2, 0, 1)[None].float() / 255.0
        binary = torch.from_numpy((padded_mask > 0).astype(np.float32))[None, None]
        with torch.inference_mode():
            out = self._model(image.to(self.device), binary.to(self.device))
        result = out[0].permute(1, 2, 0).detach().cpu().numpy()
        return np.clip(result * 255, 0, 255).astype(np.uint8)[:h, :w]

    def _load(self, torch):
        override = os.environ.get(MODEL_ENV)
        if override:
            path = override
        else:
            from pathlib import Path

            cache = Path(torch.hub.get_dir()) / "checkpoints"
            cache.mkdir(parents=True, exist_ok=True)
            path = cache / "big-lama.pt"
            if not path.is_file():
                torch.hub.download_url_to_file(LAMA_URL, str(path), progress=False)
        with warnings.catch_warnings():
            # torch.jit warns "not supported in Python 3.14+ and may break",
            # but the published big-lama artifact IS a TorchScript archive
            # (there is no torch.export copy of these weights to switch to)
            # and it demonstrably loads and runs — see test_watermark_lama.
            warnings.simplefilter("ignore", DeprecationWarning)
            model = torch.jit.load(path, map_location=self.device)
        return model.to(self.device).eval()


def get_inpainter(name: str):
    """The inpaint callable for ``name`` — one instance per batch, so LaMa
    loads once, not once per image."""
    if name == "cv2":
        return inpaint_cv2
    if name == "lama":
        return LamaInpainter()
    raise ValueError(
        f"Unknown inpainter {name!r} (choose from: {', '.join(INPAINTERS)})."
    )
