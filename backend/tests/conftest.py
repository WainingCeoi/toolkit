"""Shared fixtures: an app with injected (real-but-temp) state + a client.

The Store is real SQLite on a tmp path — it's fast and pure, so faking it
would test less for no speed win. Everything heavyweight (ffmpeg, Chrome,
LibreOffice, MinerU) stays untouched: those engines are exercised through
their pure command-builders, and routers are tested through validation
paths and small real inputs.
"""

from __future__ import annotations

import sys

import httpx2
import pytest

from subgen.db import Store
from toolkit_api.artifacts import ArtifactStore
from toolkit_api.jobs import JobRegistry
from toolkit_api.main import create_app
from toolkit_api.state import AppState

# starlette's TestClient imports its HTTP client under the bare name `httpx`.
# This project depends on httpx2 — pydantic's maintained successor — and
# starlette uses only the API surface the two share, so aliasing the module is
# enough to bridge them. conftest is imported before any test module, so every
# `from fastapi.testclient import TestClient` below sees the alias already set.
#
# This cannot be solved by upgrading instead: starlette >= 1.0 speaks httpx2
# natively, but the docmd extra pulls mineru, which pins gradio 6.8.0, which
# caps starlette < 1.0. Drop that pin and this alias can go.
sys.modules.setdefault("httpx", httpx2)


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
    # Imported here, not at module scope, so it resolves after the alias above.
    from fastapi.testclient import TestClient

    app = create_app(state=app_state)
    with TestClient(app) as test_client:
        yield test_client
