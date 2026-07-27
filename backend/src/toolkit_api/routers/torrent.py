"""Torrent Downloader: resolve, commit, dashboard, and per-torrent controls."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

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


class CommitIn(BaseModel):
    infohash: str
    selected: list[int]


class StatusOut(BaseModel):
    running: bool
    server: str | None = None
    detail: str | None = None


@router.get("/status", response_model=StatusOut)
def status(state: StateDep) -> dict:
    """Always answers, even with no engine -- it is the diagnostic endpoint, so
    gating it behind the dependency it reports on would hide the diagnosis."""
    torrents = state.torrents
    if torrents is None:
        return {
            "running": False,
            "server": None,
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
    return {"running": server is not None, "server": server, "detail": detail}


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

    The destination is chosen here rather than at commit because BitComet fixes
    a task's save folder when the task is created.
    """
    if file is not None:
        data = await file.read()
        try:
            return torrents.resolve_torrent(
                data, file.filename or "upload.torrent", save_dir
            )
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
def commit(payload: CommitIn, torrents: TorrentsDep) -> dict:
    try:
        torrents.commit(payload.infohash, payload.selected)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown torrent.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BitCometError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"infohash": payload.infohash, "state": "active"}


@router.get("")
def listing(torrents: TorrentsDep) -> list[dict]:
    return torrents.snapshot()


async def torrent_frames(torrents, interval: float = 1.0):
    """Yield an SSE frame whenever the dashboard changes, until cancelled.

    Module-level rather than a closure so it can be driven directly: the
    stream never ends on its own (a dashboard has no terminal state), so an
    HTTP-level test of it would hang rather than finish.
    """
    last = None
    while True:
        # snapshot() polls BitComet over blocking HTTP. Called inline it would
        # stall the event loop for every other request in the process, this
        # stream included.
        payload = json.dumps(await asyncio.to_thread(torrents.snapshot))
        if payload != last:
            yield {"event": "torrents", "data": payload}
            last = payload
        await asyncio.sleep(interval)


@router.get("/events")
async def events(torrents: TorrentsDep) -> EventSourceResponse:
    return EventSourceResponse(torrent_frames(torrents))


# Literal-segment routes, declared before the "/{infohash}/..." ones so a
# torrent can never be named "pause-all".
@router.post("/pause-all")
def pause_all(torrents: TorrentsDep) -> dict:
    torrents.pause_all()
    return {"paused": True}


@router.post("/resume-all")
def resume_all(torrents: TorrentsDep) -> dict:
    torrents.resume_all()
    return {"resumed": True}


@router.post("/{infohash}/pause")
def pause(infohash: str, torrents: TorrentsDep) -> dict:
    torrents.pause(infohash)
    return {"infohash": infohash, "state": "paused"}


@router.post("/{infohash}/resume")
def resume(infohash: str, torrents: TorrentsDep) -> dict:
    torrents.resume(infohash)
    return {"infohash": infohash, "state": "active"}


@router.delete("/{infohash}")
def remove(infohash: str, torrents: TorrentsDep, delete_files: bool = False) -> dict:
    torrents.remove(infohash, delete_files=delete_files)
    return {"infohash": infohash, "state": "removed"}
