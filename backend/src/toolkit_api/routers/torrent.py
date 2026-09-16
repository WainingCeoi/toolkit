"""Torrent Downloader: pick a BitComet, resolve, choose files, hand it the task."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from toolkit_engine.bitcomet import REMOTE_TIMEOUT, BitCometClient, BitCometError

from .. import state as app_state
from ..deps import DevicesDep, StateDep, TorrentsDep
from ..devices import LOCAL_ID

router = APIRouter(prefix="/torrent", tags=["torrent"])

# Names both switches: Web UI alone yields APP_ACCESS_DISABLED, like a bad password.
REMOTE_ACCESS_HINT = (
    "In BitComet, Options -> Remote Access: turn on both 'via BitComet Mobile "
    "App' and 'via Web UI', and set a username and password."
)


def _remote_hint(url: str, label: str) -> str:
    return (
        f"{label} is not answering at {url}. Check that the machine is awake, "
        f"that BitComet is running on it, and that its Remote Access is "
        f"reachable from the network rather than from that machine only."
    )


class SendIn(BaseModel):
    infohash: str
    selected: list[int]


class DeviceOut(BaseModel):
    id: str
    label: str
    url: str | None = None
    username: str = ""
    # Never the password itself.
    has_password: bool = False
    is_local: bool = True


class DeviceListOut(BaseModel):
    active: str
    devices: list[DeviceOut]


class DeviceIn(BaseModel):
    label: str = ""
    url: str = ""
    username: str = ""
    # Blank on an edit means "keep the stored one".
    password: str = ""


class DeviceTestIn(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""
    # Set when testing a saved device: a blank password falls back to the stored one.
    id: str | None = None


class DeviceTestOut(BaseModel):
    ok: bool
    server: str | None = None
    detail: str | None = None
    # Download folders on that BitComet's own disk.
    save_folders: list[str] = []


class StatusOut(BaseModel):
    running: bool
    server: str | None = None
    detail: str | None = None
    # Where BitComet's own UI lives.
    url: str | None = None
    # None only for an injected state with no device book.
    device: DeviceOut | None = None
    is_local: bool = True
    save_folders: list[str] = []


@router.get("/status", response_model=StatusOut)
def status(state: StateDep) -> dict:
    """Always answers, even with no engine: this is the diagnostic endpoint."""
    book = state.devices
    device = book.active() if book is not None else None
    torrents = state.torrents
    if torrents is None:
        return {
            "running": False,
            "server": None,
            "url": None,
            "device": device.public() if device else None,
            "is_local": device.is_local if device else True,
            "detail": (
                "BitComet's settings could not be read. Install BitComet from "
                f"https://www.bitcomet.com/, then restart the backend. "
                f"{REMOTE_ACCESS_HINT}"
            ),
        }

    server, folders = torrents.client.probe_folders()
    detail = None
    if server is None:
        if torrents.client.is_local:
            detail = (
                f"BitComet is not answering at {torrents.client.base_url}. "
                f"Start it, then check: {REMOTE_ACCESS_HINT}"
            )
        else:
            detail = _remote_hint(
                torrents.client.base_url, device.label if device else "That device"
            )
    return {
        "running": server is not None,
        "server": server,
        "detail": detail,
        "url": torrents.client.base_url,
        "device": device.public() if device else None,
        "is_local": torrents.client.is_local,
        "save_folders": folders,
    }


# --- DEVICES ---
def _listing(book) -> dict:
    return {
        "active": book.active().id,
        "devices": [device.public() for device in book.list()],
    }


@router.get("/devices", response_model=DeviceListOut)
def list_devices(book: DevicesDep) -> dict:
    return _listing(book)


@router.post("/devices", response_model=DeviceListOut)
def add_device(payload: DeviceIn, book: DevicesDep, state: StateDep) -> dict:
    """Save a remote BitComet and switch to it."""
    try:
        device = book.add(
            payload.label, payload.url, payload.username, payload.password
        )
    except BitCometError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    app_state.use_device(state, device)
    return _listing(book)


@router.patch("/devices/{device_id}", response_model=DeviceListOut)
def update_device(
    device_id: str, payload: DeviceIn, book: DevicesDep, state: StateDep
) -> dict:
    try:
        device = book.update(
            device_id,
            label=payload.label or None,
            url=payload.url or None,
            username=payload.username or None,
            password=payload.password or None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such device.") from exc
    except BitCometError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if book.active().id == device.id:
        app_state.use_device(state, device)
    return _listing(book)


@router.delete("/devices/{device_id}", response_model=DeviceListOut)
def remove_device(device_id: str, book: DevicesDep, state: StateDep) -> dict:
    try:
        book.remove(device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such device.") from exc
    except BitCometError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # book.remove may have changed the active device; the manager must follow.
    app_state.use_device(state, book.active())
    return _listing(book)


@router.post("/devices/{device_id}/select", response_model=DeviceListOut)
def select_device(device_id: str, book: DevicesDep, state: StateDep) -> dict:
    try:
        device = book.select(device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No such device.") from exc
    app_state.use_device(state, device)
    return _listing(book)


@router.post("/devices/test", response_model=DeviceTestOut)
def test_device(payload: DeviceTestIn, book: DevicesDep) -> DeviceTestOut:
    """Try an address and credentials without saving them."""
    password = payload.password
    if not password and payload.id and payload.id != LOCAL_ID:
        try:
            password = book.get(payload.id).password
        except KeyError:
            password = ""

    try:
        client = BitCometClient(
            base_url=payload.url,
            username=payload.username,
            password=password,
            timeout=REMOTE_TIMEOUT,
            # Same persisted id as other connections: a fresh id pairs a new device.
            device_id_file=app_state.data_dir() / "bitcomet-device-id",
        )
    except BitCometError as exc:
        return DeviceTestOut(ok=False, detail=str(exc))

    try:
        folders = client.save_folders()
        name = client.server_name or "BitComet"
    except BitCometError as exc:
        return DeviceTestOut(ok=False, detail=str(exc))
    finally:
        client.close()
    return DeviceTestOut(ok=True, server=name, save_folders=folders)


@router.post("/resolve")
def resolve(
    torrents: TorrentsDep,
    magnet: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    save_dir: Annotated[str, Form()] = "",
) -> dict:
    """Stage a magnet or a .torrent and report its file list when known."""
    # Keep this a sync def: blocking BitComet calls in an async def stall the loop.
    # BitComet fixes the save folder at task creation, so save_dir is taken here.
    if file is not None:
        data = file.file.read()
        try:
            return torrents.resolve_torrent(data, save_dir)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not read that .torrent: {exc}"
            ) from exc
        except BitCometError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not magnet:
        raise HTTPException(
            status_code=400, detail="Provide a magnet link or a .torrent file."
        )
    try:
        return torrents.resolve_magnet(magnet, save_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BitCometError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/resolve/{infohash}")
def poll_resolve(infohash: str, torrents: TorrentsDep) -> dict:
    try:
        return torrents.poll_resolve(infohash)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown torrent.") from exc
    except BitCometError as exc:
        # Report a BitComet outage here; never smooth it into "still waiting".
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("")
def send(payload: SendIn, torrents: TorrentsDep) -> dict:
    """The handover. After this the task is BitComet's and this app forgets it."""
    try:
        return torrents.send(payload.infohash, payload.selected)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="BitComet no longer has this torrent."
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BitCometError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/{infohash}")
def discard(infohash: str, torrents: TorrentsDep) -> dict:
    """Cancel a staged torrent the user decided against, before it is sent."""
    try:
        torrents.discard(infohash)
    except BitCometError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"infohash": infohash, "state": "discarded"}
