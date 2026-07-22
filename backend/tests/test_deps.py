"""Dependency Upgrader: engine rules, in-place rewrite, git commit, and API.

The engine's pure parts (compute/apply/message) run with a fake resolved map —
no `uv` anywhere. The git tests use a real throwaway repo; the API tests
monkeypatch `run_uv_sync`/`uv_available` so scan never shells out to uv, but let
the real lock-parse + bump computation run against temp files.
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

# What `uv sync -U` "resolved" — the versions the tool bumps floors up to.
RESOLVED = {
    "fastapi": "0.115.0",  # lagging >= floor -> bump
    "uvicorn": "0.30.0",  # equals floor -> leave
    "pinned": "9.9.9",  # == pin -> leave
    "compat": "2.5.0",  # ~= -> leave
    "ranged": "1.9.0",  # multi-clause range -> leave
    "markered": "1.5.0",  # env marker -> leave
    "mineru": "6.14.2",  # extras + lagging, major jump -> bump
    "pytest": "8.3.0",  # dev group lagging -> bump
}

SAMPLE_LOCK = """\
version = 1
requires-python = ">=3.14"

[[package]]
name = "fastapi"
version = "0.115.0"

[[package]]
name = "uvicorn"
version = "0.30.0"

[[package]]
name = "pinned"
version = "9.9.9"

[[package]]
name = "compat"
version = "2.5.0"

[[package]]
name = "ranged"
version = "1.9.0"

[[package]]
name = "markered"
version = "1.5.0"

[[package]]
name = "mineru"
version = "6.14.2"

[[package]]
name = "pytest"
version = "8.3.0"

