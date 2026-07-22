"""Dependency Upgrader: discovery, uv + npm bump rules, rewrites, git, and API.

Pure engine parts run with fake resolved maps / npm-outdated dicts — no uv or
npm anywhere. Git tests use a real throwaway repo; API tests monkeypatch the
sync/outdated calls so scan never shells out, but let the real manifest-parse +
bump computation run against temp files.
"""

from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from toolkit_engine import depsync

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)

# --------------------------------------------------------------------------- #
# Samples                                                                      #
# --------------------------------------------------------------------------- #

SAMPLE_PYPROJECT = """\
[project]
name = "sample"
version = "0.1.0"
dependencies = [
    # web layer
    "fastapi>=0.100.0",
    "uvicorn>=0.30.0",
    "pinned==1.2.3",
    "compat~=2.0",
    "ranged>=1.0,<2.0",
    "markered>=1.0; python_version < '3.10'",
]

[project.optional-dependencies]
extra = ["mineru[core]>=3.4.0"]

[dependency-groups]
dev = ["pytest>=8.0.0"]
"""

RESOLVED = {
    "fastapi": "0.115.0",  # lagging -> bump
    "uvicorn": "0.30.0",  # equal -> leave
    "pinned": "9.9.9",  # == pin -> leave
    "compat": "2.5.0",  # ~= -> leave
    "ranged": "1.9.0",  # multi-clause -> leave
    "markered": "1.5.0",  # marker -> leave
    "mineru": "6.14.2",  # extras + lagging, major -> bump
    "pytest": "8.3.0",  # dev group lagging -> bump
}

SAMPLE_LOCK = """\
version = 1

[[package]]
name = "fastapi"
version = "0.115.0"

[[package]]
name = "mineru"
version = "6.14.2"

[[package]]
name = "pytest"
version = "8.3.0"

[[package]]
name = "uvicorn"
version = "0.30.0"
"""

PACKAGE_JSON = """\
{
  "name": "web",
  "version": "0.1.0",
  "dependencies": {
    "react": "^18.2.0",
    "exact-dep": "1.0.0",
    "tilde-dep": "~2.3.0",
    "floor-dep": ">=3.0.0",
    "wild": "1.x",
    "workspace-dep": "workspace:*"
  },
  "devDependencies": {
    "eslint": "^9.15.0"
  }
}
"""

# name -> latest publishable version (the merged map npm_latest yields)
LATEST = {
    "react": "19.1.0",
    "exact-dep": "1.4.2",
    "tilde-dep": "2.9.0",
    "floor-dep": "3.5.0",
    "wild": "1.9.0",
    "workspace-dep": "5.0.0",
    "eslint": "10.7.0",
    "not-declared": "9.9.9",
}


def _uv_project(root, pyproject=SAMPLE_PYPROJECT, lock=SAMPLE_LOCK):
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (root / "uv.lock").write_text(lock, encoding="utf-8")
    return root


