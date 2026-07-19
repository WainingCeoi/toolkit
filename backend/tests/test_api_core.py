"""Core web-layer behavior: manifest, health, jobs lifecycle, SSE, artifacts."""

from __future__ import annotations

import shutil
import threading
import time

from toolkit_api.jobs import FINISHED_STATES, JobRegistry
from toolkit_api.main import create_app
from toolkit_engine import docmd


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in FINISHED_STATES:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_tools_manifest_has_nine_tools_in_three_categories(client):
    categories = client.get("/api/tools").json()
    assert [c["name"] for c in categories] == [
        "🎬 Media",
        "🗂️ Documents & Files",
        "🌐 Network",
    ]
    assert sum(len(c["tools"]) for c in categories) == 9


def test_health_reports_dependency_booleans(client):
    body = client.get("/api/health").json()
    assert set(body) == {"ok", "ffmpeg", "soffice", "mineru"}
    assert all(isinstance(v, bool) for v in body.values())


def test_health_mineru_uses_docmd_detection_not_just_path(client, monkeypatch):
    # MinerU is installed in the venv but not on PATH: shutil.which misses it,
    # while docmd.find_mineru() (the tool's own detector) finds it. Health must
    # agree with the tool, else the home lamp reads "not found" while it runs.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "mineru" else real_which(name)
    )
    monkeypatch.setattr(docmd, "find_mineru", lambda: ["mineru"])
    body = client.get("/api/health").json()
    assert body["mineru"] is True


def test_job_lifecycle_success(client, app_state):
    def worker(job):
        job.update_item(0, pct=100, state="done")
        return {"moved": 1}

    job = app_state.jobs.submit("test-tool", ["item-a"], worker)
    snap = wait_for_job(client, job.id)
    assert snap["state"] == "done"
    assert snap["result"] == {"moved": 1}
    assert snap["items"][0]["state"] == "done"


def test_job_failure_surfaces_error(client, app_state):
    def worker(job):
        raise RuntimeError("boom")

    job = app_state.jobs.submit("test-tool", ["item-a"], worker)
    snap = wait_for_job(client, job.id)
    assert snap["state"] == "failed"
    assert "boom" in snap["error"]


def test_job_cancel_stops_between_items(client, app_state):
    def worker(job):
        for _ in range(50):
            if job.cancelled:
                return None
            time.sleep(0.05)
        return {"finished": True}

    job = app_state.jobs.submit("test-tool", ["slow"], worker)
    resp = client.post(f"/api/jobs/{job.id}/cancel")
    assert resp.json() == {"cancelling": True}
    snap = wait_for_job(client, job.id)
    assert snap["state"] == "cancelled"


def test_job_events_stream_ends_with_done(client, app_state):
    def worker(job):
        job.update_item(0, pct=100, state="done")
        return {"ok": True}

    job = app_state.jobs.submit("test-tool", ["item-a"], worker)
    events = []
    with client.stream("GET", f"/api/jobs/{job.id}/events") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if events and events[-1] == "done" and line == "":
                break
    assert events[-1] == "done"


def test_unknown_job_and_artifact_are_404(client):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/events").status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    assert client.get("/api/artifacts/nope").status_code == 404


def test_artifact_roundtrip(client, app_state):
    artifact_id = app_state.artifacts.put_bytes(
        "result.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip"
    )
    resp = client.get(f"/api/artifacts/{artifact_id}")
    assert resp.status_code == 200
    assert resp.content.startswith(b"PK")
    assert "result.zip" in resp.headers["content-disposition"]


def test_all_api_routers_are_wired(app_state):
    # Guards against a router silently dropped from create_app's include list.
    # Included routers are nested wrappers in app.routes, so read the flattened
    # path list from the OpenAPI schema.
    app = create_app(state=app_state)
    paths = set(app.openapi()["paths"].keys())
    expected = {
        "/api/tools",
        "/api/health",
        "/api/fs/pick-folder",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/events",
        "/api/jobs/{job_id}/cancel",
        "/api/artifacts/{artifact_id}",
        "/api/magnet/config",
        "/api/magnet/auto",
        "/api/magnet/manual",
        "/api/magnet/dedupe",
        "/api/remux/scan",
        "/api/remux/subtitles",
        "/api/remux/start",
        "/api/gather/start",
        "/api/purge/scan",
        "/api/purge/delete",
        "/api/img-to-pdf",
        "/api/webpdf/open",
        "/api/webpdf/status",
        "/api/webpdf/capture",
        "/api/webpdf/close",
        "/api/doc-to-pdf",
        "/api/doc-to-markdown",
        "/api/doc-to-markdown/health",
        "/api/subs/generate",
        "/api/subs/history",
        "/sub/{sub_id}",
    }
    assert expected <= paths, f"unwired routes: {expected - paths}"


def _wait_finished(reg, job_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = reg.get(job_id)
        if job is not None and job.state in FINISHED_STATES:
            return
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_registry_evicts_oldest_finished_over_cap():
    reg = JobRegistry(max_jobs=3)
    ids = []
    for _ in range(5):
        job = reg.submit("quick", [], lambda job: {})
        _wait_finished(reg, job.id)  # finished before the next submit
        ids.append(job.id)
    # Eviction runs on submit: after 5 submits with cap 3, only the newest 3
    # finished jobs remain; the oldest two are dropped.
    present = [i for i in ids if reg.get(i) is not None]
    assert present == ids[-3:]


def test_registry_never_evicts_a_running_job():
    reg = JobRegistry(max_jobs=2)
    release = threading.Event()
    running = reg.submit("blocker", [], lambda job: release.wait(3.0) or {})
    try:
        for _ in range(5):
            job = reg.submit("quick", [], lambda job: {})
            _wait_finished(reg, job.id)
        # The running job outlives every finished job past the cap.
        assert reg.get(running.id) is not None
        assert reg.get(running.id).state == "running"
    finally:
        release.set()
