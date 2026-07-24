"""TorrentManager: the orchestration layer between SQLite and aria2.

The split it enforces: SQLite holds what the user asked for and is
authoritative; aria2 holds piece data and live speeds and is treated as a
cache that can vanish. The infohash -> gid map is process-lifetime only,
because aria2 re-mints a content gid whenever metadata re-resolves.

Unlike every other tool here this one is not a batch job -- a torrent outlives
the request that created it, the backend process, and the host -- so it does
not use JobRegistry.
"""

from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

from toolkit_engine import aria2
from toolkit_engine.aria2 import Aria2Error, Aria2RPC
from toolkit_engine.filetypes import categorize
from toolkit_engine.torrent import (
    TorrentFile,
    format_selection,
    parse_magnet,
    parse_torrent,
)
from toolkit_engine.torrentdb import TorrentStore

# Default destination, shown in the UI in this tidy tilde form and stored as-is;
# it is expanduser()-ed only where it meets the filesystem (aria2's dir option,
# file deletion). The frontend mirrors this string.
DEFAULT_SAVE_DIR = "~/Downloads"

# A dead magnet stalls forever in aria2, which offers no cancel, so the
# deadline is ours to impose.
METADATA_TIMEOUT = 120.0

# States that mean "work is outstanding" for reconciliation and the UI.
ACTIVE_STATES = frozenset({"active", "queued", "awaiting_metadata"})


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


