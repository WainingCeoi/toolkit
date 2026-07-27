"""TorrentManager: the orchestration layer between SQLite and BitComet.

The split it enforces: SQLite holds what the user asked for and is
authoritative for THIS app; BitComet holds the piece data, the swarm and the
live numbers. The link between them is the BitComet task id, persisted beside
the infohash because BitComet outlives this process.

BitComet is the user's own desktop application. Nothing here starts it, adopts
it or stops it, and nothing here undoes a decision they made in BitComet's own
window -- the client is a client and no more.

Unlike every other tool here this one is not a batch job -- a torrent outlives
the request that created it, the backend process, and the host -- so it does
not use JobRegistry.
"""

from __future__ import annotations

import time
from pathlib import Path

from toolkit_engine.bitcomet import (
    DESELECTED,
    SELECTED,
    STARTUP_TIMEOUT,
    BitCometClient,
    BitCometError,
)
from toolkit_engine.filetypes import categorize
from toolkit_engine.torrent import (
    TorrentFile,
    format_selection,
    parse_magnet,
    parse_torrent,
)
from toolkit_engine.torrentdb import TorrentStore

# Default destination, shown in the UI in this tidy tilde form and stored as-is;
# it is expanduser()-ed only where it meets the filesystem. The frontend
# mirrors this string.
DEFAULT_SAVE_DIR = "~/Downloads"

# A dead magnet never resolves and BitComet will wait for it indefinitely, so
# the deadline is ours to impose.
METADATA_TIMEOUT = 120.0

# BitComet's task_guid for a BitTorrent task is "bt_<infohash>", which is the
# only place the API hands back the infohash -- task_id alone is a meaningless
# integer like 1002.
GUID_PREFIX = "bt_"

# States reconciliation must not flag as lost: finished, deliberately gone, or
# not yet staged for real work. "awaiting_metadata" belongs here because
# torrent_links/add is ASYNCHRONOUS -- the task can legitimately not be in
# task_list yet, and calling that row an error would kill a magnet that is at
# that moment doing exactly what it should.
SETTLED_STATES = frozenset(
    {
        "complete",
        "removed",
        "error",
        "awaiting_selection",
        "awaiting_metadata",
    }
)


def _as_file_dicts(files: list[TorrentFile]) -> list[dict]:
    return [
        {
            "index": f.index,
            "path": f.path,
            "size": f.size,
            "category": categorize(f.path),
        }
        for f in files
    ]


def infohash_of(task: dict) -> str:
    """The infohash a BitComet task carries, or "" if it is not a BT task."""
    guid = str(task.get("task_guid") or "")
    return guid[len(GUID_PREFIX) :].lower() if guid.startswith(GUID_PREFIX) else ""


