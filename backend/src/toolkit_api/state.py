"""Shared per-process application state, built once in the app lifespan."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from subgen import config
from subgen.db import Store

from .artifacts import ArtifactStore
from .jobs import JobRegistry


@dataclass
class AppState:
    store: Store
    jobs: JobRegistry
    artifacts: ArtifactStore
    # Web Images to PDF holds one live Selenium session at a time (set lazily
    # by its router; typed loosely so tests never import selenium).
    browser: Any = None
    # LibreOffice conversions share one user profile, so they must not run
    # concurrently — Doc to PDF serializes on this lock.
    soffice_lock: threading.Lock = field(default_factory=threading.Lock)
    # Guards the single browser slot against read-check-then-set races
    # (double-click / retry): /webpdf/open, /close, and /capture serialize
    # their check-and-mutate of `browser` on this lock.
    browser_lock: threading.Lock = field(default_factory=threading.Lock)


def build_state() -> AppState:
    return AppState(
        store=Store(config.DB_PATH),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
    )
