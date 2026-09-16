"""FastAPI application factory."""

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
    photofilter,
    purge,
    remux,
    subs,
    torrent,
    watermark,
    webpdf,
)
from .routers.meta import disabled_slugs
from .state import AppState, build_state

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Shell values win; .env only fills gaps.
load_dotenv(BACKEND_DIR / ".env")

# subgen.config defaults to <repo>/data; a blank `SUB_DB_PATH=` in .env must not win.
if not os.environ.get("SUB_DB_PATH"):
    os.environ["SUB_DB_PATH"] = str(BACKEND_DIR / "data" / "sub.db")

_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def _cors_origins() -> list[str]:
    raw = os.environ.get("APP_CORS_ORIGINS", _DEFAULT_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _frontend_dist() -> Path | None:
    override = os.environ.get("APP_STATIC_DIR")
    dist = Path(override) if override else BACKEND_DIR.parent / "frontend" / "dist"
    return dist if dist.is_dir() else None


def create_app(state: AppState | None = None) -> FastAPI:
    """Build the app; pass ``state`` to inject fakes (tests)."""
    provided = state is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.state = state or build_state()
        yield
        # Jobs first, so their child processes are cleaned up before teardown.
        app.state.state.jobs.shutdown()
        if app.state.state.torrents is not None:
            app.state.state.torrents.close()
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

    for api_router in (meta.router, fs.router, jobs.router):
        app.include_router(api_router, prefix="/api")

    # TOOLKIT_DISABLED_TOOLS is a kill switch: disabled routers are never mounted.
    disabled = disabled_slugs()
    for slug, api_router in (
        ("magnet-scraper", magnet.router),
        ("remux", remux.router),
        ("file-gatherer", gather.router),
        ("cache-purge", purge.router),
        ("photos-library-filter", photofilter.router),
        ("image-to-pdf", imgpdf.router),
        ("web-images-to-pdf", webpdf.router),
        ("doc-to-pdf", docpdf.router),
        ("doc-to-markdown", docmd.router),
        ("subscription", subs.router),
        ("dep-upgrade", depsync.router),
        ("torrent-downloader", torrent.router),
        ("watermark-remover", watermark.router),
    ):
        if slug not in disabled:
            app.include_router(api_router, prefix="/api")
    # Public /sub/{id} for proxy clients; gated by SUB_ACCESS_TOKEN in the router.
    if "subscription" not in disabled:
        app.include_router(subs.public_router)

    # The built frontend, mounted last so it only catches unmatched paths.
    if not provided:
        dist = _frontend_dist()
        if dist is not None:
            app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
