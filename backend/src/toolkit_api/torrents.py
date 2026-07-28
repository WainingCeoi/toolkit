"""TorrentManager: stage a torrent in BitComet, choose its files, hand it over.

This tool is a DISPATCHER, not a download manager. It works out what is inside
a magnet or a .torrent, lets the user keep only the files worth keeping, sends
that decision to BitComet and starts the task. Everything after that -- pause,
resume, progress, removal, seeding -- belongs to BitComet, in BitComet's own
window.

Nothing is persisted here, and that is the point. BitComet already records
every task across its own restarts and is authoritative for all of it, so a
second copy of that state on this side could only drift from it -- and the
symptom of that drift is a screen confidently showing a download the user
deleted an hour ago. What BitComet knows is read from BitComet, every time.

The one piece of state this module keeps is in-memory and deliberately
process-scoped: the metadata deadline for a magnet somebody is watching right
now. A deadline that outlived the process would fire against a magnet nobody
is looking at any more.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from toolkit_engine.bitcomet import (
    DESELECTED,
    SELECTED,
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

# Default destination, shown in the UI in this tidy tilde form and passed on
# as-is; it is expanduser()-ed only where it meets the filesystem. The frontend
# mirrors this string.
DEFAULT_SAVE_DIR = "~/Downloads"

# A dead magnet never resolves and BitComet will wait for it indefinitely, so
# the deadline is ours to impose.
METADATA_TIMEOUT = 120.0

# BitComet's task_guid for a BitTorrent task is "bt_<infohash>", which is the
# only place the API hands back the infohash -- task_id alone is a meaningless
# integer like 1002. It is therefore the only way to ask "is this torrent
# already in BitComet?", which is the question every resolve starts with.
GUID_PREFIX = "bt_"

# The three answers a resolve can give. There is no "active"/"paused"/"complete"
# here any more: once a task is sent, its state is BitComet's business and this
# app deliberately stops having an opinion about it.
AWAITING_METADATA = "awaiting_metadata"
AWAITING_SELECTION = "awaiting_selection"
FAILED = "error"


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


@dataclass
class _Watch:
    """A magnet being waited on for metadata, right now, in this process.

    ``ours`` is a safety flag, not bookkeeping. A magnet must be added RUNNING
    or it never reaches the swarm to learn its own files, so the instant its
    metadata lands every file is switched off to stop it downloading the whole
    torrent while the user is still choosing. That mass-disable is only ever
    applied to a task this app created. BitComet is also full of the user's own
    downloads, and re-pasting a magnet they are already running must not switch
    every file off underneath them.
    """

    name: str | None
    started: float
    ours: bool


class TorrentManager:
    def __init__(
        self,
        client: BitCometClient,
        *,
        download_dir: str | Path,
    ) -> None:
        self.client = client
        self.download_dir = str(download_dir)
        self._watching: dict[str, _Watch] = {}

    # =======================================================
    # BITCOMET LOOKUPS
    # =======================================================
    def _task_for(self, infohash: str) -> dict | None:
        """BitComet's task for this infohash, matched on its "bt_<infohash>" guid.

        Looked up every time rather than remembered. BitComet re-mints task ids
        across a reinstall, and the user can delete a task in its own window at
        any moment, so a cached id is a guess -- and acting on a stale one means
        setting priorities on somebody else's download.

        Deliberately does NOT swallow a BitComet outage. A caller that read "no
        task" from an unreachable server would cheerfully add a duplicate.
        """
        for task in self.client.task_list():
            if infohash_of(task) == infohash:
                return task
        return None

    def _files(self, task_id: str) -> list[TorrentFile]:
        """The task's files, as BitComet itself lists them.

        BitComet's list is used even for a .torrent, whose files this app could
        just as well have read straight out of the bencode it already parsed.
        These are the indexes set_priority is about to be handed, so validating
        a selection against any other list is validating against the wrong one.

        Empty files are kept. They are real entries holding real index
        positions, and dropping them makes the review list disagree with
        BitComet's own numbering -- so the user cannot deselect what they
        cannot see, and a selection cannot be checked against it.
        """
        return [
            TorrentFile(
                index=int(entry["index"]),
                path=str(entry["name"]),
                size=int(entry["size"]),
            )
            for entry in self.client.files(task_id)
        ]

    def _files_if_ready(self, task_id: str) -> list[TorrentFile]:
        """_files, reading "cannot list them" as "metadata has not landed".

        A task still fetching metadata has no file list, and this endpoint does
        not distinguish that from a real failure -- so the deadline in
        _still_waiting, not this call, is what ends a hopeless wait.
        """
        try:
            return self._files(task_id)
        except BitCometError:
            return []

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
    # The destination is chosen here, not at send: BitComet fixes a task's save
    # folder when the task is created and publishes no way to move it.
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

    def _staged_payload(self, infohash: str, task: dict, name: str | None) -> dict:
        """The answer for a torrent already in BitComet, ready or not."""
        files = self._files_if_ready(task["task_id"])
        display = name or task.get("task_name") or None
        return self._payload(
            infohash, display, files, AWAITING_SELECTION if files else AWAITING_METADATA
        )

    def resolve_torrent(self, data: bytes, save_dir: str = "") -> dict:
        """Stage an uploaded .torrent in BitComet, stopped, and list its files.

        start_later leaves the task `stopped`, which is all a .torrent needs:
        its metadata is in the file, so the full list is readable from a task
        that has never touched the swarm, and nothing downloads until the user
        sends a selection. A MAGNET cannot be staged this way; see
        resolve_magnet.
        """
        info = parse_torrent(data)

        existing = self._task_for(info.infohash)
        if existing is not None:
            # Adding it again would mint a second task for one torrent, and
            # BitComet would then have two tasks writing the same files into
            # the same folder.
            return self._staged_payload(info.infohash, existing, info.name)

        folder = self._stage_folder(save_dir)
        added = self.client.add_torrent(data, folder, start_later=True)
        files = self._files_if_ready(added["task_id"])
        return self._payload(
            info.infohash,
            info.name,
            files,
            AWAITING_SELECTION if files else AWAITING_METADATA,
        )

    def resolve_magnet(self, uri: str, save_dir: str = "") -> dict:
        """Stage a magnet RUNNING -- the only way it can ever learn its files.

        A magnet carries no metadata, so BitComet has to reach the swarm to
        fetch it, and a task added with start_later=True is `stopped` and never
        reaches anything. There is no metadata-only mode: the task runs, and
        poll_resolve switches every file off the instant the list appears.
        """
        infohash, display = parse_magnet(uri)

        existing = self._task_for(infohash)
        if existing is not None:
            # Already in BitComet, and possibly the user's own download rather
            # than anything this app staged. Watch it for metadata so the
            # review card can fill in, but never claim it: ours=False keeps the
            # mass-disable in poll_resolve away from it.
            self._watching.setdefault(
                infohash, _Watch(name=display, started=time.monotonic(), ours=False)
            )
            return self._staged_payload(infohash, existing, display)

        folder = self._stage_folder(save_dir)
        self.client.add_magnets([uri], folder, start_later=False)
        # torrent_links/add returns no task id and answers before the task
        # exists, so there is nothing to look up yet -- poll_resolve finds it
        # by guid once BitComet has created it.
        self._watching[infohash] = _Watch(
            name=display, started=time.monotonic(), ours=True
        )
        return self._payload(infohash, display, [], AWAITING_METADATA)

    def poll_resolve(self, infohash: str) -> dict:
        """Has this magnet's metadata landed yet?"""
        watch = self._watching.get(infohash)
        task = self._task_for(infohash)

        # No task and nothing being watched: this app has never heard of it.
        if task is None and watch is None:
            raise KeyError(infohash)
        if task is None:
            # The add is asynchronous, so "not there yet" is normal for the
            # first second or two. It is also what a task the user deleted in
            # BitComet's own window looks like -- the deadline ends both.
            return self._still_waiting(infohash, watch)

        files = self._files_if_ready(task["task_id"])
        name = (watch.name if watch else None) or task.get("task_name") or None
        if not files:
            if watch is None:
                # Staged by some earlier process and still without metadata.
                # Start a deadline now rather than polling it forever.
                watch = self._watching.setdefault(
                    infohash, _Watch(name=name, started=time.monotonic(), ours=False)
                )
            return self._still_waiting(infohash, watch)

        if watch is not None and watch.ours:
            # THE MOMENT metadata lands, deselect everything. The task is
            # running -- it had to be, or the metadata would never have arrived
            # -- so from this instant it would otherwise be downloading the
            # whole torrent while the user is still deciding what they want.
            # BitComet has no "fetch metadata then pause" mode (start_later
            # just never starts), so disabling every file IS the pause: the
            # task stays in the swarm and moves no content until send()
            # re-enables what was ticked. Delete this and every magnet
            # downloads itself in full, unasked.
            self.client.set_priority(
                task["task_id"], [f.index for f in files], DESELECTED
            )

        # Metadata is in; there is nothing left to wait for. Dropping the watch
        # also makes a repeat poll harmless -- without it, a second poll after
        # send() would switch every file off again on a task that is running.
        self._watching.pop(infohash, None)
        return self._payload(infohash, name, files, AWAITING_SELECTION)

    def _still_waiting(self, infohash: str, watch: _Watch) -> dict:
        """Keep waiting, unless the deadline has passed -- then give up."""
        if time.monotonic() - watch.started <= METADATA_TIMEOUT:
            return self._payload(infohash, watch.name, [], AWAITING_METADATA)

        if watch.ours:
            # Ours, running, and with no metadata it has no file list to
            # disable -- so left alone it sits in BitComet fetching forever.
            # Only ever delete a task this app created.
            self.discard(infohash)
        self._watching.pop(infohash, None)
        return self._payload(infohash, watch.name, [], FAILED)

    # =======================================================
    # HANDOVER
    # =======================================================
    def send(self, infohash: str, selected: list[int]) -> dict:
        """Apply the tick list to BitComet and start the task. The handover.

        BOTH directions go out -- ticked files to SELECTED, unticked to
        DESELECTED -- because per-file priority is durable task state that
        outlives this call and this process. Sending only the deselections
        would mean a magnet, whose files are ALL disabled while it waits for
        the user, starts and downloads nothing whatsoever.

        Disabling runs first, so a failure part-way through can only ever leave
        a task that downloads too little, never too much.

        After this returns, the task is BitComet's. Nothing here tracks it.
        """
        task = self._task_for(infohash)
        if task is None:
            raise KeyError(infohash)

        task_id = task["task_id"]
        known = {f.index for f in self._files(task_id)}
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

        format_selection(selected)  # raises on an empty selection

        unwanted = sorted(known - chosen)
        if unwanted:
            self.client.set_priority(task_id, unwanted, DESELECTED)
        self.client.set_priority(task_id, sorted(chosen), SELECTED)
        self.client.action(task_id, "start")

        self._watching.pop(infohash, None)
        return {
            "infohash": infohash,
            "task_id": task_id,
            "name": task.get("task_name") or None,
        }

    def discard(self, infohash: str) -> None:
        """Drop a staged torrent that was never sent.

        This is not task management -- that is BitComet's now -- it is the
        other half of staging. A magnet is added RUNNING and stays that way
        until its metadata lands, so a torrent abandoned at the review step
        would otherwise keep going with every file enabled, and the page that
        started it would have no way to stop it.

        The downloaded data is kept: whatever a half-fetched magnet has already
        written is the user's to delete, in BitComet, deliberately.
        """
        task = self._task_for(infohash)
        if task is not None:
            self.client.delete(task["task_id"], delete_files=False)
        self._watching.pop(infohash, None)

    def close(self) -> None:
        """Release the HTTP session. BitComet keeps running; it is not ours.

        Downloads deliberately survive this process exiting -- that is the
        whole point of driving an application the user already runs.
        """
        self.client.close()
