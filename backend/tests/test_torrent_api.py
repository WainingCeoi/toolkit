"""Torrent Downloader: router validation and the resolve -> commit flow."""

from __future__ import annotations

import asyncio

import pytest
from fake_aria2 import FakeAria2
from fastapi.testclient import TestClient

from toolkit_api.main import create_app
from toolkit_api.routers.torrent import torrent_frames
from toolkit_api.torrents import TorrentManager
from toolkit_engine.aria2 import Aria2RPC
from toolkit_engine.torrent import bencode
from toolkit_engine.torrentdb import TorrentStore

HASH = "c9e15763f722f23e98a29decdfae341b98d53056"
TORRENT_MIME = "application/x-bittorrent"


def sample_torrent():
    return bencode(
        {
            b"info": {
                b"name": b"Example.Release",
                b"piece length": 262144,
                b"pieces": b"\x00" * 20,
                b"files": [
                    {b"length": 2_000_000_000, b"path": [b"Movie.mkv"]},
                    {b"length": 45_000, b"path": [b"Movie.chi.srt"]},
                    {b"length": 30, b"path": [b"RARBG.txt"]},
                ],
            }
        }
    )


@pytest.fixture
def fake_aria2():
    server = FakeAria2()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def torrent_client(app_state, fake_aria2, tmp_path):
    rpc = Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)
    store = TorrentStore(":memory:")
    app_state.torrents = TorrentManager(
        store, rpc, download_dir=tmp_path / "dl", owned=True
    )
    app = create_app(state=app_state)
    with TestClient(app) as client:
        yield client
    # Disarm the grace timer the presence tests arm before the store closes,
    # so it cannot fire against a closed store in a background thread.
    app_state.torrents.cancel_pending_shutdown()
    store.close()


def upload(client):
    """Resolve a .torrent and return its infohash."""
    return client.post(
        "/api/torrent/resolve",
        files={"file": ("Example.torrent", sample_torrent(), TORRENT_MIME)},
    ).json()["infohash"]


def start(client, infohash, selected=(1,), save_dir="/tmp/dest"):
    return client.post(
        "/api/torrent",
        json={
            "infohash": infohash,
            "selected": list(selected),
            "save_dir": save_dir,
        },
    )


# =======================================================
# STATUS
# =======================================================
def test_status_reports_the_daemon_version(torrent_client):
    body = torrent_client.get("/api/torrent/status").json()
    assert body["running"] is True
    assert body["version"] == "1.37.0"


def test_status_reports_a_down_daemon_without_failing(app_state, tmp_path):
    store = TorrentStore(":memory:")
    app_state.torrents = TorrentManager(
        store,
        Aria2RPC(url="http://127.0.0.1:1/jsonrpc", secret="x", timeout=0.5),
        download_dir=tmp_path,
    )
    try:
        with TestClient(create_app(state=app_state)) as client:
            body = client.get("/api/torrent/status").json()
    finally:
        # Close even if startup raises, so a leaked :memory: connection can't
        # surface as a ResourceWarning blamed on an unrelated later test.
        store.close()
    assert body["running"] is False
    assert body["version"] is None


# =======================================================
# RESOLVE
# =======================================================
def test_resolve_rejects_a_string_that_is_not_a_magnet(torrent_client):
    resp = torrent_client.post(
        "/api/torrent/resolve", data={"magnet": "http://example.com/x"}
    )
    assert resp.status_code == 400
    assert "magnet" in resp.json()["detail"]


def test_resolve_requires_either_a_magnet_or_a_file(torrent_client):
    resp = torrent_client.post("/api/torrent/resolve", data={})
    assert resp.status_code == 400


