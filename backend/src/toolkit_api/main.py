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

from .routers import (
    depsync,
    docmd,
    docpdf,
    fs,
    gather,
    imgpdf,
    jobs,
    magnet,
    meta,
    purge,
    remux,
    subs,
    webpdf,
)
from .state import AppState, build_state

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Backend-local config (WEBSITE_URL, SUB_*, ...). Values already exported in
# the shell win; the file only fills gaps.
load_dotenv(BACKEND_DIR / ".env")

# Runtime data lives under backend/data/ (covered by backend/.gitignore).
# Seeded here rather than edited into the lifted subgen config, whose own
# default still points at the old repo-root data/; setdefault keeps any
# user-set SUB_DB_PATH winning.
os.environ.setdefault("SUB_DB_PATH", str(BACKEND_DIR / "data" / "sub.db"))

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
        # Session-scoped resources die with the process. Cancel in-flight jobs
        # first so their children (ffmpeg, …) are cleaned up before teardown.
        app.state.state.jobs.shutdown()
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

    for api_router in (
        meta.router,
        fs.router,
        jobs.router,
        magnet.router,
        remux.router,
        gather.router,
        purge.router,
        imgpdf.router,
        webpdf.router,
        docpdf.router,
        docmd.router,
        subs.router,
        depsync.router,
    ):
        app.include_router(api_router, prefix="/api")
    # Public subscription route for proxy clients: GET /sub/{id} (no /api). It
    # carries its own SUB_ACCESS_TOKEN gate for token-gated fetches.
    app.include_router(subs.public_router)

    # Serve the built frontend from this same server, if present (make start /
    # make host). Mounted LAST so it only catches unmatched paths, and skipped
    # in tests (which inject state and only exercise the API).
    if not provided:
        dist = _frontend_dist()
        if dist is not None:
            app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
