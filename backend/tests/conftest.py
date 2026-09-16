"""Shared fixtures: an app with injected temp state, plus a client."""

from __future__ import annotations

import sys

import httpx2
import pytest

from subgen.db import Store
from toolkit_api.artifacts import ArtifactStore
from toolkit_api.devices import DeviceBook
from toolkit_api.jobs import JobRegistry
from toolkit_api.main import create_app
from toolkit_api.state import AppState
from toolkit_api.watermarks import WatermarkBatches

# starlette's TestClient imports `httpx`; this project ships httpx2, whose API
# it shares, so alias it before any test module imports TestClient.
sys.modules.setdefault("httpx", httpx2)


@pytest.fixture
def app_state(tmp_path):
    state = AppState(
        store=Store(tmp_path / "sub.db"),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        devices=DeviceBook(tmp_path / "torrents.db"),
        watermarks=WatermarkBatches(tmp_path / "watermark"),
    )
    yield state
    state.artifacts.cleanup()


@pytest.fixture
def client(app_state):
    # Imported here, not at module scope, so it resolves after the alias above.
    from fastapi.testclient import TestClient

    app = create_app(state=app_state)
    with TestClient(app) as test_client:
        yield test_client
