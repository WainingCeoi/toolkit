"""The /torrent/devices routes: choosing which BitComet takes the task."""

from __future__ import annotations

import pytest
from fake_bitcomet import SERVER_NAME, FakeBitComet
from fastapi.testclient import TestClient

from toolkit_api.devices import LOCAL_ID
from toolkit_api.main import create_app
from toolkit_api.torrents import TorrentManager
from toolkit_engine.bitcomet import BitCometClient

SECRET = "correct-horse-battery-staple"


@pytest.fixture
def fake():
    server = FakeBitComet(save_folders=["/volume1/downloads"])
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def local_is_the_fake(fake, tmp_path, monkeypatch):
    from toolkit_api import state as state_module
    from toolkit_engine import bitcomet

    monkeypatch.setattr(state_module, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        bitcomet.BitCometClient,
        "from_config",
        classmethod(
            lambda cls, path=None, timeout=10.0, device_id_file=None: cls(
                base_url=fake.url,
                username=fake.username,
                password=fake.password,
                timeout=timeout,
                device_id_file=device_id_file,
            )
        ),
    )


@pytest.fixture
def client(app_state, local_is_the_fake, tmp_path):
    """An app whose local BitComet is the fake, so switching AWAY is visible."""
    from toolkit_api.state import build_torrent_manager

    app_state.torrents = build_torrent_manager(app_state.devices.active())
    with TestClient(create_app(state=app_state)) as test_client:
        yield test_client


def add(client, url, label="NAS", username="admin", password=SECRET):
    return client.post(
        "/api/torrent/devices",
        json={"label": label, "url": url, "username": username, "password": password},
    )


# --- LISTING ---
def test_only_this_mac_is_listed_before_anything_is_added(client):
    body = client.get("/api/torrent/devices").json()
    assert body["active"] == LOCAL_ID
    assert [d["id"] for d in body["devices"]] == [LOCAL_ID]
    assert body["devices"][0]["is_local"] is True


def test_this_mac_is_always_first(client):
    add(client, "192.168.1.50:19377")
    ids = [d["id"] for d in client.get("/api/torrent/devices").json()["devices"]]
    assert ids[0] == LOCAL_ID


def test_a_saved_password_never_comes_back_over_the_wire(client):
    add(client, "192.168.1.50:19377")
    resp = client.get("/api/torrent/devices")
    assert SECRET not in resp.text
    remote = [d for d in resp.json()["devices"] if not d["is_local"]][0]
    assert remote["has_password"] is True
    assert "password" not in remote


# --- ADD / EDIT / REMOVE ---
def test_adding_a_device_selects_it(client):
    body = add(client, "192.168.1.50:19377").json()
    active = [d for d in body["devices"] if d["id"] == body["active"]][0]
    assert active["url"] == "http://192.168.1.50:19377"
    assert active["is_local"] is False


def test_adding_a_device_re_points_the_tool_at_it(client, fake):
    add(client, "192.168.1.50:19377")
    status = client.get("/api/torrent/status").json()
    assert status["url"] == "http://192.168.1.50:19377"
    assert status["is_local"] is False
    assert status["running"] is False  # nothing is listening there


def test_a_bad_address_is_refused_with_a_sentence(client):
    resp = add(client, "ftp://nas.local")
    assert resp.status_code == 400
    assert "http://" in resp.json()["detail"]


def test_a_device_without_credentials_is_refused(client):
    assert add(client, "192.168.1.50:19377", password="").status_code == 400


def test_renaming_keeps_the_stored_password(client):
    body = add(client, "192.168.1.50:19377").json()
    device_id = body["active"]
    resp = client.patch(
        f"/api/torrent/devices/{device_id}", json={"label": "Basement NAS"}
    )
    renamed = [d for d in resp.json()["devices"] if d["id"] == device_id][0]
    assert renamed["label"] == "Basement NAS"
    assert renamed["has_password"] is True


def test_editing_an_unknown_device_is_a_404(client):
    assert (
        client.patch("/api/torrent/devices/nope", json={"label": "x"}).status_code
        == 404
    )


def test_this_mac_cannot_be_edited_or_removed(client):
    assert (
        client.patch(
            f"/api/torrent/devices/{LOCAL_ID}", json={"label": "x"}
        ).status_code
        == 400
    )
    assert client.delete(f"/api/torrent/devices/{LOCAL_ID}").status_code == 400


