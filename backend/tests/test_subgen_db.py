"""Subscription storage protects the credentials in its JSON payloads."""

import sqlite3
from contextlib import closing

import pytest

from subgen.db import Store


@pytest.mark.parametrize("existing", [False, True])
def test_subscription_database_is_owner_only(tmp_path, existing):
    path = tmp_path / "sub.db"
    if existing:
        path.touch()
        path.chmod(0o644)

    store = Store(path)
    try:
        assert path.stat().st_mode & 0o077 == 0
        # SQLite sidecars can contain the same credentials as the database.
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute(
                "INSERT INTO subscriptions (id, payload, created_at) "
                "VALUES ('test', '{}', '2026-09-22')"
            )
            for suffix in ("-wal", "-shm"):
                assert path.with_name(path.name + suffix).stat().st_mode & 0o077 == 0
    finally:
        store.close()
