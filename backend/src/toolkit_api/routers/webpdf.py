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
    if state.browser is not None:
        state.browser.quit()
        state.browser = None
    return StatusOut(open=False)


@router.post("/capture", response_model=CaptureOut)
def capture(state: StateDep, artifacts: ArtifactsDep) -> CaptureOut:
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
            warn = add_bookmark(url, pdf_path)
            pdf_bytes = Path(pdf_path).read_bytes()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Capture failed: {e}") from e

    # Page behavior: a successful capture also closes the browser.
    session.quit()
    state.browser = None

    artifact_id = artifacts.put_bytes(pdf_name, pdf_bytes, "application/pdf")
    return CaptureOut(
        artifact_id=artifact_id,
        name=pdf_name,
        pages=len(images),
        skipped=skipped,
        warn=warn,
    )
