"""Which BitComet the Torrent Downloader talks to, and how to reach it.

BitComet's remote access answers on the LAN, so the client that runs a torrent
does not have to be the one on this Mac. The machine with the disk space, the
one that stays awake, the one already wired to the NAS -- any of them can take
the task, and this module is the address book that makes that a choice rather
than a code change.

THE LOCAL DEVICE IS NOT STORED HERE. It is synthesised on every read, because
its credentials are not ours to keep: they live in BitComet's own config file
and the user can change them in Preferences at any moment, so a copy here would
drift the first time they did (see read_credentials, which makes the same
argument). Only devices this app cannot otherwise discover -- the remote ones
-- have a stored row.

A remote device's password IS stored, in plaintext, because BitComet's login
needs the password itself rather than any digest of it. That is the same
exposure as BitComet.xml, which holds the local one in plaintext too; the file
is written 0600 so it is at least no worse. Nothing here is a secret store, and
it should not be used as one.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from toolkit_engine.bitcomet import BitCometError, normalize_base_url

# The id of the implicit local device. Reserved: a saved device can never take
# it, or selecting "this Mac" would reach whatever shadowed it.
LOCAL_ID = "local"
LOCAL_LABEL = "This Mac"

# Only the owner can read a file holding remote-access passwords.
FILE_MODE = 0o600


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

    Re-read from disk on every access rather than cached in memory. The file is
    tiny, and it means an edit made while the app is running (or by a second
    process) is never overwritten by a stale copy this one was holding.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        # Sync endpoints run in FastAPI's threadpool, so two of them really can
        # read-modify-write this file at once. The lock makes each change
        # atomic against the others; without it, adding a device while
        # selecting another loses one of the two.
        self._lock = threading.Lock()

    # --- persistence ------------------------------------------------------
    def _read(self) -> dict:
        try:
            body = json.loads(self.path.read_text())
        except OSError, ValueError:
            # Missing is the normal first-run case, and unreadable/corrupt is
            # not worth taking the whole tool down for: the local BitComet
            # still works with no file at all, which is the state this returns.
            return {"active": LOCAL_ID, "devices": []}
        if not isinstance(body, dict):
            return {"active": LOCAL_ID, "devices": []}
        devices = body.get("devices")
        return {
            "active": str(body.get("active") or LOCAL_ID),
            "devices": [d for d in devices if isinstance(d, dict)]
            if isinstance(devices, list)
            else [],
        }

    def _write(self, body: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary neighbour and renamed, so a crash mid-write
        # cannot leave a half-file that reads as "no devices at all".
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=2))
        os.chmod(tmp, FILE_MODE)
        tmp.replace(self.path)

    def _saved(self) -> list[Device]:
        out = []
        for row in self._read()["devices"]:
            try:
                out.append(
                    Device(
                        id=str(row["id"]),
                        label=str(row.get("label") or ""),
                        url=str(row["url"]),
                        username=str(row.get("username") or ""),
                        password=str(row.get("password") or ""),
                    )
                )
            except KeyError:
                # A row without an id or a url cannot be connected to; skipping
                # it keeps the rest of the book usable.
                continue
        return out

    def _store(self, devices: list[Device], active: str) -> None:
        self._write(
            {
                "active": active,
                "devices": [
                    {
                        "id": d.id,
                        "label": d.label,
                        "url": d.url,
                        "username": d.username,
                        "password": d.password,
                    }
                    for d in devices
                ],
            }
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
        the file, or one removed by another process, would otherwise leave the
        tool pointing at nothing with no way to get back.
        """
        wanted = self._read()["active"]
        try:
            return self.get(wanted)
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
        with self._lock:
            devices = self._saved()
            existing = next((d for d in devices if d.url == base), None)
            if existing is not None:
                # Re-adding an address already in the book is an EDIT. Two rows
                # for one BitComet would differ only by id, and selecting the
                # stale one would fail with credentials the user thought they
                # had just corrected.
                updated = replace(
                    existing,
                    label=_clean_label(label, base),
                    username=username,
                    password=password,
                )
                devices = [updated if d.id == existing.id else d for d in devices]
                self._store(devices, updated.id if select else self._read()["active"])
                return updated

            device = Device(
                id=uuid.uuid4().hex[:12],
                label=_clean_label(label, base),
                url=base,
                username=username,
                password=password,
            )
            devices.append(device)
            self._store(devices, device.id if select else self._read()["active"])
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
        with self._lock:
            devices = self._saved()
            current = next((d for d in devices if d.id == device_id), None)
            if current is None:
                raise KeyError(device_id)
            address = base or current.url or ""
            updated = replace(
                current,
                label=_clean_label(current.label if label is None else label, address),
                url=address,
                username=current.username if username is None else username,
                password=password or current.password,
            )
            self._store(
                [updated if d.id == device_id else d for d in devices],
                self._read()["active"],
            )
            return updated

    def remove(self, device_id: str) -> None:
        """Forget a saved device, selecting this machine if it was the active one."""
        if device_id == LOCAL_ID:
            raise BitCometError("This Mac's BitComet cannot be removed.")
        with self._lock:
            devices = self._saved()
            if not any(d.id == device_id for d in devices):
                raise KeyError(device_id)
            active = self._read()["active"]
            self._store(
                [d for d in devices if d.id != device_id],
                LOCAL_ID if active == device_id else active,
            )

    def select(self, device_id: str) -> Device:
        device = self.get(device_id)  # raises KeyError for an unknown id
        with self._lock:
            self._store(self._saved(), device.id)
        return device
