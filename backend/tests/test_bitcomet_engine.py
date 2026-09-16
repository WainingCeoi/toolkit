"""BitComet engine: the login envelope, the transport, and the API's quirks."""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid

import pytest
import requests
from fake_bitcomet import SERVER_NAME, FakeBitComet

from toolkit_engine.bitcomet import (
    DESELECTED,
    HEADER_LEN,
    MAC_LEN,
    PROBE_TIMEOUT,
    BitCometClient,
    BitCometError,
    Credentials,
    decrypt,
    encrypt,
    login_payload,
    read_credentials,
    to_engine_index,
    to_toolkit_index,
)
from toolkit_engine.torrent import bencode


def make_torrent(files, name="Example.Release"):
    return bencode(
        {
            b"announce": b"udp://tracker.example:80",
            b"info": {
                b"name": name.encode(),
                b"piece length": 262144,
                b"pieces": b"\x00" * 20,
                b"files": [
                    {b"length": size, b"path": [p.encode() for p in path.split("/")]}
                    for path, size in files
                ],
            },
        }
    )


SAMPLE_FILES = [
    ("Movie.2024.1080p.mkv", 4_000_000),
    ("Sample/sample.mkv", 100_000),
    ("Movie.2024.chi.srt", 45_000),
    ("RARBG.txt", 1_000),
]


# --- LOGIN ENVELOPE ---
def test_login_envelope_round_trips():
    client_id = str(uuid.uuid4())
    original = json.dumps({"username": "someone", "password": "hunter2"})
    assert decrypt(encrypt(original, client_id), client_id) == original


def test_login_envelope_has_the_documented_byte_layout():
    client_id = str(uuid.uuid4())
    plaintext = json.dumps({"username": "a", "password": "b"})
    raw = base64.b64decode(encrypt(plaintext, client_id))

    assert raw[0:2] == b"\x03\x01"
    pad = 16 - len(plaintext.encode()) % 16
    assert len(raw) == HEADER_LEN + len(plaintext) + pad + MAC_LEN
    # The AES salt and the HMAC salt must be independently random.
    assert raw[2:10] != raw[10:18]
    assert len(raw[18:HEADER_LEN]) == 16


def test_login_envelope_pads_by_utf8_length_not_utf16():
    client_id = str(uuid.uuid4())
    plaintext = json.dumps({"username": "你好", "password": "pässwörd"})
    raw = base64.b64decode(encrypt(plaintext, client_id))

    assert (len(raw) - HEADER_LEN - MAC_LEN) % 16 == 0
    assert decrypt(encrypt(plaintext, client_id), client_id) == plaintext


def test_login_envelope_rejects_a_tampered_blob():
    client_id = str(uuid.uuid4())
    raw = bytearray(base64.b64decode(encrypt("secret", client_id)))
    raw[-1] ^= 0xFF

    with pytest.raises(ValueError, match="HMAC"):
        decrypt(base64.b64encode(bytes(raw)).decode(), client_id)


def test_login_payload_sends_the_key_in_the_clear_beside_the_ciphertext():
    first, second = login_payload("u", "p"), login_payload("u", "p")

    assert first["client_id"] != second["client_id"]
    assert json.loads(decrypt(first["authentication"], first["client_id"])) == {
        "username": "u",
        "password": "p",
    }


# --- CREDENTIALS ---
def write_config(tmp_path, username="webui", password="s3cret", port="19377"):
    path = tmp_path / "BitComet.xml"
    path.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<BitComet><Settings>"
        "<EnableWebInterface>true</EnableWebInterface>"
        f"<WebInterfaceUsername>{username}</WebInterfaceUsername>"
        f"<WebInterfacePassword>{password}</WebInterfacePassword>"
        f"<WebInterfacePort>{port}</WebInterfacePort>"
        "</Settings></BitComet>"
    )
    return path


def test_read_credentials_reads_bitcomets_own_config(tmp_path):
    creds = read_credentials(write_config(tmp_path))
    assert creds == Credentials(username="webui", password="s3cret", port=19377)
    assert creds.base_url == "http://127.0.0.1:19377"


def test_read_credentials_falls_back_to_the_default_port(tmp_path):
    creds = read_credentials(write_config(tmp_path, port="not-a-port"))
    assert creds.port == 19377


def test_read_credentials_explains_a_missing_config(tmp_path):
    with pytest.raises(BitCometError, match="not readable"):
        read_credentials(tmp_path / "nowhere.xml")


def test_read_credentials_explains_unset_remote_access(tmp_path):
    with pytest.raises(BitCometError, match="Remote Access"):
        read_credentials(write_config(tmp_path, password=""))


