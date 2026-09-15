"""Photos Library Filter: the engine against a synthetic library, then the API.

The library is a handful of files in tmp_path with a real WAL-mode SQLite
database, so every step -- rules, plan, VACUUM INTO snapshot, copyfile clone,
skip, delete, verify -- runs for real on this Mac's APFS.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

import pytest

from toolkit_engine import photofilter as pf

RULES = [
    "# comment",
    "",
    ".DS_Store",
    "database/search/",
    "database/*.lock",
    "resources/derivatives/",
    "private/**/caches/",
    "/top.txt",
]


def make_photos_db(path, assets):
    """assets: list of (uuid, directory, filename, adjustment_ts_or_None)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(
            "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, "
            "ZDIRECTORY TEXT, ZFILENAME TEXT, ZADJUSTMENTTIMESTAMP REAL)"
        )
        c.executemany(
            "INSERT INTO ZASSET (ZUUID, ZDIRECTORY, ZFILENAME, ZADJUSTMENTTIMESTAMP) "
            "VALUES (?,?,?,?)",
            assets,
        )
        c.commit()
    # A live library always has these next to its database: Apple's SQLite
    # keeps -wal/-shm after the last connection closes, and Photos holds them
    # open besides. The upstream SQLite bundled with uv's Python deletes them
    # at close, so they are laid down explicitly -- the -wal sibling is what
    # tells the planner to snapshot instead of copy.
    for sidecar in ("-wal", "-shm"):
        Path(str(path) + sidecar).touch()


