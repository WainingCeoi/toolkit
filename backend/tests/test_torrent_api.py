"""Torrent Downloader: the resolve -> review -> send handover, and its edges."""

from __future__ import annotations

import pytest
from fake_bitcomet import SERVER_NAME, FakeBitComet
from fastapi.testclient import TestClient

from toolkit_api.main import create_app
from toolkit_api.torrents import TorrentManager
from toolkit_engine.bitcomet import BitCometClient
from toolkit_engine.torrent import bencode

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
    app_state.torrents = TorrentManager(client, download_dir=save_folder)
    with TestClient(create_app(state=app_state)) as test_client:
        yield test_client


def upload(client, save_dir=None):
    """Resolve a .torrent and return its infohash."""
    return client.post(
        "/api/torrent/resolve",
        files={"file": ("Example.torrent", sample_torrent(), TORRENT_MIME)},
        data={"save_dir": str(save_dir)} if save_dir else None,
    ).json()["infohash"]


def send(client, infohash, selected=(1,)):
    return client.post(
        "/api/torrent",
        json={"infohash": infohash, "selected": list(selected)},
    )


# --- STATUS ---
def test_status_reports_the_bitcomet_server(torrent_client):
    body = torrent_client.get("/api/torrent/status").json()
    assert body["running"] is True
    assert body["server"] == SERVER_NAME


def test_status_hands_back_bitcomets_own_url(torrent_client, fake):
    assert torrent_client.get("/api/torrent/status").json()["url"] == fake.url


def test_status_reports_an_unreachable_bitcomet_without_failing(app_state, tmp_path):
    client = BitCometClient(
        base_url="http://127.0.0.1:1", username="u", password="p", timeout=0.5
    )
    app_state.torrents = TorrentManager(client, download_dir=tmp_path)
    with TestClient(create_app(state=app_state)) as test_client:
        body = test_client.get("/api/torrent/status").json()
    assert body["running"] is False
    assert body["server"] is None
    assert "Remote Access" in body["detail"]


