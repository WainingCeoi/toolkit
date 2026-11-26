"""Web Images to PDF and shared upload limits."""

from __future__ import annotations

import base64
import threading
import time
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from pillow_heif import from_pillow

from toolkit_api.routers import webpdf
from toolkit_engine.webpdf import (
    BrowserSession,
    parse_page,
    sanitize_filename,
    scrape_images_from_source,
)

NO_IMAGES_DETAIL = (
    "No images found on the page (selector `img[class*=bi]`). "
    "Make sure every page finished loading before capturing."
)


def _png_bytes(color="red"):
    buf = BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _data_uri(image, subtype="png"):
    return f"data:image/{subtype};base64," + base64.b64encode(image).decode()


class FakeBrowserSession:
    def __init__(self, html=""):
        self._html = html
        self.url = "not-a-url"
        self.current_url = self.url
        self.quit_called = False

    @property
    def is_open(self):
        return not self.quit_called

    def page_source(self):
        return self._html

    def quit(self):
        self.quit_called = True

    shutdown = quit


def test_read_uploads_enforces_size_cap():
    from fastapi import HTTPException, UploadFile

    from toolkit_api.uploads import read_uploads

    small = UploadFile(filename="a.bin", file=BytesIO(b"x" * 10))
    assert read_uploads([small], max_total=100) == [b"x" * 10]

    small.file.seek(0)
    with pytest.raises(HTTPException) as exc:
        read_uploads([small], max_total=5)
    assert exc.value.status_code == 413


