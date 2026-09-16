"""BitComet WebUI client; most endpoints are undocumented, verified live on 2.20."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import threading
import uuid
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from toolkit_engine.aescbc import decrypt_cbc, encrypt_cbc

# BitComet's own settings file; read live so the credentials never drift.
CONFIG_PATH = (
    Path.home() / "Library" / "Application Support" / "BitComet" / "BitComet.xml"
)
DEFAULT_PORT = 19377

# Sent on every request; BitComet keys its remote-access sessions off it.
CLIENT_TYPE = "BitComet WebUI"
DEVICE_NAME = "Toolkit"
PLATFORM = "webui"

# The four priorities BitComet accepts; "none" is rejected outright.
PRIORITIES = ("very_high", "high", "normal", "disabled")
# "disabled" is how a file is DESELECTED; there is no separate selected flag.
DESELECTED = "disabled"
SELECTED = "normal"

# The verified verbs for /api_v2/tasks/action.
ACTIONS = ("start", "stop", "hash_check", "tracker_update")

# "skipped" means the task was already in that state: a no-op, not a failure.
_SUCCESS_CODES = frozenset({"OK", "SKIPPED"})

# Fallback when BitComet does not report torrent_max_size; 2.20 ships 20 MB.
DEFAULT_TORRENT_MAX_SIZE = 20 * 1024 * 1024

# probe() only: a wedged BitComet accepts the connection and never answers.
PROBE_TIMEOUT = 1.5

# A LAN peer needs ARP/mDNS and may be asleep; 1.5s would read as "not running".
REMOTE_PROBE_TIMEOUT = 4.0

# A BitComet busy starting a batch of tasks can take tens of seconds per call.
REMOTE_TIMEOUT = 30.0

# --- login envelope byte layout ------------------------------------------
HEADER_LEN = 34
MAC_LEN = 32
ITERATIONS = 10_000


class BitCometError(RuntimeError):
    """A BitComet API call failed, or the client could not be reached."""


# --- ADDRESSES ---
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain"})


def _bracketed(host: str) -> str:
    """An IPv6 literal needs its brackets back before it can go in a URL."""
    return f"[{host}]" if ":" in host else host


def is_local_host(host: str) -> bool:
    """True when `host` names THIS machine's loopback interface."""
    name = host.strip().strip("[]").lower()
    if name in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_base_url(raw: str) -> str:
    """Whatever the user typed -> `http://host:port`, or a sentence saying why not."""
    text = (raw or "").strip()
    if not text:
        raise BitCometError("Enter the address of the BitComet to connect to.")
    # `nas:19377` would parse as scheme "nas", so a scheme goes in front first.
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


# --- CREDENTIALS ---
@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    port: int = DEFAULT_PORT

    @property
    def base_url(self) -> str:
        # Local BitComet only; a remote one is configured with its own address.
        return f"http://127.0.0.1:{self.port}"


def read_credentials(path: Path = CONFIG_PATH) -> Credentials:
    """Read the WebUI username, password and port from BitComet's own config."""
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


# --- LOGIN ENVELOPE ---
# Port of the WebUI bundle's CryptoJS AES_Encrypt; the "password" is the client_id.
# Obfuscation only: the client_id travels in the clear beside the ciphertext.
def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), salt, ITERATIONS, dklen=32)


def _pkcs7(data: bytes) -> bytes:
    # Pad from the UTF-8 byte length; BitComet's JS wrongly uses the UTF-16 length.
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def encrypt(plaintext: str, client_id: str) -> str:
    salt_key, salt_mac, iv = os.urandom(8), os.urandom(8), os.urandom(16)
    key, mac_key = _derive(client_id, salt_key), _derive(client_id, salt_mac)

    ciphertext = encrypt_cbc(key, iv, _pkcs7(plaintext.encode()))

    body = b"\x03\x01" + salt_key + salt_mac + iv + ciphertext
    mac = hmac.new(mac_key, body, hashlib.sha256).digest()
    return base64.b64encode(body + mac).decode()


