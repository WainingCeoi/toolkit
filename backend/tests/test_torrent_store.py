"""Torrent Downloader: the durable queue and its reconciliation with aria2."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_aria2 import FakeAria2

from toolkit_api.torrents import DEFAULT_SAVE_DIR, METADATA_TIMEOUT, TorrentManager
from toolkit_engine.aria2 import Aria2RPC
from toolkit_engine.torrent import TorrentFile, bencode
from toolkit_engine.torrentdb import TorrentStore

HASH = "c9e15763f722f23e98a29decdfae341b98d53056"


@pytest.fixture
def store():
    s = TorrentStore(":memory:")
    try:
        yield s
    finally:
        s.close()


def add(store, infohash=HASH, selected=None, **overrides):
    """Seed a row. `selected` is set separately -- upsert never writes it, so
    that a re-resolve can never silently clobber the user's file choices."""
    store.upsert(
        **{
            "infohash": infohash,
            "source": f"magnet:?xt=urn:btih:{infohash}",
            "source_kind": "magnet",
            "name": "Example",
            "total_bytes": 1000,
            "save_dir": "/tmp/dl",
            "state": "awaiting_selection",
            **overrides,
        }
    )
    if selected is not None:
        store.set_selection(infohash, selected)


# =======================================================
# STORE
# =======================================================
def test_upsert_then_get_round_trips(store):
    add(store)
    row = store.get(HASH)
    assert row["infohash"] == HASH
    assert row["state"] == "awaiting_selection"
    assert row["selected"] is None


def test_upsert_is_idempotent_on_infohash(store):
    add(store, name="First")
    add(store, name="Second")
    assert len(store.all()) == 1
    assert store.get(HASH)["name"] == "Second"


def test_get_returns_none_for_an_unknown_infohash(store):
    assert store.get("0" * 40) is None


def test_files_round_trip_in_index_order(store):
    add(store)
    store.set_files(
        HASH,
        [
            TorrentFile(index=2, path="b.mkv", size=20),
            TorrentFile(index=1, path="a.mkv", size=10),
        ],
    )
    assert store.files(HASH) == [
        TorrentFile(index=1, path="a.mkv", size=10),
        TorrentFile(index=2, path="b.mkv", size=20),
    ]


def test_set_files_replaces_rather_than_appends(store):
    add(store)
    store.set_files(HASH, [TorrentFile(index=1, path="old.mkv", size=1)])
    store.set_files(HASH, [TorrentFile(index=1, path="new.mkv", size=2)])
    assert [f.path for f in store.files(HASH)] == ["new.mkv"]


def test_set_selection_stores_aria2_syntax(store):
    add(store)
    store.set_selection(HASH, "1,4,7")
    assert store.get(HASH)["selected"] == "1,4,7"


def test_set_save_dir_updates_the_row(store):
    add(store)
    store.set_save_dir(HASH, "~/Movies")
    # Stored verbatim; expansion happens only at the filesystem boundary.
    assert store.get(HASH)["save_dir"] == "~/Movies"


def test_pause_reason_distinguishes_user_from_shutdown(store):
    add(store)
    store.set_state(HASH, "paused", pause_reason="shutdown")
    assert store.get(HASH)["pause_reason"] == "shutdown"

    store.set_state(HASH, "active")
    # Leaving a stale reason behind would auto-resume a torrent the user
    # deliberately paused on the next boot.
    assert store.get(HASH)["pause_reason"] is None


def test_paused_by_shutdown_is_queryable(store):
    add(store, infohash="a" * 40)
    add(store, infohash="b" * 40)
    store.set_state("a" * 40, "paused", pause_reason="shutdown")
    store.set_state("b" * 40, "paused", pause_reason="user")

    assert [r["infohash"] for r in store.paused_by_shutdown()] == ["a" * 40]


def test_set_state_records_an_error_message(store):
    add(store)
    store.set_state(HASH, "error", last_error="no seeders")
    assert store.get(HASH)["last_error"] == "no seeders"


def test_completing_stamps_completed_at(store):
    add(store)
    store.set_state(HASH, "complete")
    assert store.get(HASH)["completed_at"] is not None


def test_tombstone_keeps_the_row_so_it_is_not_re_adopted(store):
    add(store)
    store.tombstone(HASH)
    assert store.get(HASH)["state"] == "removed"
    # Still present: reconciliation checks this to avoid re-adding a torrent
    # the user deleted while the daemon still had it.
    assert len(store.all()) == 1


