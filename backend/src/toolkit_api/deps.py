"""Dependency injection: routers read the shared state off app.state."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .artifacts import ArtifactStore
from .devices import DeviceBook
from .jobs import JobRegistry
from .purgescans import PurgeScans
from .state import AppState
from .watermarks import WatermarkBatches


def get_state(request: Request) -> AppState:
    return request.app.state.state


def get_jobs(request: Request) -> JobRegistry:
    return request.app.state.state.jobs


def get_artifacts(request: Request) -> ArtifactStore:
    return request.app.state.state.artifacts


def get_store(request: Request):
    return request.app.state.state.store


def get_torrents(request: Request):
    """The current TorrentManager, held for the life of the request.

    Yielded rather than returned so the in-flight count can be released at the
    end: a device switch replaces the manager and wants to close the one it
    replaced, which must not happen while this request is still talking through
    it. Whoever finishes last does the closing.
    """
    state = request.app.state.state
    with state.torrents_lock:
        manager = state.torrents
        if manager is None:
            raise HTTPException(
                status_code=503, detail="The torrent engine is not ready."
            )
        key = id(manager)
        state.torrents_users[key] = state.torrents_users.get(key, 0) + 1
    try:
        yield manager
    finally:
        with state.torrents_lock:
            remaining = state.torrents_users.get(key, 1) - 1
            if remaining > 0:
                state.torrents_users[key] = remaining
            else:
                state.torrents_users.pop(key, None)
            # Replaced while we were using it, and we were the last one.
            orphaned = remaining <= 0 and manager is not state.torrents
        if orphaned:
            manager.close()


def get_devices(request: Request) -> DeviceBook:
    book = request.app.state.state.devices
    if book is None:
        raise HTTPException(
            status_code=503, detail="The BitComet device list is not ready."
        )
    return book


def get_purge_scans(request: Request) -> PurgeScans:
    return request.app.state.state.purge_scans


def get_watermarks(request: Request) -> WatermarkBatches:
    batches = request.app.state.state.watermarks
    if batches is None:
        raise HTTPException(
            status_code=503, detail="The watermark workspace is not ready."
        )
    return batches


StateDep = Annotated[AppState, Depends(get_state)]
JobsDep = Annotated[JobRegistry, Depends(get_jobs)]
ArtifactsDep = Annotated[ArtifactStore, Depends(get_artifacts)]
StoreDep = Annotated[object, Depends(get_store)]
TorrentsDep = Annotated[object, Depends(get_torrents)]
DevicesDep = Annotated[DeviceBook, Depends(get_devices)]
WatermarksDep = Annotated[WatermarkBatches, Depends(get_watermarks)]
PurgeScansDep = Annotated[PurgeScans, Depends(get_purge_scans)]
