"""Shared per-process application state, built once in the app lifespan."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from subgen import config
from subgen.db import Store

from .artifacts import ArtifactStore
from .devices import Device, DeviceBook
from .jobs import JobRegistry
from .purgescans import PurgeScans
from .watermarks import WatermarkBatches


@dataclass
class AppState:
    store: Store
    jobs: JobRegistry
    artifacts: ArtifactStore
    # The live Selenium session; typed Any so tests never import selenium.
    browser: Any = None
    # LibreOffice shares one user profile, so conversions must not overlap.
    soffice_lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializes every check-then-set of ``browser``.
    browser_lock: threading.Lock = field(default_factory=threading.Lock)
    # The TorrentManager; None when injected (tests) or BitComet is unconfigured.
    torrents: Any = None
    devices: DeviceBook | None = None
    # With torrents_users, lets a replaced manager close only once nobody uses it.
    torrents_lock: threading.Lock = field(default_factory=threading.Lock)
    # id(manager) -> in-flight requests; safe because each holder keeps a reference.
    torrents_users: dict[int, int] = field(default_factory=dict)
    watermarks: WatermarkBatches | None = None
    purge_scans: PurgeScans = field(default_factory=PurgeScans)


def data_dir() -> Path:
    """The one data directory; derived from DB_PATH so SUB_DB_PATH moves it all."""
    return Path(config.DB_PATH).parent


def build_torrent_manager(device: Device | None = None):
    """Build a lazy client for a BitComet; None when its config cannot be read."""
    from toolkit_api.torrents import DEFAULT_SAVE_DIR, TorrentManager
    from toolkit_engine.bitcomet import (
        REMOTE_TIMEOUT,
        BitCometClient,
        BitCometError,
    )

    # Persisted so BitComet sees one paired device across restarts, not one per boot.
    device_id_file = data_dir() / "bitcomet-device-id"

    if device is None or device.is_local:
        try:
            client = BitCometClient.from_config(device_id_file=device_id_file)
        except BitCometError:
            return None
    else:
        try:
            client = BitCometClient(
                base_url=device.url or "",
                username=device.username,
                password=device.password,
                # A busy LAN BitComet is slow; the default 10s fails batch sends.
                timeout=REMOTE_TIMEOUT,
                device_id_file=device_id_file,
            )
        except BitCometError:
            # A stored address that no longer parses; /status reports it.
            return None

    return TorrentManager(client, download_dir=DEFAULT_SAVE_DIR)


def use_device(state: AppState, device: Device) -> None:
    """Point the tool at ``device``, closing the client it was using."""
    with state.torrents_lock:
        previous = state.torrents
        state.torrents = build_torrent_manager(device)
        # Close here only if idle; otherwise the last in-flight request closes it.
        idle = previous is not None and id(previous) not in state.torrents_users
    if idle and previous is not None:
        previous.close()


def build_state() -> AppState:
    # One database per tool; torrents.db also holds the device book.
    devices = DeviceBook(data_dir() / "torrents.db")
    return AppState(
        store=Store(config.DB_PATH),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        devices=devices,
        torrents=build_torrent_manager(devices.active()),
        watermarks=WatermarkBatches(data_dir() / "watermark"),
    )
