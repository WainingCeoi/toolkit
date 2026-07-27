"""SQLite persistence for the torrent queue.

BitComet is the user's own application: they can delete a task, reinstall, or
never have seen a torrent this app staged. This table -- not BitComet -- is
the source of truth for what the user asked for HERE. BitComet holds the piece
data and the live numbers; this holds the intent.

Connection-per-call and the :memory: keepalive trick mirror subgen.db.Store.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from toolkit_engine.torrent import TorrentFile

SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
  infohash     TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  source_kind  TEXT NOT NULL,
  name         TEXT,
  total_bytes  INTEGER,
  save_dir     TEXT NOT NULL,
  selected     TEXT,
  state        TEXT NOT NULL,
  pause_reason TEXT,
  task_id      TEXT,
  added_at     TEXT NOT NULL,
  completed_at TEXT,
  last_error   TEXT
);
CREATE TABLE IF NOT EXISTS torrent_files (
  infohash TEXT NOT NULL,
  idx      INTEGER NOT NULL,
  path     TEXT NOT NULL,
  length   INTEGER NOT NULL,
  PRIMARY KEY (infohash, idx)
);
"""

# Columns added after a database was first written. This app is installed and
# upgraded in place, so an existing torrents.db has to be widened rather than
# recreated -- CREATE TABLE IF NOT EXISTS alone would leave it on the old
# shape and every read of the new column would raise OperationalError.
MIGRATIONS = {
    # BitComet's own task id, so a row can be paused, started or deleted
    # without first listing every task to find it again.
    "task_id": "ALTER TABLE torrents ADD COLUMN task_id TEXT",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(torrents)")}
    for column, statement in MIGRATIONS.items():
        if column not in have:
            conn.execute(statement)


class TorrentStore:
    """SQLite-backed queue; opens a fresh connection per call (thread-safe)."""

    def __init__(self, path) -> None:
        self.path = str(path)
        self._memory = self.path == ":memory:"
        self._keepalive: sqlite3.Connection | None = None
        if self._memory:
            self._uri = f"file:torrents_mem_{id(self)}?mode=memory&cache=shared"
            # Hold one connection open so the shared-cache DB persists.
            self._keepalive = sqlite3.connect(
                self._uri, uri=True, check_same_thread=False
            )
        else:
            self._uri = None
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        if self._memory:
            conn = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
        else:
            conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:
        """Release the in-memory keepalive connection (no-op for file stores)."""
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    # --- writes -----------------------------------------------------------
    def upsert(
        self,
        *,
        infohash: str,
        source: str,
        source_kind: str,
        name: str | None,
        total_bytes: int | None,
        save_dir: str,
        state: str,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO torrents (infohash, source, source_kind, name,
                                      total_bytes, save_dir, state, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(infohash) DO UPDATE SET
                  source=excluded.source, source_kind=excluded.source_kind,
                  name=excluded.name, total_bytes=excluded.total_bytes,
                  save_dir=excluded.save_dir, state=excluded.state
                """,
                (
                    infohash,
                    source,
                    source_kind,
                    name,
                    total_bytes,
                    save_dir,
                    state,
                    _now(),
                ),
            )
            conn.commit()

    def set_files(self, infohash: str, files: list[TorrentFile]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM torrent_files WHERE infohash = ?", (infohash,))
            conn.executemany(
                "INSERT INTO torrent_files (infohash, idx, path, length) "
                "VALUES (?, ?, ?, ?)",
                [(infohash, f.index, f.path, f.size) for f in files],
            )
            conn.commit()

    def set_selection(self, infohash: str, selected: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE torrents SET selected = ? WHERE infohash = ?",
                (selected, infohash),
            )
            conn.commit()

    def set_task_id(self, infohash: str, task_id: str) -> None:
        """Remember which BitComet task this torrent became.

        Stored rather than held in memory: BitComet outlives this process, so
        after a restart the queue would otherwise have no handle on the tasks
        it staged and every control would be a no-op.
        """
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE torrents SET task_id = ? WHERE infohash = ?",
                (str(task_id), infohash),
            )
            conn.commit()

    def set_state(
        self,
        infohash: str,
        state: str,
        *,
        pause_reason: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """Set state, always rewriting pause_reason.

        pause_reason qualifies the state it was stored with, so it is
        overwritten rather than merged: a reason left behind from an earlier
        pause would sit there on a row that is running again, explaining a
        pause that is over. Every dashboard row carries the field, so a stale
        value is published, not merely kept.
        """
        completed = _now() if state == "complete" else None
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE torrents
                   SET state = ?,
                       pause_reason = ?,
                       last_error = COALESCE(?, last_error),
                       completed_at = COALESCE(?, completed_at)
                 WHERE infohash = ?
                """,
                (state, pause_reason, last_error, completed, infohash),
            )
            conn.commit()

    def tombstone(self, infohash: str) -> None:
        """Mark removed but keep the row, so reconciliation ignores it.

        Deleting outright would make the row indistinguishable from one
        BitComet lost, and the next boot would report it as an error the user
        never caused.
        """
        self.set_state(infohash, "removed")

    # --- reads ------------------------------------------------------------
    def get(self, infohash: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM torrents WHERE infohash = ?", (infohash,)
            ).fetchone()
        return dict(row) if row else None

    def all(self, *, include_removed: bool = True) -> list[dict]:
        sql = "SELECT * FROM torrents"
        if not include_removed:
            sql += " WHERE state != 'removed'"
        sql += " ORDER BY added_at, infohash"
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def files(self, infohash: str) -> list[TorrentFile]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT idx, path, length FROM torrent_files "
                "WHERE infohash = ? ORDER BY idx",
                (infohash,),
            ).fetchall()
        return [
            TorrentFile(index=r["idx"], path=r["path"], size=r["length"]) for r in rows
        ]
