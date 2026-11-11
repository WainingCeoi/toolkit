"""Bounded shared-browser scrolling; tests use no real Chrome window."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from toolkit_engine import webpdf


class ScrollSession:
    def __init__(self):
        self.calls = []
        self.quit_called = False

    @property
    def is_open(self):
        return not self.quit_called

    def scroll(self, steps, wait_ms):
        self.calls.append((steps, wait_ms))
        return {
            "steps": steps,
            "matched_images": 3,
            "loaded_images": 2,
            "at_bottom": False,
            "url": "https://example.test/chapter",
        }

    def quit(self):
        self.quit_called = True

    shutdown = quit


def test_scroll_uses_shared_session_and_keeps_it_open(client, app_state):
    session = ScrollSession()
    app_state.browser = session
    response = client.post("/api/webpdf/scroll", json={"steps": 2, "wait_ms": 100})
    assert response.status_code == 200
    assert response.json() == {
        "steps": 2,
        "matched_images": 3,
        "loaded_images": 2,
        "at_bottom": False,
        "url": "https://example.test/chapter",
    }
    assert session.calls == [(2, 100)]
    assert app_state.browser is session
    assert not session.quit_called


def test_scroll_defaults_and_no_session_error(client, app_state):
    assert client.post("/api/webpdf/scroll", json={}).status_code == 409
    app_state.browser = session = ScrollSession()
    assert client.post("/api/webpdf/scroll", json={}).status_code == 200
    assert session.calls == [(5, 500)]


@pytest.mark.parametrize(
    "payload",
    [{"steps": 0}, {"steps": 21}, {"wait_ms": -1}, {"wait_ms": 2001}],
)
def test_scroll_rejects_unbounded_operations(client, app_state, payload):
    app_state.browser = session = ScrollSession()
    assert client.post("/api/webpdf/scroll", json=payload).status_code == 422
    assert not session.calls


def test_scroll_driver_error_keeps_shared_session(client, app_state):
    class Broken(ScrollSession):
        def scroll(self, steps, wait_ms):
            raise RuntimeError("Chrome window was closed")

    app_state.browser = session = Broken()
    response = client.post("/api/webpdf/scroll", json={})
    assert response.status_code == 502
    assert response.json()["detail"] == "Scroll failed: Chrome window was closed"
    assert app_state.browser is session


def test_scroll_serializes_against_close(client, app_state):
    entered, release, close_started = (threading.Event() for _ in range(3))

    class SlowSession(ScrollSession):
        def scroll(self, steps, wait_ms):
            entered.set()
            assert release.wait(5)
            assert not self.quit_called
            return super().scroll(steps, wait_ms)

    app_state.browser = session = SlowSession()

    def close():
        close_started.set()
        return client.post("/api/webpdf/close")

    with ThreadPoolExecutor(max_workers=2) as pool:
        scrolling = pool.submit(client.post, "/api/webpdf/scroll", json={})
        try:
            assert entered.wait(2)
            closing = pool.submit(close)
            assert close_started.wait(2)
            assert not session.quit_called
        finally:
            release.set()
        assert scrolling.result().status_code == 200
        assert closing.result().json() == {"open": False}
    assert session.quit_called
    assert app_state.browser is None


def test_engine_scroll_uses_fixed_scripts_and_waits(monkeypatch):
    calls, waits = [], []

    class Driver:
        current_url = "https://example.test/current"

        def execute_script(self, script):
            calls.append(script)
            if "return" in script:
                return {"matched_images": 4, "loaded_images": 4, "at_bottom": True}

    session = webpdf.BrowserSession()
    session._driver = Driver()
    monkeypatch.setattr(webpdf.time, "sleep", waits.append)
    result = session.scroll(3, 250)
    assert waits == [0.25, 0.25, 0.25]
    assert len(calls) == 4
    assert len(set(calls[:3])) == 1
    assert "window.scrollBy" in calls[0]
    assert "img[class*=bi]" in calls[3]
    assert result == {
        "steps": 3,
        "matched_images": 4,
        "loaded_images": 4,
        "at_bottom": True,
        "url": "https://example.test/current",
    }