def test_removing_the_active_device_returns_the_tool_to_this_mac(client, fake):
    device_id = add(client, "192.168.1.50:19377").json()["active"]
    body = client.delete(f"/api/torrent/devices/{device_id}").json()
    assert body["active"] == LOCAL_ID
    assert client.get("/api/torrent/status").json()["url"] == fake.url


def test_removing_an_unknown_device_is_a_404(client):
    assert client.delete("/api/torrent/devices/nope").status_code == 404


# --- SELECT ---
def test_selecting_switches_the_tool_and_back_again(client, fake):
    device_id = add(client, "192.168.1.50:19377").json()["active"]

    back = client.post(f"/api/torrent/devices/{LOCAL_ID}/select").json()
    assert back["active"] == LOCAL_ID
    assert client.get("/api/torrent/status").json()["url"] == fake.url

    again = client.post(f"/api/torrent/devices/{device_id}/select").json()
    assert again["active"] == device_id
    assert (
        client.get("/api/torrent/status").json()["url"] == "http://192.168.1.50:19377"
    )


def test_selecting_an_unknown_device_is_a_404(client):
    assert client.post("/api/torrent/devices/nope/select").status_code == 404


# --- TEST BEFORE SAVING ---
def test_testing_a_reachable_device_reports_its_folders(client, fake):
    body = client.post(
        "/api/torrent/devices/test",
        json={"url": fake.url, "username": fake.username, "password": fake.password},
    ).json()
    assert body["ok"] is True
    assert body["server"] == SERVER_NAME
    assert body["save_folders"] == ["/volume1/downloads"]


def test_testing_a_wrong_password_says_so_rather_than_just_failing(client, fake):
    body = client.post(
        "/api/torrent/devices/test",
        json={"url": fake.url, "username": fake.username, "password": "wrong"},
    ).json()
    assert body["ok"] is False
    assert "password" in body["detail"].lower()


def test_testing_an_unreachable_address_fails_without_raising(client):
    body = client.post(
        "/api/torrent/devices/test",
        json={"url": "127.0.0.1:1", "username": "u", "password": "p"},
    ).json()
    assert body["ok"] is False
    assert body["detail"]


def test_testing_a_malformed_address_fails_without_raising(client):
    body = client.post(
        "/api/torrent/devices/test",
        json={"url": "ftp://nas.local", "username": "u", "password": "p"},
    ).json()
    assert body["ok"] is False
    assert "http://" in body["detail"]


def test_testing_a_saved_device_can_reuse_its_stored_password(client, fake):
    device_id = add(
        client, fake.url, username=fake.username, password=fake.password
    ).json()["active"]
    body = client.post(
        "/api/torrent/devices/test",
        json={
            "url": fake.url,
            "username": fake.username,
            "password": "",
            "id": device_id,
        },
    ).json()
    assert body["ok"] is True


# --- STATUS ---
def test_status_names_the_device_it_is_reporting_on(client):
    body = client.get("/api/torrent/status").json()
    assert body["device"]["id"] == LOCAL_ID
    assert body["is_local"] is True


def test_status_offers_the_active_devices_own_folders(client):
    assert client.get("/api/torrent/status").json()["save_folders"] == [
        "/volume1/downloads"
    ]


def test_status_asks_the_peer_for_its_config_only_once(client, fake):
    # The lamp and the folder list come from one body; a remote peer answers slowly.
    fake.calls.clear()
    client.get("/api/torrent/status")
    reads = [p for _m, path, p in fake.calls if path.endswith("new_task/get")]
    assert len(reads) == 1


def test_status_explains_an_unreachable_peer_in_lan_terms(client):
    add(client, "192.168.1.50:19377", label="Basement NAS")
    detail = client.get("/api/torrent/status").json()["detail"]
    assert "Basement NAS" in detail
    assert "awake" in detail


def test_status_still_answers_with_no_device_book(app_state):
    app_state.devices = None
    app_state.torrents = None
    with TestClient(create_app(state=app_state)) as test_client:
        body = test_client.get("/api/torrent/status").json()
    assert body["running"] is False
    assert body["device"] is None


