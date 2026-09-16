"""A BitComet on another machine: addresses, save folders, and the device book."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from fake_bitcomet import FakeBitComet

from toolkit_api.devices import LOCAL_ID, Device, DeviceBook
from toolkit_engine.bitcomet import (
    DEFAULT_PORT,
    BitCometClient,
    BitCometError,
    is_local_host,
    normalize_base_url,
)


@pytest.fixture
def fake():
    server = FakeBitComet(save_folders=["/volume1/downloads"])
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def remote(fake):
    """A client pointed at the fake, but treated as a machine on the LAN."""
    client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    client.is_local = False
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def book(tmp_path):
    return DeviceBook(tmp_path / "torrents.db")


# --- ADDRESSES ---
@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("http://192.168.1.50:19377", "http://192.168.1.50:19377"),
        ("192.168.1.50:19377", "http://192.168.1.50:19377"),
        ("192.168.1.50", f"http://192.168.1.50:{DEFAULT_PORT}"),
        # "nas:19377" parses as scheme "nas" unless a scheme is prepended.
        ("nas:19377", "http://nas:19377"),
        ("nas.local", f"http://nas.local:{DEFAULT_PORT}"),
        ("//nas.local:8080", "http://nas.local:8080"),
        ("http://nas.local:19377/webui/index.html", "http://nas.local:19377"),
        ("http://nas.local:19377/", "http://nas.local:19377"),
        ("  192.168.1.50:19377  ", "http://192.168.1.50:19377"),
        ("https://nas.local:443", "https://nas.local:443"),
        ("http://[fd00::1]:19377", "http://[fd00::1]:19377"),
        ("NAS.Local:19377", "http://nas.local:19377"),
    ],
)
def test_normalize_base_url_accepts_what_people_type(typed, expected):
    assert normalize_base_url(typed) == expected


@pytest.mark.parametrize(
    ("typed", "fragment"),
    [
        ("", "Enter the address"),
        ("   ", "Enter the address"),
        ("ftp://nas.local:19377", "http://"),
        ("http://nas.local:notaport", "no usable port"),
        ("http://nas.local:70000", "no usable port"),
        ("http://me:secret@nas.local:19377", "fields for them"),
    ],
)
def test_normalize_base_url_refuses_with_a_reason(typed, fragment):
    with pytest.raises(BitCometError) as caught:
        normalize_base_url(typed)
    assert fragment in str(caught.value)


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("127.0.0.5", True),
        ("192.168.1.50", False),
        ("nas.local", False),
        ("10.0.0.4", False),
    ],
)
def test_is_local_host(host, local):
    assert is_local_host(host) is local


def test_client_reads_its_own_locality_from_the_address(fake):
    client = BitCometClient(base_url=fake.url, username="u", password="p")
    assert client.is_local is True
    client.close()

    remote = BitCometClient(base_url="nas.local:19377", username="u", password="p")
    assert remote.is_local is False
    assert remote.base_url == "http://nas.local:19377"
    remote.close()


def test_client_refuses_an_unusable_address():
    with pytest.raises(BitCometError):
        BitCometClient(base_url="ftp://nas.local", username="u", password="p")


# --- SAVE FOLDERS ---
def test_remote_save_folder_is_never_created_on_this_machine(remote, tmp_path):
    wanted = tmp_path / "not-ours"
    assert remote.ensure_save_folder(str(wanted)) == str(wanted)
    assert not wanted.exists()


def test_local_save_folder_is_created_because_bitcomet_will_not_register_one(
    fake, tmp_path
):
    client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    wanted = tmp_path / "brand-new"
    assert client.ensure_save_folder(str(wanted)) == str(wanted)
    assert wanted.is_dir()
    client.close()


def test_remote_save_folder_registers_with_the_peer(remote, fake):
    remote.ensure_save_folder("/volume2/media")
    assert "/volume2/media" in fake.save_folders


def test_remote_save_folder_is_not_re_registered_when_already_known(remote, fake):
    remote.ensure_save_folder("/volume1/downloads/")  # trailing slash, same folder
    adds = [c for c in fake.calls if c[1] == "/api/config/directories/add"]
    assert adds == []


def test_remote_save_folder_refuses_a_tilde_path(remote):
    with pytest.raises(BitCometError) as caught:
        remote.ensure_save_folder("~/Downloads")
    assert "path on this Mac" in str(caught.value)


def test_remote_save_folder_refuses_an_empty_path(remote):
    with pytest.raises(BitCometError):
        remote.ensure_save_folder("   ")


def test_save_folders_lists_what_the_peer_will_accept(remote, fake):
    fake.save_folders.append("/volume2/media")
    assert remote.save_folders() == ["/volume1/downloads", "/volume2/media"]


# --- THE DEVICE BOOK ---
def test_the_local_device_exists_in_an_empty_book(book):
    assert [d.id for d in book.list()] == [LOCAL_ID]
    assert book.active().id == LOCAL_ID
    assert book.active().is_local is True


def test_adding_a_device_saves_it_and_selects_it(book):
    device = book.add("NAS", "192.168.1.50:19377", "admin", "hunter2")
    assert device.url == "http://192.168.1.50:19377"
    assert book.active().id == device.id
    assert [d.id for d in DeviceBook(book.path).list()] == [LOCAL_ID, device.id]


def test_a_device_file_is_not_world_readable(book):
    book.add("NAS", "192.168.1.50:19377", "admin", "hunter2")
    assert book.path.stat().st_mode & 0o077 == 0


def test_a_device_needs_credentials(book):
    with pytest.raises(BitCometError):
        book.add("NAS", "192.168.1.50:19377", "admin", "")
    with pytest.raises(BitCometError):
        book.add("NAS", "192.168.1.50:19377", "", "hunter2")


def test_a_nameless_device_falls_back_to_its_host(book):
    assert (
        book.add("", "192.168.1.50:19377", "admin", "pw").label == "192.168.1.50:19377"
    )


def test_re_adding_one_address_edits_it_rather_than_duplicating(book):
    first = book.add("NAS", "192.168.1.50:19377", "admin", "old")
    again = book.add("NAS v2", "http://192.168.1.50:19377/", "admin", "new")
    assert again.id == first.id
    assert again.password == "new"
    assert len(book.list()) == 2  # local + the one device


def test_updating_without_a_password_keeps_the_stored_one(book):
    device = book.add("NAS", "192.168.1.50:19377", "admin", "hunter2")
    renamed = book.update(device.id, label="Basement NAS")
    assert renamed.label == "Basement NAS"
    assert renamed.password == "hunter2"
    assert renamed.url == "http://192.168.1.50:19377"


def test_updating_an_unknown_device_raises(book):
    with pytest.raises(KeyError):
        book.update("nope", label="x")


def test_the_local_device_cannot_be_edited_or_removed(book):
    with pytest.raises(BitCometError):
        book.update(LOCAL_ID, label="Renamed")
    with pytest.raises(BitCometError):
        book.remove(LOCAL_ID)


def test_removing_the_active_device_falls_back_to_this_mac(book):
    device = book.add("NAS", "192.168.1.50:19377", "admin", "pw")
    assert book.active().id == device.id
    book.remove(device.id)
    assert book.active().id == LOCAL_ID


def test_removing_an_idle_device_leaves_the_selection_alone(book):
    keep = book.add("NAS", "192.168.1.50:19377", "admin", "pw")
    other = book.add("Desktop", "192.168.1.51:19377", "admin", "pw")
    book.select(keep.id)
    book.remove(other.id)
    assert book.active().id == keep.id


def test_selecting_an_unknown_device_raises(book):
    with pytest.raises(KeyError):
        book.select("nope")


def test_a_selection_pointing_at_nothing_falls_back(book):
    with closing(sqlite3.connect(book.path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO device_settings VALUES ('active', 'ghost')"
        )
    assert book.active().id == LOCAL_ID


def test_a_book_damaged_after_startup_still_offers_this_mac(book):
    book.path.write_bytes(b"this is not a database")
    assert [d.id for d in book.list()] == [LOCAL_ID]
    assert book.active().id == LOCAL_ID


def test_a_book_damaged_before_startup_is_set_aside_and_rebuilt(tmp_path):
    path = tmp_path / "torrents.db"
    path.write_bytes(b"SQLite format 3\x00 except not really" * 40)
    book = DeviceBook(path)
    assert (tmp_path / "torrents.db.corrupt").exists()
    assert [d.id for d in book.list()] == [LOCAL_ID]
    book.add("NAS", "192.168.1.50:19377", "admin", "pw")
    assert len(book.list()) == 2


def test_rows_without_an_address_are_skipped_rather_than_taking_the_book_down(book):
    with closing(sqlite3.connect(book.path)) as conn, conn:
        conn.execute("INSERT INTO devices (id, label, url) VALUES ('x', 'ghost', '')")
    assert [d.id for d in book.list()] == [LOCAL_ID]


# --- THE LEGACY JSON BOOK ---
def legacy_body():
    return {
        "active": "abc123",
        "devices": [
            {
                "id": "abc123",
                "label": "Intel Mac",
                "url": "http://192.168.110.27:10447",
                "username": "webui",
                "password": "s3cret",
            }
        ],
    }


def test_a_json_book_is_imported_once_and_removed(tmp_path):
    legacy = tmp_path / "bitcomet-devices.json"
    legacy.write_text(json.dumps(legacy_body()))

    book = DeviceBook(tmp_path / "torrents.db")
    assert not legacy.exists()
    device = book.active()
    assert device.id == "abc123"
    assert device.label == "Intel Mac"
    assert device.url == "http://192.168.110.27:10447"
    assert device.password == "s3cret"


def test_importing_never_overwrites_what_the_database_already_holds(tmp_path):
    book = DeviceBook(tmp_path / "torrents.db")
    kept = book.add("NAS", "192.168.110.27:10447", "admin", "new-password")

    stale = legacy_body()
    stale["devices"][0]["id"] = kept.id
    stale["devices"][0]["password"] = "old-password"
    (tmp_path / "bitcomet-devices.json").write_text(json.dumps(stale))

    again = DeviceBook(tmp_path / "torrents.db")
    assert again.get(kept.id).password == "new-password"
    assert again.active().id == kept.id


def test_a_corrupt_legacy_file_is_dropped_without_taking_the_book_down(tmp_path):
    legacy = tmp_path / "bitcomet-devices.json"
    legacy.write_text("{ not json")
    book = DeviceBook(tmp_path / "torrents.db")
    assert not legacy.exists()
    assert [d.id for d in book.list()] == [LOCAL_ID]


def test_the_public_shape_never_carries_the_password():
    secret = "correct-horse-battery-staple"
    device = Device(
        id="x", label="NAS", url="http://n:1", username="a", password=secret
    )
    public = device.public()
    assert public["has_password"] is True
    assert "password" not in public
    assert secret not in str(public)