def test_all_excludes_tombstones_on_request(store):
    add(store, infohash="a" * 40)
    add(store, infohash="b" * 40)
    store.tombstone("b" * 40)
    assert [r["infohash"] for r in store.all(include_removed=False)] == ["a" * 40]


# =======================================================
# MANAGER
# =======================================================
@pytest.fixture
def fake_aria2():
    server = FakeAria2()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def manager(store, fake_aria2, tmp_path):
    rpc = Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)
    m = TorrentManager(store, rpc, download_dir=tmp_path / "dl", owned=True)
    yield m
    # Disarm any grace timer a presence test armed, so it cannot fire after the
    # store is closed and the fake server is stopped.
    m.cancel_pending_shutdown()


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
                ],
            }
        }
    )


def test_resolving_a_torrent_file_is_ready_immediately(manager):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")

    # A .torrent is bencode: the file list is readable offline, with no
    # daemon round trip and no swarm to depend on.
    assert out["ready"] is True
    assert out["name"] == "Example.Release"
    assert [f["path"] for f in out["files"]] == ["Movie.mkv", "Movie.chi.srt"]
    assert [f["category"] for f in out["files"]] == ["video", "subtitle"]


def test_resolving_a_torrent_file_persists_it_awaiting_selection(manager, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    row = store.get(out["infohash"])
    assert row["state"] == "awaiting_selection"
    assert row["source_kind"] == "torrent"


def test_resolving_a_magnet_is_not_ready_yet(manager):
    out = manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}&dn=Some.Name")

    assert out["ready"] is False
    assert out["infohash"] == HASH
    assert out["files"] == []


def test_resolving_a_magnet_adds_it_with_pause_metadata(manager, fake_aria2):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    added = [d for d in fake_aria2.downloads.values() if d.get("uris")]
    # Without pause-metadata the daemon starts downloading content the moment
    # metadata lands, before the user has chosen anything.
    assert added[0]["options"]["pause-metadata"] == "true"


def test_poll_resolve_reports_ready_once_the_daemon_has_files(manager, fake_aria2):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    gid = manager.gid_for(HASH)
    fake_aria2.downloads[gid].update(
        status="paused",
        totalLength="2000045000",
        files=[
            {"index": "1", "path": "/dl/Example/Movie.mkv", "length": "2000000000"},
            {"index": "2", "path": "/dl/Example/Movie.chi.srt", "length": "45000"},
        ],
    )

    polled = manager.poll_resolve(HASH)
    assert polled["ready"] is True
    assert [f["path"] for f in polled["files"]] == ["Movie.mkv", "Movie.chi.srt"]


def test_poll_resolve_gives_up_after_the_metadata_timeout(manager, store, monkeypatch):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    # aria2 waits forever on a dead swarm and exposes no cancel, so the
    # deadline has to be ours.
    monkeypatch.setattr(manager, "_resolve_started", {HASH: -METADATA_TIMEOUT - 1})

    out = manager.poll_resolve(HASH)
    assert out["state"] == "error"
    assert "metadata" in store.get(HASH)["last_error"]


def test_commit_applies_the_selection_and_unpauses(manager, fake_aria2, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    infohash = out["infohash"]

    manager.commit(infohash, [1], "/tmp/dest")

    gid = manager.gid_for(infohash)
    assert fake_aria2.downloads[gid]["options"]["select-file"] == "1"
    assert fake_aria2.downloads[gid]["status"] == "active"
    assert store.get(infohash)["selected"] == "1"
    assert store.get(infohash)["state"] == "active"


def test_commit_sets_the_selection_before_unpausing(manager, fake_aria2):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1, 2], "/tmp/dest")

    methods = [m for m, _ in fake_aria2.calls]
    # Changing select-file on an ACTIVE download force-restarts it, and can be
    # discarded silently. It must land while the torrent is still paused.
    assert methods.index("aria2.addTorrent") < methods.index("aria2.unpause")


def test_commit_rejects_an_empty_selection(manager):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    with pytest.raises(ValueError, match="at least one file"):
        manager.commit(out["infohash"], [], "/tmp/dest")