def _npm_project(root, package=PACKAGE_JSON):
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(package, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Discovery                                                                    #
# --------------------------------------------------------------------------- #


def test_find_manifests_walks_subfolders_and_skips_heavy_dirs(tmp_path):
    _uv_project(tmp_path)  # root pyproject
    _uv_project(tmp_path / "backend")
    _npm_project(tmp_path / "frontend")
    # Noise that must be skipped:
    _npm_project(tmp_path / "node_modules" / "dep")  # dependency store
    _uv_project(tmp_path / ".venv")  # hidden dir
    _npm_project(tmp_path / "build")  # build output
    (tmp_path / "toolonly").mkdir()
    (tmp_path / "toolonly" / "pyproject.toml").write_text(
        "[tool.black]\nline-length = 88\n", encoding="utf-8"
    )  # not a uv project
    (tmp_path / "nodeps").mkdir()
    (tmp_path / "nodeps" / "package.json").write_text(
        '{"name": "x", "scripts": {}}', encoding="utf-8"
    )  # no dependency tables

    manifests, err = depsync.find_manifests(str(tmp_path))
    assert err is None
    rels = {(m.rel, m.kind) for m in manifests}
    assert rels == {
        ("pyproject.toml", "uv"),
        ("backend/pyproject.toml", "uv"),
        ("frontend/package.json", "npm"),
    }


def test_find_manifests_rejects_empty_and_relative(tmp_path):
    _, err = depsync.find_manifests("")
    assert err and "No folder given" in err
    _, err = depsync.find_manifests("relative/path")
    assert err and "absolute" in err
    _, err = depsync.find_manifests(str(tmp_path / "nope"))
    assert err and "Not a folder" in err


# --------------------------------------------------------------------------- #
# uv: which floors get bumped                                                  #
# --------------------------------------------------------------------------- #


def test_uv_bumps_only_lagging_ge_floors(tmp_path):
    path = _uv_project(tmp_path) / "pyproject.toml"
    by_name = {b.name: b for b in depsync.compute_uv_bumps(path, RESOLVED)}
    assert set(by_name) == {"fastapi", "mineru", "pytest"}
    assert by_name["fastapi"].new == ">=0.115.0"
    assert by_name["mineru"].table == "project.optional-dependencies.extra"
    assert by_name["mineru"].major is True
    assert by_name["pytest"].table == "dependency-groups.dev"


def test_uv_bumps_skip_equal_pinned_compat_range_and_marker(tmp_path):
    path = _uv_project(tmp_path) / "pyproject.toml"
    names = {b.name for b in depsync.compute_uv_bumps(path, RESOLVED)}
    assert names.isdisjoint({"uvicorn", "pinned", "compat", "ranged", "markered"})


def test_uv_local_version_does_not_spuriously_bump(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0.1.0"\ndependencies=["torch>=2.1.0"]\n',
        encoding="utf-8",
    )
    assert (
        depsync.compute_uv_bumps(tmp_path / "pyproject.toml", {"torch": "2.1.0+cpu"})
        == []
    )


def test_uv_local_version_bumps_to_public_only(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="x"\nversion="0.1.0"\ndependencies=["torch>=2.1.0"]\n',
        encoding="utf-8",
    )
    bumps = depsync.compute_uv_bumps(
        tmp_path / "pyproject.toml", {"torch": "2.2.0+cpu"}
    )
    assert (
        len(bumps) == 1 and bumps[0].new == ">=2.2.0" and "+cpu" not in bumps[0].raw_new
    )


def test_resolved_versions_drops_forked_packages(tmp_path):
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        '[[package]]\nname = "foo"\nversion = "1.5"\n'
        '[[package]]\nname = "foo"\nversion = "2.0"\n'
        '[[package]]\nname = "bar"\nversion = "3.0"\n',
        encoding="utf-8",
    )
    resolved, err = depsync.resolved_versions(str(tmp_path))
    assert err is None and "foo" not in resolved and resolved["bar"] == "3.0"


# --------------------------------------------------------------------------- #
# uv: rewrite safety                                                           #
# --------------------------------------------------------------------------- #


def test_apply_uv_bumps_rewrites_versions_and_preserves_the_rest(tmp_path):
    path = _uv_project(tmp_path) / "pyproject.toml"
    depsync.apply_uv_bumps(path, depsync.compute_uv_bumps(path, RESOLVED))
    text = path.read_text(encoding="utf-8")
    assert '"fastapi>=0.115.0"' in text
    assert '"mineru[core]>=6.14.2"' in text  # extras preserved
    assert '"pytest>=8.3.0"' in text
    assert "# web layer" in text  # comment preserved
    assert '"pinned==1.2.3"' in text and '"compat~=2.0"' in text  # untouched


