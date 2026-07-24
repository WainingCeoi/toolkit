"""Dependency injection: routers read the shared state off app.state."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .artifacts import ArtifactStore
from .jobs import JobRegistry
from .state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.state


def get_jobs(request: Request) -> JobRegistry:
    return request.app.state.state.jobs


def get_artifacts(request: Request) -> ArtifactStore:
    return request.app.state.state.artifacts


def get_store(request: Request):
    return request.app.state.state.store


def get_torrents(request: Request):
    manager = request.app.state.state.torrents
    if manager is None:
        raise HTTPException(status_code=503, detail="The torrent engine is not ready.")
    return manager


StateDep = Annotated[AppState, Depends(get_state)]
JobsDep = Annotated[JobRegistry, Depends(get_jobs)]
ArtifactsDep = Annotated[ArtifactStore, Depends(get_artifacts)]
StoreDep = Annotated[object, Depends(get_store)]
TorrentsDep = Annotated[object, Depends(get_torrents)]
