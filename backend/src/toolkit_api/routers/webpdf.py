"""Web Images to PDF: drive one live Chrome session, capture into a PDF.

Mirrors the page's model exactly: at most ONE browser session at a time
(state.browser), the user scrolls the real Chrome window until every image
has loaded, then capture scrapes page_source, builds the PDF, adds bookmarks
best-effort, and closes the browser. The PDF lands in the artifact store
instead of a typed output folder (download replaces the Desktop write).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from toolkit_engine.webpdf import (
    BrowserSession,
    add_bookmark,
    build_pdf,
    scrape_images_from_source,
)

from ..deps import ArtifactsDep, StateDep

router = APIRouter(prefix="/webpdf", tags=["webpdf"])


class OpenIn(BaseModel):
    url: str


class StatusOut(BaseModel):
    open: bool


class CaptureOut(BaseModel):
    artifact_id: str
    name: str
    pages: int
    skipped: int
    warn: str | None = None


def _session_open(state) -> bool:
    return state.browser is not None and state.browser.is_open


@router.post("/open", response_model=StatusOut)
def open_browser(req: OpenIn, state: StateDep) -> StatusOut:
    # Hold browser_lock across check + launch + assign so a near-simultaneous
    # second open (double-click / retry) blocks and then gets a clean 409
    # instead of both passing the check and each leaking a Chrome driver.
    with state.browser_lock:
        if _session_open(state):
            raise HTTPException(
                status_code=409, detail="A browser session is already open."
            )
        session = BrowserSession()
        try:
            session.open(req.url)
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"Could not open browser: {e}"
            ) from e
        state.browser = session
    return StatusOut(open=True)


@router.get("/status", response_model=StatusOut)
def status(state: StateDep) -> StatusOut:
    return StatusOut(open=_session_open(state))


@router.post("/close", response_model=StatusOut)
def close_browser(state: StateDep) -> StatusOut:
    # Ok even if none is open — the page's close button swallows quit errors.
    # Swap the slot out under the lock, then quit outside it (quit is slow).
    with state.browser_lock:
        session, state.browser = state.browser, None
    if session is not None:
        session.quit()
    return StatusOut(open=False)


@router.post("/capture", response_model=CaptureOut)
def capture(state: StateDep, artifacts: ArtifactsDep) -> CaptureOut:
    # Read the single browser slot under the lock (matching open/close), then do
    # the slow scrape/build work outside it.
    with state.browser_lock:
        if not _session_open(state):
            raise HTTPException(status_code=409, detail="No browser session is open.")
        session = state.browser
    url = session.url or ""
    try:
        page_source = session.page_source()
        pdf_name, images, skipped = scrape_images_from_source(page_source, url)
        if not images:
            # Page behavior: error out but leave the browser open for a retry.
            raise HTTPException(
                status_code=400,
                detail=(
                    "No images found on the page (selector `img[class*=bi]`). "
                    "Make sure every page finished loading before capturing."
                ),
            )
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = build_pdf(images, tmp_dir, pdf_name)
            warn = add_bookmark(page_source, pdf_path)
            # Move the finished PDF into the artifact store while the temp dir
            # still exists — no full-file read-into-RAM-then-write round-trip.
            artifact_id = artifacts.put_file(
                pdf_name, Path(pdf_path), "application/pdf"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Capture failed: {e}") from e

    # Page behavior: a successful capture also closes the browser. Only clear
    # the slot if it still holds the session we captured from — a newer session
    # opened meanwhile (double-click / retry) must not be clobbered.
    session.quit()
    with state.browser_lock:
        if state.browser is session:
            state.browser = None

    return CaptureOut(
        artifact_id=artifact_id,
        name=pdf_name,
        pages=len(images),
        skipped=skipped,
        warn=warn,
    )
