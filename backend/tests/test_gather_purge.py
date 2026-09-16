"""File Gatherer + Cache Purge: engine units and API end-to-end in tmp dirs."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.main import create_app
from toolkit_engine import gather, purge


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
    with TestClient(app) as c:
        yield c


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in FINISHED_STATES:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# --- engine units ---


def test_normalize_pattern():
    assert gather.normalize_pattern("srt") == "*.srt"
    assert gather.normalize_pattern(".srt") == "*.srt"
    assert gather.normalize_pattern("*.mkv") == "*.mkv"
    assert gather.normalize_pattern("report*.pdf") == "report*.pdf"
    assert gather.normalize_pattern("   ") is None


def test_move_files_numbers_duplicates_around_an_occupied_name(tmp_path):
    tgt = tmp_path / "tgt"
    tgt.mkdir()
    (tgt / "cover_1.jpg").write_text("already there")
    files = []
    for i in range(3):
        folder = tmp_path / f"src{i}"
        folder.mkdir()
        cover = folder / "cover.jpg"
        cover.write_text(str(i))
        files.append(str(cover))

    moved, failed = gather.move_files(files, tgt)

    assert failed == []
    assert moved == ["cover.jpg"] * 3
    assert sorted(p.name for p in tgt.iterdir()) == [
        "cover.jpg",
        "cover_1.jpg",
        "cover_2.jpg",
        "cover_3.jpg",
    ]
    assert (tgt / "cover_1.jpg").read_text() == "already there"


@pytest.mark.parametrize(
    "token",
    ["*", "*.*", "**", "*.", ".*", "?", "?*", "*?", "*.???", "**?"],
)
def test_purge_normalize_pattern_rejects_catch_alls(token):
    assert purge.normalize_pattern(token) is None


@pytest.mark.parametrize("token", ["*.log", "*.dwl2", "a*.bak", "log", ".tmp"])
def test_purge_normalize_pattern_keeps_real_globs(token):
    assert purge.normalize_pattern(token) is not None


# --- File Gatherer API ---


def test_gather_moves_files_and_autonumbers_duplicates(tool_client, tmp_path):
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    (src / "a").mkdir(parents=True)
    (src / "b").mkdir(parents=True)
    (src / "a" / "ep1.mkv").write_bytes(b"one")
    (src / "b" / "ep2.mp4").write_bytes(b"two")
    (src / "a" / "dup.mkv").write_bytes(b"first")
    (src / "b" / "dup.mkv").write_bytes(b"second")
    (src / "a" / "notes.txt").write_text("not a video")

    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(tgt),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"

    result = snap["result"]
    assert len(result["moved"]) == 4
    assert result["failed"] == []
    assert result["scan_errors"] == []
    assert result["warning"] is None
    assert result["target"] == str(tgt.resolve())

    moved_names = sorted(p.name for p in tgt.iterdir())
    assert moved_names == ["dup.mkv", "dup_1.mkv", "ep1.mkv", "ep2.mp4"]
    assert not (src / "a" / "ep1.mkv").exists()
    assert (src / "a" / "notes.txt").exists()


def test_gather_no_match_does_not_create_target(tool_client, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.txt").write_text("not a video")
    tgt = tmp_path / "tgt"

    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(tgt),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["moved"] == []
    assert snap["result"]["failed"] == []
    assert not tgt.exists()


def test_gather_cancel_keeps_partial_report(
    tool_client, app_state, tmp_path, monkeypatch
):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.mkv").write_bytes(b"a")
    (src / "b.mkv").write_bytes(b"b")
    tgt = tmp_path / "tgt"

    started = threading.Event()
    release = threading.Event()

    def fake_move_files(files, target, on_progress=None):
        started.set()
        release.wait(3.0)
        return ["a.mkv"], []

    monkeypatch.setattr(gather, "move_files", fake_move_files)

    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(tgt),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert started.wait(3.0)
    assert app_state.jobs.cancel(job_id)
    release.set()

    snap = wait_for_job(tool_client, job_id)
    assert snap["state"] == "cancelled"
    assert snap["result"] is not None
    assert snap["result"]["moved"] == ["a.mkv"]
    assert snap["result"]["failed"] == []
    assert snap["result"]["target"] == str(tgt.resolve())


def test_gather_rejects_target_inside_source(tool_client, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(src / "inner"),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Target must be a different folder, outside the source."
    )


def test_gather_rejects_a_case_variant_target(tool_client, tmp_path):
    src = tmp_path / "Movies"
    src.mkdir()
    (src / "ep1.mkv").write_bytes(b"one")
    alt = tmp_path / "movies"
    if not alt.is_dir():
        pytest.skip("case-sensitive filesystem")

    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(alt),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Target must be a different folder, outside the source."
    )


def test_gather_rejects_relative_paths(tool_client, tmp_path):
    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": "relative/src",
            "target": str(tmp_path / "tgt"),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Use absolute folder paths (e.g. ~/Movies or /Volumes/T7)."
    )


# --- Cache Purge API ---


def test_purge_scan_and_delete_end_to_end(tool_client, tmp_path):
    folder = tmp_path / "cache"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.log").write_text("aaaa")
    (folder / "b.tmp").write_text("bbbb")
    (folder / "sub" / "c.log").write_text("cccc")
    (folder / "keep.txt").write_text("keep")

    resp = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(folder), "patterns_raw": "*.log, tmp, *"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(Path(f).name for f in body["files"]) == ["a.log", "b.tmp", "c.log"]
    assert body["total_bytes"] > 0
    assert body["errors"] == []
    assert body["rejected_tokens"] == ["*"]

    resp = tool_client.post("/api/purge/delete", json={"scan_id": body["scan_id"]})
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert len(snap["result"]["deleted"]) == 3
    assert snap["result"]["failed"] == []
    for f in body["files"]:
        assert not Path(f).exists()
    assert (folder / "keep.txt").exists()

    # A scan record is single-use.
    again = tool_client.post("/api/purge/delete", json={"scan_id": body["scan_id"]})
    assert again.status_code == 409


def test_purge_delete_only_removes_what_the_scan_recorded(tool_client, tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    (folder / "a.log").write_text("junk")
    outside = tmp_path / "secret.txt"
    outside.write_text("do not delete me")

    scan = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(folder), "patterns_raw": "*.log"},
    ).json()
    assert [Path(f).name for f in scan["files"]] == ["a.log"]

    resp = tool_client.post("/api/purge/delete", json={"scan_id": scan["scan_id"]})
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert outside.exists()
    assert not (folder / "a.log").exists()


def test_purge_delete_refuses_an_unknown_or_expired_scan(tool_client):
    resp = tool_client.post("/api/purge/delete", json={"scan_id": "deadbeefcafe"})
    assert resp.status_code == 409
    assert "scan again" in resp.json()["detail"]


def test_purge_delete_cancel_keeps_partial_report(
    tool_client, app_state, tmp_path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()

    def fake_delete_files(paths, on_progress=None):
        started.set()
        release.wait(3.0)
        return [paths[0]], []

    (tmp_path / "a.log").write_text("a")
    (tmp_path / "b.log").write_text("b")
    scan = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(tmp_path), "patterns_raw": "*.log"},
    ).json()

    monkeypatch.setattr(purge, "delete_files", fake_delete_files)

    resp = tool_client.post("/api/purge/delete", json={"scan_id": scan["scan_id"]})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert started.wait(3.0)
    assert app_state.jobs.cancel(job_id)
    release.set()

    snap = wait_for_job(tool_client, job_id)
    assert snap["state"] == "cancelled"
    assert snap["result"] is not None
    assert snap["result"]["deleted"] == [str(tmp_path / "a.log")]
    assert snap["result"]["failed"] == []


def test_purge_delete_ignores_a_client_supplied_file_list(tool_client, tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    (folder / "a.log").write_text("junk")
    outside = tmp_path / "secret.txt"
    outside.write_text("do not delete me")

    scan = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(folder), "patterns_raw": "*.log"},
    ).json()

    resp = tool_client.post(
        "/api/purge/delete",
        json={"scan_id": scan["scan_id"], "folder": "/", "files": [str(outside)]},
    )
    assert resp.status_code == 200
    wait_for_job(tool_client, resp.json()["job_id"])
    assert outside.exists()


def test_purge_delete_reports_per_file_failures(tool_client, tmp_path, monkeypatch):
    folder = tmp_path / "cache"
    folder.mkdir()
    good = folder / "a.log"
    bad = folder / "b.log"
    good.write_text("a")
    bad.write_text("b")

    scan = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(folder), "patterns_raw": "*.log"},
    ).json()

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == "b.log":
            raise PermissionError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    resp = tool_client.post("/api/purge/delete", json={"scan_id": scan["scan_id"]})
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["deleted"] == [str(good)]
    assert snap["result"]["failed"] == [{"name": "b.log", "error": "locked"}]


def test_gather_reports_per_file_move_failures(tool_client, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.mkv").write_bytes(b"a")
    (src / "b.mkv").write_bytes(b"b")
    tgt = tmp_path / "tgt"

    import shutil as _shutil

    real_move = _shutil.move

    def flaky_move(source, dest, *args, **kwargs):
        if Path(source).name == "b.mkv":
            raise OSError("cross-device link failed")
        return real_move(source, dest, *args, **kwargs)

    monkeypatch.setattr(gather.shutil, "move", flaky_move)

    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(tgt),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["moved"] == ["a.mkv"]
    assert snap["result"]["failed"] == [
        {"name": "b.mkv", "error": "cross-device link failed"}
    ]


def test_purge_delete_cancel_reports_every_file_it_removed(tmp_path, monkeypatch):
    paths = []
    for i in range(40):
        p = tmp_path / f"f{i:02d}.log"
        p.write_text("x")
        paths.append(str(p))

    real_unlink = Path.unlink

    def slow_unlink(self, *args, **kwargs):
        time.sleep(0.02)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", slow_unlink)

    deleted, failed = purge.delete_files(paths, lambda done, total: done >= 4)

    assert failed == []
    gone = [p for p in paths if not Path(p).exists()]
    assert len(gone) < len(paths)  # cancelling actually stopped the run
    assert sorted(deleted) == sorted(gone)


@pytest.mark.parametrize("token", ["[", "*.[abc"])
def test_purge_scan_rejects_a_malformed_bracket_pattern(tool_client, tmp_path, token):
    resp = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(tmp_path), "patterns_raw": token},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"].startswith("❌ Invalid pattern:")


def test_gather_presets_cover_the_shared_file_type_table(tool_client, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "disc.m2ts").write_bytes(b"x")  # a video to Remux, but not to Gather
    tgt = tmp_path / "tgt"

    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(tgt),
            "categories": ["Video"],
            "custom": "",
        },
    )
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["moved"] == ["disc.m2ts"]


def test_gather_rejects_an_unknown_category(tool_client, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    resp = tool_client.post(
        "/api/gather/start",
        json={
            "source": str(src),
            "target": str(tmp_path / "tgt"),
            "categories": ["video"],
            "custom": "",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Unknown file type: video"


def test_purge_scan_rejects_catch_all_only_patterns(tool_client, tmp_path):
    resp = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(tmp_path), "patterns_raw": "* *.* ?"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Enter at least one extension / pattern."
