"""Magnet Scraper: dedupe endpoint, auto/manual jobs, pagination slicing."""

from __future__ import annotations

import threading
import time

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.main import create_app
from toolkit_api.routers import magnet as magnet_router
from toolkit_engine import magnet as magnet_engine


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
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


@pytest.fixture
def config_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.delenv("WEBSITE_URL", raising=False)
    monkeypatch.delenv("CUTOFF_VIDEO", raising=False)
    return env_file


# --- settings ---


def test_config_reads_only_magnet_settings_and_prefers_file_values(
    tool_client, config_env, monkeypatch
):
    config_env.write_text(
        "WEBSITE_URL='https://site.test'\n"
        "CUTOFF_VIDEO='https://site.test/v3'\n"
        "PRIVATE_TOKEN='keep-me-private'\n"
    )
    monkeypatch.setenv("WEBSITE_URL", "https://old.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://old.test/old")

    resp = tool_client.get("/api/magnet/config")

    assert resp.status_code == 200
    assert resp.json() == {
        "website_url": "https://site.test",
        "cutoff_video": "https://site.test/v3",
        "website_url_set": True,
        "cutoff_set": True,
    }


def test_config_falls_back_to_shell_when_file_keys_are_absent(
    tool_client, config_env, monkeypatch
):
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/v3")

    assert tool_client.get("/api/magnet/config").json() == {
        "website_url": "https://site.test",
        "cutoff_video": "https://site.test/v3",
        "website_url_set": True,
        "cutoff_set": True,
    }


def test_config_save_trims_and_preserves_unrelated_env_contents(
    tool_client, config_env
):
    unrelated = "# Backend settings\nPRIVATE_TOKEN='do not change # this'\n\n"
    config_env.write_text(unrelated + "WEBSITE_URL='https://old.test'\n")

    resp = tool_client.put(
        "/api/magnet/config",
        json={
            "website_url": "  https://site.test/  ",
            "cutoff_video": "\nhttps://site.test/v3?sort=new&view=full\t",
        },
    )

    assert resp.status_code == 200
    expected = {
        "website_url": "https://site.test",
        "cutoff_video": "https://site.test/v3?sort=new&view=full",
        "website_url_set": True,
        "cutoff_set": True,
    }
    assert resp.json() == expected
    assert tool_client.get("/api/magnet/config").json() == expected
    assert "# Backend settings\n" in config_env.read_text()
    assert "PRIVATE_TOKEN='do not change # this'\n" in config_env.read_text()
    assert dotenv_values(config_env) == {
        "PRIVATE_TOKEN": "do not change # this",
        "WEBSITE_URL": expected["website_url"],
        "CUTOFF_VIDEO": expected["cutoff_video"],
    }


def test_config_save_creates_missing_env_file(tool_client, config_env):
    config_env.unlink()
    resp = tool_client.put(
        "/api/magnet/config",
        json={"website_url": "http://site.test", "cutoff_video": ""},
    )

    assert resp.status_code == 200
    assert dotenv_values(config_env) == {
        "WEBSITE_URL": "http://site.test",
        "CUTOFF_VIDEO": "",
    }


@pytest.mark.parametrize("field", ["website_url", "cutoff_video"])
@pytest.mark.parametrize(
    "value",
    [
        "ftp://site.test",
        "site.test/video",
        "https://",
        "https://bad host",
        "https:///site.test",
        "https://site.test:invalid",
    ],
)
def test_config_rejects_invalid_urls_without_writing(
    tool_client, config_env, field, value
):
    original = "# Preserve this file\nPRIVATE_TOKEN=secret\n"
    config_env.write_text(original)
    settings = {"website_url": "https://site.test", "cutoff_video": ""}
    settings[field] = value

    resp = tool_client.put("/api/magnet/config", json=settings)

    assert resp.status_code == 422
    label = "Source website" if field == "website_url" else "Last watched video URL"
    assert resp.json()["detail"] == (
        f"{label} must be a valid http:// or https:// URL."
    )
    assert config_env.read_text() == original


@pytest.mark.parametrize(
    ("website", "cutoff", "error"),
    [
        ("  ", "\n", magnet_router.ERR_NO_WEBSITE),
        ("https://site.test", "\n", magnet_router.ERR_NO_CUTOFF),
    ],
)
def test_config_clear_overrides_process_env_and_blocks_auto(
    tool_client, config_env, monkeypatch, website, cutoff, error
):
    monkeypatch.setenv("WEBSITE_URL", "https://old.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://old.test/old")

    resp = tool_client.put(
        "/api/magnet/config", json={"website_url": website, "cutoff_video": cutoff}
    )

    expected = {
        "website_url": website.strip(),
        "cutoff_video": cutoff.strip(),
        "website_url_set": bool(website.strip()),
        "cutoff_set": bool(cutoff.strip()),
    }
    assert resp.status_code == 200
    assert resp.json() == expected
    assert tool_client.get("/api/magnet/config").json() == expected
    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "failed"
    assert snap["error"] == error


