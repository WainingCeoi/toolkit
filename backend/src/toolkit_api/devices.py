"""Address book of BitComet devices the Torrent Downloader can dispatch to."""

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

# Reserved for the implicit local device; a saved row must never use it.
LOCAL_ID = "local"
LOCAL_LABEL = "This Mac"

# Only the owner can read a database holding remote-access passwords.
FILE_MODE = 0o600

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
    # None = the local BitComet, whose config is read at connect time, not stored.
    url: str | None = None
    username: str = ""
    password: str = ""

    @property
    def is_local(self) -> bool:
        return self.url is None

    def public(self) -> dict:
        """API shape for the browser; the password itself must never be included."""
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
    return url.split("//", 1)[-1] or "BitComet"


class DeviceBook:
    """The saved remote BitComets and the selected one; every read hits SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # Sync endpoints run in a threadpool; check-then-write must be one unit.
        self._lock = threading.Lock()
        try:
            self._prepare()
        except sqlite3.DatabaseError:
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
        """Import the pre-database JSON book once; OR IGNORE keeps a rerun harmless."""
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
            # A database damaged after startup; local BitComet needs no rows anyway.
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
        return [LOCAL, *self._saved()]

    def get(self, device_id: str) -> Device:
        for device in self.list():
            if device.id == device_id:
                return device
        raise KeyError(device_id)

    def active(self) -> Device:
        """The selected device, falling back to this machine."""
        try:
            return self.get(self._active_id())
        except KeyError:
            return LOCAL

    # --- writes -----------------------------------------------------------
    def add(
        self, label: str, url: str, username: str, password: str, *, select: bool = True
    ) -> Device:
        """Save a remote BitComet; re-adding a saved address edits that row instead."""
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
        """Change only the fields passed; a blank password keeps the stored one."""
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