def decrypt(blob: str, client_id: str) -> str:
    """Inverse of encrypt()."""
    raw = base64.b64decode(blob)
    body, mac = raw[:-MAC_LEN], raw[-MAC_LEN:]
    salt_key, salt_mac, iv = raw[2:10], raw[10:18], raw[18:34]

    expected = hmac.new(_derive(client_id, salt_mac), body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise ValueError("HMAC mismatch")

    padded = decrypt_cbc(_derive(client_id, salt_key), iv, body[HEADER_LEN:])
    return padded[: -padded[-1]].decode()


def read_or_create_device_id(path: Path | None) -> str:
    """A device id that survives restarts; BitComet lists every id it has ever seen."""
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


# --- NORMALISATION ---
# BitComet numbers files from 0; TorrentFile.index is 1-based. Translate only here.
def to_engine_index(index: int) -> int:
    """1-based TorrentFile.index -> 0-based BitComet file index."""
    return index - 1


def to_toolkit_index(index: int) -> int:
    """0-based BitComet file index -> 1-based TorrentFile.index."""
    return index + 1


def _task_id(task_id: str | int) -> str:
    """task_id must go out as a STRING; an int is rejected as "invalid task_id"."""
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
    """Comparable form of a save folder; the backslash is for Windows/NAS peers."""
    return str(path).rstrip("/\\") or "/"


def _save_folders(body: dict) -> list[str]:
    """The save folder paths in a new_task/get body, in BitComet's own order."""
    folders = [
        str(entry.get("path", "")).strip()
        for entry in body.get("save_folders", [])
        if isinstance(entry, dict)
    ]
    return [folder for folder in folders if folder]


# --- CLIENT ---
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
        # Fixed at construction so save-folder and timeout rules always agree.
        self.is_local = is_local_host(urlsplit(self.base_url).hostname or "")
        self._device_token: str | None = None
        # One handshake at a time: ten threads on a fresh client would all log in.
        self._login_lock = threading.Lock()
        self._server_name: str | None = None
        # .torrent size cap, read from BitComet on first use.
        self._torrent_max_size: int | None = None
        # Persisted: every login binds a device in BitComet's settings, one per new id.
        self._device_id = read_or_create_device_id(device_id_file)

        self._session = requests.Session()
        # Keep off any HTTP proxy; a proxy answers with its own non-JSON error page.
        self._session.trust_env = False
        # requests' default pool is 10 per host; the send window overlaps past that.
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

    # --- transport --------------------------------------------------------
    def _http(
        self,
        method: str,
        path: str,
        payload: dict | None,
        token: str | None,
        timeout: float | None = None,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = {"Client-Type": CLIENT_TYPE}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            # Per call, never stored: one client serves every request thread.
            return self._session.request(
                method,
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout if timeout is None else timeout,
            )
        except requests.RequestException as exc:
            raise BitCometError(f"BitComet is not reachable at {url}: {exc}") from exc

    def _decode(self, response: requests.Response, path: str) -> dict:
        try:
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

        # "ok" from bt/add, "OK" elsewhere, absent from the reads: all mean success.
        code = str(body.get("error_code") or "").strip()
        if code and code.upper() not in _SUCCESS_CODES:
            detail = body.get("error_message") or code
            raise BitCometError(f"BitComet rejected {path}: {detail}")
        return body

    def _token(self, timeout: float | None = None) -> str:
        """The cached device_token, logging in on first use."""
        with self._login_lock:
            if self._device_token is None:
                self._device_token = self._login(timeout)
            return self._device_token

    def _login(self, timeout: float | None = None) -> str:
        """The two-step handshake: credentials -> invite_token -> device_token."""
        invite = self._decode(
            self._http(
                "POST",
                "/api/webui/login",
                login_payload(self.username, self.password),
                None,
                timeout,
            ),
            "/api/webui/login",
        )
        invite_token = invite.get("invite_token")
        if not invite_token:
            raise BitCometError("BitComet accepted the login but issued no token")

        # The invite_token authorises exactly one call: this trade for a device_token.
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
                timeout,
            ),
            "/api/device_token/get",
        )
        token = granted.get("device_token")
        if not token:
            raise BitCometError("BitComet issued no device_token")
        self._server_name = granted.get("server_name") or None
        return token

    def _call(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """One authenticated call, re-authenticating at most once on a 401."""
        token = self._token(timeout)
        response = self._http(method, path, payload, token, timeout)
        if response.status_code == 401:
            with self._login_lock:
                # Only the thread whose own token was rejected drops it.
                if self._device_token == token:
                    self._device_token = None
            response = self._http(method, path, payload, self._token(timeout), timeout)
        return self._decode(response, path)

    # --- liveness ---------------------------------------------------------
    def probe(self) -> str | None:
        """BitComet's server name if reachable and remote access is on; never raises."""
        return self.probe_folders()[0]

    def probe_folders(self) -> tuple[str | None, list[str]]:
        """probe() and save_folders() out of one round trip; never raises."""
        try:
            body = self.new_task_config(
                timeout=PROBE_TIMEOUT if self.is_local else REMOTE_PROBE_TIMEOUT
            )
        except BitCometError:
            return None, []
        return self._server_name or "BitComet", _save_folders(body)

    # --- reads ------------------------------------------------------------
    def new_task_config(self, timeout: float | None = None) -> dict:
        """The registered save folders and the .torrent size cap."""
        body = self._call("GET", "/api/config/new_task/get", timeout=timeout)
        if self._torrent_max_size is None:
            try:
                self._torrent_max_size = int(body["torrent_max_size"])
            except KeyError, TypeError, ValueError:
                self._torrent_max_size = DEFAULT_TORRENT_MAX_SIZE
        return body

    def torrent_max_size(self) -> int:
        """BitComet's cap on an uploaded .torrent, asked of BitComet itself."""
        if self._torrent_max_size is None:
            self.new_task_config()
        return self._torrent_max_size or DEFAULT_TORRENT_MAX_SIZE

    def save_folders(self) -> list[str]:
        """The folders this BitComet will accept as a save_folder, in its order."""
        return _save_folders(self.new_task_config())

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
        """Add one .torrent from its raw bytes. save_folder must be registered."""
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
        """Batch add; no task id comes back, find them by task_guid "bt_<infohash>"."""
        # start_later=True leaves a magnet stopped and thus without a file list.
        if not links:
            raise BitCometError("no magnet links to add")
        body = self._call(
            "POST",
            "/api/task/torrent_links/add",
            {
                # Newline-joined STRING; a JSON list fails as "torrent_links missing".
                "torrent_links": "\n".join(links),
                "save_folder": str(save_folder),
                "start_later": start_later,
            },
        )
        return _with_string_ids(body)

    def set_priority(
        self, task_id: str | int, file_indexes: list[int], priority: str
    ) -> None:
        """Set the priority of files, given this repo's 1-based indexes."""
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
        """Make `path` usable as a save_folder; BitComet rejects unregistered ones."""
        if self.is_local:
            folder = Path(path).expanduser()
            if not folder.is_absolute():
                # Relative, it would be created under the backend's own cwd.
                raise BitCometError(
                    f"{str(path).strip()!r} is not a full path. Type one like "
                    f"/Users/you/Downloads, or use Browse."
                )
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise BitCometError(
                    f"cannot create the save folder {folder}: {exc}"
                ) from exc
            wanted = str(folder)
        else:
            # Remote: the path is on the peer's disk; never expand or create it here.
            wanted = str(path).strip()
            if not wanted:
                raise BitCometError("Choose a folder on that device to download into.")
            if wanted.startswith("~"):
                # Would expand to THIS machine's home, which usually exists here.
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
