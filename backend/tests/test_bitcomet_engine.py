"""BitComet engine: the login envelope, the transport, and the API's quirks."""

from __future__ import annotations

import base64
import json
import uuid

import pytest
import requests
from fake_bitcomet import SERVER_NAME, FakeBitComet

from toolkit_engine.bitcomet import (
    DESELECTED,
    HEADER_LEN,
    MAC_LEN,
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
    """Build a real multi-file .torrent as bytes, so tests need no fixtures."""
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


# =======================================================
# LOGIN ENVELOPE
# =======================================================
def test_login_envelope_round_trips():
    client_id = str(uuid.uuid4())
    original = json.dumps({"username": "someone", "password": "hunter2"})
    assert decrypt(encrypt(original, client_id), client_id) == original


def test_login_envelope_has_the_documented_byte_layout():
    # BitComet's JS asserts the exact total length, so drift here is not a soft
    # failure -- the server refuses the login outright.
    client_id = str(uuid.uuid4())
    plaintext = json.dumps({"username": "a", "password": "b"})
    raw = base64.b64decode(encrypt(plaintext, client_id))

    assert raw[0:2] == b"\x03\x01"  # version marker
    pad = 16 - len(plaintext.encode()) % 16
    assert len(raw) == HEADER_LEN + len(plaintext) + pad + MAC_LEN
    # The AES salt, the HMAC salt and the IV are independently random: reusing
    # one blob for two of them still decrypts here and still fails on 2.20.
    assert raw[2:10] != raw[10:18]
    assert len(raw[18:HEADER_LEN]) == 16


def test_login_envelope_pads_by_utf8_length_not_utf16():
    # BitComet's own JS measures the UTF-16 string length, which is wrong for
    # any non-ASCII password; the block count must follow the encoded bytes.
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
    # The client_id IS the encryption key and travels next to the blob, so it
    # must be fresh per login and must actually open the envelope.
    first, second = login_payload("u", "p"), login_payload("u", "p")

    assert first["client_id"] != second["client_id"]
    assert json.loads(decrypt(first["authentication"], first["client_id"])) == {
        "username": "u",
        "password": "p",
    }


# =======================================================
# CREDENTIALS
# =======================================================
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
    # An empty password is the state of a fresh install, and the only symptom
    # further down would be a bare 401.
    with pytest.raises(BitCometError, match="Remote Access"):
        read_credentials(write_config(tmp_path, password=""))


def test_read_credentials_reports_a_corrupt_config(tmp_path):
    path = tmp_path / "BitComet.xml"
    path.write_text("<BitComet><Settings>")
    with pytest.raises(BitCometError, match="not valid XML"):
        read_credentials(path)


# =======================================================
# TRANSPORT
# =======================================================
@pytest.fixture
def save_folder(tmp_path):
    """A registered save folder. Real, because ensure_save_folder creates it."""
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
    # A configured HTTP proxy would intercept 127.0.0.1 and answer with a
    # non-JSON error page; BitComet must always be reached directly.
    api = BitCometClient(base_url="http://127.0.0.1:19377", username="u", password="p")
    try:
        assert api._session.trust_env is False
    finally:
        api.close()


def test_a_non_json_response_becomes_a_bitcomet_error(monkeypatch):
    # A proxy's HTML error page must be reported as unreachable, not crash the
    # caller with a JSONDecodeError.
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


# =======================================================
# AUTH LIFECYCLE
# =======================================================
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
    fake.reject_every_token = True  # no token will ever be accepted again

    with pytest.raises(BitCometError):
        client.task_list()
    # One retry, not a loop: a permanently rejecting server is not hammered.
    assert fake.logins == 2


# =======================================================
# TASK IDS
# =======================================================
def test_task_ids_go_out_as_strings(fake, client):
    # The fake refuses ints exactly as BitComet does, so an int reaching the
    # wire fails here instead of silently doing nothing in production.
    task_id = int(fake.add_task("A", SAMPLE_FILES))

    client.action(task_id, "start")
    client.set_priority(task_id, [1], "high")
    client.delete([task_id], delete_files=False)

    assert fake.deleted == [(str(task_id), False)]


def test_task_list_hands_back_string_ids(fake, client):
    seeded = fake.add_task("A", SAMPLE_FILES)
    (task,) = client.task_list()

    assert task["task_id"] == seeded
    # The API reports the id as an int inside a task object; feeding that value
    # straight back into action() must still work.
    client.action([task["task_id"]], "stop")
    assert fake.tasks[seeded]["status"] == "stopped"


def test_add_returns_a_string_task_id_despite_the_lowercase_ok(
    fake, client, save_folder
):
    # /api/task/bt/add answers error_code "ok"; every other endpoint says "OK".
    # Comparing exactly would make each successful add look like a failure.
    result = client.add_torrent(make_torrent(SAMPLE_FILES), save_folder)

    assert isinstance(result["task_id"], str)
    # start_later is the review step: nothing may move until the user commits.
    assert fake.tasks[result["task_id"]]["status"] == "stopped"


def test_add_magnets_hands_back_no_task_id_at_all(fake, client, save_folder):
    links = [f"magnet:?xt=urn:btih:{'a' * 40}", f"magnet:?xt=urn:btih:{'b' * 40}"]
    result = client.add_magnets(links, save_folder, start_later=False)

    # torrent_links/add is asynchronous: it answers "adding task in batch
    # started." and nothing else. Code reading task_id/task_ids out of this
    # gets nothing, silently, and never records a handle on the task.
    assert "task_id" not in result
    assert "task_ids" not in result
    # The only way back to what it created is the guid.
    assert {t["task_guid"] for t in fake.tasks.values()} == {
        f"bt_{'a' * 40}",
        f"bt_{'b' * 40}",
    }
    # One request for the whole batch, not one per magnet.
    assert sum(1 for _m, path, _p in fake.calls if path.endswith("links/add")) == 1


def test_magnets_go_over_the_wire_newline_joined_not_as_a_list(
    fake, client, save_folder
):
    links = [f"magnet:?xt=urn:btih:{'a' * 40}", f"magnet:?xt=urn:btih:{'b' * 40}"]
    client.add_magnets(links, save_folder, start_later=False)

    sent = next(p for _m, path, p in fake.calls if path.endswith("links/add"))
    # A JSON array is refused with "torrent_links missing" -- the field reads as
    # absent, so the error points at the wrong thing and every magnet add fails.
    assert sent["torrent_links"] == "\n".join(links)
    assert not isinstance(sent["torrent_links"], list)


# =======================================================
# MAGNET METADATA
# =======================================================
MAGNET = f"magnet:?xt=urn:btih:{'a' * 40}"


def test_a_magnet_added_start_later_is_stopped_and_stays_empty(
    fake, client, save_folder
):
    fake.publish_metadata("a" * 40, SAMPLE_FILES)
    client.add_magnets([MAGNET], save_folder, start_later=True)

    (task,) = client.task_list()
    assert task["status"] == "stopped"
    # The measured behaviour that breaks the obvious design: a stopped task
    # never contacts the swarm, so its file list stays empty forever and the
    # review step it was supposed to enable can never happen.
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


# =======================================================
# FILE INDEXES
# =======================================================
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

    # The wire carries 0-based indexes...
    (payload,) = [p for _m, path, p in fake.calls if path.endswith("set_priority")]
    assert payload["file_indexes"] == [0, 3]
    # ...and the two files that actually moved are the intended ones.
    assert [f["priority"] for f in fake.tasks[task_id]["files"]] == [
        "high",
        "normal",
        "normal",
        "high",
    ]


def test_the_last_file_is_reachable_without_running_off_the_end(fake, client):
    # The off-by-one the fake's range check exists to catch: a 1-based index
    # for the final file is one past the end in BitComet's numbering.
    task_id = fake.add_task("A", SAMPLE_FILES)
    client.set_priority(task_id, [len(SAMPLE_FILES)], "high")

    assert fake.tasks[task_id]["files"][-1]["priority"] == "high"


# =======================================================
# SELECTION
# =======================================================
def test_deselecting_uses_disabled_and_shrinks_the_selected_size(fake, client):
    task_id = fake.add_task("A", SAMPLE_FILES)
    before = client.task_list()[0]["selected_size"]

    # Keep only the feature. Everything else is deselected, which in BitComet
    # means priority "disabled" -- there is no separate select flag.
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
    # "none" reads like the deselect value and is rejected by the API; catching
    # it here stops the mistake from looking like a working deselection.
    task_id = fake.add_task("A", SAMPLE_FILES)
    with pytest.raises(BitCometError, match="unknown BitComet priority"):
        client.set_priority(task_id, [1], "none")


# =======================================================
# SAVE FOLDER
# =======================================================
def test_an_unregistered_save_folder_fails_the_add(client, tmp_path):
    with pytest.raises(BitCometError, match="save_folder invalid"):
        client.add_torrent(make_torrent(SAMPLE_FILES), tmp_path / "not-whitelisted")


def test_ensure_save_folder_registers_an_unknown_folder(fake, client, tmp_path):
    folder = tmp_path / "Torrents"
    assert client.ensure_save_folder(folder) == str(folder)

    assert str(folder) in fake.save_folders
    assert folder.is_dir()  # BitComet will not register a missing directory
    # And the add it exists for now succeeds.
    client.add_torrent(make_torrent(SAMPLE_FILES), folder)


def test_ensure_save_folder_does_not_re_register_a_known_one(fake, client, save_folder):
    # A trailing slash must not make one folder look like two.
    assert client.ensure_save_folder(f"{save_folder}/") == str(save_folder)

    assert fake.save_folders == [str(save_folder)]
    assert not any(path.endswith("directories/add") for _m, path, _p in fake.calls)


# =======================================================
# CONTROL
# =======================================================
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

    # BitComet answers error_code "skipped" for a no-op. Raising on that breaks
    # re-committing a selection: set_priority has already landed by then, so the
    # store would keep a selection BitComet does not have.
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

    # It is a constant of the running build, so paying a round trip per add to
    # re-learn it would be waste.
    reads = [p for _m, path, p in fake.calls if path.endswith("new_task/get")]
    assert len(reads) == 1


# =======================================================
# TIMEOUTS
# =======================================================
def test_deadline_applies_only_inside_the_block(client):
    with client.deadline(0.25):
        assert client.timeout == 0.25
    # Startup stays impatient without making every later call twitchy.
    assert client.timeout == 10.0


def test_deadline_restores_the_timeout_even_when_the_block_fails(client):
    with pytest.raises(BitCometError), client.deadline(0.25):
        raise BitCometError("boom")
    assert client.timeout == 10.0
