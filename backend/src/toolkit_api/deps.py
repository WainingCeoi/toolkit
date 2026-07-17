"""Dependency injection: routers read the shared state off app.state."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

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


StateDep = Annotated[AppState, Depends(get_state)]
JobsDep = Annotated[JobRegistry, Depends(get_jobs)]
ArtifactsDep = Annotated[ArtifactStore, Depends(get_artifacts)]
StoreDep = Annotated[object, Depends(get_store)]
