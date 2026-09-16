"""Inpainters: fill the masked pixels from their surroundings."""

from __future__ import annotations

import importlib.util
import os
import threading
import warnings

import cv2
import numpy as np

INPAINTERS = ("lama", "cv2")

# The simple-lama-inpainting weights; that package pins numpy<2 (no py3.14).
LAMA_URL = (
    "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/"
    "v0.1.0/big-lama.pt"
)

# Pins where LaMa runs (cpu, mps, cuda), overriding the automatic choice.
DEVICE_ENV = "WATERMARK_DEVICE"
# Points at an already-downloaded big-lama.pt, for offline machines.
MODEL_ENV = "WATERMARK_LAMA_MODEL"

_CV2_RADIUS = 3


def lama_available() -> bool:
    """Whether the lama inpainter could run (torch installed), without importing it."""
    return importlib.util.find_spec("torch") is not None


_device: str | None = None


def resolve_device() -> str:
    """The device LaMa runs on; cached because probing imports torch."""
    global _device
    override = os.environ.get(DEVICE_ENV)
    if override:
        return override
    if _device is not None:
        return _device
    if not lama_available():
        return "cpu"
    import torch

    if torch.cuda.is_available():
        _device = "cuda"
    elif torch.backends.mps.is_available():
        _device = "mps"
    else:
        _device = "cpu"
    return _device


def inpaint_cv2(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Telea inpainting: fast, dependency-free, good enough for thin strokes."""
    return cv2.inpaint(rgb, mask, _CV2_RADIUS, cv2.INPAINT_TELEA)


# Loaded TorchScript models by device, so a rerun does not re-read the checkpoint.
_models: dict[str, object] = {}
# Held across the download too: two jobs starting together want the same file.
_models_lock = threading.Lock()


class LamaInpainter:
    """Callable like inpaint_cv2; loads the model on the first call."""

    def __init__(self, device: str | None = None) -> None:
        self.device = device or resolve_device()
        self._model = None

    def load(self) -> None:
        """Fetch and load the checkpoint now, instead of on the first call."""
        import torch  # deferred: torch is an optional extra

        if self._model is not None:
            return
        with _models_lock:
            cached = _models.get(self.device)
            if cached is None:
                cached = _models[self.device] = self._load(torch)
        self._model = cached

    def __call__(self, rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import torch  # deferred: torch is an optional extra

        self.load()

        # big-lama needs dimensions in multiples of 8; the padding is cropped back.
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
            # torch.jit's 3.14 deprecation warning; the checkpoint is TorchScript-only.
            warnings.simplefilter("ignore", DeprecationWarning)
            model = torch.jit.load(path, map_location=self.device)
        return model.to(self.device).eval()


def get_inpainter(name: str):
    """The inpaint callable for ``name``."""
    if name == "cv2":
        return inpaint_cv2
    if name == "lama":
        return LamaInpainter()
    raise ValueError(
        f"Unknown inpainter {name!r} (choose from: {', '.join(INPAINTERS)})."
    )
