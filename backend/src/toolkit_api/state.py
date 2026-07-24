"""Shared per-process application state, built once in the app lifespan."""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    # Torrent Downloader's persistent manager. Unlike the job-shaped tools its
    # state outlives the process, so it is not a JobRegistry. None when the
    # state was injected (tests) or aria2 could not be reached.
    torrents: Any = None


def build_torrent_manager():
    """Attach to a running aria2, or spawn one we own.

    Only a daemon we started is ever shut down: an aria2 already running (for
    example under `brew services`) may be serving downloads this tool knows
    nothing about. Returns None when aria2 is not installed -- the tool then
    reports that through /api/torrent/status instead of failing at startup.
    """
    from toolkit_api.torrents import TorrentManager
    from toolkit_engine import aria2
    from toolkit_engine.torrentdb import TorrentStore

    state_dir = Path(config.DB_PATH).parent / "torrents"
    download_dir = Path.home() / "Downloads" / "toolkit-torrents"
    secret = os.environ.get("ARIA2_SECRET") or secrets.token_hex(16)

    rpc = aria2.Aria2RPC(secret=secret)
    owned = False
    if aria2.probe(rpc) is None:
        if not aria2.installed():
            return None
        aria2.spawn(state_dir=state_dir, download_dir=download_dir, secret=secret)
        owned = True
        for _ in range(50):  # the daemon needs a moment to bind the port
            if aria2.probe(rpc) is not None:
                break
            time.sleep(0.1)

    store = TorrentStore(state_dir / "torrents.db")
    return TorrentManager(store, rpc, download_dir=download_dir, owned=owned)


def build_state() -> AppState:
    return AppState(
        store=Store(config.DB_PATH),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        torrents=build_torrent_manager(),
    )
