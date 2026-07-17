"""Shared fixtures: an app with injected (real-but-temp) state + a client.

The Store is real SQLite on a tmp path — it's fast and pure, so faking it
would test less for no speed win. Everything heavyweight (ffmpeg, Chrome,
LibreOffice, MinerU) stays untouched: those engines are exercised through
their pure command-builders, and routers are tested through validation
paths and small real inputs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from subgen.db import Store
from toolkit_api.artifacts import ArtifactStore
from toolkit_api.jobs import JobRegistry
from toolkit_api.main import create_app
from toolkit_api.state import AppState


@pytest.fixture
def app_state(tmp_path):
    state = AppState(
        store=Store(tmp_path / "sub.db"),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
    )
    yield state
    state.artifacts.cleanup()


@pytest.fixture
def client(app_state):
    app = create_app(state=app_state)
    with TestClient(app) as test_client:
        yield test_client
