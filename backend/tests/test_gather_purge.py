"""File Gatherer + Cache Purge: engine units and API end-to-end in tmp dirs."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.main import create_app
from toolkit_api.routers import gather as gather_router
from toolkit_api.routers import purge as purge_router
from toolkit_engine import gather, purge


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
    app.include_router(gather_router.router, prefix="/api")
    app.include_router(purge_router.router, prefix="/api")
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


# --- engine units -----------------------------------------------------------


def test_normalize_pattern():
    assert gather.normalize_pattern("srt") == "*.srt"
    assert gather.normalize_pattern(".srt") == "*.srt"
    assert gather.normalize_pattern("*.mkv") == "*.mkv"
    assert gather.normalize_pattern("report*.pdf") == "report*.pdf"
    assert gather.normalize_pattern("   ") is None


@pytest.mark.parametrize("token", ["*", "*.*", "**", "*.", ".*", "?"])
def test_purge_normalize_pattern_rejects_catch_alls(token):
    assert purge.normalize_pattern(token) is None


# --- File Gatherer API ------------------------------------------------------


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
    assert (src / "a" / "notes.txt").exists()  # non-matching file stays put


def test_gather_no_match_does_not_create_target(tool_client, tmp_path):
    # Source has only a non-matching file, so the Video scan finds nothing.
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
    # The empty target folder must NOT be littered on a no-match run.
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
        # Report one moved file, then block until the job is cancelled.
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
    # The partial report of already-moved files must survive cancellation.
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


# --- Cache Purge API --------------------------------------------------------


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
    assert body["rejected_tokens"] == ["*"]  # catch-all ignored, not applied

    resp = tool_client.post(
        "/api/purge/delete", json={"folder": str(folder), "files": body["files"]}
    )
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert len(snap["result"]["deleted"]) == 3
    assert snap["result"]["failed"] == []
    for f in body["files"]:
        assert not Path(f).exists()
    assert (folder / "keep.txt").exists()


def test_purge_delete_cancel_keeps_partial_report(
    tool_client, app_state, tmp_path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()

    def fake_delete_files(paths, on_progress=None):
        # Report one deleted file, then block until the job is cancelled.
        started.set()
        release.wait(3.0)
        return [paths[0]], []

    monkeypatch.setattr(purge, "delete_files", fake_delete_files)

    resp = tool_client.post(
        "/api/purge/delete",
        json={
            "folder": str(tmp_path),
            "files": [str(tmp_path / "a.log"), str(tmp_path / "b.log")],
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert started.wait(3.0)
    assert app_state.jobs.cancel(job_id)
    release.set()

    snap = wait_for_job(tool_client, job_id)
    assert snap["state"] == "cancelled"
    # The partial report of already-deleted files must survive cancellation.
    assert snap["result"] is not None
    assert snap["result"]["deleted"] == [str(tmp_path / "a.log")]
    assert snap["result"]["failed"] == []


def test_purge_delete_rejects_path_outside_scanned_folder(tool_client, tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("do not delete me")

    resp = tool_client.post(
        "/api/purge/delete",
        json={"folder": str(folder), "files": [str(outside)]},
    )
    assert resp.status_code == 400
    assert "outside the scanned folder" in resp.json()["detail"]
    assert outside.exists()


def test_purge_delete_rejects_traversal_escape(tool_client, tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    escape = folder / ".." / "secret.txt"
    (tmp_path / "secret.txt").write_text("do not delete me")

    resp = tool_client.post(
        "/api/purge/delete",
        json={"folder": str(folder), "files": [str(escape)]},
    )
    assert resp.status_code == 400
    assert (tmp_path / "secret.txt").exists()


def test_purge_scan_rejects_catch_all_only_patterns(tool_client, tmp_path):
    resp = tool_client.post(
        "/api/purge/scan",
        json={"folder": str(tmp_path), "patterns_raw": "* *.* ?"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Enter at least one extension / pattern."
