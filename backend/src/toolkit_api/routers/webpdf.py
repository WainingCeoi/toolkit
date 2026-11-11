"""Web Images to PDF: drive one live Chrome session, capture into a PDF."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from toolkit_engine.webpdf import (
    BrowserSession,
    add_bookmark,
    build_pdf,
    parse_page,
    scrape_images_from_source,
)

from ..deps import ArtifactsDep, StateDep

router = APIRouter(prefix="/webpdf", tags=["webpdf"])


class OpenIn(BaseModel):
    url: str


class StatusOut(BaseModel):
    open: bool


class ScrollIn(BaseModel):
    steps: int = Field(default=5, ge=1, le=20)
    wait_ms: int = Field(default=500, ge=0, le=2000)


class ScrollOut(BaseModel):
    steps: int
    matched_images: int
    loaded_images: int
    at_bottom: bool
    url: str | None = None


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
    # Hold the lock across check + launch + assign, or a double open leaks a driver.
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


@router.post("/scroll", response_model=ScrollOut)
def scroll(req: ScrollIn, state: StateDep) -> ScrollOut:
    """Scroll the shared browser, with bounded waits and no arbitrary script input."""
    # Serialize against close/open and capture's snapshot: never scroll a replacement
    # session or let Chrome quit midway through this bounded operation.
    with state.browser_lock:
        if not _session_open(state):
            raise HTTPException(status_code=409, detail="No browser session is open.")
        try:
            return ScrollOut(**state.browser.scroll(req.steps, req.wait_ms))
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"Scroll failed: {exc}"
            ) from exc


@router.post("/close", response_model=StatusOut)
def close_browser(state: StateDep) -> StatusOut:
    # Swap the slot out under the lock, then quit outside it (quit is slow).
    with state.browser_lock:
        session, state.browser = state.browser, None
    if session is not None:
        session.quit()
    return StatusOut(open=False)


@router.post("/capture", response_model=CaptureOut)
def capture(state: StateDep, artifacts: ArtifactsDep) -> CaptureOut:
    try:
        with state.browser_lock:
            if not _session_open(state):
                raise HTTPException(
                    status_code=409, detail="No browser session is open."
                )
            session = state.browser
            page_source, current_url = session.page_source(), session.current_url
        soup = parse_page(page_source)
        pdf_name, images, skipped = scrape_images_from_source(soup, current_url or "")
        if not images:
            # Leave the browser open for a retry.
            raise HTTPException(
                status_code=400,
                detail=(
                    "No images found on the page (selector `img[class*=bi]`). "
                    "Make sure every page finished loading before capturing."
                ),
            )
        with tempfile.TemporaryDirectory() as tmp_dir:
            pdf_path = build_pdf(images, tmp_dir, pdf_name)
            warn = add_bookmark(soup, pdf_path)
            # Stays inside the temp-dir block: put_file moves the PDF out of it.
            artifact_id = artifacts.put_file(
                pdf_name, Path(pdf_path), "application/pdf"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Capture failed: {e}") from e

    # Clear the slot only if it still holds this session; a newer one must survive.
    with state.browser_lock:
        session.quit()
        if state.browser is session:
            state.browser = None

    return CaptureOut(
        artifact_id=artifact_id,
        name=pdf_name,
        pages=len(images),
        skipped=skipped,
        warn=warn,
    )
