"""SQLite persistence for generated subscriptions."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id TEXT PRIMARY KEY,
  source_hash TEXT UNIQUE,
  payload TEXT NOT NULL,
  name_prefix TEXT,
  keep_original_host INTEGER,
  node_count INTEGER,
  created_at TEXT NOT NULL
);
"""


class Store:
    """SQLite-backed storage; opens a fresh connection per call (thread-safe)."""

    def __init__(self, path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_subscription(
        self,
        *,
        id,
        source_hash,
        payload,
        name_prefix,
        keep_original_host,
        node_count,
        created_at,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO subscriptions "
                "(id, source_hash, payload, name_prefix, "
                "keep_original_host, node_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    id,
                    source_hash,
                    payload,
                    name_prefix or "",
                    1 if keep_original_host else 0,
                    node_count or 0,
                    created_at,
                ),
            )
            conn.commit()

    def find_subscription_by_hash(self, source_hash) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE source_hash = ?", (source_hash,)
            ).fetchone()
            return dict(row) if row else None

    def get_subscription(self, sub_id) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, payload, created_at FROM subscriptions WHERE id = ?",
                (sub_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "payload": json.loads(row["payload"]),
            "created_at": row["created_at"],
        }

    def list_subscriptions(self, limit: int = 50) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, node_count, name_prefix, created_at "
                "FROM subscriptions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_subscription(self, sub_id) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            conn.commit()