class TorrentManager:
    # How long the daemon keeps running after the last dashboard closes. A
    # refresh, a route change, and a dropped wifi link all look identical to a
    # departure, so shutdown is driven by sustained absence, not one event.
    GRACE_SECONDS = 45.0

    def __init__(
        self,
        store: TorrentStore,
        rpc: Aria2RPC,
        *,
        download_dir: Path,
        owned: bool = False,
        pid_file: Path | None = None,
    ) -> None:
        self.store = store
        self.rpc = rpc
        self.download_dir = Path(download_dir)
        self.owned = owned
        # Set only for a daemon we own, so shutdown can guarantee the process
        # is gone even if the graceful RPC did not land -- otherwise an orphan
        # would keep the port and the next boot would have to adopt it.
        self.pid_file = pid_file
        self._gids: dict[str, str] = {}
        self._resolve_started: dict[str, float] = {}
        # Uploaded .torrent bytes held until commit. Not persisted: an upload
        # the user never committed is not state worth surviving a restart.
        self._pending_torrent_data: dict[str, bytes] = {}
        self._clients = 0
        self._grace_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def gid_for(self, infohash: str) -> str | None:
        with self._lock:
            return self._gids.get(infohash)

    def _set_gid(self, infohash: str, gid: str) -> None:
        with self._lock:
            self._gids[infohash] = gid

    # =======================================================
    # RESOLVE
    # =======================================================
    def resolve_torrent(self, data: bytes, filename: str) -> dict:
        """Parse an uploaded .torrent. Offline: no daemon, no swarm."""
        info = parse_torrent(data)
        self.store.upsert(
            infohash=info.infohash,
            source=filename,
            source_kind="torrent",
            name=info.name,
            total_bytes=info.total_bytes,
            save_dir=str(self.download_dir),
            state="awaiting_selection",
        )
        self.store.set_files(info.infohash, info.files)
        self._pending_torrent_data[info.infohash] = data
        return {
            "infohash": info.infohash,
            "ready": True,
            "name": info.name,
            "files": _as_file_dicts(info.files),
            "state": "awaiting_selection",
        }

    def resolve_magnet(self, uri: str) -> dict:
        """Add a magnet with pause-metadata so its files can be listed."""
        infohash, display = parse_magnet(uri)
        self.store.upsert(
            infohash=infohash,
            source=uri,
            source_kind="magnet",
            name=display,
            total_bytes=None,
            save_dir=str(self.download_dir),
            state="awaiting_metadata",
        )
        gid = self.rpc.add_uri([uri], {"pause-metadata": "true"})
        self._set_gid(infohash, gid)
        self._resolve_started[infohash] = time.monotonic()
        return {
            "infohash": infohash,
            "ready": False,
            "name": display,
            "files": [],
            "state": "awaiting_metadata",
        }

    def poll_resolve(self, infohash: str) -> dict:
        """Check whether a magnet's metadata has landed yet."""
        row = self.store.get(infohash)
        if row is None:
            raise KeyError(infohash)

        if row["state"] != "awaiting_metadata":
            files = self.store.files(infohash)
            return {
                "infohash": infohash,
                "ready": bool(files),
                "name": row["name"],
                "files": _as_file_dicts(files),
                "state": row["state"],
            }

        started = self._resolve_started.get(infohash, time.monotonic())
        if time.monotonic() - started > METADATA_TIMEOUT:
            self._abandon(infohash)
            return {
                "infohash": infohash,
                "ready": False,
                "name": row["name"],
                "files": [],
                "state": "error",
            }

        files = self._daemon_files(infohash)
        if not files:
            return {
                "infohash": infohash,
                "ready": False,
                "name": row["name"],
                "files": [],
                "state": "awaiting_metadata",
            }

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
        return {
            "infohash": infohash,
            "ready": True,
            "name": row["name"],
            "files": _as_file_dicts(files),
            "state": "awaiting_selection",
        }

    def _daemon_files(self, infohash: str) -> list[TorrentFile]:
        """Read the content group's file list, following the metadata group.

        A magnet starts as a metadata group; once ut_metadata completes aria2
        creates a SEPARATE content group and links it via followedBy. Only the
        content group has the real file list.
        """
        gid = self.gid_for(infohash)
        if gid is None:
            return []
        try:
            status = self.rpc.tell_status(gid)
            followed = status.get("followedBy") or []
            if followed:
                gid = followed[0]
                self._set_gid(infohash, gid)
            raw = self.rpc.get_files(gid)
        except Aria2Error:
            return []

        return [
            TorrentFile(
                index=int(entry["index"]),
                path=entry["path"].rstrip("/").split("/")[-1],
                size=int(entry["length"]),
            )
            for entry in raw
            if int(entry.get("length", 0)) > 0
        ]

    def _abandon(self, infohash: str) -> None:
        gid = self.gid_for(infohash)
        if gid is not None:
            try:
                self.rpc.remove(gid)
            except Aria2Error:
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
    def commit(self, infohash: str, selected: list[int], save_dir: str) -> None:
        """Apply the selection while paused, then start the download.

        The ordering is the point: changing select-file on an ACTIVE download
        force-restarts it, and if the group is already halting the option is
        discarded with no error while the RPC still returns OK.
        """
        row = self.store.get(infohash)
        if row is None:
            raise KeyError(infohash)

        value = format_selection(selected)  # raises on an empty selection
        # aria2 gets no shell, so "~/Downloads" would become a literal ./~ dir;
        # expand here, at the filesystem boundary.
        options = {
            "select-file": value,
            "dir": str(Path(save_dir).expanduser()),
            "pause": "true",
        }

        if row["source_kind"] == "torrent":
            data = self._pending_torrent_data.get(infohash)
            if data is None:
                raise ValueError("the uploaded .torrent is no longer available")
            gid = self.rpc.add_torrent(base64.b64encode(data).decode("ascii"), options)
        else:
            gid = self.gid_for(infohash)
            if gid is None:
                gid = self.rpc.add_uri([row["source"]], options)
            else:
                self.rpc.call("aria2.changeOption", gid, {"select-file": value})

        self._set_gid(infohash, gid)
        self.store.set_selection(infohash, value)
        # Persist the tidy form the user chose so the dashboard, reconciliation,
        # and file deletion all agree with it.
        self.store.set_save_dir(infohash, save_dir)
        self.rpc.unpause(gid)
        self.store.set_state(infohash, "active")

    # =======================================================
    # RECONCILIATION
    # =======================================================
    def reconcile(self) -> None:
        """Rebuild the gid map and make the daemon agree with our record.

        Runs at boot, when SQLite and the daemon's session can disagree: the
        session may be older than the DB, or gone entirely to a kill -9.
        """
        try:
            live = self.rpc.tell_all()
        except Aria2Error:
            return  # daemon down; the UI surfaces this through /status

        by_hash: dict[str, dict] = {}
        for entry in live:
            infohash = (entry.get("infoHash") or "").lower()
            if not infohash:
                continue
            # A metadata group carries followedBy; the content group it points
            # at is the one with the real files. Prefer the content group.
            if infohash in by_hash and entry.get("followedBy"):
                continue
            by_hash[infohash] = entry

        for infohash, entry in by_hash.items():
            followed = entry.get("followedBy") or []
            self._set_gid(infohash, followed[0] if followed else entry["gid"])

        for row in self.store.all():
            if row["infohash"] in by_hash:
                continue
            if row["state"] in {"complete", "removed", "error", "awaiting_selection"}:
                continue
            self._readd(row)

        for row in self.store.paused_by_shutdown():
            self.resume(row["infohash"])

    def _readd(self, row: dict) -> None:
        """Re-add a torrent the daemon forgot, re-asserting OUR selection."""
        if not row["selected"]:
            return
        options = {
            "select-file": row["selected"],
            "dir": str(Path(row["save_dir"]).expanduser()),
            "pause": "true",
        }
        try:
            gid = self.rpc.add_uri([row["source"]], options)
        except Aria2Error:
            return
        self._set_gid(row["infohash"], gid)

    # =======================================================
    # CONTROLS
    # =======================================================
    def pause(self, infohash: str) -> None:
        gid = self.gid_for(infohash)
        if gid is not None:
            try:
                self.rpc.pause(gid)
            except Aria2Error:
                pass
        self.store.set_state(infohash, "paused", pause_reason="user")

    def resume(self, infohash: str) -> None:
        gid = self.gid_for(infohash)
        if gid is not None:
            try:
                self.rpc.unpause(gid)
            except Aria2Error:
                pass
        self.store.set_state(infohash, "active")

    def remove(self, infohash: str, *, delete_files: bool = False) -> None:
        gid = self.gid_for(infohash)
        if gid is not None:
            try:
                self.rpc.remove(gid)
            except Aria2Error:
                pass
        if delete_files:
            self._delete_files(infohash)
        self.store.tombstone(infohash)

    def _delete_files(self, infohash: str) -> None:
        row = self.store.get(infohash)
        if row is None:
            return
        base = Path(row["save_dir"]).expanduser()
        for entry in self.store.files(infohash):
            try:
                (base / entry.path).unlink(missing_ok=True)
            except OSError:
                pass

    # =======================================================
    # DASHBOARD
    # =======================================================
    def snapshot(self) -> list[dict]:
        """Durable rows joined with live daemon numbers. Nothing is cached.

        Progress, speed and ETA are read through on every call rather than
        stored: persisting them goes stale the moment the backend restarts
        mid-download.
        """
        try:
            live = {(d.get("infoHash") or "").lower(): d for d in self.rpc.tell_all()}
        except Aria2Error:
            live = {}

        rows = []
        for row in self.store.all(include_removed=False):
            entry = live.get(row["infohash"], {})
            total = int(entry.get("totalLength") or row["total_bytes"] or 0)
            done = int(entry.get("completedLength") or 0)
            speed = int(entry.get("downloadSpeed") or 0)
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
                    "progress": (done / total * 100) if total else 0.0,
                    "speed": speed,
                    # aria2 returns no ETA; a stalled download has none.
                    "eta_seconds": ((total - done) // speed) if speed else None,
                    "added_at": row["added_at"],
                    "completed_at": row["completed_at"],
                    "last_error": row["last_error"],
                }
            )
        return rows

    # =======================================================
    # PRESENCE / SHUTDOWN
    # =======================================================
    def _cancel_timer_locked(self) -> None:
        """Cancel a pending grace timer. Caller must hold self._lock."""
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None

    def client_connected(self) -> None:
        with self._lock:
            self._clients += 1
            self._cancel_timer_locked()

    def client_disconnected(self) -> None:
        with self._lock:
            self._clients = max(0, self._clients - 1)
            if self._clients > 0:
                return
            timer = threading.Timer(self.GRACE_SECONDS, self.shutdown)
            timer.daemon = True
            self._grace_timer = timer
            timer.start()

    def shutdown_pending(self) -> bool:
        with self._lock:
            return self._grace_timer is not None

    def cancel_pending_shutdown(self) -> None:
        """Disarm the grace timer without shutting down.

        Used on disposal (and in tests) so a timer can never fire against a
        torn-down manager -- a closed store or a stopped daemon -- which would
        surface as a stray exception in a background thread.
        """
        with self._lock:
            self._cancel_timer_locked()

    def shutdown(self) -> None:
        """Pause our work, flush the session, and stop a daemon we own."""
        with self._lock:
            self._cancel_timer_locked()
            owned_gids = dict(self._gids)

        try:
            if self.owned:
                self.rpc.call("aria2.forcePauseAll")
            else:
                # Another front-end's downloads are not ours to touch.
                for infohash, gid in owned_gids.items():
                    row = self.store.get(infohash)
                    if row and row["state"] == "active":
                        self.rpc.pause(gid)

            for row in self.store.all(include_removed=False):
                if row["state"] == "active":
                    self.store.set_state(
                        row["infohash"], "paused", pause_reason="shutdown"
                    )

            self.rpc.save_session()
            if self.owned:
                self.rpc.shutdown()
        except Aria2Error:
            pass  # daemon already gone; the DB state is what matters

        # Guarantee an owned daemon is actually gone, even if the RPC above
        # never landed, so it cannot linger holding the port.
        if self.owned and self.pid_file is not None:
            aria2.stop_process(self.pid_file)
