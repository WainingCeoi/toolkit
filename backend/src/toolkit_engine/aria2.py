"""aria2c JSON-RPC client and daemon supervision.

aria2 is driven as an external daemon rather than an in-process library
because libtorrent publishes no cp314 wheel and this backend runs 3.14. It is
reached over loopback JSON-RPC with a shared-secret token.

The surface used is ~10 methods, so this is hand-rolled on `requests` (already
a project dependency) rather than pulling in aria2p, which is synchronous
anyway and issues three unfiltered requests per listing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import requests

RPC_PORT = 6800
RPC_URL = f"http://127.0.0.1:{RPC_PORT}/jsonrpc"

# These sit in the app's shared data folder, next to the databases, so the
# names say who owns them.
SESSION_FILENAME = "aria2-session.txt"
LOG_FILENAME = "aria2.log"

# Fields fetched for the dashboard. aria2 returns every field when `keys` is
# omitted, including the full file list for every torrent on every poll.
STATUS_KEYS = [
    "gid",
    "status",
    "infoHash",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "errorMessage",
    "following",
    "followedBy",
]


class Aria2Error(RuntimeError):
    """An aria2 RPC call failed, or the daemon could not be reached."""


class Aria2RPC:
    def __init__(
        self, url: str = RPC_URL, secret: str = "", timeout: float = 10.0
    ) -> None:
        self.url = url
        self.timeout = timeout
        self._token = f"token:{secret}"
        self._session = requests.Session()

    def call(self, method: str, *params):
        payload = {
            "jsonrpc": "2.0",
            "id": "toolkit",
            "method": method,
            "params": [self._token, *params],
        }
        try:
            response = self._session.post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise Aria2Error(f"aria2 is not reachable at {self.url}: {exc}") from exc

        body = response.json()
        if "error" in body:
            raise Aria2Error(body["error"].get("message", "unknown aria2 error"))
        return body["result"]

    # --- reads ------------------------------------------------------------
    def version(self) -> str:
        return self.call("aria2.getVersion")["version"]

    def tell_status(self, gid: str, keys: list[str] | None = None) -> dict:
        return self.call("aria2.tellStatus", gid, keys or STATUS_KEYS)

    def get_files(self, gid: str) -> list[dict]:
        return self.call("aria2.getFiles", gid)

    def tell_all(self) -> list[dict]:
        """Every download the daemon knows about, in one round trip."""
        results = self.call(
            "system.multicall",
            [
                {"methodName": "aria2.tellActive", "params": [STATUS_KEYS]},
                {"methodName": "aria2.tellWaiting", "params": [0, 1000, STATUS_KEYS]},
                {"methodName": "aria2.tellStopped", "params": [0, 1000, STATUS_KEYS]},
            ],
        )
        return [item for group in results for item in group[0]]

    # --- writes -----------------------------------------------------------
    def add_uri(self, uris: list[str], options: dict) -> str:
        return self.call("aria2.addUri", uris, options)

    def add_torrent(self, b64_data: str, options: dict) -> str:
        return self.call("aria2.addTorrent", b64_data, [], options)

    def pause(self, gid: str) -> None:
        self.call("aria2.forcePause", gid)

    def unpause(self, gid: str) -> None:
        self.call("aria2.unpause", gid)

    def remove(self, gid: str) -> None:
        self.call("aria2.forceRemove", gid)

    def save_session(self) -> None:
        self.call("aria2.saveSession")

    def shutdown(self) -> None:
        self.call("aria2.shutdown")


def probe(rpc: Aria2RPC) -> str | None:
    """Daemon version if reachable, else None. Never raises."""
    try:
        return rpc.version()
    except Aria2Error:
        return None


def installed() -> bool:
    return shutil.which("aria2c") is not None


def daemon_flags(
    *,
    state_dir: Path,
    download_dir: Path,
    secret: str,
    port: int = RPC_PORT,
) -> list[str]:
    """The full flag set. Every entry here is load-bearing -- see the spec."""
    session = state_dir / SESSION_FILENAME
    return [
        # RPC. pause-metadata below is a silent no-op without this.
        "--enable-rpc=true",
        "--rpc-listen-all=false",
        f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}",
        # Show a magnet's file list without fetching a byte of content.
        "--pause-metadata=true",
        # Persistence. aria2 does not auto-load its own session, hence
        # --input-file; the interval defaults to 0, i.e. "clean exit only".
        f"--save-session={session}",
        f"--input-file={session}",
        "--save-session-interval=30",
        "--auto-save-interval=30",
        "--continue=true",
        # Without these, every restart re-fetches metadata from the DHT.
        "--bt-save-metadata=true",
        "--bt-load-saved-metadata=true",
        # Stop at completion; this tool does not seed.
        "--seed-time=0",
        "--max-concurrent-downloads=3",
        f"--dir={download_dir}",
        f"--log={state_dir / LOG_FILENAME}",
        "--log-level=notice",
    ]


def spawn(
    *,
    state_dir: Path,
    download_dir: Path,
    secret: str,
    port: int = RPC_PORT,
) -> subprocess.Popen:
    """Launch aria2c as a background daemon we own."""
    exe = shutil.which("aria2c")
    if exe is None:
        raise Aria2Error(
            "aria2 is not installed or not on PATH. Install it with "
            "`brew install aria2`."
        )

    state_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    # --input-file errors at startup if the path does not exist yet.
    (state_dir / SESSION_FILENAME).touch(exist_ok=True)

    flags = daemon_flags(
        state_dir=state_dir, download_dir=download_dir, secret=secret, port=port
    )
    return subprocess.Popen(
        [exe, *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
