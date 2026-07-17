"""FastAPI application factory.

Run with:  uv run uvicorn toolkit_api.main:app --reload

One AppState (SQLite store, job registry, artifact spool, browser session) is
built at startup and shared by every request — this app is single-user and
single-process (see host.py: one worker, always). Routers mount under /api so
the Vite dev proxy can forward /api/* here; the public subscription route
lives at /sub/{id} for proxy clients. In single-origin production
(make start / make host) the built frontend in frontend/dist is served from
this same server.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routers import fs, jobs, meta
from .state import AppState, build_state

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Backend-local config (WEBSITE_URL, SUB_*, ...). Values already exported in
# the shell win; the file only fills gaps.
load_dotenv(BACKEND_DIR / ".env")

# Dev origins for the Vite frontend. Override with a comma-separated env var.
# In single-origin production the UI is same-origin, so CORS is only
# exercised when hitting the API directly.
_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def _cors_origins() -> list[str]:
    raw = os.environ.get("APP_CORS_ORIGINS", _DEFAULT_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _frontend_dist() -> Path | None:
    """Built frontend location, if present (APP_STATIC_DIR or ../frontend/dist)."""
    override = os.environ.get("APP_STATIC_DIR")
    dist = Path(override) if override else BACKEND_DIR.parent / "frontend" / "dist"
    return dist if dist.is_dir() else None


def create_app(state: AppState | None = None) -> FastAPI:
    """Build the app. Pass `state` to inject fakes (tests); otherwise the real
    shared state is built at startup."""
    provided = state is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.state = state or build_state()
        yield
        # Session-scoped resources die with the process.
        if app.state.state.browser is not None:
            app.state.state.browser.shutdown()
        app.state.state.artifacts.cleanup()

    app = FastAPI(title="Toolkit API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(meta.router, prefix="/api")
    app.include_router(fs.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")

    # Serve the built frontend from this same server, if present (make start /
    # make host). Mounted LAST so it only catches unmatched paths, and skipped
    # in tests (which inject state and only exercise the API).
    if not provided:
        dist = _frontend_dist()
        if dist is not None:
            app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
