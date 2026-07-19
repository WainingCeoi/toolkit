"""Remux Processor: engine command-builder units + router validation tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.main import create_app
from toolkit_api.routers import remux as remux_router
from toolkit_engine import remux


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
    app.include_router(remux_router.router, prefix="/api")
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


def start_payload(**overrides):
    payload = {
        "selected": ["/abs/a.mkv"],
        "include_video": True,
        "video_index": 0,
        "multi_audio": False,
        "audio_value": "0",
        "include_subtitle": True,
        "subtitle_index": 0,
        "sub_lang": "chi",
        "use_external_sub": False,
        "external_sub_map": {},
        "out_folder": "/abs/out",
        "max_workers": 2,
    }
    payload.update(overrides)
    return payload


# --- ported engine units (assertions unchanged) ---------------------------


def test_build_ffmpeg_cmd_copies_and_tags_subtitle():
    cmd = remux.build_ffmpeg_cmd(
        "in.mkv",
        None,
        "out.mkv",
        {"video": 0, "audio": [0], "subtitle": 0},
        "chi",
    )
    assert cmd[0] == "ffmpeg"
    assert "copy" in cmd  # stream-copy, no re-encode
    joined = " ".join(cmd)
    assert "title=in" in joined
    assert "language=chi" in joined


def test_build_ffmpeg_cmd_omits_subtitle_metadata_when_absent():
    cmd = remux.build_ffmpeg_cmd(
        "in.mkv",
        None,
        "out.mkv",
        {"video": 0, "audio": [0], "subtitle": None},
        "chi",
    )
    joined = " ".join(cmd)
    assert "language=" not in joined
    assert "disposition" not in joined


# --- /api/remux/scan -------------------------------------------------------


def test_scan_lists_videos_natural_sorted(tool_client, tmp_path):
    for name in ["ep10.mkv", "ep2.mkv", "ep1.mp4", "notes.txt"]:
        (tmp_path / name).write_bytes(b"")
    resp = tool_client.post("/api/remux/scan", json={"folder": str(tmp_path)})
    assert resp.status_code == 200
    videos = resp.json()["videos"]
    assert [v["name"] for v in videos] == ["ep1.mp4", "ep2.mkv", "ep10.mkv"]
    assert all(v["path"] == str(tmp_path / v["name"]) for v in videos)


def test_scan_relative_folder_is_rejected(tool_client):
    resp = tool_client.post("/api/remux/scan", json={"folder": "Movies"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Folder not found — use an absolute path (e.g. ~/Movies)."
    )


# --- /api/remux/subtitles --------------------------------------------------


def test_subtitles_match_by_stem_with_none_case(tool_client, tmp_path):
    vid_dir = tmp_path / "vids"
    sub_dir = tmp_path / "subs"
    vid_dir.mkdir()
    sub_dir.mkdir()
    a = vid_dir / "show.e01.mkv"
    b = vid_dir / "show.e02.mkv"
    a.write_bytes(b"")
    b.write_bytes(b"")
    sub = sub_dir / "show.e01.srt"
    sub.write_text("1")
    (sub_dir / "notes.txt").write_text("not a subtitle")
    resp = tool_client.post(
        "/api/remux/subtitles",
        json={"sub_folder": str(sub_dir), "selected": [str(a), str(b)]},
    )
    assert resp.status_code == 200
    assert resp.json()["matches"] == [
        {"video": str(a), "subtitle": str(sub)},
        {"video": str(b), "subtitle": None},
    ]


def test_subtitles_relative_folder_is_rejected(tool_client):
    resp = tool_client.post(
        "/api/remux/subtitles", json={"sub_folder": "subs", "selected": []}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Subtitle folder not found — use an absolute path."
    )


# --- /api/remux/start validations (never reach ffmpeg) ----------------------


def test_start_requires_selection(tool_client):
    resp = tool_client.post("/api/remux/start", json=start_payload(selected=[]))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Please select at least one video."


def test_start_requires_absolute_out_folder(tool_client):
    resp = tool_client.post("/api/remux/start", json=start_payload(out_folder="out"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Use an absolute output folder path."


def test_start_rejects_bad_audio_indices(tool_client, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/opt/fake/ffmpeg")
    resp = tool_client.post(
        "/api/remux/start",
        json=start_payload(multi_audio=True, audio_value="0,x"),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Audio track indices must be integers, e.g. 0,1"


def test_start_refuses_all_empty_stream_map(tool_client, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/opt/fake/ffmpeg")
    resp = tool_client.post(
        "/api/remux/start",
        json=start_payload(
            include_video=False,
            multi_audio=True,
            audio_value="",
            include_subtitle=False,
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Select at least one video, audio, or subtitle track."
    )


# --- /api/remux/start happy path (fake per-task worker, no ffmpeg) ----------


def test_start_runs_batch_and_reports_results(tool_client, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/opt/fake/ffmpeg")

    def fake_run_remux_task(task, progress_state, lock, ff_registry=None):
        with lock:
            progress_state[task["task_id"]] = 100.0
        title = Path(task["input_video"]).name
        if title == "bad.mkv":
            return {
                "task_id": task["task_id"],
                "title": title,
                "success": False,
                "error": "boom",
            }
        return {
            "task_id": task["task_id"],
            "title": title,
            "success": True,
            "error": None,
        }

    monkeypatch.setattr(remux, "run_remux_task", fake_run_remux_task)

    out_dir = tmp_path / "out"
    resp = tool_client.post(
        "/api/remux/start",
        json=start_payload(
            selected=[str(tmp_path / "good.mkv"), str(tmp_path / "bad.mkv")],
            out_folder=str(out_dir),
        ),
    )
    assert resp.status_code == 200
    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["total"] == 2
    assert snap["result"]["successful"] == 1
    assert snap["result"]["failed"] == [{"title": "bad.mkv", "error": "boom"}]
    assert snap["result"]["out_folder"] == str(out_dir)
    states = {item["name"]: item["state"] for item in snap["items"]}
    assert states == {"good.mkv": "done", "bad.mkv": "failed"}
    assert out_dir.is_dir()


def test_start_cancel_keeps_partial_report(
    tool_client, app_state, tmp_path, monkeypatch
):
    monkeypatch.setattr("shutil.which", lambda cmd: "/opt/fake/ffmpeg")

    started = threading.Event()
    release = threading.Event()

    def fake_run_remux_task(task, progress_state, lock, ff_registry=None):
        # The running ffmpeg finishes its current file; block until cancelled.
        title = Path(task["input_video"]).name
        started.set()
        release.wait(3.0)
        with lock:
            progress_state[task["task_id"]] = 100.0
        return {
            "task_id": task["task_id"],
            "title": title,
            "success": True,
            "error": None,
        }

    monkeypatch.setattr(remux, "run_remux_task", fake_run_remux_task)

    out_dir = tmp_path / "out"
    resp = tool_client.post(
        "/api/remux/start",
        json=start_payload(
            selected=[str(tmp_path / "a.mkv")],
            out_folder=str(out_dir),
        ),
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert started.wait(3.0)
    assert app_state.jobs.cancel(job_id)
    release.set()

    snap = wait_for_job(tool_client, job_id)
    assert snap["state"] == "cancelled"
    # The partial report of the already-remuxed file must survive cancellation.
    assert snap["result"] is not None
    assert snap["result"]["total"] == 1
    assert snap["result"]["successful"] == 1
    assert snap["result"]["out_folder"] == str(out_dir)
