"""SQLite persistence for the torrent queue.

aria2 forgets everything it finished the moment it restarts, and its session
file can be lost outright to a kill -9, so this table -- not the daemon -- is
the source of truth for what the user asked for. The daemon holds the piece
data; this holds the intent.

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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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

    def set_save_dir(self, infohash: str, save_dir: str) -> None:
        """Persist the destination the user actually chose at commit.

        Stored verbatim (e.g. "~/Downloads"), so the dashboard shows the tidy
        form; callers expanduser() it at the filesystem boundary. Without this
        a picked folder was lost -- reconciliation and file deletion would use
        the stale default the row was seeded with at resolve time.
        """
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE torrents SET save_dir = ? WHERE infohash = ?",
                (save_dir, infohash),
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

        pause_reason is overwritten rather than merged on purpose: a stale
        'shutdown' left behind on a torrent the user later paused by hand
        would auto-resume it on the next boot.
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
        """Mark removed but keep the row, so reconciliation cannot re-adopt it."""
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

    def paused_by_shutdown(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM torrents WHERE state = 'paused' "
                "AND pause_reason = 'shutdown' ORDER BY added_at, infohash"
            ).fetchall()
        return [dict(r) for r in rows]

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