def test_status_answers_even_with_no_engine(app_state):
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as client:
        resp = client.get("/api/torrent/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is False
    assert "Install BitComet" in resp.json()["detail"]


def test_endpoints_503_when_the_engine_was_never_built(app_state):
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as client:
        resp = client.post("/api/torrent/resolve", data={"magnet": MAGNET})
    assert resp.status_code == 503
    assert "not ready" in resp.json()["detail"]


# --- RESOLVE ---
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


def test_a_torrent_is_staged_stopped_so_nothing_downloads_during_review(
    torrent_client, fake
):
    upload(torrent_client)
    (task,) = fake.tasks.values()
    assert task["status"] == "stopped"


def test_resolving_the_same_torrent_twice_reuses_the_one_task(torrent_client, fake):
    first = upload(torrent_client)
    second = upload(torrent_client)
    assert first == second
    assert len(fake.tasks) == 1


def test_resolve_rejects_a_corrupt_torrent_upload(torrent_client):
    resp = torrent_client.post(
        "/api/torrent/resolve",
        files={"file": ("bad.torrent", b"not bencode at all", TORRENT_MIME)},
    )
    assert resp.status_code == 400
    assert "torrent" in resp.json()["detail"].lower()


def test_resolve_400s_on_a_bencoded_torrent_with_a_broken_info_dict(torrent_client):
    # Valid bencode, wrong shape: unguarded field access would answer 500.
    resp = torrent_client.post(
        "/api/torrent/resolve",
        files={
            "file": ("bad.torrent", bencode({b"info": {b"name": b"a"}}), TORRENT_MIME)
        },
    )
    assert resp.status_code == 400
    assert "Could not read that .torrent" in resp.json()["detail"]


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


def test_a_magnet_is_staged_running_or_it_never_learns_its_files(torrent_client, fake):
    torrent_client.post("/api/torrent/resolve", data={"magnet": MAGNET})
    (task,) = fake.tasks.values()
    assert task["status"] == "running"


def test_resolve_registers_the_destination_bitcomet_fixes_at_add_time(
    torrent_client, fake, tmp_path
):
    folder = tmp_path / "Films"
    upload(torrent_client, save_dir=folder)
    assert str(folder) in fake.save_folders
    add = next(p for m, path, p in fake.calls if path == "/api/task/bt/add")
    assert add["save_folder"] == str(folder)


def test_resolve_503s_when_bitcomet_refuses(torrent_client, fake):
    fake.reject_every_token = True
    resp = torrent_client.post(
        "/api/torrent/resolve", data={"magnet": f"magnet:?xt=urn:btih:{HASH}"}
    )
    assert resp.status_code == 503


def test_poll_resolve_404s_on_an_unknown_infohash(torrent_client):
    assert torrent_client.get(f"/api/torrent/resolve/{'0' * 40}").status_code == 404


# --- METADATA ---
def test_a_magnets_files_are_all_disabled_the_moment_metadata_lands(
    torrent_client, fake
):
    fake.publish_metadata(HASH, [("Movie.mkv", 2_000_000_000), ("RARBG.txt", 30)])
    torrent_client.post("/api/torrent/resolve", data={"magnet": MAGNET})

    polled = torrent_client.get(f"/api/torrent/resolve/{HASH}").json()
    assert polled["state"] == "awaiting_selection"
    assert [f["path"] for f in polled["files"]] == ["Movie.mkv", "RARBG.txt"]

    # BitComet has no metadata-only mode; disabling every file is the pause.
    (task,) = fake.tasks.values()
    assert {f["priority"] for f in task["files"]} == {"disabled"}


def test_a_torrent_bitcomet_already_had_is_never_mass_disabled(torrent_client, fake):
    fake.add_task("Someone Else's", [("Movie.mkv", 10), ("Extra.nfo", 5)], HASH)
    resolved = torrent_client.post(
        "/api/torrent/resolve", data={"magnet": MAGNET}
    ).json()
    assert resolved["state"] == "awaiting_selection"

    torrent_client.get(f"/api/torrent/resolve/{HASH}")
    (task,) = fake.tasks.values()
    assert {f["priority"] for f in task["files"]} == {"normal"}


def test_a_dead_magnet_is_given_up_on_and_deleted(torrent_client, fake, monkeypatch):
    monkeypatch.setattr("toolkit_api.torrents.METADATA_TIMEOUT", -1.0)
    torrent_client.post("/api/torrent/resolve", data={"magnet": MAGNET})
    assert fake.tasks

    polled = torrent_client.get(f"/api/torrent/resolve/{HASH}").json()
    assert polled["state"] == "error"
    assert fake.tasks == {}


# --- SEND ---
def test_send_starts_the_download_and_returns_a_receipt(torrent_client, fake):
    resp = send(torrent_client, upload(torrent_client))
    assert resp.status_code == 200
    (task,) = fake.tasks.values()
    assert resp.json()["task_id"] == task["task_id"]
    assert task["status"] == "running"


def test_send_applies_both_directions_of_the_tick_list(torrent_client, fake):
    send(torrent_client, upload(torrent_client), selected=(1, 3))
    (task,) = fake.tasks.values()
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled", "normal"]


def test_send_rejects_an_empty_selection(torrent_client):
    resp = send(torrent_client, upload(torrent_client), selected=())
    assert resp.status_code == 400
    assert "at least one file" in resp.json()["detail"]


def test_send_400s_on_a_file_the_torrent_does_not_have(torrent_client):
    resp = send(torrent_client, upload(torrent_client), selected=(1, 99))
    assert resp.status_code == 400
    assert "no file 99" in resp.json()["detail"]


def test_a_magnet_resolves_reviews_and_sends_end_to_end(torrent_client, fake):
    fake.publish_metadata(HASH, [("Movie.mkv", 2_000_000_000), ("RARBG.txt", 30)])
    assert (
        torrent_client.post("/api/torrent/resolve", data={"magnet": MAGNET}).json()[
            "state"
        ]
        == "awaiting_metadata"
    )
    assert (
        torrent_client.get(f"/api/torrent/resolve/{HASH}").json()["state"]
        == "awaiting_selection"
    )

    assert send(torrent_client, HASH, selected=(1,)).status_code == 200
    (task,) = fake.tasks.values()
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled"]
    assert task["status"] == "running"


def test_send_404s_on_an_unknown_infohash(torrent_client):
    assert send(torrent_client, "0" * 40).status_code == 404


def test_send_503s_when_bitcomet_refuses(torrent_client, fake):
    infohash = upload(torrent_client)
    fake.reject_every_token = True
    assert send(torrent_client, infohash).status_code == 503


# --- DISCARD ---
def test_discard_removes_the_staged_task_from_bitcomet(torrent_client, fake):
    infohash = upload(torrent_client)
    assert torrent_client.delete(f"/api/torrent/{infohash}").status_code == 200
    assert fake.tasks == {}


def test_discard_stops_a_magnet_that_is_still_fetching_metadata(torrent_client, fake):
    torrent_client.post("/api/torrent/resolve", data={"magnet": MAGNET})
    assert fake.tasks

    torrent_client.delete(f"/api/torrent/{HASH}")
    assert fake.tasks == {}
    assert fake.deleted and fake.deleted[0][1] is False  # (task_id, delete_all)


def test_discard_keeps_the_downloaded_data(torrent_client, fake):
    infohash = upload(torrent_client)
    torrent_client.delete(f"/api/torrent/{infohash}")
    assert fake.deleted == [(next(iter(fake.deleted))[0], False)]


# --- not served here ---
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/torrent"),
        ("GET", "/api/torrent/events"),
        ("POST", "/api/torrent/pause-all"),
        ("POST", "/api/torrent/resume-all"),
        ("POST", f"/api/torrent/{HASH}/pause"),
        ("POST", f"/api/torrent/{HASH}/resume"),
    ],
)
def test_task_management_is_bitcomets_and_is_not_served_here(
    torrent_client, method, path
):
    assert torrent_client.request(method, path).status_code in {404, 405}
