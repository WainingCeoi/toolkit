"""TorrentManager: stage a torrent in BitComet, choose its files, hand it over."""

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

# The frontend mirrors this string; expanduser() happens at the filesystem.
DEFAULT_SAVE_DIR = "~/Downloads"

# BitComet waits on a dead magnet forever, so the deadline is ours.
METADATA_TIMEOUT = 120.0

# task_guid is "bt_<infohash>", the only place BitComet's API exposes the infohash.
GUID_PREFIX = "bt_"

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
    """A magnet awaiting metadata; only tasks with ``ours`` get the mass-disable."""

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
        # Infohashes this app added to BitComet; only these may be deleted again.
        self._staged: set[str] = set()

    # --- BITCOMET LOOKUPS ---
    def _task_for(self, infohash: str) -> dict | None:
        """BitComet's task for this infohash; never cached, and an outage must raise."""
        for task in self.client.task_list():
            if infohash_of(task) == infohash:
                return task
        return None

    def _files(self, task_id: str) -> list[TorrentFile]:
        """The task's files, empty ones too, numbered as set_priority expects."""
        return [
            TorrentFile(
                index=int(entry["index"]),
                path=str(entry["name"]),
                size=int(entry["size"]),
            )
            for entry in self.client.files(task_id)
        ]

    def _files_if_ready(self, task_id: str) -> list[TorrentFile]:
        """_files, reading a listing failure as "metadata not landed yet"."""
        try:
            return self._files(task_id)
        except BitCometError:
            return []

    def _stage_folder(self, save_dir: str | None) -> str:
        """Register the folder with BitComet, which refuses an unregistered one."""
        wanted = (save_dir or "").strip()
        if not wanted and not self.client.is_local:
            # ~/Downloads names a folder on this Mac, not on a LAN peer.
            folders = self.client.save_folders()
            if not folders:
                raise BitCometError(
                    "That BitComet has no download folder configured. Add one "
                    "in its own settings, then choose it here."
                )
            wanted = folders[0]
        return self.client.ensure_save_folder(wanted or self.download_dir)

    # --- RESOLVE ---
    # The folder is fixed at resolve: BitComet cannot move a task once created.
    def _payload(
        self, infohash: str, name: str | None, files: list[TorrentFile], state: str
    ) -> dict:
        return {
            "infohash": infohash,
            "ready": bool(files),
            "name": name,
            "files": _as_file_dicts(files),
            "state": state,
        }

    def _staged_payload(self, infohash: str, task: dict, name: str | None) -> dict:
        files = self._files_if_ready(task["task_id"])
        display = name or task.get("task_name") or None
        return self._payload(
            infohash, display, files, AWAITING_SELECTION if files else AWAITING_METADATA
        )

    def resolve_torrent(self, data: bytes, save_dir: str = "") -> dict:
        """Stage an uploaded .torrent in BitComet, stopped, and list its files."""
        info = parse_torrent(data)

        existing = self._task_for(info.infohash)
        if existing is not None:
            # Adding it again would mint a second task writing the same files.
            return self._staged_payload(info.infohash, existing, info.name)

        folder = self._stage_folder(save_dir)
        added = self.client.add_torrent(data, folder, start_later=True)
        self._staged.add(info.infohash)
        # Nothing polls a .torrent card, so it must never answer awaiting_metadata.
        files = self._files_if_ready(added["task_id"]) or info.files
        return self._payload(
            info.infohash,
            info.name,
            files,
            AWAITING_SELECTION if files else AWAITING_METADATA,
        )

    def resolve_magnet(self, uri: str, save_dir: str = "") -> dict:
        """Stage a magnet RUNNING: a stopped task never fetches its metadata."""
        infohash, display = parse_magnet(uri)

        existing = self._task_for(infohash)
        if existing is not None:
            # May be the user's own download: ours=False keeps the mass-disable off it.
            self._watching.setdefault(
                infohash, _Watch(name=display, started=time.monotonic(), ours=False)
            )
            return self._staged_payload(infohash, existing, display)

        folder = self._stage_folder(save_dir)
        self.client.add_magnets([uri], folder, start_later=False)
        self._staged.add(infohash)
        # add_magnets returns no task id and answers before the task exists.
        self._watching[infohash] = _Watch(
            name=display, started=time.monotonic(), ours=True
        )
        return self._payload(infohash, display, [], AWAITING_METADATA)

    def poll_resolve(self, infohash: str) -> dict:
        watch = self._watching.get(infohash)
        task = self._task_for(infohash)

        if task is None and watch is None:
            raise KeyError(infohash)
        if task is None:
            # The add is asynchronous, so "not there yet" is normal at first.
            return self._still_waiting(infohash, watch)

        files = self._files_if_ready(task["task_id"])
        name = (watch.name if watch else None) or task.get("task_name") or None
        if not files:
            if watch is None:
                # Staged by an earlier process; give it a deadline, not forever.
                watch = self._watching.setdefault(
                    infohash, _Watch(name=name, started=time.monotonic(), ours=False)
                )
            return self._still_waiting(infohash, watch)

        if watch is not None and watch.ours:
            # Deselect at once: the task is running and would fetch the whole torrent.
            self.client.set_priority(
                task["task_id"], [f.index for f in files], DESELECTED
            )

        # Drop the watch, or a repeat poll after send() would disable files again.
        self._watching.pop(infohash, None)
        return self._payload(infohash, name, files, AWAITING_SELECTION)

    def _still_waiting(self, infohash: str, watch: _Watch) -> dict:
        if time.monotonic() - watch.started <= METADATA_TIMEOUT:
            return self._payload(infohash, watch.name, [], AWAITING_METADATA)

        if watch.ours:
            # Only a task this app created is deleted; left alone it fetches forever.
            self.discard(infohash)
        self._watching.pop(infohash, None)
        return self._payload(infohash, watch.name, [], FAILED)

    # --- HANDOVER ---
    def send(self, infohash: str, selected: list[int]) -> dict:
        """Apply the tick list to BitComet and start the task; both ways are sent."""
        task = self._task_for(infohash)
        if task is None:
            raise KeyError(infohash)

        task_id = task["task_id"]
        known = {f.index for f in self._files(task_id)}
        chosen = set(selected)

        # Unchecked, an unknown index would silently deselect every real file.
        unknown = sorted(chosen - known)
        if unknown:
            raise ValueError(
                f"this torrent has no file {', '.join(str(index) for index in unknown)}"
            )

        format_selection(selected)  # raises on an empty selection

        # Deselect first: a partial failure then downloads too little, never too much.
        unwanted = sorted(known - chosen)
        if unwanted:
            self.client.set_priority(task_id, unwanted, DESELECTED)
        self.client.set_priority(task_id, sorted(chosen), SELECTED)
        self.client.action(task_id, "start")

        self._watching.pop(infohash, None)
        self._staged.discard(infohash)
        return {
            "infohash": infohash,
            "task_id": task_id,
            "name": task.get("task_name") or None,
        }

    def discard(self, infohash: str) -> None:
        """Drop a staged torrent that was never sent, keeping any downloaded data."""
        self._watching.pop(infohash, None)
        if infohash not in self._staged:
            # BitComet already had this one: the user's own download, not ours.
            return
        task = self._task_for(infohash)
        if task is not None:
            self.client.delete(task["task_id"], delete_files=False)
        self._staged.discard(infohash)

    def close(self) -> None:
        """Release the HTTP session; BitComet and its downloads keep running."""
        self.client.close()
