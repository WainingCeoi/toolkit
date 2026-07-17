"""Magnet Scraper: dedupe endpoint, auto/manual jobs, pagination slicing.

Hermetic: requests.get and get_magnet_link are monkeypatched (no network),
and ENV_PATH is pointed at a tmp .env so the real backend/.env is never read
or written.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.main import create_app
from toolkit_api.routers import magnet as magnet_router
from toolkit_engine import magnet as magnet_engine


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
    app.include_router(magnet_router.router, prefix="/api")
    with TestClient(app) as c:
        yield c


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in FINISHED_STATES:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# ---------------------------------------------------------------- dedupe ---


def test_dedupe_removes_duplicates_and_preserves_first_seen_order(tool_client):
    links = ["magnet:?b", "magnet:?a", "magnet:?b", "magnet:?c", "magnet:?a"]
    resp = tool_client.post("/api/magnet/dedupe", json={"links": links})
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique"] == ["magnet:?b", "magnet:?a", "magnet:?c"]
    assert body["count"] == 3


def test_dedupe_empty_is_400_with_page_warning(tool_client):
    resp = tool_client.post("/api/magnet/dedupe", json={"links": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Please enter at least one magnet link"


# ------------------------------------------------------------------ auto ---


def test_auto_missing_website_url_fails_with_page_error(
    tool_client, monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    monkeypatch.delenv("CUTOFF_VIDEO", raising=False)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "failed"
    assert snap["error"] == "❌ WEBSITE_URL is not set in .env."


def test_auto_missing_cutoff_fails_with_page_error(tool_client, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.delenv("CUTOFF_VIDEO", raising=False)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "failed"
    assert snap["error"] == "❌ CUTOFF_VIDEO is not set in .env (no stopping point)."


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _page_html(hrefs):
    anchors = "".join(f'<a rel="bookmark" href="{h}">video</a>' for h in hrefs)
    return f"<html><body>{anchors}</body></html>"


def test_auto_happy_path_advances_cutoff_and_scrapes(
    tool_client, monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text('CUTOFF_VIDEO="https://site.test/v3"\n')
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/v3")

    pages = {
        "https://site.test/page/1/": _page_html(
            ["https://site.test/v5", "https://site.test/v4"]
        ),
        "https://site.test/page/2/": _page_html(
            ["https://site.test/v3", "https://site.test/v2"]
        ),
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    def fake_get_magnet_link(url):
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)
    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    result = snap["result"]
    assert result["cutoff_found"] is True
    assert result["urls"] == ["https://site.test/v5", "https://site.test/v4"]
    assert [r["result"] for r in result["successful"]] == [
        "magnet:?xt=https://site.test/v5",
        "magnet:?xt=https://site.test/v4",
    ]
    assert result["failed"] == []
    # The cutoff was advanced to the newest video, in the .env file only.
    assert "https://site.test/v5" in env_file.read_text()


def test_auto_cancel_during_scrape_does_not_advance_cutoff(
    tool_client, monkeypatch, tmp_path
):
    # Regression: cancelling mid-scrape must NOT advance the cutoff — otherwise
    # the un-fetched videos fall below the anchor and are skipped forever.
    env_file = tmp_path / ".env"
    env_file.write_text('CUTOFF_VIDEO="https://site.test/v0"\n')
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/v0")

    new_urls = ["https://site.test/v3", "https://site.test/v2", "https://site.test/v1"]
    monkeypatch.setattr(
        magnet_engine,
        "find_unwatched_urls",
        lambda *a, **k: (new_urls, True, None),
    )

    # Simulate a scrape that gets cancelled after fetching the first video.
    def fake_scrape(job, urls, should_stop=None):
        job._cancel.set()
        return {
            "urls": urls,
            "successful": [{"success": True, "result": "magnet:?xt=v3"}],
            "failed": [],
            "total": len(urls),
            "successful_count": 1,
            "failed_count": 0,
        }

    monkeypatch.setattr(magnet_router, "_scrape", fake_scrape)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "cancelled"
    # Cutoff left untouched, and the partial result is preserved (not None).
    assert "https://site.test/v0" in env_file.read_text()
    assert "https://site.test/v3" not in env_file.read_text()
    assert snap["result"] is not None
    assert snap["result"]["successful_count"] == 1


def test_auto_cutoff_not_found_warns_and_leaves_env_alone(
    tool_client, monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text('CUTOFF_VIDEO="https://site.test/gone"\n')
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/gone")

    pages = {
        "https://site.test/page/1/": _page_html(["https://site.test/v5"]),
        "https://site.test/page/2/": _page_html([]),  # empty page ends the walk
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"] == {
        "cutoff_found": False,
        "warning": (
            "Cutoff video not found — check CUTOFF_VIDEO or raise the page "
            "limit. Nothing was scraped and the cutoff was left unchanged."
        ),
        "error": None,
    }
    assert env_file.read_text() == 'CUTOFF_VIDEO="https://site.test/gone"\n'


# ---------------------------------------------------------------- manual ---


def test_manual_scrape_splits_successful_and_failed(tool_client, monkeypatch):
    def fake_get_magnet_link(url):
        if url.endswith("/bad"):
            return {"success": False, "url": url, "reason": "no magnet link on page"}
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    urls = [
        "https://site.test/a",
        "https://site.test/bad",
        "https://site.test/b",
    ]
    resp = tool_client.post("/api/magnet/manual", json={"urls": urls})
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    result = snap["result"]
    assert result["urls"] == urls
    assert result["total"] == 3
    assert [r["result"] for r in result["successful"]] == [
        "magnet:?xt=https://site.test/a",
        "magnet:?xt=https://site.test/b",
    ]
    assert result["failed"] == [
        {
            "success": False,
            "url": "https://site.test/bad",
            "reason": "no magnet link on page",
        }
    ]
    assert result["successful_count"] == 2
    assert result["failed_count"] == 1


def test_manual_empty_is_400_with_page_warning(tool_client):
    resp = tool_client.post("/api/magnet/manual", json={"urls": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Please enter at least one URL"


# ------------------------------------------------- find_unwatched_urls -----


def test_find_unwatched_urls_slices_at_cutoff(monkeypatch):
    pages = {
        "https://site.test/page/1/": _page_html(
            ["https://site.test/v5", "https://site.test/v4"]
        ),
        "https://site.test/page/2/": _page_html(
            ["https://site.test/v3", "https://site.test/v2"]
        ),
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)

    seen_pages = []
    urls, found, error = magnet_engine.find_unwatched_urls(
        "https://site.test",
        "https://site.test/v3",
        1,
        on_page=seen_pages.append,
    )
    assert found is True
    assert error is None
    # Only the videos newer than the cutoff, newest first; the cutoff itself
    # and everything older are dropped.
    assert urls == ["https://site.test/v5", "https://site.test/v4"]
    assert seen_pages == [1, 2]


def test_find_unwatched_urls_cutoff_on_first_link_yields_empty(monkeypatch):
    pages = {
        "https://site.test/page/1/": _page_html(
            ["https://site.test/v3", "https://site.test/v2"]
        ),
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)

    urls, found, error = magnet_engine.find_unwatched_urls(
        "https://site.test", "https://site.test/v3", 1
    )
    assert found is True
    assert error is None
    assert urls == []


def test_find_unwatched_urls_stops_on_empty_page_without_cutoff(monkeypatch):
    pages = {
        "https://site.test/page/1/": _page_html(["https://site.test/v5"]),
        "https://site.test/page/2/": _page_html([]),
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)

    urls, found, error = magnet_engine.find_unwatched_urls(
        "https://site.test", "https://site.test/nope", 1
    )
    assert found is False
    assert error is None
    assert urls == ["https://site.test/v5"]


def test_find_unwatched_urls_reports_page_error(monkeypatch):
    def fake_requests_get(url, timeout=10):
        raise OSError("connection refused")

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)

    urls, found, error = magnet_engine.find_unwatched_urls(
        "https://site.test", "https://site.test/v3", 7
    )
    assert found is False
    assert urls == []
    assert error == "❌ Error on page 7: connection refused"


# ----------------------------------------------- regression: stale cutoff --


def test_auto_rereads_advanced_cutoff_from_env_file_within_process(
    tool_client, monkeypatch, tmp_path
):
    """DEFECT 1: within a long-lived process, a second auto run must honour the
    cutoff that the first run advanced via set_key (written to the file only).
    The old load_dotenv(override=False) + os.getenv read kept the process-start
    cutoff in os.environ forever, so every later run re-scraped the whole batch.
    """
    env_file = tmp_path / ".env"
    env_file.write_text('CUTOFF_VIDEO="https://site.test/v3"\n')
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    # Simulate the daemon: os.environ was populated at process start and never
    # changes; set_key only rewrites the file.
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/v3")

    pages: dict[str, str] = {}

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    def fake_get_magnet_link(url):
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)
    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    # Run 1: cutoff v3; newest videos are v5, v4.
    pages["https://site.test/page/1/"] = _page_html(
        ["https://site.test/v5", "https://site.test/v4"]
    )
    pages["https://site.test/page/2/"] = _page_html(
        ["https://site.test/v3", "https://site.test/v2"]
    )
    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["urls"] == [
        "https://site.test/v5",
        "https://site.test/v4",
    ]
    # set_key advanced the file's cutoff to v5; os.environ still says v3.
    assert "https://site.test/v5" in env_file.read_text()

    # Run 2: one genuinely new video (v6) appeared at the top.
    pages["https://site.test/page/1/"] = _page_html(
        ["https://site.test/v6", "https://site.test/v5"]
    )
    pages["https://site.test/page/2/"] = _page_html(
        ["https://site.test/v4", "https://site.test/v3"]
    )
    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    # Only v6 is new. Before the fix the worker read the stale v3 from
    # os.environ and re-scraped [v6, v5, v4].
    assert snap["result"]["urls"] == ["https://site.test/v6"]


# --------------------------------------------------- regression: cancel ----


def test_find_unwatched_urls_should_stop_aborts_before_walking_all_pages(
    monkeypatch,
):
    """DEFECT 2a: a should_stop that trips mid-walk stops paging and reports
    cutoff_found=False so the caller never advances the cutoff."""
    pages = {
        "https://site.test/page/1/": _page_html(["https://site.test/v5"]),
        "https://site.test/page/2/": _page_html(["https://site.test/v4"]),
    }
    visited: list[str] = []

    def fake_requests_get(url, timeout=10):
        visited.append(url)
        return FakeResponse(pages[url])

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)

    polls = {"n": 0}

    def should_stop() -> bool:
        # Allow page 1, then abort before page 2.
        stop = polls["n"] >= 1
        polls["n"] += 1
        return stop

    urls, found, error = magnet_engine.find_unwatched_urls(
        "https://site.test",
        "https://site.test/nope",
        1,
        should_stop=should_stop,
    )
    assert found is False  # cutoff not located -> caller leaves cutoff alone
    assert error is None
    assert visited == ["https://site.test/page/1/"]  # stopped before page 2
    assert urls == ["https://site.test/v5"]


def test_scrape_magnets_should_stop_returns_partial_results(monkeypatch):
    """DEFECT 2b: once should_stop trips after k completions, scrape_magnets
    stops fanning out and returns <= k results instead of the full batch."""

    def fake_get_magnet_link(url):
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    urls = [f"https://site.test/v{i}" for i in range(20)]

    completed = {"n": 0}

    def should_stop() -> bool:
        completed["n"] += 1
        return completed["n"] >= 3  # stop after 3 completed results

    successful, failed = magnet_engine.scrape_magnets(
        urls, should_stop=should_stop
    )
    total = len(successful) + len(failed)
    assert total <= 3
    assert total < len(urls)  # aborted well before the full fan-out finished
