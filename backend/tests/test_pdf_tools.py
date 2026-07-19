"""Image to PDF + Web Images to PDF: engines and routers.

Hermetic by construction: images are generated in-memory with PIL, scraped
pages are canned HTML whose images are data: URIs (nothing is fetched), and
the browser is a fake injected onto app_state — Chrome is never launched.
add_bookmark parses the captured page_source directly (no network), and the
canned pages carry no TOC anchors so it simply reports none were found.
"""

from __future__ import annotations

import base64
import threading
import time
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from toolkit_api.main import create_app
from toolkit_api.routers import imgpdf, webpdf
from toolkit_engine.webpdf import (
    BrowserSession,
    sanitize_filename,
    scrape_images_from_source,
)

NO_IMAGES_DETAIL = (
    "No images found on the page (selector `img[class*=bi]`). "
    "Make sure every page finished loading before capturing."
)


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
    app.include_router(imgpdf.router, prefix="/api")
    app.include_router(webpdf.router, prefix="/api")
    with TestClient(app) as c:
        yield c


def _png_bytes(color="red"):
    buf = BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _data_uri(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


class FakeBrowserSession:
    """Stands in for toolkit_engine.webpdf.BrowserSession — no Chrome."""

    def __init__(self, html=""):
        self._html = html
        self.url = "not-a-url"  # never fetched — capture uses page_source
        self.quit_called = False

    @property
    def is_open(self):
        return not self.quit_called

    def page_source(self):
        return self._html

    def quit(self):
        self.quit_called = True

    shutdown = quit


# =======================================================
# Image to PDF
# =======================================================
def test_img_to_pdf_end_to_end(tool_client):
    files = [
        ("files", ("b.png", _png_bytes("red"), "image/png")),
        ("files", ("a.png", _png_bytes("blue"), "image/png")),
    ]
    resp = tool_client.post("/api/img-to-pdf", data={"name": "scan"}, files=files)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
    assert resp.headers["content-type"] == "application/pdf"
    assert 'filename="scan.pdf"' in resp.headers["content-disposition"]


def test_img_to_pdf_keeps_explicit_pdf_extension(tool_client):
    files = [("files", ("a.png", _png_bytes(), "image/png"))]
    resp = tool_client.post("/api/img-to-pdf", data={"name": " docs.PDF "}, files=files)
    assert resp.status_code == 200
    assert 'filename="docs.PDF"' in resp.headers["content-disposition"]


def test_img_to_pdf_blank_name_is_400(tool_client):
    files = [("files", ("a.png", _png_bytes(), "image/png"))]
    resp = tool_client.post("/api/img-to-pdf", data={"name": "   "}, files=files)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Please enter a PDF file name."


def test_img_to_pdf_no_files_is_400(tool_client):
    resp = tool_client.post("/api/img-to-pdf", data={"name": "scan"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Please select at least one image first."


def test_img_to_pdf_non_ascii_name_uses_rfc5987(tool_client):
    from urllib.parse import quote

    files = [("files", ("a.png", _png_bytes(), "image/png"))]
    resp = tool_client.post("/api/img-to-pdf", data={"name": "扫描文档"}, files=files)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
    assert resp.headers["content-type"] == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert "filename*=utf-8''" in disposition
    assert quote("扫描文档.pdf") in disposition


def test_img_to_pdf_name_with_quote_is_well_formed(tool_client):
    files = [("files", ("a.png", _png_bytes(), "image/png"))]
    resp = tool_client.post("/api/img-to-pdf", data={"name": 'a"b'}, files=files)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")
    # The inner double-quote must be backslash-escaped, not left bare.
    assert resp.headers["content-disposition"] == 'attachment; filename="a\\"b.pdf"'


# =======================================================
# Web Images to PDF — engine
# =======================================================
def test_sanitize_filename():
    assert sanitize_filename('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_filename("My Comic: Vol 1") == "My Comic_ Vol 1"
    assert sanitize_filename("a<<>>b") == "a_b"  # runs collapse to one _
    assert sanitize_filename("plain name") == "plain name"
    assert sanitize_filename("") == "web"
    assert sanitize_filename("   ") == "web"


def test_scrape_images_from_source_data_uris():
    html = (
        "<html><head><title>My Comic: Vol 1</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        "</body></html>"
    )
    pdf_name, images, skipped = scrape_images_from_source(html, "http://example.com")
    assert pdf_name == "My Comic_ Vol 1.pdf"
    assert len(images) == 1
    assert skipped == 0
    assert images[0].size == (4, 4)


def test_scrape_images_selector_and_skip_counting():
    html = (
        "<html><body>"  # no <title> -> "web.pdf"
        f'<img class="bi" src="{_data_uri(_png_bytes())}">'
        '<img class="bi" src="data:image/png;base64,@@@@">'  # broken -> skipped
        '<img class="bi">'  # no src -> ignored entirely
        f'<img class="other" src="{_data_uri(_png_bytes())}">'  # not selected
        "</body></html>"
    )
    pdf_name, images, skipped = scrape_images_from_source(html, "http://example.com")
    assert pdf_name == "web.pdf"
    assert len(images) == 1
    assert skipped == 1


def test_browser_session_shutdown_alias_and_safe_quit():
    session = BrowserSession()
    assert session.is_open is False
    session.shutdown()  # no driver — must not raise (lifespan calls this)
    assert BrowserSession.shutdown is BrowserSession.quit


# =======================================================
# Web Images to PDF — router
# =======================================================
def test_webpdf_status_reflects_browser_slot(tool_client, app_state):
    assert tool_client.get("/api/webpdf/status").json() == {"open": False}
    app_state.browser = FakeBrowserSession()
    assert tool_client.get("/api/webpdf/status").json() == {"open": True}


def test_webpdf_open_conflicts_when_already_open(tool_client, app_state):
    app_state.browser = FakeBrowserSession()
    resp = tool_client.post("/api/webpdf/open", json={"url": "http://example.com"})
    assert resp.status_code == 409


def test_webpdf_open_error_returns_502(tool_client, app_state, monkeypatch):
    class BoomSession:
        def open(self, url):
            raise RuntimeError("no chrome")

    monkeypatch.setattr(webpdf, "BrowserSession", BoomSession)
    resp = tool_client.post("/api/webpdf/open", json={"url": "http://example.com"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Could not open browser: no chrome"
    assert app_state.browser is None


def test_webpdf_open_stores_session(tool_client, app_state, monkeypatch):
    opened = []

    class RecordingSession(FakeBrowserSession):
        def open(self, url):
            opened.append(url)

    monkeypatch.setattr(webpdf, "BrowserSession", RecordingSession)
    resp = tool_client.post("/api/webpdf/open", json={"url": "http://example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"open": True}
    assert opened == ["http://example.com"]
    assert isinstance(app_state.browser, RecordingSession)


def test_webpdf_capture_without_session_is_409(tool_client):
    assert tool_client.post("/api/webpdf/capture").status_code == 409


def test_webpdf_capture_no_images_is_400_and_keeps_browser(tool_client, app_state):
    fake = FakeBrowserSession("<html><head><title>t</title></head><body></body></html>")
    app_state.browser = fake
    resp = tool_client.post("/api/webpdf/capture")
    assert resp.status_code == 400
    assert resp.json()["detail"] == NO_IMAGES_DETAIL
    assert app_state.browser is fake  # page behavior: retry without relaunching
    assert fake.quit_called is False


def test_webpdf_capture_builds_pdf_and_closes_browser(tool_client, app_state):
    html = (
        "<html><head><title>Book: One</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        f'<img class="bi" src="{_data_uri(_png_bytes("blue"))}">'
        "</body></html>"
    )
    fake = FakeBrowserSession(html)
    app_state.browser = fake

    resp = tool_client.post("/api/webpdf/capture")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Book_ One.pdf"
    assert body["pages"] == 2
    assert body["skipped"] == 0
    assert body["warn"]  # bookmark re-fetch of "not-a-url" fails offline

    # Page behavior: a successful capture closes the browser.
    assert fake.quit_called is True
    assert app_state.browser is None

    download = tool_client.get(f"/api/artifacts/{body['artifact_id']}")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    # FileResponse RFC-5987-encodes the space in the filename.
    assert "Book_%20One.pdf" in download.headers["content-disposition"]


def test_webpdf_close_is_idempotent(tool_client, app_state):
    assert tool_client.post("/api/webpdf/close").json() == {"open": False}
    fake = FakeBrowserSession()
    app_state.browser = fake
    assert tool_client.post("/api/webpdf/close").json() == {"open": False}
    assert fake.quit_called is True
    assert app_state.browser is None


def test_webpdf_open_is_race_safe(tool_client, app_state, monkeypatch):
    # Two near-simultaneous /open calls (double-click / retry) must launch
    # exactly ONE Chrome: the check + launch + assign is serialized, so the
    # loser blocks and then gets a clean 409 instead of leaking a driver.
    launches: list[str] = []
    launches_lock = threading.Lock()
    in_open = threading.Event()
    may_finish = threading.Event()

    class SlowSession(FakeBrowserSession):
        def open(self, url):
            with launches_lock:
                launches.append(url)
            in_open.set()
            may_finish.wait(timeout=5)

    monkeypatch.setattr(webpdf, "BrowserSession", SlowSession)

    results: dict[str, int] = {}

    def call(key):
        r = tool_client.post("/api/webpdf/open", json={"url": "http://example.com"})
        results[key] = r.status_code

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert in_open.wait(timeout=5)  # first has passed the check and is launching
    second = threading.Thread(target=call, args=("second",))
    second.start()
    time.sleep(0.2)  # let the second reach its check (buggy) or block (fixed)
    may_finish.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert len(launches) == 1  # only one driver spawned; no leak
    assert sorted(results.values()) == [200, 409]
    assert isinstance(app_state.browser, SlowSession)


def test_webpdf_capture_preserves_newer_session(tool_client, app_state, monkeypatch):
    # If a newer session is opened mid-capture, the success path must clear the
    # slot only when it still holds the session it captured from (identity
    # check) — otherwise it clobbers the newer session.
    html = (
        "<html><head><title>Book</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        "</body></html>"
    )
    captured = FakeBrowserSession(html)
    newer = FakeBrowserSession(html)
    app_state.browser = captured

    def reassign(page_source, pdf_path):
        # Stand in for a newer /open that landed while this capture was running.
        app_state.browser = newer
        return None

    monkeypatch.setattr(webpdf, "add_bookmark", reassign)
    resp = tool_client.post("/api/webpdf/capture")
    assert resp.status_code == 200

    assert captured.quit_called is True  # the captured session is closed
    assert app_state.browser is newer  # the newer session is NOT clobbered
    assert newer.quit_called is False
