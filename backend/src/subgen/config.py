"""Environment-driven settings, read lazily so tests can override them per-case."""

from __future__ import annotations

import os
from pathlib import Path

# The default DB lives under <repo>/data; toolkit_api.main points it at backend/data.
REPO_ROOT = Path(__file__).resolve().parents[3]


def __getattr__(name: str):
    if name == "DB_PATH":
        return Path(os.environ.get("SUB_DB_PATH") or (REPO_ROOT / "data" / "sub.db"))
    if name == "ACCESS_TOKEN":
        return os.environ.get("SUB_ACCESS_TOKEN", "")
    if name == "PUBLIC_HOST":
        return os.environ.get("SUB_PUBLIC_HOST", "")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
