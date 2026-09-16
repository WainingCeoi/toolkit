"""Photos Library Filter: engine against a synthetic library, then the API."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
from contextlib import closing
from pathlib import Path

import pytest

from toolkit_api.jobs import FINISHED_STATES
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
    # SQLite drops -wal/-shm at close; the -wal sibling makes the planner snapshot.
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


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in FINISHED_STATES:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# --- rules ------------------------------------------------------------------


def test_rules_match_rsync_subset():
    rules = pf.compile_rules(RULES)

    def m(p):
        return pf.first_match(rules, p)

    assert m("database/search/leo.sqlite") == "database/search/"
    assert m("database/searchx") is None
    assert m("database/Photos.sqlite.lock") == "database/*.lock"
    assert m("database/sub/x.lock") is None
    assert m("private/a/caches/g/x.db") == "private/**/caches/"
    assert m("private/a/b/caches/x") == "private/**/caches/"
    assert m("a/b/.DS_Store") == ".DS_Store"
    assert m("a/.DS_Store_x") is None
    assert m("top.txt") == "/top.txt"
    assert m("a/top.txt") is None
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
    # An excluded dir is still mirrored as an empty skeleton; dirs inside are not.
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
        shutil.copyfile(src, main_only)
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
    assert (dest / "resources/derivatives").is_dir()
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
        pf.run(src, outer, [])
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, src, [])
    with pytest.raises(pf.PhotoFilterError, match=r"\*\.photoslibrary"):
        pf.run(src, tmp_path / "not-a-library", [])
    with pytest.raises(pf.PhotoFilterError, match="not a Photos library"):
        pf.run(tmp_path / "missing.photoslibrary", tmp_path / "Dest.photoslibrary", [])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Outer.photoslibrary"]


def test_refuses_a_dest_that_only_looks_different_from_the_source(tmp_path):
    outer = tmp_path / "Café.photoslibrary"
    src = build_src(outer)
    if not os.path.samefile(src, outer / "SRC.photoslibrary"):
        pytest.skip("case-sensitive volume")
    rules = pf.compile_rules(RULES)

    # Same directory, spelt in another case or in NFD: still the live library.
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, outer / "SRC.photoslibrary", rules)
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, tmp_path / "CAFÉ.photoslibrary", rules)
    nfd = Path(unicodedata.normalize("NFD", str(outer))) / src.name
    assert str(nfd) != str(src)
    with pytest.raises(pf.PhotoFilterError, match="overlap"):
        pf.run(src, nfd, rules)
    assert (src / ".DS_Store").exists()


def test_copy_failure_is_recorded_and_the_run_carries_on(tmp_path, monkeypatch):
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    real_copy = pf.copy_file

    def flaky_copy(source, target):
        if Path(source).name == "AAAA-1.heic":
            raise OSError(2, "No such file or directory", str(source))
        return real_copy(source, target)

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
    assert (dest / "database").is_dir()
    assert not (dest / "database/Photos.sqlite").exists()


def test_an_unreadable_source_dir_is_reported_and_spares_the_mirror(tmp_path):
    # scandir-rs omits a directory it cannot open and reports no error for it.
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    pf.run(src, dest, pf.compile_rules(RULES))
    os.chmod(src / "originals/A", 0)
    try:
        r = pf.run(src, dest, pf.compile_rules(RULES))
    finally:
        os.chmod(src / "originals/A", 0o755)

    assert "unreadable directory: originals/A" in r.errors
    assert "delete skipped: the scan of SRC was incomplete" in r.errors
    assert r.deleted == []
    assert (dest / "originals/A/AAAA-1.heic").exists()


def test_a_symlinked_directory_does_not_abort_the_delete_phase(tmp_path):
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    elsewhere = tmp_path / "elsewhere"
    touch(elsewhere, "inner.txt")
    os.symlink(elsewhere, src / "linkdir")

    r = pf.run(src, dest, pf.compile_rules(RULES))

    assert r.errors == [] and r.verified
    assert os.path.islink(dest / "linkdir")

    # A symlinked dir left in the destination is unlinked, never walked through.
    os.symlink(elsewhere, dest / "stale-link")
    r2 = pf.run(src, dest, pf.compile_rules(RULES))

    assert r2.deleted == ["stale-link"]
    assert (elsewhere / "inner.txt").exists()


def test_an_undeletable_entry_is_reported_and_the_run_carries_on(tmp_path):
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    pf.run(src, dest, pf.compile_rules(RULES))
    stale = touch(dest, "resources/derivatives/stale.jpeg")
    os.chmod(stale.parent, 0o500)
    try:
        r = pf.run(src, dest, pf.compile_rules(RULES))
    finally:
        os.chmod(stale.parent, 0o700)

    assert r.errors == [
        "delete failed for resources/derivatives/stale.jpeg: "
        f"[Errno 13] Permission denied: '{stale}'"
    ]
    assert r.deleted == [] and r.verified


def test_skip_needs_the_exact_mtime_not_a_near_one(tmp_path):
    # scandir-rs mtimes are doubles (~0.5us); the skip decision uses an exact stat.
    src, dest = build_src(tmp_path), tmp_path / "Dest.photoslibrary"
    pf.run(src, dest, pf.compile_rules(RULES))
    copy = dest / "originals/A/AAAA-1.heic"
    exact = os.stat(src / "originals/A/AAAA-1.heic").st_mtime_ns
    assert os.stat(copy).st_mtime_ns == exact

    for off in (1_000, 1_000_000):
        os.utime(copy, ns=(exact + off, exact + off))
        r = pf.run(src, dest, pf.compile_rules(RULES))
        assert (r.copied, r.skipped) == (1, 2)
        assert os.stat(copy).st_mtime_ns == exact


def test_plan_sizes_every_file_and_orders_parents_first(tmp_path):
    src = build_src(tmp_path)
    touch(src, "originals/B/big.heic", b"x" * 4096)

    plan = pf.plan(src, pf.compile_rules(RULES))

    assert plan.stat["originals/B/big.heic"].size == 4096
    assert plan.stat["resources/derivatives/A/AAAA-1_1_105_c.jpeg"].size == 5
    st = os.stat(src / "originals/B/big.heic")
    # Times come back as doubles; within the skip window of the truth.
    assert abs(plan.stat["originals/B/big.heic"].mtime_ns - st.st_mtime_ns) < 2_000
    assert plan.dirs.index("originals") < plan.dirs.index("originals/B")
    assert plan.keep == sorted(plan.keep)
    assert plan.errors == []


def test_summary_sizes_the_excluded_files_per_rule(tmp_path):
    src = build_src(tmp_path)
    touch(src, "resources/derivatives/B/big.jpeg", b"x" * 100)
    rules = pf.compile_rules(RULES)

    s = pf.summary(
        pf.run(src, tmp_path / "Dest.photoslibrary", rules, dry_run=True), rules
    )

    # 2 originals + the recipe, 6 B each; the database is a snapshot, not kept.
    assert s["kept"] == {"files": 3, "bytes": 18}
    assert s["snapshots"] == ["database/Photos.sqlite"]
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


# --- API --------------------------------------------------------------------


def test_photofilter_dry_run_then_run_end_to_end(client, tmp_path):
    src = build_src(tmp_path)
    dest = tmp_path / "Dest.photoslibrary"
    touch(dest, "resources/derivatives/stale.jpeg")
    body = {"source": str(src), "dest": str(dest), "rules": "\n".join(RULES)}

    resp = client.post("/api/photofilter/dry-run", json=body)
    assert resp.status_code == 200
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    dry = snap["result"]
    assert dry["dry_run"] is True
    assert (dry["source"], dry["dest"]) == (str(src.resolve()), str(dest.resolve()))
    assert dry["kept"] == {"files": 3, "bytes": 18}
    assert dry["snapshots"] == ["database/Photos.sqlite"]
    assert dry["verify"]["ran"] and dry["verify"]["problems"] == []
    assert dry["deleted"] == [] and dry["copied"] == 0
    assert (dest / "resources/derivatives/stale.jpeg").exists()
    assert not (dest / "originals").exists()

    resp = client.post("/api/photofilter/run", json=body)
    assert resp.status_code == 200
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    real = snap["result"]
    assert real["dry_run"] is False
    assert (real["copied"], real["skipped"], real["snapshotted"]) == (3, 0, 1)
    assert real["deleted"] == ["resources/derivatives/stale.jpeg"]
    assert real["errors"] == []
    assert real["verify"] == {"ran": True, "assets": 2, "edited": 1, "problems": []}
    assert isinstance(real["seconds"], float)
    assert (dest / "originals/A/AAAA-1.heic").read_bytes() == b"orig-a"
    assert (
        query_one(dest / "database/Photos.sqlite", "SELECT count(*) FROM ZASSET") == 2
    )


def test_photofilter_uses_the_shipped_rules_when_none_are_sent(client, tmp_path):
    src = build_src(tmp_path)
    dest = tmp_path / "Dest.photoslibrary"

    resp = client.post(
        "/api/photofilter/run", json={"source": str(src), "dest": str(dest)}
    )
    snap = wait_for_job(client, resp.json()["job_id"])

    assert snap["state"] == "done"
    rows = {r["rule"]: r["files"] for r in snap["result"]["rules"]}
    assert set(rows) == {r.pattern for r in pf.compile_rules(pf.DEFAULT_RULES)}
    assert rows["resources/derivatives/"] == 1
    assert not (dest / "resources/derivatives/A/AAAA-1_1_105_c.jpeg").exists()
    assert (dest / "resources/renders/B/BBBB-2.plist").exists()

    # An empty rules string is a choice -- exclude nothing -- not an omission.
    resp = client.post(
        "/api/photofilter/run",
        json={"source": str(src), "dest": str(dest), "rules": ""},
    )
    snap = wait_for_job(client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["rules"] == []
    assert (dest / "resources/derivatives/A/AAAA-1_1_105_c.jpeg").exists()


@pytest.mark.parametrize("endpoint", ["dry-run", "run"])
def test_photofilter_refuses_unsafe_paths_before_starting(client, tmp_path, endpoint):
    outer = tmp_path / "Outer.photoslibrary"
    src = build_src(outer)
    url = f"/api/photofilter/{endpoint}"

    def post(source, dest):
        return client.post(url, json={"source": str(source), "dest": str(dest)})

    resp = post("relative/Src.photoslibrary", tmp_path / "Dest.photoslibrary")
    assert resp.status_code == 400
    assert "absolute" in resp.json()["detail"]

    resp = post(tmp_path / "missing.photoslibrary", tmp_path / "Dest.photoslibrary")
    assert resp.status_code == 400
    assert "not a Photos library" in resp.json()["detail"]

    resp = post(src, tmp_path / "not-a-library")
    assert resp.status_code == 400
    assert "*.photoslibrary" in resp.json()["detail"]

    resp = post(src, src / "inner.photoslibrary")
    assert resp.status_code == 400
    assert "overlap" in resp.json()["detail"]

    resp = post(src, outer)
    assert resp.status_code == 400
    assert "overlap" in resp.json()["detail"]

    assert not (tmp_path / "Dest.photoslibrary").exists()
    assert not (tmp_path / "not-a-library").exists()
    assert not (src / "inner.photoslibrary").exists()


def test_photofilter_cancel_keeps_the_partial_report(
    client, app_state, tmp_path, monkeypatch
):
    src = build_src(tmp_path)
    dest = tmp_path / "Dest.photoslibrary"
    started = threading.Event()
    release = threading.Event()
    real_run = pf.run

    def slow_run(source, target, rules, dry_run=False, on_progress=None):
        def gate(phase, done, total):
            if phase == "copy" and done == 1:
                started.set()
                release.wait(3.0)
            return on_progress(phase, done, total)

        return real_run(source, target, rules, dry_run, gate)

    monkeypatch.setattr(pf, "run", slow_run)

    resp = client.post(
        "/api/photofilter/run", json={"source": str(src), "dest": str(dest)}
    )
    job_id = resp.json()["job_id"]
    assert started.wait(3.0)
    assert app_state.jobs.cancel(job_id)
    release.set()

    snap = wait_for_job(client, job_id)
    assert snap["state"] == "cancelled"
    assert snap["result"]["copied"] == 1
    assert snap["result"]["verify"]["ran"] is False


def test_photofilter_refuses_a_second_writer_on_the_same_destination(
    client, app_state, tmp_path, monkeypatch
):
    src = build_src(tmp_path)
    dest = tmp_path / "Dest.photoslibrary"
    started = threading.Event()
    release = threading.Event()

    def blocking_run(source, target, rules, dry_run=False, on_progress=None):
        started.set()
        release.wait(3.0)
        return pf.Result(plan=pf.Plan())

    monkeypatch.setattr(pf, "run", blocking_run)
    body = {"source": str(src), "dest": str(dest)}

    first = client.post("/api/photofilter/run", json=body).json()["job_id"]
    assert started.wait(3.0)
    second = client.post("/api/photofilter/run", json=body).json()["job_id"]
    # A case variant of the path names the same destination on APFS.
    variant = {"source": str(src), "dest": str(tmp_path / "DEST.photoslibrary")}
    variant_job = client.post("/api/photofilter/run", json=variant).json()["job_id"]
    # A dry run reads only, so it is not turned away.
    dry = client.post("/api/photofilter/dry-run", json=body).json()["job_id"]
    for job_id in (second, variant_job):
        snap = wait_for_job(client, job_id)
        assert snap["state"] == "failed"
        assert "already writing" in snap["error"]
    release.set()

    assert wait_for_job(client, first)["state"] == "done"
    assert wait_for_job(client, dry)["state"] == "done"
    third = client.post("/api/photofilter/run", json=body).json()["job_id"]
    assert wait_for_job(client, third)["state"] == "done"