def test_commit_expands_a_tilde_save_dir_for_aria2(manager, fake_aria2):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1], "~/Downloads")

    got = fake_aria2.downloads[manager.gid_for(out["infohash"])]["options"]["dir"]
    # aria2 gets no shell, so a literal ~ would become ./~/Downloads.
    assert got == str(Path("~/Downloads").expanduser())
    assert "~" not in got


def test_commit_persists_the_chosen_save_dir(manager, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1], "~/Movies")

    # Stored in the tidy form the user chose, so the dashboard, reconciliation,
    # and deletion all use it instead of the resolve-time default.
    assert store.get(out["infohash"])["save_dir"] == "~/Movies"


def test_resolve_seeds_the_default_save_dir(store, fake_aria2):
    rpc = Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)
    m = TorrentManager(store, rpc, download_dir=DEFAULT_SAVE_DIR, owned=True)
    try:
        out = m.resolve_torrent(sample_torrent(), "Example.torrent")
        assert store.get(out["infohash"])["save_dir"] == "~/Downloads"
    finally:
        m.cancel_pending_shutdown()


# =======================================================
# RECONCILIATION
# =======================================================
def test_reconcile_rebuilds_the_gid_map_from_infohash(manager, fake_aria2, store):
    add(store, state="active", selected="1")
    gid = fake_aria2.add_download(infohash=HASH, status="active")

    manager.reconcile()

    # GIDs are re-minted across restarts, so the map must be rebuilt by
    # infohash rather than persisted.
    assert manager.gid_for(HASH) == gid


def test_reconcile_prefers_the_content_group_over_the_metadata_group(
    manager, fake_aria2, store
):
    add(store, state="active", selected="1")
    meta_gid = fake_aria2.add_download(infohash=HASH, status="waiting")
    content_gid = fake_aria2.add_download(infohash=HASH, status="active")
    fake_aria2.downloads[meta_gid]["followedBy"] = [content_gid]

    manager.reconcile()
    assert manager.gid_for(HASH) == content_gid


def test_reconcile_readds_a_row_the_daemon_forgot(manager, fake_aria2, store):
    add(store, state="active", selected="1,3")
    manager.reconcile()

    added = [d for d in fake_aria2.downloads.values() if d.get("uris")]
    assert len(added) == 1
    # The selection has to be re-asserted from OUR record -- the daemon's copy
    # is a cache that a lost session file takes with it.
    assert added[0]["options"]["select-file"] == "1,3"


def test_reconcile_readds_with_an_expanded_save_dir(manager, fake_aria2, store):
    add(store, state="active", selected="1", save_dir="~/Downloads")
    manager.reconcile()

    added = [d for d in fake_aria2.downloads.values() if d.get("uris")]
    got = added[0]["options"]["dir"]
    assert got == str(Path("~/Downloads").expanduser())
    assert "~" not in got


def test_reconcile_does_not_readd_completed_or_removed_rows(manager, fake_aria2, store):
    add(store, infohash="a" * 40, state="complete")
    add(store, infohash="b" * 40, state="removed")

    manager.reconcile()
    assert [d for d in fake_aria2.downloads.values() if d.get("uris")] == []


def test_reconcile_resumes_only_what_shutdown_paused(manager, fake_aria2, store):
    add(store, infohash="a" * 40, state="paused", selected="1")
    add(store, infohash="b" * 40, state="paused", selected="1")
    store.set_state("a" * 40, "paused", pause_reason="shutdown")
    store.set_state("b" * 40, "paused", pause_reason="user")
    fake_aria2.add_download(infohash="a" * 40, status="paused")
    fake_aria2.add_download(infohash="b" * 40, status="paused")

    manager.reconcile()

    assert store.get("a" * 40)["state"] == "active"
    # A deliberate pause must survive the restart.
    assert store.get("b" * 40)["state"] == "paused"


def test_shutdown_pauses_everything_and_stops_an_owned_daemon(
    manager, fake_aria2, store
):
    add(store, state="active", selected="1")
    fake_aria2.add_download(infohash=HASH, status="active")
    manager.reconcile()

    manager.shutdown()

    methods = [m for m, _ in fake_aria2.calls]
    assert "aria2.forcePauseAll" in methods
    assert "aria2.saveSession" in methods
    assert "aria2.shutdown" in methods
    assert store.get(HASH)["pause_reason"] == "shutdown"