def test_config_save_failure_keeps_original_file_and_removes_temp_file(
    tool_client, config_env, monkeypatch
):
    original = "WEBSITE_URL='https://old.test'\nPRIVATE_TOKEN=secret\n"
    config_env.write_text(original)
    real_set_key = magnet_router.set_key

    def fail_second_write(path, key, value):
        if key == "CUTOFF_VIDEO":
            raise PermissionError("read-only file")
        return real_set_key(path, key, value)

    monkeypatch.setattr(magnet_router, "set_key", fail_second_write)
    resp = tool_client.put(
        "/api/magnet/config",
        json={"website_url": "https://new.test", "cutoff_video": ""},
    )

    assert resp.status_code == 500
    assert "Could not save magnet settings" in resp.json()["detail"]
    assert config_env.read_text() == original
    assert list(config_env.parent.glob(".env.*")) == []


def test_auto_uses_saved_settings_without_restart(tool_client, config_env, monkeypatch):
    monkeypatch.setenv("WEBSITE_URL", "https://old.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://old.test/old")
    calls = []

    def find_urls(website, cutoff, start_page, **kwargs):
        calls.append((website, cutoff, start_page))
        return [], True, None

    monkeypatch.setattr(magnet_engine, "find_unwatched_urls", find_urls)
    tool_client.put(
        "/api/magnet/config",
        json={
            "website_url": "https://new.test",
            "cutoff_video": "https://new.test/video",
        },
    ).raise_for_status()

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 3})
    snap = wait_for_job(tool_client, resp.json()["job_id"])

    assert snap["state"] == "done"
    assert calls == [("https://new.test", "https://new.test/video", 3)]


@pytest.mark.parametrize(
    ("website", "cutoff"),
    [
        ("https://site.test", "https://site.test/manual"),
        ("https://other.test", "https://site.test/v3"),
        ("https://site.test", "https://site.test/v3"),
    ],
)
def test_auto_does_not_advance_cutoff_over_settings_saved_during_run(
    tool_client, config_env, monkeypatch, website, cutoff
):
    config_env.write_text(
        "WEBSITE_URL='https://site.test'\nCUTOFF_VIDEO='https://site.test/v3'\n"
    )
    started = threading.Event()
    resume = threading.Event()

    def find_urls(*args, **kwargs):
        started.set()
        assert resume.wait(timeout=3)
        return ["https://site.test/v4"], True, None

    monkeypatch.setattr(magnet_engine, "find_unwatched_urls", find_urls)
    monkeypatch.setattr(
        magnet_engine,
        "get_magnet_link",
        lambda url: {"success": True, "result": "magnet:?xt=v4"},
    )
    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    try:
        assert started.wait(timeout=3)
        saved = tool_client.put(
            "/api/magnet/config",
            json={"website_url": website, "cutoff_video": cutoff},
        )
        assert saved.status_code == 200
    finally:
        resume.set()
    snap = wait_for_job(tool_client, resp.json()["job_id"])

    assert snap["state"] == "done"
    assert tool_client.get("/api/magnet/config").json()["cutoff_video"] == cutoff


# --- dedupe ---


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


# --- auto ---


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
    assert snap["error"] == (
        "Add a source website in Settings before using Automatic mode."
    )


def test_auto_missing_cutoff_fails_with_page_error(tool_client, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.delenv("CUTOFF_VIDEO", raising=False)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "failed"
    assert snap["error"] == (
        "Add a last watched video URL in Settings before using Automatic mode."
    )


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
    assert "https://site.test/v5" in env_file.read_text()


def test_auto_holds_the_cutoff_behind_a_failed_fetch(
    tool_client, monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text('CUTOFF_VIDEO="https://site.test/v3"\n')
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/v3")

    pages = {
        "https://site.test/page/1/": _page_html(
            ["https://site.test/v6", "https://site.test/v5", "https://site.test/v4"]
        ),
        "https://site.test/page/2/": _page_html(["https://site.test/v3"]),
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    def fake_get_magnet_link(url):
        if url == "https://site.test/v5":
            return {"success": False, "url": url, "reason": "timed out"}
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)
    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert [r["url"] for r in snap["result"]["failed"]] == ["https://site.test/v5"]
    # v4 is the newest video older than the failure, so a rerun still sees v5.
    assert "CUTOFF_VIDEO='https://site.test/v4'" in env_file.read_text()


def test_auto_leaves_the_cutoff_alone_when_the_oldest_fetch_failed(
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
        "https://site.test/page/2/": _page_html(["https://site.test/v3"]),
    }

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    def fake_get_magnet_link(url):
        if url == "https://site.test/v4":
            return {"success": False, "url": url, "reason": "timed out"}
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)
    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert 'CUTOFF_VIDEO="https://site.test/v3"' in env_file.read_text()


def test_auto_cancel_during_scrape_does_not_advance_cutoff(
    tool_client, monkeypatch, tmp_path
):
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
            "Last watched video not found. Check its URL in Settings and the "
            "start page. "
            "Nothing was scraped and the stopping point was left unchanged."
        ),
        "error": None,
    }
    assert env_file.read_text() == 'CUTOFF_VIDEO="https://site.test/gone"\n'


