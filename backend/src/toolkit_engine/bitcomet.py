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
import json
import os
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

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

# Deliberately impatient, for the boot-time reconcile only. A BitComet that is
# wedged rather than absent accepts the connection and then never answers, and
# at the steady-state timeout that stalls the whole web server's startup behind
# a torrent list nobody has asked for yet.
STARTUP_TIMEOUT = 1.5

# --- login envelope byte layout ------------------------------------------
HEADER_LEN = 34
MAC_LEN = 32
ITERATIONS = 10_000


class BitCometError(RuntimeError):
    """A BitComet API call failed, or the client could not be reached."""


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
        # Remote access is bound to this machine; the API is never reached
        # over the LAN by this app.
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
    """Comparable form of a save folder, so "/x" and "/x/" are one folder."""
    return str(path).rstrip("/") or "/"


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._device_token: str | None = None
        self._server_name: str | None = None
        # BitComet's .torrent size cap, filled on first use. It is a constant
        # of the running build, so re-reading it before every add would spend a
        # round trip to learn a number that cannot have changed.
        self._torrent_max_size: int | None = None
        # Stable for the life of the client: BitComet keeps a list of paired
        # devices, and a new id per login would fill it with duplicates of us.
        self._device_id = str(uuid.uuid4())

        self._session = requests.Session()
        # BitComet is always on loopback. A configured HTTP proxy (env vars or
        # the macOS system proxy -- likely on a machine that also runs a proxy
        # subscription tool) would otherwise intercept 127.0.0.1 and answer
        # with its own non-JSON error page, which is neither BitComet nor a
        # connection error. This bug has bitten this app before;
        # trust_env=False is what keeps these calls off any proxy.
        self._session.trust_env = False

    @classmethod
    def from_config(
        cls, path: Path = CONFIG_PATH, timeout: float = 10.0
    ) -> BitCometClient:
        creds = read_credentials(path)
        return cls(
            base_url=creds.base_url,
            username=creds.username,
            password=creds.password,
            timeout=timeout,
        )

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

        Never raises: startup and the dashboard both call this on machines
        where BitComet may simply not be running. A real authenticated round
        trip, not a look at the cached token -- the question is whether the API
        answers now.
        """
        try:
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
        """
        folder = Path(path).expanduser()
        try:
            # BitComet will not register a directory that does not exist, and
            # the folder the user picked may be brand new.
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BitCometError(
                f"cannot create the save folder {folder}: {exc}"
            ) from exc

        body = self.new_task_config()
        known = {
            _folder_key(entry.get("path", "")) for entry in body.get("save_folders", [])
        }
        if _folder_key(folder) not in known:
            self._call("POST", "/api/config/directories/add", {"dir_path": str(folder)})
        return str(folder)
