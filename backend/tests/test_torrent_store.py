"""Torrent Downloader: the durable queue and its reconciliation with BitComet."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from fake_bitcomet import FakeBitComet

from toolkit_api.torrents import DEFAULT_SAVE_DIR, METADATA_TIMEOUT, TorrentManager
from toolkit_engine.bitcomet import STARTUP_TIMEOUT, BitCometClient
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


def test_set_selection_stores_the_compact_form(store):
    add(store)
    store.set_selection(HASH, "1,4,7")
    assert store.get(HASH)["selected"] == "1,4,7"


def test_task_id_is_persisted_as_a_string(store):
    add(store)
    assert store.get(HASH)["task_id"] is None

    # BitComet reports the id as an int inside a task object; storing that
    # verbatim would send an int back on the wire, which the API rejects.
    store.set_task_id(HASH, 1002)
    assert store.get(HASH)["task_id"] == "1002"


def test_an_older_database_gains_the_task_id_column(tmp_path):
    # The upgraded-in-place case: CREATE TABLE IF NOT EXISTS leaves an
    # existing table on its old shape, so without the migration every read of
    # task_id on a pre-BitComet torrents.db would raise OperationalError.
    path = tmp_path / "torrents.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE torrents ("
            "  infohash TEXT PRIMARY KEY, source TEXT NOT NULL,"
            "  source_kind TEXT NOT NULL, name TEXT, total_bytes INTEGER,"
            "  save_dir TEXT NOT NULL, selected TEXT, state TEXT NOT NULL,"
            "  pause_reason TEXT, added_at TEXT NOT NULL, completed_at TEXT,"
            "  last_error TEXT)"
        )
        conn.commit()

    store = TorrentStore(path)
    try:
        add(store)
        store.set_task_id(HASH, "7")
        assert store.get(HASH)["task_id"] == "7"
    finally:
        store.close()


def test_pause_reason_is_cleared_when_the_row_moves_on(store):
    add(store)
    store.set_state(HASH, "paused", pause_reason="user")
    assert store.get(HASH)["pause_reason"] == "user"

    store.set_state(HASH, "active")
    # A reason left behind would be reported by the dashboard on a row that is
    # running again.
    assert store.get(HASH)["pause_reason"] is None


def test_set_state_records_an_error_message(store):
    add(store)
    store.set_state(HASH, "error", last_error="no seeders")
    assert store.get(HASH)["last_error"] == "no seeders"


def test_completing_stamps_completed_at(store):
    add(store)
    store.set_state(HASH, "complete")
    assert store.get(HASH)["completed_at"] is not None


def test_tombstone_keeps_the_row_so_it_is_not_flagged(store):
    add(store)
    store.tombstone(HASH)
    assert store.get(HASH)["state"] == "removed"
    # Still present: deleting it outright would make it indistinguishable from
    # a task BitComet lost, and the next boot would report a phantom error.
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
def manager(store, fake, save_folder):
    client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    m = TorrentManager(store, client, download_dir=save_folder)
    try:
        yield m
    finally:
        m.close()


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


# What a magnet's swarm hands back once its task is running: the same two
# files sample_torrent() carries, so the two resolve paths stay comparable.
MAGNET_FILES = [("Movie.mkv", 2_000_000_000), ("Movie.chi.srt", 45_000)]


def test_resolving_a_torrent_file_is_ready_immediately(manager):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")

    # A .torrent is bencode: the file list is readable offline, with no round
    # trip to BitComet and no swarm to depend on.
    assert out["ready"] is True
    assert out["name"] == "Example.Release"
    assert [f["path"] for f in out["files"]] == ["Movie.mkv", "Movie.chi.srt"]
    assert [f["category"] for f in out["files"]] == ["video", "subtitle"]


def test_resolving_a_torrent_file_stages_it_stopped(manager, store, fake):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    row = store.get(out["infohash"])

    assert row["state"] == "awaiting_selection"
    assert row["source_kind"] == "torrent"
    # start_later is what makes review possible: not one byte may move before
    # the user has said which files they want.
    assert fake.tasks[row["task_id"]]["status"] == "stopped"


def test_resolving_a_magnet_is_not_ready_yet(manager):
    out = manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}&dn=Some.Name")

    assert out["ready"] is False
    assert out["infohash"] == HASH
    assert out["files"] == []


def test_a_magnet_is_added_running_or_it_can_never_fetch_metadata(manager, fake):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")

    (payload,) = [p for _m, path, p in fake.calls if path.endswith("links/add")]
    # THE defect this test exists for: start_later=True leaves the task
    # "stopped", a stopped task never contacts the swarm, and a magnet that
    # never contacts the swarm never learns its files -- so every magnet
    # staged that way sits empty until the metadata timeout kills it.
    assert payload["start_later"] is False
    assert [t["status"] for t in fake.tasks.values()] == ["running"]


def test_a_magnets_task_is_found_by_its_guid_since_the_add_returns_no_id(
    manager, store, fake
):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")

    # torrent_links/add answers "adding task in batch started." and nothing
    # else, so the id has to come from matching bt_<infohash> in the task list.
    (task_id,) = fake.tasks
    assert store.get(HASH)["task_id"] == task_id


def test_a_magnet_is_re_resolved_onto_its_existing_task(manager, fake):
    first = manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    again = manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")

    # Adding again would mint a second task and overwrite task_id with its id,
    # leaving the first one alive inside BitComet and unreachable from here.
    assert again == first
    assert len(fake.tasks) == 1
    assert sum(1 for _m, path, _p in fake.calls if path.endswith("links/add")) == 1


def test_a_torrent_is_re_resolved_onto_its_existing_task(manager, fake):
    first = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    again = manager.resolve_torrent(sample_torrent(), "Example.torrent")

    assert again["infohash"] == first["infohash"]
    assert len(fake.tasks) == 1


def test_a_re_resolve_re_adds_once_bitcomet_has_dropped_the_task(manager, fake):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    fake.tasks.clear()  # the user deleted it in BitComet's own window

    manager.resolve_torrent(sample_torrent(), "Example.torrent")

    # Reuse must not become "never add again": with no live task there is
    # nothing to strand, and the torrent has to be staged afresh.
    assert len(fake.tasks) == 1
    assert manager.task_id_for(out["infohash"]) in fake.tasks


def test_an_unregistered_destination_is_registered_before_the_add(manager, tmp_path):
    # BitComet whitelists save_folder and refuses anything else, with nothing
    # in the add request explaining why.
    folder = tmp_path / "Elsewhere"
    manager.resolve_torrent(sample_torrent(), "Example.torrent", str(folder))
    assert folder.is_dir()


def test_the_chosen_destination_is_stored_verbatim(manager, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent", "~/Movies")
    # Tidy form for the dashboard; expansion happens at the filesystem edge.
    assert store.get(out["infohash"])["save_dir"] == "~/Movies"


def test_resolve_falls_back_to_the_default_destination(store, fake, tmp_path):
    client = BitCometClient(
        base_url=fake.url, username=fake.username, password=fake.password
    )
    m = TorrentManager(store, client, download_dir=DEFAULT_SAVE_DIR)
    try:
        out = m.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
        assert store.get(out["infohash"])["save_dir"] == "~/Downloads"
    finally:
        m.close()


def test_poll_resolve_reports_ready_once_bitcomet_has_the_files(manager, fake):
    # The swarm has the metadata; the running task will pick it up.
    fake.publish_metadata(HASH, MAGNET_FILES)
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")

    polled = manager.poll_resolve(HASH)
    assert polled["ready"] is True
    assert [f["path"] for f in polled["files"]] == ["Movie.mkv", "Movie.chi.srt"]
    # 0-based on the wire, 1-based everywhere in this app.
    assert [f["index"] for f in polled["files"]] == [1, 2]


def test_metadata_landing_disables_every_file_until_the_user_chooses(manager, fake):
    fake.publish_metadata(HASH, MAGNET_FILES)
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")

    manager.poll_resolve(HASH)

    task = fake.tasks[manager.task_id_for(HASH)]
    # The task is RUNNING -- it had to be, or the metadata would never have
    # arrived -- so without this the torrent downloads itself in full while
    # the user is still deciding. Disabling every file IS the pause.
    assert {f["priority"] for f in task["files"]} == {"disabled"}
    assert task["status"] == "running"


def test_a_zero_byte_file_is_still_offered_for_review(manager, fake):
    fake.publish_metadata(HASH, [("Movie.mkv", 2_000_000_000), ("EMPTY.nfo", 0)])
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")

    polled = manager.poll_resolve(HASH)
    # An empty file is a real file of the torrent's, holding a real index.
    # Dropping it hid it from the review list and punched a hole in the index
    # space that the selection is validated against.
    assert [f["path"] for f in polled["files"]] == ["Movie.mkv", "EMPTY.nfo"]
    assert manager.store.files(HASH)[1].size == 0


def test_poll_resolve_gives_up_after_the_metadata_timeout(manager, store, fake):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    task_id = manager.task_id_for(HASH)
    # A dead swarm never resolves and BitComet waits forever, so the deadline
    # has to be ours.
    manager._resolve_started[HASH] = -METADATA_TIMEOUT - 1

    out = manager.poll_resolve(HASH)
    assert out["state"] == "error"
    assert "metadata" in store.get(HASH)["last_error"]
    # The abandoned task is dropped, never with its files: there are none, and
    # erasing on a timeout is not a risk worth taking.
    assert fake.deleted == [(task_id, False)]


# =======================================================
# COMMIT
# =======================================================
def test_commit_disables_the_unticked_files_then_starts(manager, fake, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    infohash = out["infohash"]

    manager.commit(infohash, [1])

    task = fake.tasks[manager.task_id_for(infohash)]
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled"]
    assert task["status"] == "running"
    assert store.get(infohash)["selected"] == "1"
    assert store.get(infohash)["state"] == "active"


def test_commit_deselects_before_it_starts(manager, fake):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1])

    paths = [path for _method, path, _payload in fake.calls]
    # A file stops downloading only once its priority is "disabled", so
    # starting first would fetch pieces of files the user unticked.
    assert paths.index("/api/task/files/set_priority") < paths.index(
        "/api_v2/tasks/action"
    )


def test_commit_re_enables_what_a_previous_commit_disabled(manager, fake):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    infohash = out["infohash"]
    task = fake.tasks[manager.task_id_for(infohash)]

    manager.commit(infohash, [1])
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled"]

    # Per-file priority is durable task state, so a commit that only ever
    # disabled files would be a one-way door: re-ticking one in the UI would
    # change nothing at all in BitComet.
    manager.commit(infohash, [1, 2])
    assert [f["priority"] for f in task["files"]] == ["normal", "normal"]


def test_commit_enables_the_ticked_files_of_a_magnet_it_had_disabled(manager, fake):
    fake.publish_metadata(HASH, MAGNET_FILES)
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    manager.poll_resolve(HASH)

    manager.commit(HASH, [1])

    # A magnet reaches commit with EVERY file disabled (that is how it was
    # held during review), so a commit that only disables starts a task with
    # nothing selected: it downloads zero bytes and calls itself finished.
    task = fake.tasks[manager.task_id_for(HASH)]
    assert [f["priority"] for f in task["files"]] == ["normal", "disabled"]
    assert task["status"] == "running"


def test_commit_rejects_a_file_the_torrent_does_not_have(manager, fake):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")

    # Unvalidated, index 99 falls into neither wanted nor unwanted, so every
    # real file is disabled and the task starts with selected_size 0.
    with pytest.raises(ValueError, match="no file 99"):
        manager.commit(out["infohash"], [1, 99])
    assert not any(path.endswith("set_priority") for _m, path, _p in fake.calls)


def test_commit_rejects_an_empty_selection(manager):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    with pytest.raises(ValueError, match="at least one file"):
        manager.commit(out["infohash"], [])


def test_commit_refuses_a_row_bitcomet_never_staged(manager, store):
    add(store, state="awaiting_selection")
    store.set_files(HASH, [TorrentFile(index=1, path="a.mkv", size=10)])

    with pytest.raises(ValueError, match="no longer staged"):
        manager.commit(HASH, [1])


# =======================================================
# RECONCILIATION
# =======================================================
def test_reconcile_relinks_rows_to_bitcomets_current_task_ids(manager, fake, store):
    add(store, state="active", selected="1")
    task_id = fake.add_task("Example", [("a.mkv", 10)], infohash=HASH)

    manager.reconcile()

    # Task ids are BitComet's to re-mint, so the link is rebuilt from the
    # task_guid ("bt_<infohash>") rather than trusted from our own record.
    assert manager.task_id_for(HASH) == task_id


def test_reconcile_finds_a_task_by_the_id_it_stored(manager, fake, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1])
    task_id = manager.task_id_for(out["infohash"])
    # Nothing guarantees a task's guid carries the infohash. Matched on the
    # guid alone this live, downloading task was invisible and its row was
    # flagged as lost.
    fake.tasks[task_id]["task_guid"] = "cloud_7f3a"

    manager.reconcile()

    assert store.get(out["infohash"])["state"] == "active"
    assert manager.task_id_for(out["infohash"]) == task_id


def test_reconcile_leaves_a_magnet_that_is_still_waiting_for_its_task(
    manager, fake, store
):
    manager.resolve_magnet(f"magnet:?xt=urn:btih:{HASH}")
    fake.tasks.clear()  # the asynchronous add has not produced a task yet

    manager.reconcile()

    # torrent_links/add is asynchronous, so "not in the task list yet" is the
    # normal state of a magnet mid-resolve, not a task BitComet lost.
    assert store.get(HASH)["state"] == "awaiting_metadata"


def test_reconcile_flags_a_task_bitcomet_no_longer_has(manager, store):
    add(store, state="active", selected="1")

    manager.reconcile()

    row = store.get(HASH)
    assert row["state"] == "error"
    assert "no longer has" in row["last_error"]


def test_reconcile_never_resumes_what_the_user_stopped(manager, fake, store):
    add(store, state="paused", selected="1")
    store.set_state(HASH, "paused", pause_reason="user")
    task_id = fake.add_task("Example", [("a.mkv", 10)], infohash=HASH)

    manager.reconcile()

    # This app does not stop BitComet, so a stopped task is the user's own
    # decision -- starting it every boot would silently overrule them.
    assert store.get(HASH)["state"] == "paused"
    assert fake.tasks[task_id]["status"] == "stopped"


def test_reconcile_leaves_settled_rows_alone(manager, store):
    add(store, infohash="a" * 40, state="complete")
    add(store, infohash="b" * 40, state="removed")

    manager.reconcile()

    assert store.get("a" * 40)["state"] == "complete"
    assert store.get("b" * 40)["state"] == "removed"


def test_reconcile_asks_under_a_short_deadline(manager, monkeypatch):
    seen: list[float] = []
    listing = manager.client.task_list

    def spy() -> list[dict]:
        seen.append(manager.client.timeout)
        return listing()

    monkeypatch.setattr(manager.client, "task_list", spy)

    manager.reconcile()

    # This runs before the app serves its first request, and a BitComet that is
    # wedged rather than absent accepts the connection and then never answers.
    assert seen == [STARTUP_TIMEOUT]
    # ...and the impatience is confined to boot: steady-state calls keep the
    # forgiving timeout.
    assert manager.client.timeout == 10.0


def test_reconcile_survives_bitcomet_being_down(store, tmp_path):
    add(store, state="active", selected="1")
    client = BitCometClient(
        base_url="http://127.0.0.1:1", username="u", password="p", timeout=0.5
    )
    dead = TorrentManager(store, client, download_dir=tmp_path)
    try:
        dead.reconcile()
    finally:
        dead.close()
    # "Unreachable" is not "deleted": flagging here would turn every boot with
    # BitComet closed into a queue full of errors.
    assert store.get(HASH)["state"] == "active"


# =======================================================
# CONTROLS
# =======================================================
def test_pause_and_resume_drive_stop_and_start(manager, fake, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    infohash = out["infohash"]
    manager.commit(infohash, [1])
    task_id = manager.task_id_for(infohash)

    manager.pause(infohash)
    assert fake.tasks[task_id]["status"] == "stopped"
    assert store.get(infohash)["pause_reason"] == "user"

    manager.resume(infohash)
    assert fake.tasks[task_id]["status"] == "running"
    assert store.get(infohash)["state"] == "active"


def test_pause_all_names_only_our_tasks_in_one_call(manager, fake, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1])
    theirs = fake.add_task("Someone else's", [("x.mkv", 5)], infohash="f" * 40)
    fake.tasks[theirs]["status"] = "running"

    manager.pause_all()

    # BitComet carries the user's own downloads; a blanket stop would sweep
    # them up, so only rows this app staged are named.
    assert fake.tasks[theirs]["status"] == "running"
    assert fake.tasks[manager.task_id_for(out["infohash"])]["status"] == "stopped"
    actions = [p for _m, path, p in fake.calls if path.endswith("tasks/action")]
    assert len([a for a in actions if a["action"] == "stop"]) == 1


def test_resume_all_restarts_every_paused_row(manager, fake, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    manager.commit(out["infohash"], [1])
    manager.pause_all()

    manager.resume_all()

    assert store.get(out["infohash"])["state"] == "active"
    assert fake.tasks[manager.task_id_for(out["infohash"])]["status"] == "running"


def test_remove_lets_bitcomet_delete_the_files(manager, fake, store):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    infohash = out["infohash"]
    task_id = manager.task_id_for(infohash)

    manager.remove(infohash, delete_files=True)

    # BitComet knows where the pieces actually landed, part files included --
    # a save_dir plus a file list cannot reconstruct that.
    assert fake.deleted == [(task_id, True)]
    assert store.get(infohash)["state"] == "removed"


def test_remove_keeps_the_files_by_default(manager, fake):
    out = manager.resolve_torrent(sample_torrent(), "Example.torrent")
    task_id = manager.task_id_for(out["infohash"])

    manager.remove(out["infohash"])
    assert fake.deleted == [(task_id, False)]


# =======================================================
# DASHBOARD
# =======================================================
def test_snapshot_uses_bitcomets_own_progress_and_eta(manager, fake, store):
    add(store, state="active", selected="1")
    task_id = fake.add_task("Example", [("a.mkv", 1000)], infohash=HASH)
    fake.tasks[task_id].update(permillage=250, download_rate=50, left_time="00:00:15")
    manager.reconcile()

    row = manager.snapshot()[0]
    # BitComet computes both; re-deriving them from bytes and speed would only
    # repeat its arithmetic, worse.
    assert row["progress"] == pytest.approx(25.0)
    assert row["speed"] == 50
    assert row["eta"] == "00:00:15"


def test_snapshot_reports_no_eta_when_bitcomet_offers_none(manager, fake, store):
    add(store, state="active", selected="1")
    fake.add_task("Example", [("a.mkv", 1000)], infohash=HASH)
    manager.reconcile()

    assert manager.snapshot()[0]["eta"] is None


def test_snapshot_measures_progress_against_the_selected_size(manager, fake, store):
    add(store, state="active", selected="1")
    task_id = fake.add_task(
        "Example", [("a.mkv", 1000), ("b.mkv", 9000)], infohash=HASH
    )
    fake.tasks[task_id]["files"][1]["priority"] = "disabled"
    fake.tasks[task_id]["files"][0]["downloaded_size"] = 400
    manager.reconcile()

    row = manager.snapshot()[0]
    # A denominator counting the deselected 9000 bytes would stick below 100%
    # forever on a torrent that is, as asked, finished.
    assert (row["total_bytes"], row["completed_bytes"]) == (1000, 400)


def test_snapshot_hides_tombstoned_rows(manager, store):
    add(store, state="active", selected="1")
    store.tombstone(HASH)
    assert manager.snapshot() == []


def test_snapshot_survives_bitcomet_being_down(store, tmp_path):
    add(store, state="active", selected="1")
    client = BitCometClient(
        base_url="http://127.0.0.1:1", username="u", password="p", timeout=0.5
    )
    dead = TorrentManager(store, client, download_dir=tmp_path)
    try:
        row = dead.snapshot()[0]
    finally:
        dead.close()
    # The durable rows still render; only the live numbers are missing.
    assert row["infohash"] == HASH
    assert row["speed"] == 0