# --- Web Images to PDF — engine ---
def test_sanitize_filename():
    assert sanitize_filename('a\\b/c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_filename("My Comic: Vol 1") == "My Comic_ Vol 1"
    assert sanitize_filename("a<<>>b") == "a_b"
    assert sanitize_filename("plain name") == "plain name"
    assert sanitize_filename("") == "web"
    assert sanitize_filename("   ") == "web"


def test_sanitize_filename_caps_long_titles():
    # An SEO-stuffed <title> otherwise fails the capture with "File name too long".
    assert len(sanitize_filename("书名" * 300)) == 200


def test_scrape_images_from_source_data_uris():
    with BytesIO() as encoded, Image.new("RGB", (16, 16), "blue") as image:
        from_pillow(image).save(encoded)
        heic = _data_uri(encoded.getvalue(), "heic")
    html = (
        "<html><head><title>My Comic: Vol 1</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        f'<img class="bi" src="{heic}">'
        "</body></html>"
    )
    pdf_name, images, skipped = scrape_images_from_source(
        parse_page(html), "http://example.com"
    )
    assert pdf_name == "My Comic_ Vol 1.pdf"
    assert skipped == 0
    assert [image.size for image in images] == [(4, 4), (16, 16)]


def test_scrape_images_selector_and_skip_counting():
    html = (
        "<html><body>"  # no <title> -> "web.pdf"
        f'<img class="bi" src="{_data_uri(_png_bytes())}">'
        '<img class="bi" src="data:image/png;base64,@@@@">'  # broken -> skipped
        '<img class="bi">'  # no src -> ignored entirely
        f'<img class="other" src="{_data_uri(_png_bytes())}">'  # not selected
        "</body></html>"
    )
    pdf_name, images, skipped = scrape_images_from_source(
        parse_page(html), "http://example.com"
    )
    assert pdf_name == "web.pdf"
    assert len(images) == 1
    assert skipped == 1


def test_browser_session_shutdown_alias_and_safe_quit():
    session = BrowserSession()
    assert session.is_open is False
    session.shutdown()  # no driver; must not raise
    assert BrowserSession.shutdown is BrowserSession.quit


def test_browser_session_current_url_follows_the_driver():
    session = BrowserSession()
    session.url = "http://example.com/start"
    session._driver = SimpleNamespace(current_url="http://example.com/chapter-2")
    assert session.current_url == "http://example.com/chapter-2"


# --- Web Images to PDF — router ---
def test_webpdf_status_reflects_browser_slot(client, app_state):
    assert client.get("/api/webpdf/status").json() == {"open": False}
    app_state.browser = FakeBrowserSession()
    assert client.get("/api/webpdf/status").json() == {"open": True}


def test_webpdf_open_error_returns_502(client, app_state, monkeypatch):
    class BoomSession:
        def open(self, url):
            raise RuntimeError("no chrome")

    monkeypatch.setattr(webpdf, "BrowserSession", BoomSession)
    resp = client.post("/api/webpdf/open", json={"url": "http://example.com"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Could not open browser: no chrome"
    assert app_state.browser is None


def test_webpdf_open_stores_session(client, app_state, monkeypatch):
    opened = []

    class RecordingSession(FakeBrowserSession):
        def open(self, url):
            opened.append(url)

    monkeypatch.setattr(webpdf, "BrowserSession", RecordingSession)
    resp = client.post("/api/webpdf/open", json={"url": "http://example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"open": True}
    assert opened == ["http://example.com"]
    assert isinstance(app_state.browser, RecordingSession)


def test_webpdf_capture_without_session_is_409(client):
    assert client.post("/api/webpdf/capture").status_code == 409


def test_webpdf_capture_no_images_is_400_and_keeps_browser(client, app_state):
    fake = FakeBrowserSession("<html><head><title>t</title></head><body></body></html>")
    app_state.browser = fake
    resp = client.post("/api/webpdf/capture")
    assert resp.status_code == 400
    assert resp.json()["detail"] == NO_IMAGES_DETAIL
    assert app_state.browser is fake
    assert fake.quit_called is False


def test_webpdf_capture_resolves_images_against_the_current_page(
    client, app_state, monkeypatch
):
    seen = {}

    def spy(soup, page_url):
        seen["url"] = page_url
        return "x.pdf", [], 0

    monkeypatch.setattr(webpdf, "scrape_images_from_source", spy)
    fake = FakeBrowserSession("<html><body></body></html>")
    fake.current_url = "http://example.com/chapter-2"  # the user browsed on
    app_state.browser = fake

    assert client.post("/api/webpdf/capture").status_code == 400
    assert seen["url"] == "http://example.com/chapter-2"


def test_webpdf_capture_builds_pdf_and_closes_browser(client, app_state):
    html = (
        "<html><head><title>Book: One</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        f'<img class="bi" src="{_data_uri(_png_bytes("blue"))}">'
        "</body></html>"
    )
    fake = FakeBrowserSession(html)
    app_state.browser = fake

    resp = client.post("/api/webpdf/capture")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Book_ One.pdf"
    assert body["pages"] == 2
    assert body["skipped"] == 0
    assert body["warn"]

    assert fake.quit_called is True
    assert app_state.browser is None

    download = client.get(f"/api/artifacts/{body['artifact_id']}")
    assert download.status_code == 200
    assert download.content.startswith(b"%PDF")
    # FileResponse RFC-5987-encodes the space in the filename.
    assert "Book_%20One.pdf" in download.headers["content-disposition"]


def test_webpdf_capture_parses_the_page_once(client, app_state, monkeypatch):
    parsed = []
    real_parse = webpdf.parse_page

    def counting(page_source):
        parsed.append(page_source)
        return real_parse(page_source)

    monkeypatch.setattr(webpdf, "parse_page", counting)
    html = (
        "<html><head><title>Book</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        "</body></html>"
    )
    app_state.browser = FakeBrowserSession(html)

    assert client.post("/api/webpdf/capture").status_code == 200
    assert len(parsed) == 1


def test_webpdf_close_is_idempotent(client, app_state):
    assert client.post("/api/webpdf/close").json() == {"open": False}
    fake = FakeBrowserSession()
    app_state.browser = fake
    assert client.post("/api/webpdf/close").json() == {"open": False}
    assert fake.quit_called is True
    assert app_state.browser is None


def test_webpdf_open_is_race_safe(client, app_state, monkeypatch):
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
        r = client.post("/api/webpdf/open", json={"url": "http://example.com"})
        results[key] = r.status_code

    first = threading.Thread(target=call, args=("first",))
    first.start()
    assert in_open.wait(timeout=5)
    second = threading.Thread(target=call, args=("second",))
    second.start()
    time.sleep(0.2)  # let the second call reach the lock
    may_finish.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert len(launches) == 1
    assert sorted(results.values()) == [200, 409]
    assert isinstance(app_state.browser, SlowSession)


def test_webpdf_capture_preserves_newer_session(client, app_state, monkeypatch):
    html = (
        "<html><head><title>Book</title></head><body>"
        f'<img class="bi" src="{_data_uri(_png_bytes("red"))}">'
        "</body></html>"
    )
    captured = FakeBrowserSession(html)
    newer = FakeBrowserSession(html)
    app_state.browser = captured

    def reassign(soup, pdf_path):
        # A newer /open lands mid-capture.
        app_state.browser = newer
        return None

    monkeypatch.setattr(webpdf, "add_bookmark", reassign)
    resp = client.post("/api/webpdf/capture")
    assert resp.status_code == 200

    assert captured.quit_called is True
    assert app_state.browser is newer
    assert newer.quit_called is False