def test_resolve_uploads_a_torrent_and_lists_its_files(torrent_client):
    resp = torrent_client.post(
        "/api/torrent/resolve",
        files={"file": ("Example.torrent", sample_torrent(), TORRENT_MIME)},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["ready"] is True
    assert [f["path"] for f in body["files"]] == [
        "Movie.mkv",
        "Movie.chi.srt",
        "RARBG.txt",
    ]
    assert [f["category"] for f in body["files"]] == ["video", "subtitle", "document"]


def test_resolve_rejects_a_corrupt_torrent_upload(torrent_client):
    resp = torrent_client.post(
        "/api/torrent/resolve",
        files={"file": ("bad.torrent", b"not bencode at all", TORRENT_MIME)},
    )
    assert resp.status_code == 400
    assert "torrent" in resp.json()["detail"].lower()


def test_resolve_accepts_a_magnet_as_form_data(torrent_client):
    resp = torrent_client.post(
        "/api/torrent/resolve", data={"magnet": f"magnet:?xt=urn:btih:{HASH}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "infohash": HASH,
        "ready": False,
        "name": None,
        "files": [],
        "state": "awaiting_metadata",
    }


def test_poll_resolve_404s_on_an_unknown_infohash(torrent_client):
    assert torrent_client.get(f"/api/torrent/resolve/{'0' * 40}").status_code == 404


# =======================================================
# COMMIT
# =======================================================
def test_commit_starts_the_download(torrent_client):
    resp = start(torrent_client, upload(torrent_client))
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"


def test_commit_rejects_an_empty_selection(torrent_client):
    resp = start(torrent_client, upload(torrent_client), selected=())
    # aria2 would accept it and immediately call the torrent complete with
    # nothing downloaded.
    assert resp.status_code == 400
    assert "at least one file" in resp.json()["detail"]


def test_commit_404s_on_an_unknown_infohash(torrent_client):
    assert start(torrent_client, "0" * 40).status_code == 404


# =======================================================
# DASHBOARD + CONTROLS
# =======================================================
def test_listing_returns_committed_torrents(torrent_client):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)

    rows = torrent_client.get("/api/torrent").json()
    assert [r["infohash"] for r in rows] == [infohash]
    assert rows[0]["name"] == "Example.Release"


def test_pause_and_resume_round_trip(torrent_client):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)

    torrent_client.post(f"/api/torrent/{infohash}/pause")
    rows = torrent_client.get("/api/torrent").json()
    assert rows[0]["state"] == "paused"
    assert rows[0]["pause_reason"] == "user"

    torrent_client.post(f"/api/torrent/{infohash}/resume")
    assert torrent_client.get("/api/torrent").json()[0]["state"] == "active"


def test_delete_tombstones_the_row(torrent_client):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)

    assert torrent_client.delete(f"/api/torrent/{infohash}").status_code == 200
    assert torrent_client.get("/api/torrent").json() == []


def read_one_frame(manager):
    """Drive the dashboard generator for exactly one frame, then close it.

    The stream is infinite by design, so it is driven directly rather than
    over HTTP: aclose() runs its finally block, which is also how the
    presence counter gets decremented in production.
    """

    async def run():
        frames = torrent_frames(manager, interval=0.01)
        try:
            return await anext(frames)
        finally:
            await frames.aclose()

    return asyncio.run(run())


def test_events_streams_a_dashboard_frame(torrent_client, app_state):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)

    frame = read_one_frame(app_state.torrents)

    assert frame["event"] == "torrents"
    assert infohash in frame["data"]


def test_the_events_stream_is_the_presence_signal(torrent_client, app_state):
    manager = app_state.torrents
    manager.GRACE_SECONDS = 30.0

    read_one_frame(manager)

    # Opening and closing the dashboard is what arms auto-shutdown; without
    # this wiring the daemon would outlive the last tab forever.
    assert manager.shutdown_pending() is True


def test_explicit_shutdown_pauses_and_stops(torrent_client, fake_aria2):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)

    assert torrent_client.post("/api/torrent/shutdown").status_code == 200
    assert "aria2.shutdown" in [m for m, _ in fake_aria2.calls]


def test_status_answers_even_with_no_engine(app_state):
    # The diagnostic endpoint must not be gated behind the thing it diagnoses,
    # or the UI has no way to say WHY the tool is unavailable.
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as client:
        resp = client.get("/api/torrent/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False
    assert "brew install aria2" in resp.json()["detail"]


def test_endpoints_503_when_the_engine_was_never_built(app_state):
    # aria2 not installed -> build_torrent_manager returns None rather than
    # blowing up at startup; the tool says so instead of 500-ing.
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as client:
        resp = client.get("/api/torrent")
    assert resp.status_code == 503
    assert "not ready" in resp.json()["detail"]
