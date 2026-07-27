"""Torrent Downloader: router validation and the resolve -> commit flow."""

from __future__ import annotations

import asyncio

import pytest
from fake_bitcomet import SERVER_NAME, FakeBitComet
from fastapi.testclient import TestClient

from toolkit_api.main import create_app
from toolkit_api.routers.torrent import torrent_frames
from toolkit_api.torrents import TorrentManager
from toolkit_engine.bitcomet import BitCometClient
from toolkit_engine.torrent import bencode
from toolkit_engine.torrentdb import TorrentStore

HASH = "c9e15763f722f23e98a29decdfae341b98d53056"
TORRENT_MIME = "application/x-bittorrent"
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=Example.Release"


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
def save_folder(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


@pytest.fixture
def fake(save_folder):
    server = FakeBitComet(save_folders=[str(save_folder)])
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def torrent_client(app_state, fake, save_folder):
    client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    store = TorrentStore(":memory:")
    app_state.torrents = TorrentManager(store, client, download_dir=save_folder)
    app = create_app(state=app_state)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        store.close()


def upload(client):
    """Resolve a .torrent and return its infohash."""
    return client.post(
        "/api/torrent/resolve",
        files={"file": ("Example.torrent", sample_torrent(), TORRENT_MIME)},
    ).json()["infohash"]


def start(client, infohash, selected=(1,)):
    return client.post(
        "/api/torrent",
        json={"infohash": infohash, "selected": list(selected)},
    )


# =======================================================
# STATUS
# =======================================================
def test_status_reports_the_bitcomet_server(torrent_client):
    body = torrent_client.get("/api/torrent/status").json()
    assert body["running"] is True
    assert body["server"] == SERVER_NAME


def test_status_reports_an_unreachable_bitcomet_without_failing(app_state, tmp_path):
    store = TorrentStore(":memory:")
    client = BitCometClient(
        base_url="http://127.0.0.1:1", username="u", password="p", timeout=0.5
    )
    app_state.torrents = TorrentManager(store, client, download_dir=tmp_path)
    try:
        with TestClient(create_app(state=app_state)) as test_client:
            body = test_client.get("/api/torrent/status").json()
    finally:
        # Close even if startup raises, so a leaked :memory: connection can't
        # surface as a ResourceWarning blamed on an unrelated later test.
        store.close()
    assert body["running"] is False
    assert body["server"] is None
    assert "Remote Access" in body["detail"]


def test_status_answers_even_with_no_engine(app_state):
    # The diagnostic endpoint must not be gated behind the thing it diagnoses,
    # or the UI has no way to say WHY the tool is unavailable.
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as client:
        resp = client.get("/api/torrent/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False
    assert "Install BitComet" in resp.json()["detail"]


def test_endpoints_503_when_the_engine_was_never_built(app_state):
    # BitComet unconfigured -> build_torrent_manager returns None rather than
    # blowing up at startup; the tool says so instead of 500-ing.
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as client:
        resp = client.get("/api/torrent")
    assert resp.status_code == 503
    assert "not ready" in resp.json()["detail"]


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


def test_resolve_takes_the_destination_because_bitcomet_fixes_it_at_add_time(
    torrent_client, app_state, tmp_path
):
    folder = tmp_path / "Films"
    torrent_client.post(
        "/api/torrent/resolve",
        files={"file": ("Example.torrent", sample_torrent(), TORRENT_MIME)},
        data={"save_dir": str(folder)},
    )
    rows = torrent_client.get("/api/torrent").json()
    assert rows[0]["save_dir"] == str(folder)


def test_resolve_503s_when_bitcomet_refuses(torrent_client, fake):
    fake.reject_every_token = True
    resp = torrent_client.post(
        "/api/torrent/resolve", data={"magnet": f"magnet:?xt=urn:btih:{HASH}"}
    )
    # A BitComet that is down or rejecting us is a 503, not a 500.
    assert resp.status_code == 503


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
    # A torrent with everything deselected downloads nothing and calls itself
    # finished.
    assert resp.status_code == 400
    assert "at least one file" in resp.json()["detail"]


def test_commit_400s_on_a_file_the_torrent_does_not_have(torrent_client):
    resp = start(torrent_client, upload(torrent_client), selected=(1, 99))
    # A client mistake, so a 4xx -- not the 503 that means "BitComet is down".
    assert resp.status_code == 400
    assert "no file 99" in resp.json()["detail"]


def test_a_magnet_resolves_reviews_and_commits_end_to_end(torrent_client, fake):
    fake.publish_metadata(HASH, [("Movie.mkv", 2_000_000_000), ("RARBG.txt", 30)])
    assert (
        torrent_client.post("/api/torrent/resolve", data={"magnet": MAGNET}).json()[
            "state"
        ]
        == "awaiting_metadata"
    )

    polled = torrent_client.get(f"/api/torrent/resolve/{HASH}").json()
    assert polled["state"] == "awaiting_selection"
    assert [f["path"] for f in polled["files"]] == ["Movie.mkv", "RARBG.txt"]
    # Held, not paused: BitComet has no metadata-only mode, so every file is
    # disabled while the user reviews.
    (task,) = fake.tasks.values()
    assert {f["priority"] for f in task["files"]} == {"disabled"}

    assert start(torrent_client, HASH, selected=(1,)).status_code == 200
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled"]


def test_commit_404s_on_an_unknown_infohash(torrent_client):
    assert start(torrent_client, "0" * 40).status_code == 404


def test_commit_503s_when_bitcomet_refuses(torrent_client, fake):
    infohash = upload(torrent_client)
    fake.reject_every_token = True
    assert start(torrent_client, infohash).status_code == 503


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


def test_pause_all_stops_every_running_torrent(torrent_client):
    first = upload(torrent_client)
    start(torrent_client, first)

    assert torrent_client.post("/api/torrent/pause-all").status_code == 200
    rows = torrent_client.get("/api/torrent").json()
    assert {r["state"] for r in rows} == {"paused"}
    assert all(r["pause_reason"] == "user" for r in rows)


def test_resume_all_restarts_every_paused_torrent(torrent_client):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)
    torrent_client.post("/api/torrent/pause-all")
    assert torrent_client.get("/api/torrent").json()[0]["state"] == "paused"

    assert torrent_client.post("/api/torrent/resume-all").status_code == 200
    assert torrent_client.get("/api/torrent").json()[0]["state"] == "active"


def test_pause_all_is_not_captured_by_the_infohash_route(torrent_client):
    # "/torrent/pause-all" must hit the batch endpoint, not be read as an
    # infohash named "pause-all"; a 200 with the batch body proves the routing.
    assert torrent_client.post("/api/torrent/pause-all").json() == {"paused": True}


def test_delete_tombstones_the_row(torrent_client):
    infohash = upload(torrent_client)
    start(torrent_client, infohash)

    assert torrent_client.delete(f"/api/torrent/{infohash}").status_code == 200
    assert torrent_client.get("/api/torrent").json() == []


def read_one_frame(manager):
    """Drive the dashboard generator for exactly one frame, then close it.

    The stream is infinite by design, so it is driven directly rather than
    over HTTP: an HTTP-level test of it would hang rather than finish.
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
