"""Which BitComet the Torrent Downloader talks to, and how to reach it.

BitComet's remote access answers on the LAN, so the client that runs a torrent
does not have to be the one on this Mac. The machine with the disk space, the
one that stays awake, the one already wired to the NAS -- any of them can take
the task, and this module is the address book that makes that a choice rather
than a code change.

The book lives in the torrent tool's own SQLite database (data/torrents.db),
alongside the app's other stores, rather than in a config file of its own.
An earlier revision kept a JSON file next to the database; a book found there
is imported once and the file removed, so nothing has to be re-entered.

THE LOCAL DEVICE IS NOT STORED HERE. It is synthesised on every read, because
its credentials are not ours to keep: they live in BitComet's own config file
and the user can change them in Preferences at any moment, so a copy here would
drift the first time they did (see read_credentials, which makes the same
argument). Only devices this app cannot otherwise discover -- the remote ones
-- have a stored row.

A remote device's password IS stored, in plaintext, because BitComet's login
needs the password itself rather than any digest of it. That is the same
exposure as BitComet.xml, which holds the local one in plaintext too; the
database file is kept 0600 so it is at least no worse (SQLite gives its
journal files a copy of the database file's permissions, so they inherit the
restriction). Nothing here is a secret store, and it should not be used as one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from toolkit_engine.bitcomet import BitCometError, normalize_base_url

# The id of the implicit local device. Reserved: a saved device can never take
# it, or selecting "this Mac" would reach whatever shadowed it.
LOCAL_ID = "local"
LOCAL_LABEL = "This Mac"

# Only the owner can read a database holding remote-access passwords.
FILE_MODE = 0o600

# Where the pre-database revision kept the book, relative to the database:
# the same data directory, as bitcomet-devices.json.
LEGACY_FILENAME = "bitcomet-devices.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  password TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS device_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Device:
    """One BitComet this app can talk to."""

    id: str
    label: str
    # None means "the BitComet on this machine", whose address and credentials
    # are read from its own config at connect time rather than stored.
    url: str | None = None
    username: str = ""
    password: str = ""

    @property
    def is_local(self) -> bool:
        return self.url is None

    def public(self) -> dict:
        """The shape the API hands to the browser -- WITHOUT the password.

        `has_password` rather than the password itself: the UI only ever needs
        to know whether to show "saved" next to the field, and a password that
        never crosses the wire cannot be read out of a response by anything
        else on the LAN. It is also what lets an edit leave the field blank to
        mean "keep the one you have".
        """
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "username": self.username,
            "has_password": bool(self.password),
            "is_local": self.is_local,
        }


LOCAL = Device(id=LOCAL_ID, label=LOCAL_LABEL, url=None)


def _clean_label(label: str, url: str) -> str:
    """A display name, falling back to the host so no device is ever nameless."""
    text = (label or "").strip()
    if text:
        return text[:60]
    # The host alone, which is what the user typed and will recognise.
    return url.split("//", 1)[-1] or "BitComet"


class DeviceBook:
    """The saved remote BitComets and which one is currently selected.

    Every read goes to the database rather than to a cached copy in memory, so
    an edit made while the app is running (or by a second process) is never
    overwritten by a stale copy this one was holding. Connections are opened
    per call, the same way the app's other stores work.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # Sync endpoints run in FastAPI's threadpool, so two of them really can
        # read-modify-write the book at once. SQLite serializes the writes
        # themselves, but the check-then-write logic (re-add-as-edit, remove
        # falling back to local) needs the whole read-modify-write to be one
        # unit; the lock is what makes it one.
        self._lock = threading.Lock()
        try:
            self._prepare()
        except sqlite3.DatabaseError:
            # The file exists but is not something sqlite can read -- damaged,
            # or not a database at all. Set it aside rather than deleting it:
            # the book must come up (the local BitComet needs no stored row at
            # all), and the bytes stay for a post-mortem.
            self.path.rename(self.path.with_name(self.path.name + ".corrupt"))
            self._prepare()

    # --- persistence ------------------------------------------------------
    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            self._import_legacy(conn)
        # After the schema lands, so the chmod always has a file to act on.
        os.chmod(self.path, FILE_MODE)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _import_legacy(self, conn: sqlite3.Connection) -> None:
        """One-time import of the JSON book an earlier revision kept.

        OR IGNORE on every insert: rows already in the database win over the
        file, so re-running against a half-imported book (a crash between the
        insert and the unlink) never duplicates or downgrades anything. The
        file is deleted afterwards either way -- once the database exists it
        is the only place the truth lives.
        """
        legacy = self.path.parent / LEGACY_FILENAME
        if not legacy.exists():
            return
        try:
            body = json.loads(legacy.read_text())
        except OSError, ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        rows = body.get("devices")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or "id" not in row or "url" not in row:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO devices (id, label, url, username, password)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    str(row["id"]),
                    str(row.get("label") or ""),
                    str(row["url"]),
                    str(row.get("username") or ""),
                    str(row.get("password") or ""),
                ),
            )
        active = str(body.get("active") or "")
        if active:
            conn.execute(
                "INSERT OR IGNORE INTO device_settings (key, value)"
                " VALUES ('active', ?)",
                (active,),
            )
        legacy.unlink(missing_ok=True)

    def _saved(self) -> list[Device]:
        try:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT id, label, url, username, password FROM devices"
                    " ORDER BY rowid"
                ).fetchall()
        except sqlite3.Error:
            # A database damaged after startup. Not worth taking the tool down
            # for: the local BitComet works with no stored rows at all, which
            # is the state this returns.
            return []
        return [
            Device(
                id=row["id"],
                label=row["label"],
                url=row["url"],
                username=row["username"],
                password=row["password"],
            )
            for row in rows
            # A row without an address cannot be connected to; skipping it
            # keeps the rest of the book usable.
            if row["url"]
        ]

    def _active_id(self) -> str:
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT value FROM device_settings WHERE key = 'active'"
                ).fetchone()
        except sqlite3.Error:
            return LOCAL_ID
        return row["value"] if row else LOCAL_ID

    @staticmethod
    def _set_active(conn: sqlite3.Connection, device_id: str) -> None:
        conn.execute(
            "INSERT INTO device_settings (key, value) VALUES ('active', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (device_id,),
        )

    # --- reads ------------------------------------------------------------
    def list(self) -> list[Device]:
        """Every device, this machine first -- it is the one that needs no setup."""
        return [LOCAL, *self._saved()]

    def get(self, device_id: str) -> Device:
        for device in self.list():
            if device.id == device_id:
                return device
        raise KeyError(device_id)

    def active(self) -> Device:
        """The selected device, falling back to this machine.

        The fallback is not defensive padding: a device deleted by hand out of
        the database, or one removed by another process, would otherwise leave
        the tool pointing at nothing with no way to get back.
        """
        try:
            return self.get(self._active_id())
        except KeyError:
            return LOCAL

    # --- writes -----------------------------------------------------------
    def add(
        self, label: str, url: str, username: str, password: str, *, select: bool = True
    ) -> Device:
        """Save a remote BitComet. The address is normalised before it is stored.

        Normalising here rather than at connect time means a typo is rejected
        while the user is still looking at the form, instead of becoming a
        connection failure days later against an address they cannot see.
        """
        base = normalize_base_url(url)
        if not username or not password:
            raise BitCometError(
                "A remote BitComet needs the Web UI username and password it "
                "is configured with."
            )
        with self._lock, closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT id FROM devices WHERE url = ?", (base,)
            ).fetchone()
            if existing is not None:
                # Re-adding an address already in the book is an EDIT. Two rows
                # for one BitComet would differ only by id, and selecting the
                # stale one would fail with credentials the user thought they
                # had just corrected.
                device = Device(
                    id=existing["id"],
                    label=_clean_label(label, base),
                    url=base,
                    username=username,
                    password=password,
                )
                conn.execute(
                    "UPDATE devices SET label = ?, username = ?, password = ?"
                    " WHERE id = ?",
                    (device.label, device.username, device.password, device.id),
                )
            else:
                device = Device(
                    id=uuid.uuid4().hex[:12],
                    label=_clean_label(label, base),
                    url=base,
                    username=username,
                    password=password,
                )
                conn.execute(
                    "INSERT INTO devices (id, label, url, username, password)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        device.id,
                        device.label,
                        device.url,
                        device.username,
                        device.password,
                    ),
                )
            if select:
                self._set_active(conn, device.id)
            return device

    def update(
        self,
        device_id: str,
        *,
        label: str | None = None,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> Device:
        """Change a saved device. Only the fields passed are touched.

        A blank password means "keep the stored one", so the UI can show an
        empty field (it is never sent the real value) without an edit to the
        label silently wiping the credentials.
        """
        if device_id == LOCAL_ID:
            raise BitCometError(
                "This Mac's BitComet is configured in BitComet itself, under "
                "Options -> Remote Access."
            )
        base = normalize_base_url(url) if url else None
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT id, label, url, username, password FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                raise KeyError(device_id)
            address = base or row["url"]
            updated = Device(
                id=device_id,
                label=_clean_label(row["label"] if label is None else label, address),
                url=address,
                username=row["username"] if username is None else username,
                password=password or row["password"],
            )
            conn.execute(
                "UPDATE devices SET label = ?, url = ?, username = ?, password = ?"
                " WHERE id = ?",
                (
                    updated.label,
                    updated.url,
                    updated.username,
                    updated.password,
                    device_id,
                ),
            )
            return updated

    def remove(self, device_id: str) -> None:
        """Forget a saved device, selecting this machine if it was the active one."""
        if device_id == LOCAL_ID:
            raise BitCometError("This Mac's BitComet cannot be removed.")
        with self._lock, closing(self._connect()) as conn, conn:
            gone = conn.execute(
                "DELETE FROM devices WHERE id = ?", (device_id,)
            ).rowcount
            if not gone:
                raise KeyError(device_id)
            active = conn.execute(
                "SELECT value FROM device_settings WHERE key = 'active'"
            ).fetchone()
            if active is not None and active["value"] == device_id:
                self._set_active(conn, LOCAL_ID)

    def select(self, device_id: str) -> Device:
        device = self.get(device_id)  # raises KeyError for an unknown id
        with self._lock, closing(self._connect()) as conn, conn:
            self._set_active(conn, device.id)
        return device