def test_shutdown_never_stops_a_daemon_we_did_not_start(store, fake_aria2, tmp_path):
    rpc = Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)
    attached = TorrentManager(store, rpc, download_dir=tmp_path, owned=False)
    add(store, state="active", selected="1")
    gid = fake_aria2.add_download(infohash=HASH, status="active")
    attached.reconcile()

    attached.shutdown()

    methods = [m for m, _ in fake_aria2.calls]
    # Someone else's aria2 may be running downloads this tool knows nothing
    # about: pause only our own gids, and never shut it down.
    assert "aria2.shutdown" not in methods
    assert "aria2.forcePauseAll" not in methods
    assert ("aria2.forcePause", [gid]) in fake_aria2.calls


def test_a_reconnect_inside_the_grace_window_cancels_shutdown(manager):
    manager.GRACE_SECONDS = 30.0
    manager.client_connected()
    manager.client_disconnected()
    assert manager.shutdown_pending() is True

    manager.client_connected()
    # A page refresh fires the same disconnect a real departure does; killing
    # the daemon on it would stop downloads on every F5.
    assert manager.shutdown_pending() is False


def test_a_second_tab_keeps_the_daemon_alive(manager):
    manager.GRACE_SECONDS = 30.0
    manager.client_connected()
    manager.client_connected()
    manager.client_disconnected()

    # One tab closing while another is still open is not an absence.
    assert manager.shutdown_pending() is False


def test_cancel_pending_shutdown_disarms_the_timer(manager):
    manager.GRACE_SECONDS = 30.0
    manager.client_connected()
    manager.client_disconnected()
    assert manager.shutdown_pending() is True

    # Disposal must be able to disarm the timer, or it fires later against a
    # closed store / stopped daemon -- a stray background-thread exception.
    manager.cancel_pending_shutdown()
    assert manager.shutdown_pending() is False


def test_shutdown_stops_an_owned_daemon_process(store, fake_aria2, tmp_path):
    pid_file = tmp_path / "aria2.pid"
    pid_file.write_text("4242")
    rpc = Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)
    m = TorrentManager(store, rpc, download_dir=tmp_path, owned=True, pid_file=pid_file)

    m.shutdown()

    # The graceful RPC is best-effort; the PID-file stop is the guarantee that
    # a daemon we own is gone and the file cleaned up.
    assert not pid_file.exists()


def test_shutdown_leaves_no_pid_file_untouched_when_not_owned(
    store, fake_aria2, tmp_path
):
    pid_file = tmp_path / "aria2.pid"
    pid_file.write_text("4242")
    rpc = Aria2RPC(url=fake_aria2.url, secret=fake_aria2.secret)
    # owned=False and no pid_file passed: an external daemon's PID file (were
    # one to exist) must never be removed by us.
    m = TorrentManager(store, rpc, download_dir=tmp_path, owned=False)

    m.shutdown()
    assert pid_file.exists()


# =======================================================
# DASHBOARD
# =======================================================
def test_snapshot_computes_progress_and_eta(manager, fake_aria2, store):
    add(store, state="active", selected="1")
    fake_aria2.add_download(
        infohash=HASH,
        status="active",
        totalLength="1000",
        completedLength="250",
        downloadSpeed="50",
    )
    manager.reconcile()

    row = manager.snapshot()[0]
    assert row["progress"] == pytest.approx(25.0)
    assert row["speed"] == 50
    assert row["eta_seconds"] == 15  # (1000-250)/50


def test_snapshot_reports_no_eta_when_stalled(manager, fake_aria2, store):
    add(store, state="active", selected="1")
    fake_aria2.add_download(
        infohash=HASH, status="active", totalLength="1000", downloadSpeed="0"
    )
    manager.reconcile()
    assert manager.snapshot()[0]["eta_seconds"] is None


def test_snapshot_hides_tombstoned_rows(manager, store):
    add(store, state="active", selected="1")
    store.tombstone(HASH)
    assert manager.snapshot() == []


def test_snapshot_survives_the_daemon_being_down(manager, store, tmp_path):
    add(store, state="active", selected="1")
    dead = TorrentManager(
        store,
        Aria2RPC(url="http://127.0.0.1:1/jsonrpc", secret="x", timeout=0.5),
        download_dir=tmp_path,
    )
    # The durable rows still render; only the live numbers are missing.
    row = dead.snapshot()[0]
    assert row["infohash"] == HASH
    assert row["speed"] == 0
