"""Torrent Downloader: resolve, choose files, hand the task to BitComet.

There is no queue endpoint, no event stream and no pause/resume/remove here on
purpose. Once a task is sent it belongs to BitComet, which already has a UI for
managing it and is the only thing that actually knows what the download is
doing. The single non-resolve write left is discard(), which cancels a staging
this app started and the user never sent -- see TorrentManager.discard.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from toolkit_engine.bitcomet import BitCometError

from ..deps import StateDep, TorrentsDep

router = APIRouter(prefix="/torrent", tags=["torrent"])

# Shown wherever BitComet is unreachable or unconfigured. Both switches are
# named because the API answers APP_ACCESS_DISABLED with only the Web UI one
# on, which looks exactly like a wrong password.
REMOTE_ACCESS_HINT = (
    "In BitComet, Options -> Remote Access: turn on both 'via BitComet Mobile "
    "App' and 'via Web UI', and set a username and password."
)


class SendIn(BaseModel):
    infohash: str
    selected: list[int]


class StatusOut(BaseModel):
    running: bool
    server: str | None = None
    detail: str | None = None
    # Where BitComet's own UI lives, so the page can hand the user straight
    # over to it after sending instead of describing how to find it.
    url: str | None = None


@router.get("/status", response_model=StatusOut)
def status(state: StateDep) -> dict:
    """Always answers, even with no engine -- it is the diagnostic endpoint, so
    gating it behind the dependency it reports on would hide the diagnosis."""
    torrents = state.torrents
    if torrents is None:
        return {
            "running": False,
            "server": None,
            "url": None,
            "detail": (
                "BitComet's settings could not be read. Install BitComet from "
                f"https://www.bitcomet.com/, then restart the backend. "
                f"{REMOTE_ACCESS_HINT}"
            ),
        }

    server = torrents.client.probe()
    detail = None
    if server is None:
        # Credentials were readable, so BitComet is installed -- it is either
        # not running or not serving the API.
        detail = (
            f"BitComet is not answering at {torrents.client.base_url}. "
            f"Start it, then check: {REMOTE_ACCESS_HINT}"
        )
    return {
        "running": server is not None,
        "server": server,
        "detail": detail,
        "url": torrents.client.base_url,
    }


@router.post("/resolve")
async def resolve(
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
    """
    if file is not None:
        data = await file.read()
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
