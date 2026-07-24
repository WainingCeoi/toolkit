"""Shared per-process application state, built once in the app lifespan."""

from __future__ import annotations

import os
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
    # state was injected (tests) or aria2 could not be reached.
    torrents: Any = None


def build_torrent_manager():
    """Attach to a running aria2, or spawn one we own.

    Ownership decides what happens on shutdown, and the four cases are:

    - reachable + our PID file names it  -> our daemon (this run's, or an
      orphan a previous unclean exit left behind). owned=True, stopped on close.
    - reachable + no PID file            -> external (e.g. `brew services`
      aria2 with a matching ARIA2_SECRET). owned=False, never touched.
    - not reachable + port free          -> spawn one. owned=True.
    - not reachable + port occupied      -> someone else's daemon on 6800 with
      a different secret. Do NOT spawn a second onto the taken port (it would
      fail to bind and stall startup); attach unreachable and let /status
      report the conflict. owned=False.

    Returns None only when aria2 is not installed and nothing is running, so
    the tool reports that through /status rather than failing the whole app.
    """
    from toolkit_api.torrents import DEFAULT_SAVE_DIR, TorrentManager
    from toolkit_engine import aria2
    from toolkit_engine.torrentdb import TorrentStore

    # Same folder as the subscription DB, not a subfolder of it: one data
    # directory for the whole app. Derived from DB_PATH so SUB_DB_PATH moves
    # both databases together. Separate FILE though -- subgen's Store owns a
    # subscriptions schema, and the two tools share nothing.
    data_dir = Path(config.DB_PATH).parent
    # Display form ("~/Downloads") for the manager's default and every new
    # row; the daemon's own --dir needs the real path, since aria2 gets no
    # shell to expand a tilde.
    download_dir = DEFAULT_SAVE_DIR
    daemon_dir = Path(DEFAULT_SAVE_DIR).expanduser()
    pid_file = data_dir / aria2.PID_FILENAME

    # Persisted, not random-per-boot: a restart must reconnect to a daemon a
    # previous run left behind rather than being rejected by it and hanging.
    secret = os.environ.get("ARIA2_SECRET") or aria2.read_or_create_secret(
        data_dir / aria2.SECRET_FILENAME
    )

    # Short timeout so bring-up can never stall the web server's startup.
    rpc = aria2.Aria2RPC(secret=secret, timeout=aria2.BRINGUP_TIMEOUT)
    owned = False

    if aria2.probe(rpc) is not None:
        owned = aria2.pid_file_names_a_live_process(pid_file)
    elif not aria2.installed():
        return None  # nothing running and nothing to run; /status says so
    elif aria2.port_is_open():
        pass  # occupied by a daemon we can't authenticate against; /status warns
    else:
        proc = aria2.spawn(state_dir=data_dir, download_dir=daemon_dir, secret=secret)
        aria2.write_pid(pid_file, proc.pid)
        owned = True
        aria2.wait_until_ready(rpc)

    # Steady-state calls get the normal, more forgiving timeout.
    rpc.timeout = aria2.Aria2RPC().timeout
    store = TorrentStore(data_dir / "torrents.db")
    return TorrentManager(
        store,
        rpc,
        download_dir=download_dir,
        owned=owned,
        pid_file=pid_file if owned else None,
    )


def build_state() -> AppState:
    return AppState(
        store=Store(config.DB_PATH),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        torrents=build_torrent_manager(),
    )
