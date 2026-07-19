"""Environment-driven settings, read lazily so tests can override them per-case."""

from __future__ import annotations

import os
from pathlib import Path

# Repo root, three levels up from src/lib/subgen/config.py — the database lives
# in <repo>/data/ so it sits at the Toolkit project root, not under src/.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Settings are read from the environment on each access (via module __getattr__)
# so tests can override them per-case without import-order surprises.


def __getattr__(name: str):
    if name == "DB_PATH":
        return Path(os.environ.get("SUB_DB_PATH") or (REPO_ROOT / "data" / "sub.db"))
    if name == "ACCESS_TOKEN":
        return os.environ.get("SUB_ACCESS_TOKEN", "")
    if name == "PUBLIC_HOST":
        return os.environ.get("SUB_PUBLIC_HOST", "")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