def test_device_routes_report_a_missing_book_rather_than_crashing(app_state):
    app_state.devices = None
    with TestClient(create_app(state=app_state)) as test_client:
        assert test_client.get("/api/torrent/devices").status_code == 503


# --- handover to a remote device ---
def test_a_torrent_can_be_resolved_and_sent_to_a_remote_bitcomet(
    app_state, fake, tmp_path
):
    from test_torrent_api import sample_torrent

    remote_client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    # The fake binds loopback only, so mark it remote by hand.
    remote_client.is_local = False
    app_state.torrents = TorrentManager(remote_client, download_dir=tmp_path)

    with TestClient(create_app(state=app_state)) as test_client:
        resolved = test_client.post(
            "/api/torrent/resolve",
            files={
                "file": (
                    "Example.torrent",
                    sample_torrent(),
                    "application/x-bittorrent",
                )
            },
            data={"save_dir": "/volume1/downloads"},
        ).json()
        assert resolved["ready"] is True

        sent = test_client.post(
            "/api/torrent",
            json={"infohash": resolved["infohash"], "selected": [1]},
        )
        assert sent.status_code == 200

    task = next(iter(fake.tasks.values()))
    assert task["status"] == "running"
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled", "disabled"]
    assert not (tmp_path / "volume1").exists()


def test_a_remote_add_with_no_folder_falls_back_to_the_peers_own(
    app_state, fake, tmp_path
):
    from test_torrent_api import sample_torrent

    remote_client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    remote_client.is_local = False
    app_state.torrents = TorrentManager(remote_client, download_dir=tmp_path)

    with TestClient(create_app(state=app_state)) as test_client:
        resp = test_client.post(
            "/api/torrent/resolve",
            files={
                "file": (
                    "Example.torrent",
                    sample_torrent(),
                    "application/x-bittorrent",
                )
            },
        )
    assert resp.status_code == 200
    assert fake.save_folders == ["/volume1/downloads"]


# --- DEVICE EDITS DURING A BATCH ---
def test_forgetting_another_device_keeps_a_fetching_magnet_watched(
    client, fake, tmp_path
):
    """A rebuilt manager forgets the magnets it staged, and stops disabling them."""
    from test_torrent_api import HASH, MAGNET

    spare = add(client, "192.168.1.50:19377", label="Spare").json()["active"]
    client.post(f"/api/torrent/devices/{LOCAL_ID}/select")
    client.post(
        "/api/torrent/resolve",
        data={"magnet": MAGNET, "save_dir": str(tmp_path / "dl")},
    )

    assert client.delete(f"/api/torrent/devices/{spare}").status_code == 200

    fake.publish_metadata(HASH, [("Movie.mkv", 2_000_000_000), ("RARBG.txt", 30)])
    polled = client.get(f"/api/torrent/resolve/{HASH}").json()
    assert polled["state"] == "awaiting_selection"
    (task,) = fake.tasks.values()
    assert {f["priority"] for f in task["files"]} == {"disabled"}


def test_renaming_the_active_device_does_not_reconnect_it(client, app_state):
    device_id = add(client, "192.168.1.50:19377").json()["active"]
    before = app_state.torrents

    client.patch(f"/api/torrent/devices/{device_id}", json={"label": "Basement NAS"})
    assert app_state.torrents is before


def test_changing_the_active_devices_address_does_reconnect_it(client, app_state):
    device_id = add(client, "192.168.1.50:19377").json()["active"]
    before = app_state.torrents

    client.patch(f"/api/torrent/devices/{device_id}", json={"url": "192.168.1.51"})
    assert app_state.torrents is not before
    assert app_state.torrents.client.base_url == "http://192.168.1.51:19377"


# --- TIMEOUTS ---
def test_a_remote_device_gets_the_patient_timeout(client, app_state):
    from toolkit_engine.bitcomet import REMOTE_TIMEOUT

    add(client, "192.168.1.50:19377")
    assert app_state.torrents.client.timeout == REMOTE_TIMEOUT


def test_this_mac_keeps_the_snappy_timeout(client, app_state):
    add(client, "192.168.1.50:19377")
    client.post(f"/api/torrent/devices/{LOCAL_ID}/select")
    # 10.0 is BitCometClient's default timeout.
    assert app_state.torrents.client.timeout == 10.0