def test_apply_uv_bumps_never_touches_comments_or_unscanned_tables(tmp_path):
    text = (
        "[build-system]\n"
        'requires = [\n    "hatchling>=1.0.0",\n]\n\n'
        "[project]\n"
        'name = "x"\nversion = "0.1.0"\n'
        "dependencies = [\n"
        '    # keep "hatchling>=1.0.0" in sync with the build backend\n'
        '    "hatchling>=1.0.0",\n'
        "]\n"
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    bumps = depsync.compute_uv_bumps(path, {"hatchling": "1.25.0"})
    assert [b.table for b in bumps] == ["project.dependencies"]
    depsync.apply_uv_bumps(path, bumps)
    out = path.read_text(encoding="utf-8")
    assert '    "hatchling>=1.25.0",\n]' in out  # dep bumped
    assert 'requires = [\n    "hatchling>=1.0.0",' in out  # build-system untouched
    assert '# keep "hatchling>=1.0.0" in sync' in out  # comment untouched


def test_apply_uv_bumps_rewrites_both_tables_across_quote_styles(tmp_path):
    text = (
        "[project]\nname='x'\nversion='0.1.0'\n"
        'dependencies = [\n    "click>=8.0.0",\n]\n'
        "[dependency-groups]\ndev = [\n    'click>=8.0.0',\n]\n"
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    bumps = depsync.compute_uv_bumps(path, {"click": "8.1.7"})
    assert len(bumps) == 2
    depsync.apply_uv_bumps(path, bumps)
    out = path.read_text(encoding="utf-8")
    assert '"click>=8.1.7",' in out and "'click>=8.1.7'," in out and "8.0.0" not in out


def test_apply_uv_bumps_raises_when_string_missing(tmp_path):
    path = _uv_project(tmp_path) / "pyproject.toml"
    ghost = depsync.Bump(
        "ghost", "project.dependencies", ">=1", ">=2", True, "ghost>=1", "ghost>=2"
    )
    with pytest.raises(ValueError, match="could not locate"):
        depsync.apply_uv_bumps(path, [ghost])


# --------------------------------------------------------------------------- #
# npm: bump rules                                                              #
# --------------------------------------------------------------------------- #


def test_npm_bumps_to_latest_preserving_operator(tmp_path):
    path = _npm_project(tmp_path) / "package.json"
    by_name = {b.name: b for b in depsync.compute_npm_bumps(path, LATEST)}
    assert set(by_name) == {"react", "exact-dep", "tilde-dep", "floor-dep", "eslint"}
    assert by_name["react"].new == "^19.1.0" and by_name["react"].major is True
    assert by_name["tilde-dep"].new == "~2.9.0"
    assert by_name["exact-dep"].new == "1.4.2"  # exact stays exact
    assert by_name["floor-dep"].new == ">=3.5.0"
    assert (
        by_name["eslint"].table == "devDependencies" and by_name["eslint"].major is True
    )


def test_npm_bumps_skip_complex_ranges_and_non_declared(tmp_path):
    path = _npm_project(tmp_path) / "package.json"
    names = {b.name for b in depsync.compute_npm_bumps(path, LATEST)}
    assert "wild" not in names  # "1.x"
    assert "workspace-dep" not in names  # "workspace:*"
    assert "not-declared" not in names  # not in package.json


def test_npm_bumps_skip_when_not_actually_newer(tmp_path):
    path = _npm_project(tmp_path) / "package.json"
    same = {"react": "18.2.0"}  # equal to declared floor base
    assert depsync.compute_npm_bumps(path, same) == []


def test_apply_npm_bumps_rewrites_ranges_and_leaves_others(tmp_path):
    path = _npm_project(tmp_path) / "package.json"
    depsync.apply_npm_bumps(path, depsync.compute_npm_bumps(path, LATEST))
    text = path.read_text(encoding="utf-8")
    assert '"react": "^19.1.0"' in text
    assert '"tilde-dep": "~2.9.0"' in text
    assert '"exact-dep": "1.4.2"' in text
    assert '"eslint": "^10.7.0"' in text
    assert '"wild": "1.x"' in text  # complex range untouched
    assert '"workspace-dep": "workspace:*"' in text  # untouched


def test_apply_npm_bumps_tolerates_spacing(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"^18.2.0"}}', encoding="utf-8"
    )
    path = tmp_path / "package.json"
    depsync.apply_npm_bumps(path, depsync.compute_npm_bumps(path, LATEST))
    assert '"react":"^19.1.0"' in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# git: subject + path-limited commit                                          #
# --------------------------------------------------------------------------- #