def test_read_credentials_reports_a_corrupt_config(tmp_path):
    path = tmp_path / "BitComet.xml"
    path.write_text("<BitComet><Settings>")
    with pytest.raises(BitCometError, match="not valid XML"):
        read_credentials(path)


# --- TRANSPORT ---
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
def client(fake):
    api = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    try:
        yield api
    finally:
        api.close()


def test_client_never_routes_loopback_through_a_proxy():
    api = BitCometClient(base_url="http://127.0.0.1:19377", username="u", password="p")
    try:
        assert api._session.trust_env is False
    finally:
        api.close()


def test_a_non_json_response_becomes_a_bitcomet_error(monkeypatch):
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    api = BitCometClient(base_url="http://127.0.0.1:1", username="u", password="p")
    try:
        monkeypatch.setattr(api._session, "request", lambda *a, **k: FakeResponse())
        with pytest.raises(BitCometError, match="non-JSON"):
            api.task_list()
        assert api.probe() is None
    finally:
        api.close()


def test_an_http_error_status_becomes_a_bitcomet_error(monkeypatch):
    class FakeResponse:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("503 Server Error")

        def json(self):  # pragma: no cover - must never be reached
            raise AssertionError("json() must not run on a 5xx")

    api = BitCometClient(base_url="http://127.0.0.1:1", username="u", password="p")
    try:
        monkeypatch.setattr(api._session, "request", lambda *a, **k: FakeResponse())
        with pytest.raises(BitCometError, match="HTTP 503"):
            api.task_list()
        assert api.probe() is None
    finally:
        api.close()


def test_an_unreachable_bitcomet_raises_a_clear_error():
    api = BitCometClient(
        base_url="http://127.0.0.1:1", username="u", password="p", timeout=0.5
    )
    try:
        with pytest.raises(BitCometError, match="not reachable"):
            api.task_list()
        assert api.probe() is None
    finally:
        api.close()


def test_probe_reports_the_server_name_when_reachable(client):
    assert client.probe() == SERVER_NAME


def test_bad_credentials_surface_as_an_error(fake):
    api = BitCometClient(base_url=fake.url, username=fake.username, password="wrong")
    try:
        with pytest.raises(BitCometError, match="invalid username or password"):
            api.task_list()
    finally:
        api.close()


# --- AUTH LIFECYCLE ---
def test_login_is_lazy_and_the_token_is_reused(fake, client):
    assert fake.logins == 0  # construction alone must not touch the network

    client.task_list()
    client.task_list()
    assert fake.logins == 1


def test_a_401_triggers_exactly_one_silent_reauth(fake, client):
    client.task_list()
    fake.revoke_tokens()  # BitComet restarted; the cached token is dead

    assert client.task_list() == []
    assert fake.logins == 2


def test_reauth_is_attempted_only_once_before_giving_up(fake, client):
    client.task_list()
    fake.reject_every_token = True

    with pytest.raises(BitCometError):
        client.task_list()
    assert fake.logins == 2


def test_threads_starting_together_share_one_handshake(fake, client):
    """A send window puts ten calls on one tokenless client at the same moment."""
    ready = threading.Barrier(10)
    failures: list[BaseException] = []

    def call():
        try:
            ready.wait(10.0)
            client.task_list()
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=call) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15.0)

    assert not failures
    assert fake.logins == 1


# --- TASK IDS ---
def test_task_ids_go_out_as_strings(fake, client):
    task_id = int(fake.add_task("A", SAMPLE_FILES))

    client.action(task_id, "start")
    client.set_priority(task_id, [1], "high")
    client.delete([task_id], delete_files=False)

    assert fake.deleted == [(str(task_id), False)]


def test_task_list_hands_back_string_ids(fake, client):
    seeded = fake.add_task("A", SAMPLE_FILES)
    (task,) = client.task_list()

    assert task["task_id"] == seeded
    client.action([task["task_id"]], "stop")
    assert fake.tasks[seeded]["status"] == "stopped"


def test_add_returns_a_string_task_id_despite_the_lowercase_ok(
    fake, client, save_folder
):
    result = client.add_torrent(make_torrent(SAMPLE_FILES), save_folder)

    assert isinstance(result["task_id"], str)
    assert fake.tasks[result["task_id"]]["status"] == "stopped"


def test_add_magnets_hands_back_no_task_id_at_all(fake, client, save_folder):
    links = [f"magnet:?xt=urn:btih:{'a' * 40}", f"magnet:?xt=urn:btih:{'b' * 40}"]
    result = client.add_magnets(links, save_folder, start_later=False)

    assert "task_id" not in result
    assert "task_ids" not in result
    assert {t["task_guid"] for t in fake.tasks.values()} == {
        f"bt_{'a' * 40}",
        f"bt_{'b' * 40}",
    }
    assert sum(1 for _m, path, _p in fake.calls if path.endswith("links/add")) == 1