[[package]]
name = "sample"
source = { virtual = "." }
"""


def _write_project(root, pyproject=SAMPLE_PYPROJECT, lock=SAMPLE_LOCK):
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (root / "uv.lock").write_text(lock, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Engine: which floors get bumped                                             #
# --------------------------------------------------------------------------- #


def test_compute_bumps_only_touches_lagging_ge_floors(tmp_path):
    _write_project(tmp_path)
    bumps = depsync.compute_bumps(tmp_path / "pyproject.toml", RESOLVED)
    by_name = {b.name: b for b in bumps}
    # Exactly the three lagging >= floors across all three tables.
    assert set(by_name) == {"fastapi", "mineru", "pytest"}
    assert by_name["fastapi"].old == ">=0.100.0"
    assert by_name["fastapi"].new == ">=0.115.0"
    assert by_name["mineru"].table == "project.optional-dependencies.extra"
    assert by_name["pytest"].table == "dependency-groups.dev"


def test_compute_bumps_flags_major_version_jumps(tmp_path):
    _write_project(tmp_path)
    by_name = {
        b.name: b for b in depsync.compute_bumps(tmp_path / "pyproject.toml", RESOLVED)
    }
    assert by_name["mineru"].major is True  # 3.x -> 6.x
    assert by_name["fastapi"].major is False  # 0.100 -> 0.115, major stays 0
    assert by_name["pytest"].major is False  # 8.x -> 8.x


def test_compute_bumps_skips_equal_pinned_compat_range_and_marker(tmp_path):
    _write_project(tmp_path)
    names = {
        b.name for b in depsync.compute_bumps(tmp_path / "pyproject.toml", RESOLVED)
    }
    assert "uvicorn" not in names  # resolved == floor
    assert "pinned" not in names  # == pin
    assert "compat" not in names  # ~=
    assert "ranged" not in names  # multi-clause
    assert "markered" not in names  # environment marker


def test_compute_bumps_empty_when_everything_current(tmp_path):
    # Resolved map exactly equal to the declared floors -> nothing lags.
    current = {
        "fastapi": "0.100.0",
        "uvicorn": "0.30.0",
        "mineru": "3.4.0",
        "pytest": "8.0.0",
    }
    _write_project(tmp_path)
    assert depsync.compute_bumps(tmp_path / "pyproject.toml", current) == []


# --------------------------------------------------------------------------- #
# Engine: in-place rewrite preserves everything else                          #
# --------------------------------------------------------------------------- #


def test_apply_bumps_rewrites_only_the_versions(tmp_path):
    path = _write_project(tmp_path) / "pyproject.toml"
    bumps = depsync.compute_bumps(path, RESOLVED)
    depsync.apply_bumps(path, bumps)
    text = path.read_text(encoding="utf-8")

    assert '"fastapi>=0.115.0"' in text
    assert '"mineru[core]>=6.14.2"' in text  # extras marker preserved
    assert '"pytest>=8.3.0"' in text


def test_apply_bumps_leaves_comments_and_untouched_deps_verbatim(tmp_path):
    path = _write_project(tmp_path) / "pyproject.toml"
    depsync.apply_bumps(path, depsync.compute_bumps(path, RESOLVED))
    text = path.read_text(encoding="utf-8")

    assert "# web layer" in text  # comment survived
    assert '"uvicorn>=0.30.0"' in text  # up-to-date floor untouched
    assert '"pinned==1.2.3"' in text  # == untouched
    assert '"compat~=2.0"' in text  # ~= untouched
    assert '"ranged>=1.0,<2.0"' in text  # range untouched
    assert "\"markered>=1.0; python_version < '3.10'\"" in text  # marker untouched


def test_apply_bumps_raises_if_a_string_cannot_be_located(tmp_path):
    path = _write_project(tmp_path) / "pyproject.toml"
    ghost = depsync.Bump(
        name="ghost",
        table="project.dependencies",
        old=">=1.0",
        new=">=2.0",
        major=True,
        raw="ghost>=1.0",
        raw_new="ghost>=2.0",
    )
    with pytest.raises(ValueError, match="could not locate"):
        depsync.apply_bumps(path, [ghost])


# --------------------------------------------------------------------------- #
# Engine: lock parsing, discovery, commit message                            #
# --------------------------------------------------------------------------- #


def test_resolved_versions_reads_lock_and_skips_versionless(tmp_path):
    _write_project(tmp_path)
    resolved, err = depsync.resolved_versions(str(tmp_path))
    assert err is None
    assert resolved["fastapi"] == "0.115.0"
    assert "sample" not in resolved  # editable root has no version


def test_resolved_versions_missing_lock_is_an_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text(SAMPLE_PYPROJECT, encoding="utf-8")
    resolved, err = depsync.resolved_versions(str(tmp_path))
    assert resolved == {}
    assert err is not None and err.startswith("❌")


def test_find_pyproject_errors(tmp_path):
    _, err = depsync.find_pyproject(str(tmp_path / "nope"))
    assert err and "Not a folder" in err
    _, err = depsync.find_pyproject(str(tmp_path))
    assert err and "No pyproject.toml" in err


def test_build_commit_message_lists_bumps_and_tags_majors(tmp_path):
    path = _write_project(tmp_path) / "pyproject.toml"
    msg = depsync.build_commit_message(depsync.compute_bumps(path, RESOLVED))
    assert msg.startswith("chore(deps): bump 3 dependency floors to installed versions")
    assert "(major)" in msg  # mineru 3->6
    assert "fastapi" in msg and ">=0.115.0" in msg


# --------------------------------------------------------------------------- #
# Engine: git commit stages only the two files                                #
# --------------------------------------------------------------------------- #


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def _init_repo(repo, track_lock=True):
    _write_project(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    # track_lock=False leaves uv.lock untracked, mirroring a real scan that just
    # created it via `uv sync` before the project had ever committed a lock.
    _git(repo, "add", "-A" if track_lock else "pyproject.toml")
    _git(repo, "commit", "-qm", "init")
    return repo


@requires_git
def test_is_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert depsync.is_git_repo(str(plain)) is False
    repo = _init_repo(tmp_path / "repo")
    assert depsync.is_git_repo(str(repo)) is True


@requires_git
def test_commit_bumps_stages_only_the_two_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = repo / "pyproject.toml"
    bumps = depsync.compute_bumps(path, RESOLVED)
    depsync.apply_bumps(path, bumps)

    # Unrelated work that must NOT be swept into the commit.
    (repo / "untracked.txt").write_text("wip\n", encoding="utf-8")

    sha, err = depsync.commit_bumps(str(repo), bumps)
    assert err is None
    assert sha  # short hash returned

    files = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert files == ["pyproject.toml"]  # uv.lock unchanged here, so only this
    # The unrelated file is still sitting untracked, never committed.
    assert "?? untracked.txt" in _git(repo, "status", "--porcelain").stdout


@requires_git
def test_commit_bumps_includes_an_untracked_lock(tmp_path):
    # A lock uv just created (never committed) must still land in the commit —
    # `git commit -- uv.lock` alone would fail on the untracked pathspec.
    repo = _init_repo(tmp_path / "repo", track_lock=False)
    path = repo / "pyproject.toml"
    bumps = depsync.compute_bumps(path, RESOLVED)
    depsync.apply_bumps(path, bumps)

    sha, err = depsync.commit_bumps(str(repo), bumps)
    assert err is None and sha
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert sorted(files) == ["pyproject.toml", "uv.lock"]


@requires_git
def test_commit_bumps_leaves_pre_staged_work_out(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    path = repo / "pyproject.toml"
    bumps = depsync.compute_bumps(path, RESOLVED)
    depsync.apply_bumps(path, bumps)

    # An unrelated file the user has already `git add`ed must NOT be swept in.
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "other.py")

    sha, err = depsync.commit_bumps(str(repo), bumps)
    assert err is None and sha
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert files == ["pyproject.toml"]  # uv.lock unchanged here
    assert "A  other.py" in _git(repo, "status", "--porcelain").stdout  # still staged


@requires_git
def test_commit_bumps_reports_nothing_to_commit(tmp_path):
    repo = _init_repo(tmp_path / "repo")  # clean tree, no edits applied
    sha, err = depsync.commit_bumps(str(repo), [])
    assert sha is None
    assert err and "Nothing to commit" in err


# --------------------------------------------------------------------------- #
# API: scan (uv monkeypatched) + apply                                        #
# --------------------------------------------------------------------------- #


def _wait(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in {"done", "failed", "cancelled"}:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_scan_rejects_missing_folder_and_missing_pyproject(client, tmp_path):
    r = client.post("/api/deps/scan", json={"folder": str(tmp_path / "nope")})
    assert r.status_code == 400 and r.json()["detail"].startswith("❌")

    empty = tmp_path / "empty"
    empty.mkdir()
    r = client.post("/api/deps/scan", json={"folder": str(empty)})
    assert r.status_code == 400 and "No pyproject.toml" in r.json()["detail"]


def test_scan_rejects_when_uv_missing(client, tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr(depsync, "uv_available", lambda: False)
    r = client.post("/api/deps/scan", json={"folder": str(tmp_path)})
    assert r.status_code == 400 and "uv is not installed" in r.json()["detail"]


def test_scan_returns_proposed_bumps(client, tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr(depsync, "uv_available", lambda: True)
    monkeypatch.setattr(
        depsync, "run_uv_sync", lambda *a, **k: (True, "Resolved 9 packages")
    )
    r = client.post("/api/deps/scan", json={"folder": str(tmp_path)})
    assert r.status_code == 200
    snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "done"
    result = snap["result"]
    assert result["count"] == 3
    assert {b["name"] for b in result["bumps"]} == {"fastapi", "mineru", "pytest"}


def test_scan_surfaces_uv_failure_as_failed_job(client, tmp_path, monkeypatch):
    _write_project(tmp_path)
    monkeypatch.setattr(depsync, "uv_available", lambda: True)
    monkeypatch.setattr(
        depsync, "run_uv_sync", lambda *a, **k: (False, "error: no solution found")
    )
    r = client.post("/api/deps/scan", json={"folder": str(tmp_path)})
    snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "failed"
    assert "uv sync -U failed" in snap["error"]


@requires_git
def test_apply_writes_and_commits(client, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    r = client.post("/api/deps/apply", json={"folder": str(repo), "commit": True})
    assert r.status_code == 200
    body = r.json()
    assert body["written"] == 3
    assert body["committed"] is True
    assert body["commit_sha"]
    assert '"fastapi>=0.115.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert "chore(deps)" in _git(repo, "log", "-1", "--format=%s").stdout


@requires_git
def test_apply_without_commit_writes_only(client, tmp_path):
    repo = _init_repo(tmp_path / "repo")
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    r = client.post("/api/deps/apply", json={"folder": str(repo), "commit": False})
    body = r.json()
    assert body["written"] == 3 and body["committed"] is False
    assert '"pytest>=8.3.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before  # no new commit


def test_apply_on_non_git_folder_refuses_before_writing(client, tmp_path):
    root = _write_project(tmp_path / "plain")
    original = (root / "pyproject.toml").read_text(encoding="utf-8")
    r = client.post("/api/deps/apply", json={"folder": str(root), "commit": True})
    assert r.status_code == 400 and "Not a git repository" in r.json()["detail"]
    assert (root / "pyproject.toml").read_text(
        encoding="utf-8"
    ) == original  # untouched


def test_apply_reports_nothing_to_bump(client, tmp_path):
    # Lock versions equal to the floors -> nothing lags.
    lock = (
        SAMPLE_LOCK.replace("0.115.0", "0.100.0")
        .replace("6.14.2", "3.4.0")
        .replace("8.3.0", "8.0.0")
    )
    root = _write_project(tmp_path / "current", lock=lock)
    r = client.post("/api/deps/apply", json={"folder": str(root), "commit": False})
    body = r.json()
    assert body["written"] == 0 and body["committed"] is False
    assert "Nothing to bump" in body["note"]


def test_apply_rejects_empty_folder(client):
    # An empty/relative folder must never resolve to the server's own CWD.
    r = client.post("/api/deps/apply", json={"folder": "", "commit": True})
    assert r.status_code == 400 and r.json()["detail"].startswith("❌")


@requires_git
def test_apply_rolls_back_pyproject_when_commit_fails(client, tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    original = (repo / "pyproject.toml").read_text(encoding="utf-8")
    monkeypatch.setattr(
        depsync, "commit_bumps", lambda *a, **k: (None, "❌ git commit failed: boom")
    )
    r = client.post("/api/deps/apply", json={"folder": str(repo), "commit": True})
    assert r.status_code == 500 and "left unchanged" in r.json()["detail"]
    # Rolled back: the file is byte-identical to before, so a retry re-bumps.
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- #
# Engine edge cases surfaced by adversarial review                            #
# --------------------------------------------------------------------------- #


def _single_dep_project(root, dep):
    root.mkdir(parents=True, exist_ok=True)
    body = (
        f'[project]\nname = "x"\nversion = "0.1.0"\ndependencies = [\n    "{dep}",\n]\n'
    )
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    return root / "pyproject.toml"


def test_local_version_does_not_spuriously_bump(tmp_path):
    # torch 2.1.0 resolved to a local build 2.1.0+cpu: public version unchanged,
    # so no bump — and never a ">=2.1.0+cpu" (an invalid PEP 440 specifier).
    path = _single_dep_project(tmp_path, "torch>=2.1.0")
    assert depsync.compute_bumps(path, {"torch": "2.1.0+cpu"}) == []


def test_local_version_bumps_to_public_only(tmp_path):
    path = _single_dep_project(tmp_path, "torch>=2.1.0")
    bumps = depsync.compute_bumps(path, {"torch": "2.2.0+cpu"})
    assert len(bumps) == 1
    assert bumps[0].new == ">=2.2.0"  # local "+cpu" segment stripped
    assert "+cpu" not in bumps[0].raw_new


def test_resolved_versions_drops_forked_packages(tmp_path):
    # A forked resolution lists the same name at two versions — ambiguous, drop it.
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        '[[package]]\nname = "foo"\nversion = "1.5"\n'
        '[[package]]\nname = "foo"\nversion = "2.0"\n'
        '[[package]]\nname = "bar"\nversion = "3.0"\n',
        encoding="utf-8",
    )
    resolved, err = depsync.resolved_versions(str(tmp_path))
    assert err is None
    assert "foo" not in resolved  # ambiguous fork left alone
    assert resolved["bar"] == "3.0"


def test_apply_bumps_never_touches_comments_or_unscanned_tables(tmp_path):
    text = (
        "[build-system]\n"
        "requires = [\n"
        '    "hatchling>=1.0.0",\n'  # unscanned table, byte-identical string
        "]\n\n"
        "[project]\n"
        'name = "x"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '    # keep "hatchling>=1.0.0" in sync with the build backend\n'  # comment
        '    "hatchling>=1.0.0",\n'
        "]\n"
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    bumps = depsync.compute_bumps(path, {"hatchling": "1.25.0"})
    assert [b.table for b in bumps] == ["project.dependencies"]  # only the dep

    depsync.apply_bumps(path, bumps)
    out = path.read_text(encoding="utf-8")
    assert '    "hatchling>=1.25.0",\n]' in out  # the project dep bumped
    assert 'requires = [\n    "hatchling>=1.0.0",' in out  # build-system untouched
    assert '# keep "hatchling>=1.0.0" in sync' in out  # comment untouched


def test_apply_bumps_rewrites_both_tables_across_quote_styles(tmp_path):
    text = (
        "[project]\n"
        'name = "x"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '    "click>=8.0.0",\n'  # double-quoted
        "]\n"
        "[dependency-groups]\n"
        "dev = [\n"
        "    'click>=8.0.0',\n"  # same package, single-quoted
        "]\n"
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    bumps = depsync.compute_bumps(path, {"click": "8.1.7"})
    assert len(bumps) == 2  # one per table

    depsync.apply_bumps(path, bumps)
    out = path.read_text(encoding="utf-8")
    assert '"click>=8.1.7",' in out  # double-quoted rewritten
    assert "'click>=8.1.7'," in out  # single-quoted rewritten too
    assert "8.0.0" not in out  # nothing stale left behind


def test_find_pyproject_rejects_empty_and_relative(tmp_path):
    _, err = depsync.find_pyproject("")
    assert err and "No folder given" in err
    _, err = depsync.find_pyproject("relative/path")
    assert err and "absolute" in err