# --- manual ---


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


def test_manual_scrape_auto_dedupes_successful_magnets(tool_client, monkeypatch):
    magnets = {
        "https://site.test/a": "magnet:?xt=shared",
        "https://site.test/mirror-of-a": "magnet:?xt=shared",
        "https://site.test/b": "magnet:?xt=b",
    }

    def fake_get_magnet_link(url):
        if url.endswith("/bad"):
            return {"success": False, "url": url, "reason": "no magnet link on page"}
        return {"success": True, "result": magnets[url]}

    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    urls = [
        "https://site.test/a",
        "https://site.test/mirror-of-a",
        "https://site.test/bad",
        "https://site.test/b",
    ]
    resp = tool_client.post("/api/magnet/manual", json={"urls": urls})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    result = snap["result"]
    assert result["total"] == 4
    assert [r["result"] for r in result["successful"]] == [
        "magnet:?xt=shared",
        "magnet:?xt=b",
    ]
    assert result["successful_count"] == 2
    assert result["duplicate_count"] == 1
    assert result["failed_count"] == 1


def test_manual_empty_is_400_with_page_warning(tool_client):
    resp = tool_client.post("/api/magnet/manual", json={"urls": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Please enter at least one URL"


# --- find_unwatched_urls ---


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


# --- stale cutoff ---


def test_auto_rereads_advanced_cutoff_from_env_file_within_process(
    tool_client, monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text('CUTOFF_VIDEO="https://site.test/v3"\n')
    monkeypatch.setattr(magnet_engine, "ENV_PATH", env_file)
    # os.environ holds the process-start cutoff; set_key rewrites only the file.
    monkeypatch.setenv("WEBSITE_URL", "https://site.test")
    monkeypatch.setenv("CUTOFF_VIDEO", "https://site.test/v3")

    pages: dict[str, str] = {}

    def fake_requests_get(url, timeout=10):
        return FakeResponse(pages[url])

    def fake_get_magnet_link(url):
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine.requests, "get", fake_requests_get)
    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

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
    assert "https://site.test/v5" in env_file.read_text()

    pages["https://site.test/page/1/"] = _page_html(
        ["https://site.test/v6", "https://site.test/v5"]
    )
    pages["https://site.test/page/2/"] = _page_html(
        ["https://site.test/v4", "https://site.test/v3"]
    )
    resp = tool_client.post("/api/magnet/auto", json={"start_page": 1})
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["urls"] == ["https://site.test/v6"]


# --- cancel ---


def test_find_unwatched_urls_should_stop_aborts_before_walking_all_pages(
    monkeypatch,
):
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
        stop = polls["n"] >= 1
        polls["n"] += 1
        return stop

    urls, found, error = magnet_engine.find_unwatched_urls(
        "https://site.test",
        "https://site.test/nope",
        1,
        should_stop=should_stop,
    )
    assert found is False
    assert error is None
    assert visited == ["https://site.test/page/1/"]
    assert urls == ["https://site.test/v5"]


def test_scrape_magnets_should_stop_returns_partial_results(monkeypatch):
    def fake_get_magnet_link(url):
        return {"success": True, "result": f"magnet:?xt={url}"}

    monkeypatch.setattr(magnet_engine, "get_magnet_link", fake_get_magnet_link)

    urls = [f"https://site.test/v{i}" for i in range(20)]

    completed = {"n": 0}

    def should_stop() -> bool:
        completed["n"] += 1
        return completed["n"] >= 3

    successful, failed = magnet_engine.scrape_magnets(urls, should_stop=should_stop)
    total = len(successful) + len(failed)
    assert total <= 3
    assert total < len(urls)