def test_magnets_go_over_the_wire_newline_joined_not_as_a_list(
    fake, client, save_folder
):
    links = [f"magnet:?xt=urn:btih:{'a' * 40}", f"magnet:?xt=urn:btih:{'b' * 40}"]
    client.add_magnets(links, save_folder, start_later=False)

    sent = next(p for _m, path, p in fake.calls if path.endswith("links/add"))
    assert sent["torrent_links"] == "\n".join(links)
    assert not isinstance(sent["torrent_links"], list)


# --- MAGNET METADATA ---
MAGNET = f"magnet:?xt=urn:btih:{'a' * 40}"


def test_a_magnet_added_start_later_is_stopped_and_stays_empty(
    fake, client, save_folder
):
    fake.publish_metadata("a" * 40, SAMPLE_FILES)
    client.add_magnets([MAGNET], save_folder, start_later=True)

    (task,) = client.task_list()
    assert task["status"] == "stopped"
    assert client.files(task["task_id"]) == []


def test_a_running_magnet_reaches_the_swarm_and_learns_its_files(
    fake, client, save_folder
):
    fake.publish_metadata("a" * 40, SAMPLE_FILES)
    client.add_magnets([MAGNET], save_folder, start_later=False)

    (task,) = client.task_list()
    assert task["status"] == "running"
    assert [f["name"] for f in client.files(task["task_id"])] == [
        path for path, _size in SAMPLE_FILES
    ]


# --- FILE INDEXES ---
def test_index_translation_is_a_matched_pair():
    assert to_engine_index(1) == 0
    assert to_toolkit_index(0) == 1


