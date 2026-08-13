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
    # Which BitComet the manager above is pointed at, and the other saved ones
    # it can be switched to. None only when an injected state left it out.
    devices: DeviceBook | None = None
    # Switching devices swaps `torrents` for a manager holding a different HTTP
    # session and closes the old one. Serializing the read-and-replace is not
    # enough on its own: a request reads the manager once and then talks
    # through it for its whole duration, which for a slow device is tens of
    # seconds. Both this lock and the in-flight count below are what let the
    # replaced manager be closed only when nobody is still using it.
    torrents_lock: threading.Lock = field(default_factory=threading.Lock)
    # id(manager) -> number of requests currently holding it. Keyed by id
    # because each holder keeps a strong reference for the whole scope, so the
    # id cannot be recycled underneath its own entry.
    torrents_users: dict[int, int] = field(default_factory=dict)
    # Watermark Remover's upload staging: normalized working copies on disk,
    # TTL-swept. None only when an injected state left it out.
    watermarks: WatermarkBatches | None = None
    # What each Cache Purge scan found. Delete names a scan, not a file list,
    # so the paths it removes are always ones this server chose.
    purge_scans: PurgeScans = field(default_factory=PurgeScans)


def data_dir() -> Path:
    """The app's one data directory.

    Same folder as the subscription DB, not a subfolder of it, and derived from
    DB_PATH so SUB_DB_PATH moves everything together.
    """
    return Path(config.DB_PATH).parent


def build_torrent_manager(device: Device | None = None):
    """Build a client for a BitComet. Starts nothing, owns nothing.

    BitComet is a desktop application the user installs, launches and quits, so
    there is no daemon to spawn or adopt here and no liveness question worth
    asking at startup -- the client logs in lazily, and /status is what reports
    whether BitComet is answering right now. That holds doubly for a device on
    the LAN, which may be asleep when this app boots and awake ten minutes
    later; connecting eagerly would only turn that into a startup failure.

    There is no database. The tool dispatches tasks to BitComet and BitComet
    keeps them, so the only thing this app could store is a stale second
    opinion about state it does not own.

    Returns None only for the LOCAL device with an unreadable config (BitComet
    not installed, or remote access never configured), so the tool reports that
    through /status rather than failing the whole app. A remote device always
    builds: its address and credentials were given to us, so there is nothing
    to fail on until the first call.
    """
    from toolkit_api.torrents import DEFAULT_SAVE_DIR, TorrentManager
    from toolkit_engine.bitcomet import (
        REMOTE_TIMEOUT,
        BitCometClient,
        BitCometError,
    )

    # The device id lives here so BitComet sees the same paired device across
    # restarts instead of collecting one entry per boot.
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
                # The patient budget: a LAN BitComet mid-way through starting a
                # batch of tasks answers slowly without being unhealthy, and at
                # the default 10s a batch send fails task after task against a
                # client that is merely busy. See REMOTE_TIMEOUT.
                timeout=REMOTE_TIMEOUT,
                device_id_file=device_id_file,
            )
        except BitCometError:
            # A stored address that no longer parses (hand-edited file). The
            # tool reports it through /status like any other unreachable one.
            return None

    return TorrentManager(client, download_dir=DEFAULT_SAVE_DIR)


def use_device(state: AppState, device: Device) -> None:
    """Point the tool at `device`, closing the client it was using.

    Closing matters: the old manager holds a live HTTP session (and, for a
    device that was reachable, a device_token BitComet is tracking). Dropping
    the reference without closing leaks the socket for as long as the process
    runs, and switching back and forth is exactly the thing a user with two
    machines does repeatedly.
    """
    with state.torrents_lock:
        previous = state.torrents
        state.torrents = build_torrent_manager(device)
        # Only close it here if no request is mid-call through it; otherwise the
        # last one out does (see deps.get_torrents). Closing under an in-flight
        # request tore down the session it was still reading from.
        idle = previous is not None and id(previous) not in state.torrents_users
    if idle and previous is not None:
        previous.close()


def build_state() -> AppState:
    # The torrent tool's own database. The file name predates the device book
    # (the pre-dispatcher queue lived here), which is exactly why it is reused:
    # one database per tool, whatever tables the tool currently needs. A JSON
    # book from the previous revision is imported on first open -- see
    # DeviceBook._import_legacy.
    devices = DeviceBook(data_dir() / "torrents.db")
    return AppState(
        store=Store(config.DB_PATH),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        devices=devices,
        # Whichever device was selected last time, so the choice survives a
        # restart the way the rest of BitComet's state does.
        torrents=build_torrent_manager(devices.active()),
        # Same one-data-directory convention as the BitComet device id above.
        watermarks=WatermarkBatches(data_dir() / "watermark"),
    )
