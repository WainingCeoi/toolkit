"""HTTP additions for discovering shared jobs and cancelling dependency apply."""

from __future__ import annotations

import threading
import time

import pytest

from toolkit_api.routers.depsync import _exclusive_root
from toolkit_engine import depsync


def _finished(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["state"] != "running":
            return snapshot
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def _manifests(tmp_path, monkeypatch, count=2):
    manifests = []
    for index in range(count):
        path = tmp_path / f"project{index}" / "pyproject.toml"
        path.parent.mkdir()
        path.write_text("original", encoding="utf-8")
        manifests.append(depsync.Manifest(path, "uv", str(path.relative_to(tmp_path))))
    monkeypatch.setattr(depsync, "find_manifests", lambda _: (manifests, None))
    monkeypatch.setattr(depsync, "git_root", lambda folder: folder)
    return manifests


def _written(manifest):
    original = manifest.path.read_text(encoding="utf-8")
    manifest.path.write_text("updated", encoding="utf-8")
    return {
        "rel": manifest.rel,
        "kind": manifest.kind,
        "written": 1,
        "bumps": [],
        "skipped": [],
        "error": None,
        "changed": [manifest.path],
        "originals": {str(manifest.path): original},
    }


def test_list_jobs_discovers_other_clients_filters_and_limits(client, app_state):
    first = app_state.jobs.submit("doc-to-pdf", [], lambda job: {"artifact_id": "a"})
    _finished(client, first.id)
    second = app_state.jobs.submit("magnet-scraper", [], lambda job: {"count": 1})
    _finished(client, second.id)
    assert [job["id"] for job in client.get("/api/jobs").json()] == [
        second.id,
        first.id,
    ]
    assert client.get("/api/jobs?limit=1").json()[0]["id"] == second.id
    assert client.get("/api/jobs?tool=doc-to-pdf").json() == [
        client.get(f"/api/jobs/{first.id}").json()
    ]
    assert client.get("/api/jobs?state=running").json() == []
    assert len(client.get("/api/jobs?state=done").json()) == 2


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "state=unknown"])
def test_list_jobs_validates_query(client, query):
    assert client.get(f"/api/jobs?{query}").status_code == 422


def test_async_apply_uses_shared_registry_and_returns_existing_result(
    client, app_state, tmp_path, monkeypatch
):
    manifests = _manifests(tmp_path, monkeypatch)
    monkeypatch.setattr(depsync, "write_manifest", _written)
    response = client.post(
        "/api/deps/apply/start", json={"folder": str(tmp_path), "commit": False}
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert app_state.jobs.get(job_id) is not None
    snapshot = _finished(client, job_id)
    assert snapshot["state"] == "done"
    assert snapshot["tool"] == "dep-upgrade"
    assert snapshot["result"]["written_total"] == 2
    assert snapshot["result"]["commits"] == []
    assert [r["rel"] for r in snapshot["result"]["results"]] == [
        manifest.rel for manifest in manifests
    ]


def test_async_apply_cancel_waits_for_write_then_skips_remaining_and_commit(
    client, tmp_path, monkeypatch
):
    manifests = _manifests(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def write(manifest):
        calls.append(manifest.rel)
        entered.set()
        assert release.wait(5)
        return _written(manifest)

    def unexpected_commit(*args):
        raise AssertionError("Cancelled apply attempted a commit")

    monkeypatch.setattr(depsync, "write_manifest", write)
    monkeypatch.setattr(depsync, "commit_paths", unexpected_commit)
    response = client.post("/api/deps/apply/start", json={"folder": str(tmp_path)})
    job_id = response.json()["job_id"]
    try:
        assert entered.wait(2)
        assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelling": True}
        assert client.get(f"/api/jobs/{job_id}").json()["state"] == "running"
    finally:
        release.set()
    snapshot = _finished(client, job_id)
    assert snapshot["state"] == "cancelled"
    assert snapshot["result"]["written_total"] == 1
    assert snapshot["result"]["commits"] == []
    assert calls == [manifests[0].rel]
    assert manifests[0].path.read_text() == "updated"
    assert manifests[1].path.read_text() == "original"
    assert "Completed writes and commits remain" in snapshot["message"]


def test_async_apply_cancel_during_commit_preserves_it_and_skips_next_repo(
    client, tmp_path, monkeypatch
):
    manifests = _manifests(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    committed = []
    monkeypatch.setattr(depsync, "write_manifest", _written)

    def commit(root, subject, paths):
        committed.append(root)
        entered.set()
        assert release.wait(5)
        return "isolated-test-sha", [path.name for path in paths], None

    monkeypatch.setattr(depsync, "commit_paths", commit)
    response = client.post("/api/deps/apply/start", json={"folder": str(tmp_path)})
    job_id = response.json()["job_id"]
    try:
        assert entered.wait(2)
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    finally:
        release.set()
    snapshot = _finished(client, job_id)
    assert snapshot["state"] == "cancelled"
    assert snapshot["result"]["written_total"] == 2
    assert snapshot["result"]["commits"] == [
        {"sha": "isolated-test-sha", "files": ["pyproject.toml"]}
    ]
    assert committed == [str(manifests[0].path.parent)]
    assert all(manifest.path.read_text() == "updated" for manifest in manifests)


def test_async_apply_preflight_rejects_non_repo_before_writes(
    client, tmp_path, monkeypatch
):
    manifests = _manifests(tmp_path, monkeypatch)
    monkeypatch.setattr(depsync, "git_root", lambda _: None)
    response = client.post("/api/deps/apply/start", json={"folder": str(tmp_path)})
    assert response.status_code == 400
    assert "Not a git repository" in response.json()["detail"]
    assert all(manifest.path.read_text() == "original" for manifest in manifests)


def test_async_apply_obeys_shared_scan_apply_lock(client, tmp_path, monkeypatch):
    manifests = _manifests(tmp_path, monkeypatch)
    with _exclusive_root(str(tmp_path)):
        response = client.post(
            "/api/deps/apply/start", json={"folder": str(tmp_path), "commit": False}
        )
        snapshot = _finished(client, response.json()["job_id"])
    assert snapshot["state"] == "failed"
    assert "already running" in snapshot["error"]
    assert all(manifest.path.read_text() == "original" for manifest in manifests)
