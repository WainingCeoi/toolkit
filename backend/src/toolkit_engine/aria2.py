"""aria2c JSON-RPC client and daemon supervision.

aria2 is driven as an external daemon rather than an in-process library
because libtorrent publishes no cp314 wheel and this backend runs 3.14. It is
reached over loopback JSON-RPC with a shared-secret token.

The surface used is ~10 methods, so this is hand-rolled on `requests` (already
a project dependency) rather than pulling in aria2p, which is synchronous
anyway and issues three unfiltered requests per listing.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import requests

RPC_PORT = 6800
RPC_URL = f"http://127.0.0.1:{RPC_PORT}/jsonrpc"

# Short timeout while bringing the daemon up, so a wrong/occupied port can
# never stall the whole web server's startup; steady-state calls use the
# longer default on Aria2RPC.
BRINGUP_TIMEOUT = 1.5
# How long to wait for a freshly spawned daemon to bind its RPC port.
READY_TIMEOUT = 5.0

# These sit in the app's shared data folder, next to the databases, so the
# names say who owns them.
SESSION_FILENAME = "aria2-session.txt"
LOG_FILENAME = "aria2.log"
SECRET_FILENAME = "aria2-secret"
PID_FILENAME = "aria2.pid"

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
        # aria2 is always on loopback. A configured HTTP proxy (env vars or the
        # macOS system proxy -- likely on a machine that also runs a proxy
        # subscription tool) would otherwise intercept 127.0.0.1 and answer
        # with its own non-JSON error page, which is neither the daemon nor a
        # connection error. trust_env=False keeps these calls off any proxy.
        self._session.trust_env = False

    def call(self, method: str, *params):
        payload = {
            "jsonrpc": "2.0",
            "id": "toolkit",
            "method": method,
            "params": [self._token, *params],
        }
        try:
            response = self._session.post(self.url, json=payload, timeout=self.timeout)
            # A non-2xx (e.g. a proxy's 503, or aria2 mid-restart) is "not
            # reachable", not a crash -- raise_for_status makes it a
            # RequestException handled below.
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise Aria2Error(f"aria2 is not reachable at {self.url}: {exc}") from exc
        except ValueError as exc:  # body was not JSON (json.JSONDecodeError)
            raise Aria2Error(
                f"aria2 returned a non-JSON response from {self.url}: {exc}"
            ) from exc

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
        # Detached so an editor-triggered `uvicorn --reload` restart does not
        # take the daemon (and its in-flight downloads) down with it. The cost
        # is that an unclean exit orphans it; the PID file below is how the
        # next boot re-adopts that orphan instead of fighting it for the port.
        start_new_session=True,
    )


# =======================================================
# DAEMON LIFECYCLE
# =======================================================
def port_is_open(host: str = "127.0.0.1", port: int = RPC_PORT) -> bool:
    """True if something is already listening on the RPC port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def read_or_create_secret(path: Path) -> str:
    """Return a stable RPC secret, generating and persisting one if needed.

    Persisted, not random-per-boot, so a restart authenticates against a
    daemon a previous run left behind (an unclean-exit orphan, or the same
    daemon across a reload) rather than being rejected and stalling.
    """
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_hex(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return token


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid))


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except OSError, ValueError:
        return None


def pid_is_alive(pid: int) -> bool:
    """True if a process with this PID exists (whether or not we own it)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def pid_file_names_a_live_process(path: Path) -> bool:
    """True when the PID file points at a process still running.

    This is the ownership marker: a daemon reachable with our secret AND named
    by our PID file is one we spawned (so we manage and stop it); a daemon
    reachable with our secret but with no PID file is external -- e.g. a
    `brew services` aria2 the user set ARIA2_SECRET for -- and is left alone.
    """
    pid = read_pid(path)
    return pid is not None and pid_is_alive(pid)


def wait_until_ready(rpc: Aria2RPC, timeout: float = READY_TIMEOUT) -> bool:
    """Poll until the daemon answers, or the (bounded) timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(rpc) is not None:
            return True
        time.sleep(0.1)
    return False


def stop_process(path: Path, timeout: float = 3.0) -> None:
    """Stop the daemon named by the PID file, then remove the file.

    A graceful aria2.shutdown RPC is the primary path; this is the fallback
    that guarantees a daemon we own is actually gone on close even if the RPC
    did not land, so it cannot linger holding the port.
    """
    pid = read_pid(path)
    if pid is not None and pid_is_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and pid_is_alive(pid):
            time.sleep(0.1)
        if pid_is_alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        path.unlink()
