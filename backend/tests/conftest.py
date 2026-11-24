"""Shared fixtures: an app with injected temp state, plus a client."""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

import httpx2
import pytest

# starlette's TestClient imports `httpx`; httpx2 shares its API, so alias it first.
sys.modules.setdefault("httpx", httpx2)


def pytest_configure(config):
    # Direct pytest runs must isolate settings before test modules import the app.
    directory = tempfile.TemporaryDirectory(prefix="toolkit-pytest-")
    config.add_cleanup(directory.cleanup)
    environment = pytest.MonkeyPatch()
    config.add_cleanup(environment.undo)
    root = Path(directory.name)
    for setting, name in (
        ("HOME", "home"),
        ("TMPDIR", "tmp"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_CONFIG_HOME", "config"),
    ):
        path = root / name
        path.mkdir()
        environment.setenv(setting, str(path))
    environment.setattr(tempfile, "tempdir", str(root / "tmp"))
    for setting in tuple(os.environ):
        if setting.startswith("GIT_") or setting.lower().endswith("_proxy"):
            environment.delenv(setting)
    environment.setenv("PYTHON_DOTENV_DISABLED", "1")
    environment.setenv("APP_STATIC_DIR", str(root / "dist"))
    environment.setenv("SUB_DB_PATH", str(root / "sub.db"))
    environment.setenv("TOOLKIT_DISABLED_TOOLS", "")
    environment.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Fixtures can live under this checkout when TMPDIR is set by make test.
    base_temp = config.getoption("basetemp")
    git_ceiling = Path(base_temp).resolve() if base_temp else root
    environment.setenv("GIT_CEILING_DIRECTORIES", str(git_ceiling))


@pytest.fixture
def refusing_loopback_url():
    # Reserve a port without listening, so clients get a deterministic refusal.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


@pytest.fixture
def app_state(tmp_path):
    from subgen.db import Store
    from toolkit_api.artifacts import ArtifactStore
    from toolkit_api.devices import DeviceBook
    from toolkit_api.jobs import JobRegistry
    from toolkit_api.state import AppState

    state = AppState(
        store=Store(tmp_path / "sub.db"),
        jobs=JobRegistry(),
        artifacts=ArtifactStore(),
        devices=DeviceBook(tmp_path / "torrents.db"),
    )
    yield state
    state.artifacts.cleanup()


@pytest.fixture
def client(app_state):
    # Imported here, not at module scope, so it resolves after the alias above.
    from fastapi.testclient import TestClient

    from toolkit_api.main import create_app

    app = create_app(state=app_state)
    with TestClient(app) as test_client:
        yield test_client