class TorrentManager:
    def __init__(
        self,
        store: TorrentStore,
        client: BitCometClient,
        *,
        download_dir: str | Path,
    ) -> None:
        self.store = store
        self.client = client
        self.download_dir = str(download_dir)
        # Wall-clock start of each magnet's metadata fetch. Process-lifetime
        # only: a deadline that survived a restart would fire against a magnet
        # nobody is watching, and only a live poll ever reads it.
        self._resolve_started: dict[str, float] = {}

    def task_id_for(self, infohash: str) -> str | None:
        row = self.store.get(infohash)
        return row["task_id"] if row else None

    def _stage_folder(self, save_dir: str | None) -> str:
        """Register the destination with BitComet and return the real path.

        BitComet whitelists save_folder against its own configured directory
        list and refuses anything else, with nothing in the add request
        hinting at why -- so every folder is registered before it is used.
        """
        return self.client.ensure_save_folder(save_dir or self.download_dir)

    # =======================================================
    # RESOLVE
    # =======================================================
    # The destination is chosen here, not at commit: BitComet fixes a task's
    # save folder when the task is created and publishes no way to move it.
    def _payload(
        self, infohash: str, name: str | None, files: list[TorrentFile], state: str
    ) -> dict:
        """The shape every resolve/poll answer takes."""
        return {
            "infohash": infohash,
            "ready": bool(files),
            "name": name,
            "files": _as_file_dicts(files),
            "state": state,
        }

    def _already_staged(self, infohash: str) -> dict | None:
        """The payload for a torrent this app already staged, if it still lives.

        Adding the same infohash twice would mint a second BitComet task and
        overwrite task_id with its id, leaving the first task running inside
        BitComet with nothing in this app able to pause, start or delete it.
        A row whose task BitComet no longer has returns None -- then adding
        again is exactly the right move.
        """
        row = self.store.get(infohash)
        if row is None or row["state"] == "removed":
            return None
        task = self._live_task(infohash)
        if task is None:
            return None
        self.store.set_task_id(infohash, task["task_id"])
        return self._payload(
            infohash, row["name"], self.store.files(infohash), row["state"]
        )

    def _live_task(self, infohash: str) -> dict | None:
        """The BitComet task behind this row, by stored id then by guid."""
        task_id = self.task_id_for(infohash)
        try:
            tasks = self.client.task_list()
        except BitCometError:
            return None
        for task in tasks:
            if task["task_id"] == task_id or infohash_of(task) == infohash:
                return task
        return None

    def resolve_torrent(self, data: bytes, filename: str, save_dir: str = "") -> dict:
        """Parse an uploaded .torrent, then stage it in BitComet, stopped.

        The file list comes from the bencode, offline -- a .torrent carries it,
        so there is no reason to ask BitComet and wait. The add still happens
        now, because start_later leaves the task stopped and that stopped task
        is what the selection is applied to at commit. A magnet cannot be
        staged this way; see resolve_magnet.
        """
        info = parse_torrent(data)
        staged = self._already_staged(info.infohash)
        if staged is not None:
            return staged

        folder = self._stage_folder(save_dir)
        added = self.client.add_torrent(data, folder, start_later=True)

        self.store.upsert(
            infohash=info.infohash,
            source=filename,
            source_kind="torrent",
            name=info.name,
            total_bytes=info.total_bytes,
            save_dir=save_dir or self.download_dir,
            state="awaiting_selection",
        )
        self.store.set_files(info.infohash, info.files)
        self.store.set_task_id(info.infohash, added["task_id"])
        return self._payload(info.infohash, info.name, info.files, "awaiting_selection")

    def resolve_magnet(self, uri: str, save_dir: str = "") -> dict:
        """Stage a magnet RUNNING -- the only way it can ever learn its files.

        A magnet carries no metadata, so BitComet has to reach the swarm to
        fetch it, and a task added with start_later=True is `stopped` and never
        reaches anything. There is no metadata-only mode: the task runs, and
        poll_resolve disables every file the instant the list appears.
        """
        infohash, display = parse_magnet(uri)
        staged = self._already_staged(infohash)
        if staged is not None:
            # Keep the original deadline. Re-resolving must not hand a dead
            # magnet another 120 seconds every time the user hits the button.
            self._resolve_started.setdefault(infohash, time.monotonic())
            return staged

        folder = self._stage_folder(save_dir)
        self.client.add_magnets([uri], folder, start_later=False)

        self.store.upsert(
            infohash=infohash,
            source=uri,
            source_kind="magnet",
            name=display,
            total_bytes=None,
            save_dir=save_dir or self.download_dir,
            state="awaiting_metadata",
        )
        self._resolve_started[infohash] = time.monotonic()
        # torrent_links/add returns no task id and answers before the task
        # exists, so this first look usually finds nothing and poll_resolve
        # keeps trying.
        self._locate_task(infohash)
        return self._payload(infohash, display, [], "awaiting_metadata")

    def _locate_task(self, infohash: str) -> str | None:
        """Find and remember the task torrent_links/add created, by its guid.

        The add is asynchronous and hands back neither task_id nor task_ids,
        so a magnet's task can only be reached by matching "bt_<infohash>" in
        the task list -- and until that match is made and stored, nothing in
        this app can read the task's files or stop it.
        """
        task_id = self.task_id_for(infohash)
        if task_id is not None:
            return task_id
        try:
            tasks = self.client.task_list()
        except BitCometError:
            return None
        for task in tasks:
            if infohash_of(task) == infohash:
                self.store.set_task_id(infohash, task["task_id"])
                return task["task_id"]
        return None

    def poll_resolve(self, infohash: str) -> dict:
        """Check whether a magnet's metadata has landed yet."""
        row = self.store.get(infohash)
        if row is None:
            raise KeyError(infohash)

        if row["state"] != "awaiting_metadata":
            return self._payload(
                infohash, row["name"], self.store.files(infohash), row["state"]
            )

        # setdefault, not get: a get() default restarts the clock on every
        # poll, so a magnet resolved before a restart would never time out.
        started = self._resolve_started.setdefault(infohash, time.monotonic())
        if time.monotonic() - started > METADATA_TIMEOUT:
            self._abandon(infohash)
            return self._payload(infohash, row["name"], [], "error")

        task_id = self._locate_task(infohash)
        files = self._staged_files(task_id) if task_id is not None else []
        if not files:
            return self._payload(infohash, row["name"], [], "awaiting_metadata")

        # THE MOMENT metadata lands, deselect everything. The task is running
        # -- it had to be, or the metadata would never have arrived -- so from
        # this instant it would otherwise be downloading the whole torrent
        # while the user is still deciding what they want. BitComet has no
        # "fetch metadata then pause" mode (start_later just never starts), so
        # disabling every file IS the pause: the task stays in the swarm and
        # moves no content until commit re-enables what was ticked.
        # Deleting this makes every magnet download itself in full, unasked.
        self.client.set_priority(task_id, [f.index for f in files], DESELECTED)

        self.store.set_files(infohash, files)
        self.store.upsert(
            infohash=infohash,
            source=row["source"],
            source_kind=row["source_kind"],
            name=row["name"],
            total_bytes=sum(f.size for f in files),
            save_dir=row["save_dir"],
            state="awaiting_selection",
        )
        return self._payload(infohash, row["name"], files, "awaiting_selection")

    def _staged_files(self, task_id: str) -> list[TorrentFile]:
        """The staged task's file list, empty until metadata has landed.

        The client hands indexes back already translated to this repo's 1-based
        form, so nothing here does arithmetic on them.

        Empty files are kept. They are real entries in the torrent, they hold
        real index positions, and hiding them meant the review list disagreed
        with BitComet's own numbering -- so a selection could not be validated
        against it and the user could not deselect what they could not see.
        """
        try:
            entries = self.client.files(task_id)
        except BitCometError:
            return []

        return [
            TorrentFile(
                index=int(entry["index"]),
                path=str(entry["name"]),
                size=int(entry["size"]),
            )
            for entry in entries
        ]

    def _abandon(self, infohash: str) -> None:
        task_id = self.task_id_for(infohash)
        if task_id is not None:
            try:
                # delete_files stays off: the task never got past metadata, so
                # there is nothing of the user's to erase and no reason to risk
                # it if BitComet disagrees about which task this is.
                self.client.delete(task_id, delete_files=False)
            except BitCometError:
                pass
        self.store.set_state(
            infohash,
            "error",
            last_error=(
                f"could not fetch metadata within {int(METADATA_TIMEOUT)}s - "
                "the magnet may be dead or have no seeders"
            ),
        )

    # =======================================================
    # COMMIT
    # =======================================================
    def commit(self, infohash: str, selected: list[int]) -> None:
        """Apply the whole tick list to BitComet, then start the task.

        BOTH directions go out every time -- ticked files to SELECTED, unticked
        to DESELECTED -- because per-file priority is durable task state that
        outlives this call, this process and this app. Sending only the
        deselections would make commit a one-way door: re-ticking a file in the
        UI would change nothing in BitComet, a magnet (whose files are all
        disabled while it waits for the user) would start and download nothing
        at all, and a commit interrupted between set_priority and start would
        leave the store and BitComet disagreeing forever.

        Disabling runs first so that a failure part-way through can only ever
        leave a task that downloads too little, never too much.
        """
        row = self.store.get(infohash)
        if row is None:
            raise KeyError(infohash)

        known = {f.index for f in self.store.files(infohash)}
        chosen = set(selected)
        # An index this torrent does not have is the caller's mistake, and an
        # invisible one: unchecked it lands in neither set, so every real file
        # is deselected and the task starts with nothing to download and calls
        # itself finished.
        unknown = sorted(chosen - known)
        if unknown:
            raise ValueError(
                f"this torrent has no file {', '.join(str(index) for index in unknown)}"
            )

        value = format_selection(selected)  # raises on an empty selection
        task_id = row["task_id"]
        if task_id is None:
            raise ValueError("this torrent is no longer staged in BitComet")

        unwanted = sorted(known - chosen)
        if unwanted:
            self.client.set_priority(task_id, unwanted, DESELECTED)
        self.client.set_priority(task_id, sorted(chosen), SELECTED)
        self.client.action(task_id, "start")

        self.store.set_selection(infohash, value)
        self.store.set_state(infohash, "active")

    # =======================================================
    # RECONCILIATION
    # =======================================================
    def reconcile(self) -> None:
        """Re-link our rows to BitComet's tasks, and flag the ones it lost.

        Runs at boot, when the two can disagree: BitComet re-mints task ids
        across a reinstall, and the user may have deleted a task in its own
        window.

        Nothing is auto-resumed here. This app no longer stops BitComet, so a
        stopped task is a decision -- the user's, made in BitComet -- and
        starting it again on every boot would silently overrule them.
        """
        try:
            # Short deadline: this runs before the app serves its first
            # request, and a BitComet that is wedged rather than absent
            # accepts the connection and then never replies.
            with self.client.deadline(STARTUP_TIMEOUT):
                tasks = self.client.task_list()
        except BitCometError:
            return  # BitComet down; the UI surfaces this through /status

        by_id = {task["task_id"]: task for task in tasks}
        by_infohash: dict[str, dict] = {}
        for task in tasks:
            infohash = infohash_of(task)
            if infohash:
                by_infohash[infohash] = task

        for row in self.store.all():
            # The stored task_id is the primary handle, the guid only a
            # fallback: task_guid is the sole place the API mentions an
            # infohash, but nothing guarantees a task carries one, and matching
            # on it alone made every other task invisible here -- their rows
            # were flagged as lost while the tasks were alive and downloading.
            task = by_id.get(row["task_id"]) or by_infohash.get(row["infohash"])
            if task is not None:
                if task["task_id"] != row["task_id"]:
                    self.store.set_task_id(row["infohash"], task["task_id"])
                continue
            if row["state"] in SETTLED_STATES:
                continue
            self._forget(row)

    def _forget(self, row: dict) -> None:
        """Flag a row whose BitComet task is gone, rather than re-adding it.

        BitComet keeps its task list across restarts, so a missing task was
        deleted deliberately. Re-adding would resurrect that download AND
        re-fetch every file the user deselected, because the selection lives
        in the task's per-file priorities and a fresh add starts with all of
        them enabled.
        """
        self.store.set_state(
            row["infohash"],
            "error",
            last_error="BitComet no longer has this task - add it again to restart it",
        )

    # =======================================================
    # CONTROLS
    # =======================================================
    def pause(self, infohash: str) -> None:
        self._action([infohash], "stop")
        self.store.set_state(infohash, "paused", pause_reason="user")

    def resume(self, infohash: str) -> None:
        self._action([infohash], "start")
        self.store.set_state(infohash, "active")

    def pause_all(self) -> None:
        """Stop every running torrent at once ("Stop all")."""
        rows = [
            row
            for row in self.store.all(include_removed=False)
            if row["state"] in {"active", "queued"}
        ]
        self._action([row["infohash"] for row in rows], "stop")
        # The DB is the authoritative record, so it is updated per row whether
        # or not BitComet answered.
        for row in rows:
            self.store.set_state(row["infohash"], "paused", pause_reason="user")

    def resume_all(self) -> None:
        """Resume every paused torrent at once ("Resume all")."""
        rows = [
            row
            for row in self.store.all(include_removed=False)
            if row["state"] == "paused"
        ]
        self._action([row["infohash"] for row in rows], "start")
        for row in rows:
            self.store.set_state(row["infohash"], "active")

    def _action(self, infohashes: list[str], verb: str) -> None:
        """Start or stop our tasks in ONE call, skipping unstaged rows.

        Only rows this app staged are named. BitComet is carrying the user's
        own downloads too, and a blanket start/stop would sweep those up.
        """
        task_ids = [
            task_id
            for task_id in (self.task_id_for(infohash) for infohash in infohashes)
            if task_id is not None
        ]
        if not task_ids:
            return
        try:
            self.client.action(task_ids, verb)
        except BitCometError:
            pass  # the DB state is what the dashboard renders

    def remove(self, infohash: str, *, delete_files: bool = False) -> None:
        """Drop the task, optionally erasing what it downloaded.

        BitComet does the deleting: it knows where the pieces actually landed,
        including the part files a half-finished torrent leaves behind, which
        a save_dir plus a file list cannot reconstruct.
        """
        task_id = self.task_id_for(infohash)
        if task_id is not None:
            try:
                self.client.delete(task_id, delete_files=delete_files)
            except BitCometError:
                pass
        self.store.tombstone(infohash)

    # =======================================================
    # DASHBOARD
    # =======================================================
    def snapshot(self) -> list[dict]:
        """Durable rows joined with BitComet's live numbers. Nothing is cached.

        Progress, speed and ETA are read through on every call rather than
        stored: persisting them goes stale the moment the backend restarts
        mid-download.
        """
        try:
            live = {infohash_of(t): t for t in self.client.task_list()}
        except BitCometError:
            live = {}

        rows = []
        for row in self.store.all(include_removed=False):
            task = live.get(row["infohash"], {})
            # The SELECTED sizes, not the torrent's totals: the user deselected
            # files, and a denominator counting them would stick below 100%.
            total = int(task.get("selected_size") or row["total_bytes"] or 0)
            done = int(task.get("selected_downloaded_size") or 0)
            rows.append(
                {
                    "infohash": row["infohash"],
                    "name": row["name"],
                    "state": row["state"],
                    "pause_reason": row["pause_reason"],
                    "save_dir": row["save_dir"],
                    "selected": row["selected"],
                    "total_bytes": total,
                    "completed_bytes": done,
                    # permillage is progress in tenths of a percent, and it is
                    # BitComet's own answer over the selected files -- deriving
                    # it from the byte counts would only re-do its arithmetic.
                    "progress": int(task.get("permillage") or 0) / 10,
                    "speed": int(task.get("download_rate") or 0),
                    # BitComet formats the remaining time itself, so this is
                    # its string, not seconds we guessed from bytes and speed.
                    "eta": task.get("left_time") or None,
                    "added_at": row["added_at"],
                    "completed_at": row["completed_at"],
                    "last_error": row["last_error"],
                }
            )
        return rows

    def close(self) -> None:
        """Release the HTTP session. BitComet keeps running; it is not ours.

        Downloads deliberately survive this process exiting -- that is the
        whole point of driving an application the user already runs.
        """
        self.client.close()