def test_files_are_reported_with_this_repos_1_based_indexes(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    files = client.files(task_id)

    assert [f["index"] for f in files] == [1, 2, 3, 4]
    assert files[0]["name"] == "Movie.2024.1080p.mkv"


def test_set_priority_translates_down_to_0_based_indexes(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    client.set_priority(task_id, [1, 4], "high")

    (payload,) = [p for _m, path, p in fake.calls if path.endswith("set_priority")]
    assert payload["file_indexes"] == [0, 3]
    assert [f["priority"] for f in fake.tasks[task_id]["files"]] == [
        "high",
        "normal",
        "normal",
        "high",
    ]


def test_the_last_file_is_reachable_without_running_off_the_end(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    client.set_priority(task_id, [len(SAMPLE_FILES)], "high")

    assert fake.tasks[task_id]["files"][-1]["priority"] == "high"


# --- SELECTION ---
def test_deselecting_uses_disabled_and_shrinks_the_selected_size(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    before = client.task_list()[0]["selected_size"]

    client.set_priority(task_id, [2, 3, 4], DESELECTED)
    after = client.task_list()[0]["selected_size"]

    assert DESELECTED == "disabled"
    assert (before, after) == (4_146_000, 4_000_000)


def test_reselecting_a_file_restores_it(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    client.set_priority(task_id, [2, 3, 4], DESELECTED)
    client.set_priority(task_id, [3], "normal")

    assert client.task_list()[0]["selected_size"] == 4_045_000


def test_none_is_not_a_priority(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    with pytest.raises(BitCometError, match="unknown BitComet priority"):
        client.set_priority(task_id, [1], "none")


# --- SAVE FOLDER ---
def test_an_unregistered_save_folder_fails_the_add(client, tmp_path):
    with pytest.raises(BitCometError, match="save_folder invalid"):
        client.add_torrent(make_torrent(SAMPLE_FILES), tmp_path / "not-whitelisted")


def test_ensure_save_folder_registers_an_unknown_folder(fake, client, tmp_path):
    folder = tmp_path / "Torrents"
    assert client.ensure_save_folder(folder) == str(folder)

    assert str(folder) in fake.save_folders
    assert folder.is_dir()  # BitComet will not register a missing directory
    client.add_torrent(make_torrent(SAMPLE_FILES), folder)


def test_a_relative_local_save_folder_is_refused(fake, client, tmp_path, monkeypatch):
    # "Save to" is a free-text box; a bare name would land in the backend's cwd.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BitCometError, match="full path"):
        client.ensure_save_folder("Torrents")

    assert not (tmp_path / "Torrents").exists()
    assert not any(path.endswith("directories/add") for _m, path, _p in fake.calls)


def test_ensure_save_folder_does_not_re_register_a_known_one(fake, client, save_folder):
    # A trailing slash must not make one folder look like two.
    assert client.ensure_save_folder(f"{save_folder}/") == str(save_folder)

    assert fake.save_folders == [str(save_folder)]
    assert not any(path.endswith("directories/add") for _m, path, _p in fake.calls)


# --- CONTROL ---
def test_action_starts_and_stops(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)

    client.action([task_id], "start")
    assert fake.tasks[task_id]["status"] == "running"
    client.action([task_id], "stop")
    assert fake.tasks[task_id]["status"] == "stopped"


def test_action_rejects_an_unknown_verb(client):
    with pytest.raises(BitCometError, match="unknown BitComet action"):
        client.action(["1001"], "pause")


def test_starting_an_already_running_task_is_not_an_error(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    client.action([task_id], "start")

    client.action([task_id], "start")
    assert fake.tasks[task_id]["status"] == "running"


def test_stopping_an_already_stopped_task_is_not_an_error(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)

    client.action([task_id], "stop")
    assert fake.tasks[task_id]["status"] == "stopped"


def test_delete_files_selects_delete_all(fake, client):
    keep, wipe = fake.add_task("A", SAMPLE_FILES), fake.add_task("B", SAMPLE_FILES)

    client.delete([keep], delete_files=False)
    client.delete([wipe], delete_files=True)

    assert fake.deleted == [(keep, False), (wipe, True)]
    assert fake.tasks == {}


def test_a_torrent_over_the_cap_is_refused_before_it_is_sent(fake, client, save_folder):
    with pytest.raises(BitCometError, match="at most"):
        client.add_torrent(b"x" * (20 * 1024 * 1024 + 1), save_folder)
    assert not any(path.endswith("bt/add") for _m, path, _p in fake.calls)


def test_the_size_cap_is_read_from_bitcomet_not_hardcoded(fake, client, save_folder):
    fake.torrent_max_size = 4096

    with pytest.raises(BitCometError, match="at most 4096"):
        client.add_torrent(b"x" * 5000, save_folder)


def test_the_size_cap_is_asked_for_once_and_remembered(fake, client, save_folder):
    client.add_torrent(make_torrent(SAMPLE_FILES), save_folder)
    client.add_torrent(make_torrent(SAMPLE_FILES, name="Other"), save_folder)

    reads = [p for _m, path, p in fake.calls if path.endswith("new_task/get")]
    assert len(reads) == 1


# --- TIMEOUTS ---
def test_probe_gives_up_quickly_on_a_wedged_bitcomet(client):
    client.base_url = "http://10.255.255.1:19377"  # black-holes, never refuses
    started = time.monotonic()
    assert client.probe() is None
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"probe took {elapsed:.1f}s; PROBE_TIMEOUT is not applied"
    assert client.timeout == 10.0


def test_a_probe_racing_a_slow_login_keeps_its_own_budget(client):
    """Waiting for another thread's handshake must not outlast the probe's budget."""
    logging_in, release = threading.Event(), threading.Event()
    request = client._session.request

    def wrapped(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            logging_in.set()
            release.wait(5.0)
        return request(*args, **kwargs)

    client._session.request = wrapped
    thread = threading.Thread(target=client.task_list)
    thread.start()
    try:
        assert logging_in.wait(5.0)
        started = time.monotonic()
        answer = client.probe()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(10.0)

    assert elapsed < PROBE_TIMEOUT + 1.0, (
        f"probe spent {elapsed:.1f}s on another thread's login"
    )
    assert answer is None  # it gave up on the lock, not on a dead BitComet


def test_a_probe_cannot_shorten_a_call_running_beside_it(client):
    """One client serves every request thread, so no call may set the timeout."""
    client.task_list()  # log in first, so each call below is a single request
    probing, release, seen = threading.Event(), threading.Event(), []
    request = client._session.request

    def wrapped(*args, **kwargs):
        seen.append(kwargs["timeout"])
        if threading.current_thread() is not threading.main_thread():
            probing.set()
            release.wait(5.0)
        return request(*args, **kwargs)

    client._session.request = wrapped
    thread = threading.Thread(target=client.probe)
    thread.start()
    try:
        assert probing.wait(5.0)
        client.task_list()
    finally:
        release.set()
        thread.join(5.0)

    assert seen == [PROBE_TIMEOUT, 10.0]


# --- DEVICE IDENTITY ---
def test_the_device_id_survives_a_restart(tmp_path):
    from toolkit_engine.bitcomet import read_or_create_device_id

    path = tmp_path / "bitcomet-device-id"
    first = read_or_create_device_id(path)

    assert read_or_create_device_id(path) == first
    assert path.read_text().strip() == first


def test_a_client_without_a_device_id_file_still_works(tmp_path):
    from toolkit_engine.bitcomet import read_or_create_device_id

    assert read_or_create_device_id(None) != read_or_create_device_id(None)


def test_an_unwritable_device_id_path_does_not_break_the_client(tmp_path):
    from toolkit_engine.bitcomet import read_or_create_device_id

    blocked = tmp_path / "nope"
    blocked.write_text("")
    assert read_or_create_device_id(blocked / "sub" / "id")
