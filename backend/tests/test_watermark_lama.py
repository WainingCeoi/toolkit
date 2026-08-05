"""LaMa inpainting, for real: torch plus the big-lama checkpoint (~200 MB).

Everything here is behind the slow marker, deselected by default (the first
run downloads the checkpoint into torch.hub's cache). Run with:

    uv sync --extra watermark
    uv run pytest -m slow
"""

from __future__ import annotations

import numpy as np
import pytest
from test_watermark_engine import synthetic_pair

from watermark.inpaint import LamaInpainter, lama_available
from watermark.pipeline import remove_watermark

pytestmark = pytest.mark.slow

if not lama_available():
    pytest.skip(
        "torch is not installed — uv sync --extra watermark",
        allow_module_level=True,
    )


def test_lama_erases_a_synthetic_watermark_better_than_it_found_it():
    clean, marked, true_mask = synthetic_pair(size=(200, 160))
    cleaned = remove_watermark(marked, true_mask, LamaInpainter())
    assert cleaned.shape == marked.shape
    assert cleaned.dtype == np.uint8

    region = true_mask > 0
    err_before = np.abs(marked[region].astype(int) - clean[region]).mean()
    err_after = np.abs(cleaned[region].astype(int) - clean[region]).mean()
    assert err_after < err_before * 0.5


def test_lama_pads_odd_sizes_and_crops_them_back():
    # 8-multiple padding must be invisible to the caller: a 201×157 image
    # comes back 201×157.
    _clean, marked, true_mask = synthetic_pair(size=(201, 157))
    cleaned = remove_watermark(marked, true_mask, LamaInpainter())
    assert cleaned.shape == marked.shape