def test_commit_subject_labels_by_folder():
    def m(rel):
        from pathlib import Path

        return depsync.Manifest(Path("/x") / rel, "uv", rel)

    assert depsync.commit_subject(m("backend/pyproject.toml")) == (
        "chore(deps): update backend dependencies"
    )
    assert depsync.commit_subject(m("frontend/package.json")) == (
        "chore(deps): update frontend dependencies"
    )
    assert depsync.commit_subject(m("pyproject.toml")) == (
        "chore(deps): update project dependencies"
    )


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _init_repo(repo, track_lock=True):
    _uv_project(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A" if track_lock else "pyproject.toml")
    _git(repo, "commit", "-qm", "init")
    return repo


@requires_git
def test_commit_files_includes_untracked_lock_and_skips_unrelated(tmp_path):
    repo = _init_repo(tmp_path / "repo", track_lock=False)  # uv.lock untracked
    path = repo / "pyproject.toml"
    depsync.apply_uv_bumps(path, depsync.compute_uv_bumps(path, RESOLVED))
    (repo / "other.txt").write_text("wip\n", encoding="utf-8")
    _git(repo, "add", "other.txt")

    sha, err = depsync.commit_files(
        str(repo),
        "chore(deps): update project dependencies",
        ["pyproject.toml", "uv.lock"],
    )
    assert err is None and sha
    files = sorted(
        _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    )
    assert files == ["pyproject.toml", "uv.lock"]  # untracked lock included
    assert "A  other.txt" in _git(repo, "status", "--porcelain").stdout  # left staged


@requires_git
def test_commit_files_nothing_to_commit(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    sha, err = depsync.commit_files(str(repo), "msg", ["pyproject.toml", "uv.lock"])
    assert sha is None and err and "Nothing to commit" in err


# --------------------------------------------------------------------------- #
# Orchestration: apply_manifest                                               #
# --------------------------------------------------------------------------- #


@requires_git
def test_apply_manifest_uv_writes_and_commits(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    manifest = depsync.Manifest(repo / "pyproject.toml", "uv", "pyproject.toml")
    result = depsync.apply_manifest(manifest, commit=True)
    assert result["written"] == 3 and result["committed"] and result["commit_sha"]
    assert '"fastapi>=0.115.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        "chore(deps): update project dependencies"
        == _git(repo, "log", "-1", "--format=%s").stdout.strip()
    )


@requires_git
def test_apply_manifest_rolls_back_on_commit_failure(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    original = (repo / "pyproject.toml").read_text(encoding="utf-8")
    monkeypatch.setattr(depsync, "commit_files", lambda *a, **k: (None, "❌ boom"))
    manifest = depsync.Manifest(repo / "pyproject.toml", "uv", "pyproject.toml")
    result = depsync.apply_manifest(manifest, commit=True)
    assert result["error"] and "rolled back" in result["error"]
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == original  # restored


# --------------------------------------------------------------------------- #
# API: scan (syncs monkeypatched) + apply                                     #
# --------------------------------------------------------------------------- #


def _wait(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in {"done", "failed", "cancelled"}:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def _monorepo(root):
    _uv_project(root / "backend")
    _npm_project(root / "frontend")
    return root


def _fake_syncs(monkeypatch):
    monkeypatch.setattr(depsync, "run_uv_sync", lambda *a, **k: (True, "Resolved"))
    monkeypatch.setattr(
        depsync, "run_npm_install", lambda *a, **k: (True, "up to date")
    )
    monkeypatch.setattr(depsync, "npm_latest", lambda folder: (LATEST, None))


def test_scan_rejects_bad_folder_and_no_manifests(client, tmp_path):
    r = client.post("/api/deps/scan", json={"folder": str(tmp_path / "nope")})
    assert r.status_code == 400 and r.json()["detail"].startswith("❌")
    empty = tmp_path / "empty"
    empty.mkdir()
    r = client.post("/api/deps/scan", json={"folder": str(empty)})
    assert (
        r.status_code == 400
        and "No pyproject.toml or package.json" in r.json()["detail"]
    )


def test_scan_returns_per_manifest_bumps(client, tmp_path, monkeypatch):
    _monorepo(tmp_path)
    _fake_syncs(monkeypatch)
    r = client.post("/api/deps/scan", json={"folder": str(tmp_path)})
    assert r.status_code == 200
    snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "done"
    targets = {t["rel"]: t for t in snap["result"]["targets"]}
    assert set(targets) == {"backend/pyproject.toml", "frontend/package.json"}
    assert {b["name"] for b in targets["backend/pyproject.toml"]["bumps"]} == {
        "fastapi",
        "mineru",
        "pytest",
    }
    assert "react" in {b["name"] for b in targets["frontend/package.json"]["bumps"]}
    assert snap["result"]["total_bumps"] == 8  # 3 uv + 5 npm


def test_apply_rejects_empty_folder(client):
    r = client.post("/api/deps/apply", json={"folder": "", "commit": True})
    assert r.status_code == 400 and r.json()["detail"].startswith("❌")


@requires_git
def test_apply_upgrades_both_ecosystems(client, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _monorepo(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    # npm bits are faked so no real npm runs.
    monkeypatch.setattr(depsync, "npm_latest", lambda folder: (LATEST, None))
    monkeypatch.setattr(depsync, "npm_lock_refresh", lambda folder: (True, "ok"))

    r = client.post("/api/deps/apply", json={"folder": str(repo), "commit": True})
    assert r.status_code == 200
    body = r.json()
    assert body["written_total"] == 8 and body["committed_count"] == 2
    results = {x["rel"]: x for x in body["results"]}
    assert results["backend/pyproject.toml"]["committed"]
    assert results["frontend/package.json"]["committed"]
    assert '"react": "^19.1.0"' in (repo / "frontend" / "package.json").read_text(
        encoding="utf-8"
    )
    subjects = _git(repo, "log", "--format=%s", "-2").stdout
    assert "chore(deps): update backend dependencies" in subjects
    assert "chore(deps): update frontend dependencies" in subjects