def touch(root, rel, data=b"x"):
    p = Path(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def query_one(db, sql, query="mode=ro"):
    with closing(sqlite3.connect(f"file:{db}?{query}", uri=True)) as c:
        return c.execute(sql).fetchone()[0]


def xattr_set(path, name, value):
    subprocess.run(["xattr", "-w", name, value, str(path)], check=True)


def xattr_get(path, name):
    out = subprocess.run(
        ["xattr", "-p", name, str(path)], capture_output=True, text=True
    )
    return out.stdout.strip()


def build_src(tmp):
    src = Path(tmp, "Src.photoslibrary")
    make_photos_db(
        src / "database" / "Photos.sqlite",
        [("AAAA-1", "A", "AAAA-1.heic", None), ("BBBB-2", "B", "BBBB-2.heic", 1.0)],
    )
    touch(src, "originals/A/AAAA-1.heic", b"orig-a")
    touch(src, "originals/B/BBBB-2.heic", b"orig-b")
    touch(src, "resources/renders/B/BBBB-2.plist", b"recipe")
    touch(src, "resources/derivatives/A/AAAA-1_1_105_c.jpeg", b"thumb")
    touch(src, "database/search/leo.sqlite")
    touch(src, ".DS_Store")
    return src


# --- rules ------------------------------------------------------------------


def test_rules_match_rsync_subset():
    rules = pf.compile_rules(RULES)

    def m(p):
        return pf.first_match(rules, p)

    assert m("database/search/leo.sqlite") == "database/search/"
    assert m("database/searchx") is None  # dir rule needs the dir
    assert m("database/Photos.sqlite.lock") == "database/*.lock"
    assert m("database/sub/x.lock") is None  # * stays in one component
    assert m("private/a/caches/g/x.db") == "private/**/caches/"
    assert m("private/a/b/caches/x") == "private/**/caches/"
    assert m("a/b/.DS_Store") == ".DS_Store"  # unanchored, any depth
    assert m("a/.DS_Store_x") is None
    assert m("top.txt") == "/top.txt"
    assert m("a/top.txt") is None  # leading / anchors
    assert m("originals/0/a.heic") is None


def test_compile_rules_accepts_the_whole_file_as_one_string():
    assert [r.pattern for r in pf.compile_rules("\n".join(RULES))] == [
        r.pattern for r in pf.compile_rules(RULES)
    ]


def test_default_rules_drop_caches_and_keep_originals_and_renders():
    rules = pf.compile_rules(pf.DEFAULT_RULES)
    assert pf.first_match(rules, "resources/derivatives/A/x.jpeg")
    assert pf.first_match(rules, "database/search/psi.sqlite")
    assert pf.first_match(rules, "database/Photos.sqlite.lock")
    assert pf.first_match(rules, "private/com.apple.x/caches/graph/x.db")
    assert pf.first_match(rules, "originals/A/.DS_Store")
    # Never excluded: the edit recipes, the originals, the database itself.
    assert pf.first_match(rules, "resources/renders/B/BBBB-2.plist") is None
    assert pf.first_match(rules, "originals/A/AAAA-1.heic") is None
    assert pf.first_match(rules, "database/Photos.sqlite") is None


# --- plan / snapshot --------------------------------------------------------


def test_plan_classifies_files(tmp_path):
    src = tmp_path / "Src.photoslibrary"
    touch(src, "originals/0/a.heic")
    touch(src, ".DS_Store")
    touch(src, "database/search/leo.sqlite")
    touch(src, "database/search/Spotlight/idx")  # inside an excluded dir
    touch(src, "resources/derivatives/x.jpeg")
    make_photos_db(src / "database" / "Photos.sqlite", [])
    touch(src, "database/Photos.sqlite-wal")
    touch(src, "database/Photos.sqlite-shm")

    plan = pf.plan(src, pf.compile_rules(RULES))

    assert plan.keep == ["originals/0/a.heic"]
    assert plan.snapshot == ["database/Photos.sqlite"]
    assert plan.excluded == {
        ".DS_Store": ".DS_Store",
        "database/search/leo.sqlite": "database/search/",
        "database/search/Spotlight/idx": "database/search/",
        "resources/derivatives/x.jpeg": "resources/derivatives/",
    }
    # An excluded directory is still mirrored (as an empty skeleton); a
    # directory inside one is not.
    assert sorted(plan.dirs) == [
        "database",
        "database/search",
        "originals",
        "originals/0",
        "resources",
        "resources/derivatives",
    ]


def test_snapshot_includes_unflushed_wal(tmp_path):
    src = tmp_path / "live.sqlite"
    with closing(sqlite3.connect(src)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (v)")
        writer.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
        writer.commit()  # committed, but only in the WAL
        main_only = tmp_path / "main_only.sqlite"
        shutil.copyfile(src, main_only)  # what a raw copy without -wal ships
        assert not query_one(
            main_only,
            "SELECT count(*) FROM sqlite_master WHERE name='t'",
            "mode=ro&immutable=1",
        )
        dest = tmp_path / "out" / "snap.sqlite"
        dest.parent.mkdir()
        pf.snapshot_sqlite(src, dest)  # while the writer still holds it open
    assert query_one(dest, "SELECT count(*) FROM t") == 3
    assert not Path(str(dest) + "-wal").exists()
    assert not list(dest.parent.glob("*.tmp"))


# --- run --------------------------------------------------------------------


def test_run_copies_snapshots_skips_and_deletes(tmp_path):
    src = build_src(tmp_path)
    dest = tmp_path / "Dest.photoslibrary"
    old = os.stat(src / "originals/A/AAAA-1.heic")
    os.utime(
        src / "originals/A/AAAA-1.heic",
        ns=(old.st_atime_ns, old.st_mtime_ns - 5_000_000_000),
    )
    xattr_set(src / "originals/A/AAAA-1.heic", "com.apple.assetsd.UUID", "abc")
    touch(dest, "resources/derivatives/stale.jpeg")  # stale from an older run
    touch(dest, "database/Photos.sqlite-wal")  # must never survive

    r1 = pf.run(src, dest, pf.compile_rules(RULES))

    assert (dest / "originals/A/AAAA-1.heic").read_bytes() == b"orig-a"
    assert (
        os.stat(dest / "originals/A/AAAA-1.heic").st_mtime_ns
        == os.stat(src / "originals/A/AAAA-1.heic").st_mtime_ns
    )
    assert xattr_get(dest / "originals/A/AAAA-1.heic", "com.apple.assetsd.UUID") == (
        "abc"
    )
    assert (dest / "resources/renders/B/BBBB-2.plist").exists()
    assert (dest / "resources/derivatives").is_dir()  # skeleton kept, contents not
    assert list((dest / "resources/derivatives").iterdir()) == []
    assert not (dest / "database/search/leo.sqlite").exists()
    assert not (dest / ".DS_Store").exists()
    assert not (dest / "database/Photos.sqlite-wal").exists()
    assert (
        query_one(dest / "database/Photos.sqlite", "SELECT count(*) FROM ZASSET") == 2
    )
    assert r1.copied == 3
    assert r1.snapshotted == 1
    assert r1.deleted == [
        "database/Photos.sqlite-wal",
        "resources/derivatives/stale.jpeg",
    ]
    assert r1.errors == []
    assert r1.verified and r1.problems == []
    assert (r1.assets, r1.edited) == (2, 1)

    r2 = pf.run(src, dest, pf.compile_rules(RULES))
    assert (r2.copied, r2.skipped, r2.deleted) == (0, 3, [])


def test_xattr_change_without_mtime_change_is_recopied(tmp_path):
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    pf.run(src, dest, pf.compile_rules(RULES))
    time.sleep(0.01)
    f = src / "originals/B/BBBB-2.heic"
    # Photos does exactly this when you favourite a photo: the xattr changes,
    # mtime does not, ctime does.
    xattr_set(f, "com.apple.assetsd.favorite", "1")

    r = pf.run(src, dest, pf.compile_rules(RULES))

    assert (r.copied, r.skipped) == (1, 2)
    assert xattr_get(
        dest / "originals/B/BBBB-2.heic", "com.apple.assetsd.favorite"
    ) == ("1")


def test_dry_run_writes_nothing_but_verifies_plan(tmp_path):
    src = build_src(tmp_path)
    (src / "resources/renders/B/BBBB-2.plist").unlink()  # edited, recipe missing
    dest = tmp_path / "Dest.photoslibrary"

    r = pf.run(src, dest, pf.compile_rules(RULES), dry_run=True)

    assert not dest.exists()
    assert r.verified
    assert len(r.problems) == 1
    assert "resources/renders/B/BBBB-2.plist" in r.problems[0]


def test_verify_reports_missing_original(tmp_path):
    src = build_src(tmp_path)
    (src / "originals/A/AAAA-1.heic").unlink()

    r = pf.run(src, tmp_path / "Dest.photoslibrary", pf.compile_rules(RULES))

    assert len(r.problems) == 1
    assert "originals/A/AAAA-1.heic" in r.problems[0]


def test_refuses_overlapping_or_unsafe_dest(tmp_path):
    outer = tmp_path / "Outer.photoslibrary"
    src = build_src(outer)
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, src / "inner.photoslibrary", [])
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, outer, [])  # DEST above SRC would delete SRC
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, src, [])
    with pytest.raises(pf.PhotoFilterError, match=r"\*\.photoslibrary"):
        pf.run(src, tmp_path / "not-a-library", [])
    with pytest.raises(pf.PhotoFilterError, match="not a Photos library"):
        pf.run(tmp_path / "missing.photoslibrary", tmp_path / "Dest.photoslibrary", [])
    # Nothing was created by any of the refused runs.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Outer.photoslibrary"]


