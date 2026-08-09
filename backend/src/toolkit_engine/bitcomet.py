"""BitComet WebUI client: credentials, login envelope, and the JSON API.

BitComet is this app's torrent engine. It is a desktop client the user already
installs, runs and configures, so there is no daemon to spawn, no session file
to keep in sync and no orphan process to adopt -- this module is a client and
nothing else. It also answers questions a bare BitTorrent RPC cannot: a native
ETA, swarm health, and a real seeding lifecycle.

Almost none of the endpoints below appear in BitComet's published API
reference. They were verified live against BitComet 2.20 on loopback, so
version drift is a genuine risk: probe() exists to make that visible early,
and every quirk that would otherwise fail SILENTLY is commented at the point
it is handled rather than merely worked around.

The surface used is ~10 calls, so this is hand-rolled on `requests` (already a
project dependency) rather than pulling in a client library for it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# BitComet writes its settings here on every change. See read_credentials for
# why we read this file instead of storing our own copy of the credentials.
CONFIG_PATH = (
    Path.home() / "Library" / "Application Support" / "BitComet" / "BitComet.xml"
)
DEFAULT_PORT = 19377

# Sent on every request; BitComet keys its remote-access sessions off it.
CLIENT_TYPE = "BitComet WebUI"
DEVICE_NAME = "Toolkit"
PLATFORM = "webui"

# The four priorities BitComet accepts. "disabled" is how a file is DESELECTED
# -- the UI greys it out and never downloads it. The obvious-looking "none" is
# rejected outright, so a caller reaching for it gets a clear error here rather
# than a torrent that quietly downloads everything.
PRIORITIES = ("very_high", "high", "normal", "disabled")
DESELECTED = "disabled"
# ...and the priority that undoes it. There is no separate "selected" flag, so
# re-ticking a file means giving it a downloading priority again.
SELECTED = "normal"

# The verified verbs for /api_v2/tasks/action. This app only uses start/stop.
ACTIONS = ("start", "stop", "hash_check", "tracker_update")

# "skipped" is what /api_v2/tasks/action answers when the task is ALREADY in the
# state asked for -- start on a running task, stop on a stopped one. That is a
# successful no-op, not a failure, and treating it as an error is actively
# harmful: commit() would raise after its set_priority calls had already landed,
# leaving the store and BitComet permanently disagreeing about the selection.
_SUCCESS_CODES = frozenset({"OK", "SKIPPED"})

# Used only when BitComet does not report torrent_max_size (an older build, or
# a reply we could not read). The live value is read from
# /api/config/new_task/get -- 20 MB is what 2.20 happens to ship, not a rule.
DEFAULT_TORRENT_MAX_SIZE = 20 * 1024 * 1024

# Deliberately impatient, for probe() only. A BitComet that is WEDGED rather
# than absent accepts the connection and then never answers, so it cannot be
# told apart from a healthy one by connecting -- only by waiting. The page asks
# /status the moment it loads, and at the steady-state timeout that wedged case
# would sit there for ten seconds before admitting anything is wrong.
PROBE_TIMEOUT = 1.5

# ...and the same impatience over the LAN would be a bug. A loopback round trip
# is sub-millisecond, but a peer across the Wi-Fi has to be found (ARP, or an
# mDNS lookup for a `.local` name) before the first byte moves, and a sleeping
# machine answers only after it wakes. At 1.5s a perfectly healthy NAS reads as
# "not running", so remote probes get a budget sized for a network instead.
REMOTE_PROBE_TIMEOUT = 4.0

# The STEADY-STATE budget splits the same way, and for a different reason than
# distance. Starting a task is not free for BitComet: it allocates the files,
# hash-checks whatever is on disk and reaches for the swarm, and while a batch
# of fresh starts grinds through that its web server can take tens of seconds
# over a single call. Measured live, batch-sending 34 tasks to a LAN peer:
# most sends "failed", and every failure was this client's 10s read timeout
# against a BitComet that was merely busy -- the tasks themselves were fine.
# A timeout should mean absent, not working hard, so remote clients get a
# budget sized for the grind. Loopback keeps 10s: the same grind exists there,
# but no user-visible path waits on a local call this long without wanting to
# know sooner.
REMOTE_TIMEOUT = 30.0

# --- login envelope byte layout ------------------------------------------
HEADER_LEN = 34
MAC_LEN = 32
ITERATIONS = 10_000


class BitCometError(RuntimeError):
    """A BitComet API call failed, or the client could not be reached."""


# =======================================================
# ADDRESSES
# =======================================================
# BitComet's remote access answers on the LAN, not only on loopback, so this
# app can hand a task to the BitComet running on another machine on the same
# Wi-Fi -- a NAS, a desktop, the machine that actually has the disk space. What
# arrives from the UI is whatever the user typed, so it is normalised here,
# once, into the `scheme://host:port` form every call is built from.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _bracketed(host: str) -> str:
    """An IPv6 literal needs its brackets back before it can go in a URL."""
    return f"[{host}]" if ":" in host else host


def is_local_host(host: str) -> bool:
    """True when `host` names THIS machine's loopback interface.

    The distinction is not cosmetic. A save folder on loopback is a directory
    this process can create; the same string aimed at a machine across the room
    names a path on ITS disk, which this process must not touch -- see
    ensure_save_folder. Anything not provably loopback is treated as remote,
    because the cost of guessing wrong that way is a longer timeout, while
    guessing wrong the other way is a stray directory on the wrong filesystem.
    """
    name = host.strip().strip("[]").lower()
    if name in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_base_url(raw: str) -> str:
    """Whatever the user typed -> `http://host:port`, or a sentence saying why not.

    Accepts the forms people actually paste: a bare host, host:port, a full URL,
    a URL with the Web UI's own path still on the end. Everything after the
    authority is dropped -- every API path this module calls is absolute from
    the root, so a leftover `/webui/index.html` would corrupt all of them.

    Two traps are handled explicitly because both fail in a way that names the
    wrong problem:

    * `nas:19377` parses as the SCHEME `nas` with path `19377`, not as a host
      and a port, so it is only ever read as a URL once a scheme is in front.
    * a missing port is not an error but a silent connection refused, because
      nothing is listening on 80 -- so the default port is filled in instead.
    """
    text = (raw or "").strip()
    if not text:
        raise BitCometError("Enter the address of the BitComet to connect to.")
    if "://" not in text:
        text = f"http://{text.lstrip('/')}"

    parsed = urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise BitCometError(
            f"{parsed.scheme}:// is not a Web UI address -- use http:// or https://"
        )
    if parsed.username or parsed.password:
        raise BitCometError(
            "Leave the username and password out of the address; there are "
            "fields for them."
        )
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:  # a port that is not a number, or out of range
        raise BitCometError(f"{raw.strip()!r} has no usable port: {exc}") from exc
    if not host:
        raise BitCometError(f"{raw.strip()!r} does not name a host.")
    return f"{parsed.scheme}://{_bracketed(host)}:{port or DEFAULT_PORT}"


# =======================================================
# CREDENTIALS
# =======================================================
@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    port: int = DEFAULT_PORT

    @property
    def base_url(self) -> str:
        # This machine's own BitComet only. Reaching another one goes through
        # an explicitly configured address and its own credentials, because
        # BitComet's config file is the only place the password lives and that
        # file is on the other machine (see read_credentials).
        return f"http://127.0.0.1:{self.port}"


def read_credentials(path: Path = CONFIG_PATH) -> Credentials:
    """Read the WebUI username, password and port from BitComet's own config.

    These are the user's settings, editable at any moment in BitComet's
    Preferences. Keeping a second copy in this app's config would mean the two
    drift the first time they change one -- and the symptom of that drift is an
    opaque 401 with nothing on screen explaining why. There is exactly one
    place the truth lives and it is not ours, so read it, every time.

    The path is a parameter so tests can point at a fixture instead of the
    developer's real BitComet install.
    """
    try:
        root = ET.parse(path).getroot()
    except OSError as exc:
        raise BitCometError(
            f"BitComet's config is not readable at {path}. Is BitComet installed?"
        ) from exc
    except ET.ParseError as exc:
        raise BitCometError(
            f"BitComet's config at {path} is not valid XML: {exc}"
        ) from exc

    def setting(name: str) -> str:
        node = root.find(f".//{name}")
        return (node.text or "").strip() if node is not None else ""

    username = setting("WebInterfaceUsername")
    password = setting("WebInterfacePassword")
    if not username or not password:
        raise BitCometError(
            "BitComet has no Web UI username/password set. Turn on "
            "Options -> Remote Access and set both, then try again."
        )

    try:
        port = int(setting("WebInterfacePort"))
    except ValueError:
        port = DEFAULT_PORT
    return Credentials(username=username, password=password, port=port)


# =======================================================
# LOGIN ENVELOPE
# =======================================================
# Reimplemented from the shipped WebUI bundle's CryptoJS AES_Encrypt. All
# offsets in bytes:
#
#     [0:2]    0x03 0x01            version marker
#     [2:10]   salt_key   (8)       PBKDF2 salt for the AES key
#     [10:18]  salt_mac   (8)       PBKDF2 salt for the HMAC key
#     [18:34]  iv         (16)      AES-CBC IV
#     [34:-32] ciphertext           AES-256-CBC / PKCS7 of the JSON credentials
#     [-32:]   hmac       (32)      HMAC-SHA256 over everything preceding it
#
# Both keys are PBKDF2-HMAC-SHA1(client_id, salt, 10_000 iterations, 32 bytes)
# and the whole blob is base64'd. The "password" is the client_id -- a UUID the
# client invents and then sends in the clear next to the ciphertext -- so this
# is obfuscation, not transport security, and there is nothing to protect by
# deriving it any other way.
def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), salt, ITERATIONS, dklen=32)


def _pkcs7(data: bytes) -> bytes:
    # Padded from the UTF-8 byte length. BitComet's own JS measures the UTF-16
    # string length, which is simply wrong for any non-ASCII password.
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def encrypt(plaintext: str, client_id: str) -> str:
    salt_key, salt_mac, iv = os.urandom(8), os.urandom(8), os.urandom(16)
    key, mac_key = _derive(client_id, salt_key), _derive(client_id, salt_mac)

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(_pkcs7(plaintext.encode())) + encryptor.finalize()

    body = b"\x03\x01" + salt_key + salt_mac + iv + ciphertext
    mac = hmac.new(mac_key, body, hashlib.sha256).digest()
    return base64.b64encode(body + mac).decode()


def decrypt(blob: str, client_id: str) -> str:
    """Inverse of encrypt(). Only the fake server and the tests need this."""
    raw = base64.b64decode(blob)
    body, mac = raw[:-MAC_LEN], raw[-MAC_LEN:]
    salt_key, salt_mac, iv = raw[2:10], raw[10:18], raw[18:34]

    expected = hmac.new(_derive(client_id, salt_mac), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("HMAC mismatch")

    cipher = Cipher(algorithms.AES(_derive(client_id, salt_key)), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(body[HEADER_LEN:]) + decryptor.finalize()
    return padded[: -padded[-1]].decode()


def read_or_create_device_id(path: Path | None) -> str:
    """A device id that survives restarts, generating and storing one if needed.

    Pairing is per device id, and BitComet lists every one it has ever seen in
    its Remote Access settings. Handing it a new id on each boot leaves the user
    scrolling past a screenful of identical entries, so the id lives on disk and
    we re-present the same one.

    With no path (tests, throwaway clients) this degrades to a per-process id,
    which pollutes nothing that outlives the process.
    """
    if path is None:
        return str(uuid.uuid4())
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass

    device_id = str(uuid.uuid4())
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(device_id)
    return device_id


def login_payload(username: str, password: str) -> dict:
    """The body of POST /api/webui/login, with a fresh single-use client_id."""
    client_id = str(uuid.uuid4())
    creds = json.dumps({"username": username, "password": password})
    return {"client_id": client_id, "authentication": encrypt(creds, client_id)}


# =======================================================
# NORMALISATION
# =======================================================
# BitComet numbers a task's files from 0. This repo's TorrentFile.index is
# 1-based, and the store, the API and the UI all carry that form. Every
# crossing of the boundary goes through this one pair, so an off-by-one is a
# single bug in a single place instead of a silent wrong-file-downloaded
# spread across the client.
def to_engine_index(index: int) -> int:
    """1-based TorrentFile.index -> 0-based BitComet file index."""
    return index - 1


def to_toolkit_index(index: int) -> int:
    """0-based BitComet file index -> 1-based TorrentFile.index."""
    return index + 1


def _task_id(task_id: str | int) -> str:
    """task_id and task_ids must go out as STRINGS.

    An int is rejected with "invalid task_id" / "task_ids invalid" -- and since
    the API itself returns the id as a string at the top level but an int
    inside `task`, a value round-tripped through our store can arrive here as
    either. Pinning the type at the boundary is the only reliable fix.
    """
    return str(task_id)


def _task_ids(task_ids: list[str | int] | str | int) -> list[str]:
    if isinstance(task_ids, (str, int)):
        task_ids = [task_ids]
    return [_task_id(one) for one in task_ids]


def _with_string_ids(body: dict) -> dict:
    """Coerce the ids an add returns, so callers never store an int."""
    fixed = dict(body)
    if "task_id" in fixed:
        fixed["task_id"] = _task_id(fixed["task_id"])
    if isinstance(fixed.get("task_ids"), list):
        fixed["task_ids"] = _task_ids(fixed["task_ids"])
    return fixed


def _folder_key(path: str | Path) -> str:
    """Comparable form of a save folder, so "/x" and "/x/" are one folder.

    The backslash is here for a REMOTE BitComet: the peer on the LAN may be a
    Windows box or a NAS, whose folders come back as `D:\\Downloads\\`, and a
    separator this misses means the folder is re-registered on every add.
    """
    return str(path).rstrip("/\\") or "/"


# =======================================================
# CLIENT
# =======================================================
class BitCometClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        device_id_file: Path | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.timeout = timeout
        # Whether this BitComet is the one on this machine. Read once here
        # rather than re-derived at each use, so "is the save folder ours to
        # create?" and "how long is a healthy answer allowed to take?" can
        # never disagree about the same client.
        self.is_local = is_local_host(urlsplit(self.base_url).hostname or "")
        self._device_token: str | None = None
        self._server_name: str | None = None
        # BitComet's .torrent size cap, filled on first use. It is a constant
        # of the running build, so re-reading it before every add would spend a
        # round trip to learn a number that cannot have changed.
        self._torrent_max_size: int | None = None
        # Persisted, not per-process: every login registers a bound device in
        # BitComet's settings, so a fresh id per start would add one entry per
        # restart -- and under `--reload` that is one per code edit. Measured at
        # 37 after a single afternoon before this was persisted.
        self._device_id = read_or_create_device_id(device_id_file)

        self._session = requests.Session()
        # BitComet is on loopback or on the LAN, and NEITHER belongs to a
        # proxy. A configured HTTP proxy (env vars or the macOS system proxy --
        # likely on a machine that also runs a proxy subscription tool) would
        # otherwise intercept the address and answer with its own non-JSON
        # error page, which is neither BitComet nor a connection error. This
        # bug has bitten this app before; trust_env=False is what keeps these
        # calls off any proxy. It matters MORE for a LAN peer than for
        # loopback: 127.0.0.1 is in most no_proxy lists by default and
        # 192.168.x.x is not.
        self._session.trust_env = False
        # The page sends in windows of ten that can briefly overlap into the
        # mid-teens of concurrent calls (see the Torrent Downloader's
        # SEND_WINDOW). requests' default pool keeps 10 connections per host
        # and quietly discards any opened beyond that, so every call past the
        # tenth would pay a fresh TCP handshake against the very client the
        # window exists to go easy on. One host, so one pool sized past the
        # overlap.
        adapter = requests.adapters.HTTPAdapter(pool_maxsize=20)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    @classmethod
    def from_config(
        cls,
        path: Path = CONFIG_PATH,
        timeout: float = 10.0,
        device_id_file: Path | None = None,
    ) -> BitCometClient:
        creds = read_credentials(path)
        return cls(
            base_url=creds.base_url,
            username=creds.username,
            password=creds.password,
            timeout=timeout,
            device_id_file=device_id_file,
        )

    @property
    def server_name(self) -> str | None:
        """The name this BitComet gave itself, known only after a login."""
        return self._server_name

    def close(self) -> None:
        self._session.close()

    @contextmanager
    def deadline(self, timeout: float) -> Iterator[None]:
        """Run a block against a different per-call timeout, then restore it.

        Startup uses it to stay impatient (see STARTUP_TIMEOUT) without making
        every later call, some of which BitComet genuinely takes its time over,
        equally twitchy.
        """
        previous = self.timeout
        self.timeout = timeout
        try:
            yield
        finally:
            self.timeout = previous

    # --- transport --------------------------------------------------------
    def _http(
        self, method: str, path: str, payload: dict | None, token: str | None
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = {"Client-Type": CLIENT_TYPE}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return self._session.request(
                method, url, json=payload, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise BitCometError(f"BitComet is not reachable at {url}: {exc}") from exc

    def _decode(self, response: requests.Response, path: str) -> dict:
        try:
            # A non-2xx (a proxy's 503, BitComet mid-restart) is "not
            # reachable", not a crash -- raise_for_status routes it below.
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise BitCometError(
                f"BitComet returned HTTP {response.status_code} for {path}: {exc}"
            ) from exc
        except ValueError as exc:  # body was not JSON (json.JSONDecodeError)
            raise BitCometError(
                f"BitComet returned a non-JSON response from {path}: {exc}"
            ) from exc

        if not isinstance(body, dict):
            raise BitCometError(f"BitComet returned an unexpected body from {path}")

        # error_code is "ok" from /api/task/bt/add and "OK" everywhere else, so
        # comparing exactly makes half the calls look like failures. Absent or
        # blank (task_list, the config reads) also means success.
        code = str(body.get("error_code") or "").strip()
        if code and code.upper() not in _SUCCESS_CODES:
            detail = body.get("error_message") or code
            raise BitCometError(f"BitComet rejected {path}: {detail}")
        return body

    def _token(self) -> str:
        """The cached device_token, logging in on first use."""
        if self._device_token is None:
            self._device_token = self._login()
        return self._device_token

    def _login(self) -> str:
        """The two-step handshake: credentials -> invite_token -> device_token."""
        invite = self._decode(
            self._http(
                "POST",
                "/api/webui/login",
                login_payload(self.username, self.password),
                None,
            ),
            "/api/webui/login",
        )
        invite_token = invite.get("invite_token")
        if not invite_token:
            raise BitCometError("BitComet accepted the login but issued no token")

        # The invite_token authorises exactly one call: the one that trades it
        # for the long-lived device_token.
        granted = self._decode(
            self._http(
                "POST",
                "/api/device_token/get",
                {
                    "invite_token": invite_token,
                    "device_id": self._device_id,
                    "device_name": DEVICE_NAME,
                    "platform": PLATFORM,
                },
                invite_token,
            ),
            "/api/device_token/get",
        )
        token = granted.get("device_token")
        if not token:
            raise BitCometError("BitComet issued no device_token")
        self._server_name = granted.get("server_name") or None
        return token

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One authenticated call, re-authenticating at most once on a 401.

        The device_token outlives a single call but not BitComet restarting or
        the user revoking the device, and the failure then looks identical to a
        wrong password. Retrying exactly once turns the recoverable case into a
        hiccup while still surfacing genuinely bad credentials as an error.
        """
        response = self._http(method, path, payload, self._token())
        if response.status_code == 401:
            self._device_token = None
            response = self._http(method, path, payload, self._token())
        return self._decode(response, path)

    # --- liveness ---------------------------------------------------------
    def probe(self) -> str | None:
        """BitComet's server name if it is reachable and remote access is on.

        Never raises: the page calls this on machines where BitComet may simply
        not be running. A real authenticated round trip, not a look at the
        cached token -- the question is whether the API answers now.

        Answers within the probe timeout rather than the steady-state one,
        because this is the call the UI blocks on before it can render
        anything -- and a LAN peer gets the longer of the two budgets, since a
        machine across the Wi-Fi is slower to reach than one on loopback
        without being any less healthy.
        """
        try:
            with self.deadline(
                PROBE_TIMEOUT if self.is_local else REMOTE_PROBE_TIMEOUT
            ):
                self.new_task_config()
        except BitCometError:
            return None
        return self._server_name or "BitComet"

    # --- reads ------------------------------------------------------------
    def new_task_config(self) -> dict:
        """The registered save folders and the .torrent size cap."""
        body = self._call("GET", "/api/config/new_task/get")
        # The cap is picked up on the way past rather than fetched on demand:
        # every add already registers its save folder through this endpoint,
        # so a separate read would be a round trip for a number we just saw.
        if self._torrent_max_size is None:
            try:
                self._torrent_max_size = int(body["torrent_max_size"])
            except KeyError, TypeError, ValueError:
                self._torrent_max_size = DEFAULT_TORRENT_MAX_SIZE
        return body

    def torrent_max_size(self) -> int:
        """BitComet's cap on an uploaded .torrent, asked of BitComet itself.

        Hardcoding 20 MB would be a second copy of a setting that lives in the
        client, and the only symptom of it drifting is an add refused here for
        a file BitComet would have accepted -- or the reverse, a bare error
        code from the server where we could have given a sentence.
        """
        if self._torrent_max_size is None:
            self.new_task_config()
        return self._torrent_max_size or DEFAULT_TORRENT_MAX_SIZE

    def save_folders(self) -> list[str]:
        """The folders this BitComet will accept as a save_folder, in its order.

        The UI needs these for a REMOTE BitComet and cannot work them out: the
        paths are on the peer's disk, so there is nothing on this machine to
        browse and no `~` this side can expand. Offering the list the peer
        already has is the only way to fill that field without the user
        guessing at a path they cannot see.
        """
        body = self.new_task_config()
        folders = [
            str(entry.get("path", "")).strip()
            for entry in body.get("save_folders", [])
            if isinstance(entry, dict)
        ]
        return [folder for folder in folders if folder]

    def task_list(self) -> list[dict]:
        """Every task BitComet knows about, in one round trip."""
        body = self._call("GET", "/api_v2/task_list/get")
        return [
            {**task, "task_id": _task_id(task.get("task_id", ""))}
            for task in body.get("tasks", [])
        ]

    def files(self, task_id: str | int) -> list[dict]:
        """A task's files, with `index` translated to this repo's 1-based form."""
        body = self._call("POST", "/api/task/files/get", {"task_id": _task_id(task_id)})
        return [
            {**entry, "index": to_toolkit_index(int(entry["index"]))}
            for entry in body.get("files", [])
        ]

    # --- writes -----------------------------------------------------------
    def add_torrent(
        self, data: bytes, save_folder: str | Path, *, start_later: bool = True
    ) -> dict:
        """Add one .torrent from its raw bytes. save_folder must be registered.

        start_later leaves the task `stopped`, and for a .torrent that is
        enough to make the app's review-then-commit step work: the metadata is
        already in the file, so the full list is readable from a task that has
        never touched the swarm. A MAGNET cannot be staged this way -- see
        add_magnets.
        """
        cap = self.torrent_max_size()
        if len(data) > cap:
            raise BitCometError(
                f"the .torrent is {len(data)} bytes; BitComet accepts at most {cap}"
            )
        body = self._call(
            "POST",
            "/api/task/bt/add",
            {
                "torrent_file": base64.b64encode(data).decode(),
                "save_folder": str(save_folder),
                "start_later": start_later,
            },
        )
        return _with_string_ids(body)

    def add_magnets(
        self, links: list[str], save_folder: str | Path, *, start_later: bool
    ) -> dict:
        """Add magnets in one batch -- this endpoint takes the whole list.

        RETURNS NO TASK ID. The reply is only
        {"error_code":"OK","error_message":"adding task in batch started."}:
        the add is asynchronous, so the tasks it creates have to be found
        afterwards in task_list() by their "bt_<infohash>" task_guid.

        start_later has no default because the obvious value is the wrong one.
        A magnet added with start_later=True is `stopped`, a stopped task never
        contacts the swarm, and a magnet that never contacts the swarm never
        learns its own file list -- it just sits there, empty, forever. Pass
        False and disable the files once the metadata lands.
        """
        if not links:
            raise BitCometError("no magnet links to add")
        body = self._call(
            "POST",
            "/api/task/torrent_links/add",
            {
                # One newline-joined STRING, not a list. A JSON array is
                # rejected with "torrent_links missing" -- the field reads as
                # absent rather than malformed, so the error names the wrong
                # problem and every magnet add fails with nothing to go on.
                "torrent_links": "\n".join(links),
                "save_folder": str(save_folder),
                "start_later": start_later,
            },
        )
        return _with_string_ids(body)

    def set_priority(
        self, task_id: str | int, file_indexes: list[int], priority: str
    ) -> None:
        """Set the priority of files, given this repo's 1-based indexes.

        Deselecting is `priority=DESELECTED` ("disabled"); BitComet has no
        separate select flag, and "none" is rejected.
        """
        if priority not in PRIORITIES:
            raise BitCometError(
                f"unknown BitComet priority {priority!r}; expected one of "
                f"{', '.join(PRIORITIES)}"
            )
        self._call(
            "POST",
            "/api/task/files/set_priority",
            {
                "task_id": _task_id(task_id),
                "file_indexes": [to_engine_index(i) for i in file_indexes],
                "priority": priority,
            },
        )

    def action(self, task_ids: list[str | int] | str | int, verb: str) -> None:
        if verb not in ACTIONS:
            raise BitCometError(
                f"unknown BitComet action {verb!r}; expected one of "
                f"{', '.join(ACTIONS)}"
            )
        self._call(
            "POST",
            "/api_v2/tasks/action",
            {"task_ids": _task_ids(task_ids), "action": verb},
        )

    def delete(
        self, task_ids: list[str | int] | str | int, *, delete_files: bool
    ) -> None:
        """Remove tasks. delete_files=True also erases the downloaded data."""
        self._call(
            "POST",
            "/api_v2/tasks/delete",
            {
                "task_ids": _task_ids(task_ids),
                "action": "delete_all" if delete_files else "delete_task",
            },
        )

    def ensure_save_folder(self, path: str | Path) -> str:
        """Make `path` usable as a save_folder, registering it if unknown.

        save_folder is whitelisted against BitComet's configured directory
        list; anything else fails the add with "save_folder invalid". Nothing
        in the add request hints at that, so every add would fail on a folder
        the user picked but BitComet has never seen -- this is the fix.

        WHOSE filesystem the path names is the whole reason this is split. For
        the BitComet on this machine the path is ours: `~` is our home, the
        directory is ours to create, and creating it is required because
        BitComet will not register one that does not exist. For a BitComet
        across the LAN every one of those is false -- the path is on the peer's
        disk, `~` is the peer's home, and expanding or creating it here would
        quietly make a directory on the WRONG machine and then hand BitComet a
        path it has never heard of. So the remote path is passed through
        untouched and the peer is left to judge it; if it refuses, its own
        message is what the user sees.
        """
        if self.is_local:
            folder = Path(path).expanduser()
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BitCometError(
                    f"cannot create the save folder {folder}: {exc}"
                ) from exc
            wanted = str(folder)
        else:
            wanted = str(path).strip()
            if not wanted:
                raise BitCometError("Choose a folder on that device to download into.")
            if wanted.startswith("~"):
                # It would expand to THIS machine's home, which is meaningless
                # on the peer -- and the resulting path very often exists here,
                # so the mistake would look like it worked.
                raise BitCometError(
                    f"{wanted!r} is a path on this Mac. Pick one of that "
                    f"device's own folders, or type its full path."
                )

        known = {
            _folder_key(entry.get("path", ""))
            for entry in self.new_task_config().get("save_folders", [])
        }
        if _folder_key(wanted) not in known:
            self._call("POST", "/api/config/directories/add", {"dir_path": wanted})
        return wanted
