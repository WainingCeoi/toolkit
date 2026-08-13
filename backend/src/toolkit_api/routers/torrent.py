"""Torrent Downloader: pick a BitComet, resolve, choose files, hand it the task.

There is no queue endpoint, no event stream and no pause/resume/remove here on
purpose. Once a task is sent it belongs to BitComet, which already has a UI for
managing it and is the only thing that actually knows what the download is
doing. The single non-resolve write left is discard(), which cancels a staging
this app started and the user never sent -- see TorrentManager.discard.

The /devices routes are the exception to "this tool stores nothing": WHICH
BitComet to talk to is a fact about this app's configuration, not about any
download, so BitComet cannot be the one to remember it. See devices.py.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from toolkit_engine.bitcomet import REMOTE_TIMEOUT, BitCometClient, BitCometError

from .. import state as app_state
from ..deps import DevicesDep, StateDep, TorrentsDep
from ..devices import LOCAL_ID

router = APIRouter(prefix="/torrent", tags=["torrent"])

# Shown wherever BitComet is unreachable or unconfigured. Both switches are
# named because the API answers APP_ACCESS_DISABLED with only the Web UI one
# on, which looks exactly like a wrong password.
REMOTE_ACCESS_HINT = (
    "In BitComet, Options -> Remote Access: turn on both 'via BitComet Mobile "
    "App' and 'via Web UI', and set a username and password."
)


# The same advice for a machine that is not this one. "Start BitComet" is not
# actionable when BitComet is in another room, and the LAN adds two failure
# modes loopback does not have: the peer asleep, and its remote access bound to
# loopback only (BitComet's own default), which refuses every LAN client while
# looking perfectly healthy from that machine's own browser.
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
    # Never the password itself -- see Device.public().
    has_password: bool = False
    is_local: bool = True


class DeviceListOut(BaseModel):
    active: str
    devices: list[DeviceOut]


class DeviceIn(BaseModel):
    label: str = ""
    url: str = ""
    username: str = ""
    # Blank on an edit means "keep the stored one", so the UI can show an empty
    # field without wiping credentials it was never sent.
    password: str = ""


class DeviceTestIn(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""
    # Set when testing a device already saved, so a blank password above can
    # fall back to the stored one instead of failing a test the real connection
    # would have passed.
    id: str | None = None


class DeviceTestOut(BaseModel):
    ok: bool
    server: str | None = None
    detail: str | None = None
    # What that BitComet will accept as a download folder. The point of testing
    # before saving: these paths are on the peer's disk, and this is the first
    # moment the user can see them.
    save_folders: list[str] = []


class StatusOut(BaseModel):
    running: bool
    server: str | None = None
    detail: str | None = None
    # Where BitComet's own UI lives, so the page can hand the user straight
    # over to it after sending instead of describing how to find it.
    url: str | None = None
    # Which BitComet the answers above are about. None only for an injected
    # state with no device book.
    device: DeviceOut | None = None
    # Whether that BitComet is the one on this machine. Decides whether the
    # page can offer a native folder picker (it browses THIS filesystem, which
    # is the wrong one for a peer) or must offer the peer's own folder list.
    is_local: bool = True
    save_folders: list[str] = []


def _folders(torrents) -> list[str]:
    """The active BitComet's save folders, or [] if it cannot be asked."""
    try:
        return torrents.client.save_folders()
    except BitCometError:
        return []


@router.get("/status", response_model=StatusOut)
def status(state: StateDep) -> dict:
    """Always answers, even with no engine -- it is the diagnostic endpoint, so
    gating it behind the dependency it reports on would hide the diagnosis."""
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

    server = torrents.client.probe()
    detail = None
    if server is None:
        if torrents.client.is_local:
            # Credentials were readable, so BitComet is installed -- it is
            # either not running or not serving the API.
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
        "save_folders": _folders(torrents) if server is not None else [],
    }


# =======================================================
# DEVICES
# =======================================================
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
    """Save a remote BitComet and switch to it.

    Switching immediately is the point of adding one: nobody types in a NAS's
    address to leave the tool pointed somewhere else.
    """
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
    # Rebuild only if the edit was to the device in use; otherwise a change to
    # an idle device would needlessly drop a working connection.
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
    # Removing the device in use drops the tool back to this Mac (DeviceBook
    # does the falling back), so the manager has to follow it there.
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
    """Try an address and credentials WITHOUT saving them.

    Answers with BitComet's own words on failure rather than a bare "not
    running": on the LAN the three plausible causes -- wrong address, wrong
    password, remote access not reachable from the network -- are
    indistinguishable from the outside, and only the error text separates them.
    """
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
            # The remote steady-state budget, for the same reason the saved
            # devices get it: a Test pressed while that BitComet is grinding
            # through fresh tasks must report "reached", not time out.
            timeout=REMOTE_TIMEOUT,
            # The SAME persisted id every other connection uses. A throwaway id
            # here would add one paired-device entry to that BitComet's
            # settings for every press of Test.
            device_id_file=app_state.data_dir() / "bitcomet-device-id",
        )
    except BitCometError as exc:
        return DeviceTestOut(ok=False, detail=str(exc))

    try:
        # save_folders() rather than probe(): it is one authenticated round
        # trip either way, and this one comes back with something to show.
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
    """Stage a magnet or a .torrent and report its file list when known.

    One endpoint for both because the two differ only in how long the file
    list takes to appear: a .torrent carries it, a magnet has to fetch it.

    The destination is chosen here rather than at send because BitComet fixes
    a task's save folder when the task is created.

    Deliberately a sync `def`, like every other route here, so FastAPI runs it
    on the threadpool. It talks to BitComet over blocking `requests` with a
    30s-per-round-trip budget for a remote device; as an `async def` those
    calls sat on the event loop, and one asleep LAN peer froze every SSE
    progress stream and every other request in the process along with it. The
    upload is read through the sync file object for the same reason.
    """
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
        # A magnet's files are deselected on this path, and the torrent is
        # running while that happens -- so a BitComet that stops answering here
        # is reported, never smoothed over into "still waiting for metadata".
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
