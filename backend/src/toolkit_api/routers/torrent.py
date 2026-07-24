"""Torrent Downloader: resolve, commit, dashboard, and per-torrent controls."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from toolkit_engine.aria2 import Aria2Error, probe

from ..deps import TorrentsDep

router = APIRouter(prefix="/torrent", tags=["torrent"])


class CommitIn(BaseModel):
    infohash: str
    selected: list[int]
    save_dir: str


class StatusOut(BaseModel):
    running: bool
    owned: bool
    version: str | None = None
    detail: str | None = None


@router.get("/status", response_model=StatusOut)
def status(torrents: TorrentsDep) -> dict:
    version = probe(torrents.rpc)
    detail = None
    if version is None:
        # Distinguish "nothing listening" from "listening but rejecting us":
        # an aria2 started outside this app has its own --rpc-secret.
        try:
            torrents.rpc.version()
        except Aria2Error as exc:
            if "unauthorized" in str(exc).lower():
                detail = (
                    "aria2 is running but rejected our token. Set ARIA2_SECRET "
                    "to match that daemon's --rpc-secret."
                )
    return {
        "running": version is not None,
        "owned": torrents.owned,
        "version": version,
        "detail": detail,
    }


@router.post("/resolve")
async def resolve(
    torrents: TorrentsDep,
    magnet: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict:
    """Stage a magnet or a .torrent and report its file list when known.

    One endpoint for both because the two differ only in how long the file
    list takes to appear: a .torrent carries it, a magnet has to fetch it.
    """
    if file is not None:
        data = await file.read()
        try:
            return torrents.resolve_torrent(data, file.filename or "upload.torrent")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Could not read that .torrent: {exc}"
            ) from exc

    if not magnet:
        raise HTTPException(
            status_code=400, detail="Provide a magnet link or a .torrent file."
        )
    try:
        return torrents.resolve_magnet(magnet)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Aria2Error as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/resolve/{infohash}")
def poll_resolve(infohash: str, torrents: TorrentsDep) -> dict:
    try:
        return torrents.poll_resolve(infohash)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown torrent.") from exc


@router.post("")
def commit(payload: CommitIn, torrents: TorrentsDep) -> dict:
    try:
        torrents.commit(payload.infohash, payload.selected, payload.save_dir)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown torrent.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Aria2Error as exc:
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
    torrents.client_connected()
    last = None
    try:
        while True:
            # snapshot() polls aria2 over blocking HTTP. Called inline it
            # would stall the event loop for every other request in the
            # process, this stream included.
            payload = json.dumps(await asyncio.to_thread(torrents.snapshot))
            if payload != last:
                yield {"event": "torrents", "data": payload}
                last = payload
            await asyncio.sleep(interval)
    finally:
        # Runs on client disconnect and on server shutdown alike.
        torrents.client_disconnected()


@router.get("/events")
async def events(torrents: TorrentsDep) -> EventSourceResponse:
    """Dashboard stream. Doubles as the presence signal for auto-shutdown."""
    return EventSourceResponse(torrent_frames(torrents))


@router.post("/shutdown")
def shutdown(torrents: TorrentsDep) -> dict:
    torrents.shutdown()
    return {"stopped": True}


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
