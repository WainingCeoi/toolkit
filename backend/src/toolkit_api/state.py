"""Shared per-process application state, built once in the app lifespan."""

from __future__ import annotations

import threading
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
    # state was injected (tests) or BitComet's settings could not be read.
    torrents: Any = None


def build_torrent_manager():
    """Build a client for the user's BitComet. Starts nothing, owns nothing.

    BitComet is a desktop application the user installs, launches and quits, so
    there is no daemon to spawn or adopt here and no liveness question worth
    asking at startup -- the client logs in lazily, and /status is what reports
    whether BitComet is answering right now.

    Returns None only when BitComet's config is unreadable (not installed, or
    remote access never configured), so the tool reports that through /status
    rather than failing the whole app.
    """
    from toolkit_api.torrents import DEFAULT_SAVE_DIR, TorrentManager
    from toolkit_engine.bitcomet import BitCometClient, BitCometError
    from toolkit_engine.torrentdb import TorrentStore

    try:
        client = BitCometClient.from_config()
    except BitCometError:
        return None

    # Same folder as the subscription DB, not a subfolder of it: one data
    # directory for the whole app. Derived from DB_PATH so SUB_DB_PATH moves
    # both databases together. Separate FILE though -- subgen's Store owns a
    # subscriptions schema, and the two tools share nothing.
    data_dir = Path(config.DB_PATH).parent
    store = TorrentStore(data_dir / "torrents.db")
    return TorrentManager(store, client, download_dir=DEFAULT_SAVE_DIR)


def build_state() -> AppState:
    return AppState(
        store=Store(config.DB_PATH),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        torrents=build_torrent_manager(),
    )