def test_copy_failure_is_recorded_and_the_run_carries_on(tmp_path, monkeypatch):
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    real_copy = pf.copy_file

    def flaky_copy(source, target, st):
        if Path(source).name == "AAAA-1.heic":
            raise OSError(2, "No such file or directory", str(source))
        return real_copy(source, target, st)

    monkeypatch.setattr(pf, "copy_file", flaky_copy)

    r = pf.run(src, dest, pf.compile_rules(RULES))

    assert r.copied == 2
    assert r.errors == [
        "copy failed for originals/A/AAAA-1.heic: "
        f"[Errno 2] No such file or directory: '{src / 'originals/A/AAAA-1.heic'}'"
    ]
    assert (dest / "originals/B/BBBB-2.heic").exists()
    assert r.verified
    assert r.problems == ["missing original for AAAA-1: originals/A/AAAA-1.heic"]


def test_stop_request_returns_the_partial_result_unverified(tmp_path):
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    seen = []

    def stop_after_first_copy(phase, done, total):
        seen.append((phase, done, total))
        return phase == "copy" and done == 1

    r = pf.run(src, dest, pf.compile_rules(RULES), on_progress=stop_after_first_copy)

    assert r.copied == 1
    assert r.snapshotted == 0
    assert not r.verified and r.problems == []
    assert seen[0] == ("plan", 0, 0)
    assert seen[-1] == ("copy", 1, 3)
    assert (dest / "database").is_dir()  # the skeleton was laid down first
    assert not (dest / "database/Photos.sqlite").exists()


def test_summary_sizes_the_excluded_files_per_rule(tmp_path):
    src = build_src(tmp_path)
    touch(src, "resources/derivatives/B/big.jpeg", b"x" * 100)
    rules = pf.compile_rules(RULES)

    s = pf.summary(
        pf.run(src, tmp_path / "Dest.photoslibrary", rules, dry_run=True), rules
    )

    # keep: 2 originals (6 B each) + the recipe (6 B); the database is a
    # snapshot, not a kept file.
    assert s["kept"] == {"files": 3, "bytes": 18}
    assert s["snapshots"] == ["database/Photos.sqlite"]
    # Biggest saving first; rules that matched nothing still listed, in file
    # order, so a rule that does nothing is visible rather than absent.
    assert s["rules"] == [
        {"rule": "resources/derivatives/", "files": 2, "bytes": 105},
        {"rule": ".DS_Store", "files": 1, "bytes": 1},
        {"rule": "database/search/", "files": 1, "bytes": 1},
        {"rule": "database/*.lock", "files": 0, "bytes": 0},
        {"rule": "private/**/caches/", "files": 0, "bytes": 0},
        {"rule": "/top.txt", "files": 0, "bytes": 0},
    ]
    assert s["excluded"] == {"files": 4, "bytes": 107}
    assert s["verify"] == {"ran": True, "assets": 2, "edited": 1, "problems": []}
    assert (s["copied"], s["skipped"], s["snapshotted"], s["deleted"]) == (0, 0, 0, [])
