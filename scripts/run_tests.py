"""Run Toolkit's existing checks with private source, state, and reports."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from process_groups import stop_groups

ROOT = Path(__file__).resolve().parents[1]
SUITES = ("backend", "frontend")
PREPARE = "Run make dev to prepare dependencies, then retry make test."

# Each Vitest worker loads jsdom and the frontend modules. Leave memory for the
# host and Vitest's main process, and use a small default pool for this suite.
NODE_PREFLIGHT = """
const os = require('node:os');
const { engines } = require('./package.json');
const semver = require('node:module').createRequire(process.argv[1])('semver');
if (!semver.satisfies(process.version, engines.node)) {
  console.error(`Node ${engines.node} is required; found ${process.version}.`);
  process.exit(1);
}
const mib = 1024 ** 2;
const available = Math.min(os.totalmem(), process.availableMemory());
const memoryWorkers = Math.max(0, Math.floor((available / mib - 512) / 256));
console.log(Math.min(os.availableParallelism(), memoryWorkers));
"""


class TestInterruptedError(Exception):
    def __init__(self, signum):
        self.signum = signum


def interrupt(signum, _frame):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise TestInterruptedError(signum)


def run_step(label, command, cwd, env, log, capture=False):
    print(f"  {label}", flush=True)
    with log.open("a+b", buffering=0) as stream:
        stream.write(f"\n--- {label} ---\n".encode())
        output_start = stream.tell()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            status = process.wait()
        finally:
            try:
                stop_groups([process])
            except OSError as exc:
                raise RuntimeError(
                    f"Could not stop test process group {process.pid}: {exc}"
                ) from exc
        output = ""
        if capture or status:
            stream.seek(max(output_start, log.stat().st_size - 4096))
            output = stream.read().decode(errors="replace").strip()
        stream.write(f"\nExit status: {status}\n".encode())
    if status:
        if output:
            print(output, flush=True)
        print(f"  {label} failed (exit {status}).", flush=True)
    return status, output


def test_environment(work):
    paths = {name: work / name for name in ("home", "tmp", "cache", "config")}
    for path in paths.values():
        path.mkdir()
    # Do not import the user's shell settings, proxies, Git hooks, pytest options,
    # converter opt-in, or toolkit settings into the test processes.
    env = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
        if name in os.environ
    }
    env.update(
        HOME=str(paths["home"]),
        TMPDIR=str(paths["tmp"]),
        XDG_CACHE_HOME=str(paths["cache"]),
        XDG_CONFIG_HOME=str(paths["config"]),
        GIT_CONFIG_NOSYSTEM="1",
        NO_COLOR="1",
    )
    return env


def run_backend(work, env, log):
    project = ROOT / "backend"
    venv = project / ".venv"
    python = venv / "bin" / "python"
    ruff = venv / "bin" / "ruff"
    uv = shutil.which("uv")
    if not uv or not os.access(python, os.X_OK) or not os.access(ruff, os.X_OK):
        raise ValueError(f"Backend tools or dependencies are missing. {PREPARE}")

    env = dict(env, UV_PROJECT_ENVIRONMENT=str(venv))
    status, _ = run_step(
        "Check locked Python dependencies",
        [
            uv,
            "sync",
            "--locked",
            "--dev",
            "--inexact",
            "--no-install-project",
            "--check",
            "--offline",
            "--no-python-downloads",
            "--no-cache",
        ],
        project,
        env,
        log,
    )
    if status:
        raise ValueError(PREPARE)

    source = work / "backend"
    source.mkdir()
    for name in ("src", "tests"):
        shutil.copytree(
            project / name,
            source / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    shutil.copy2(project / "pyproject.toml", source)
    env.update(
        PYTHONPATH=str(source / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHON_DOTENV_DISABLED="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        SUB_DB_PATH=str(source / "data" / "sub.db"),
        RUFF_CACHE_DIR=str(work / "cache" / "ruff"),
    )
    print("  Pytest runs serially.", flush=True)
    steps = (
        (
            "Pytest",
            [
                python,
                "-m",
                "pytest",
                "-p",
                "anyio.pytest_plugin",
                "-q",
                "--basetemp",
                work / "pytest",
            ],
        ),
        ("Ruff lint", [ruff, "check", "src", "tests"]),
        ("Ruff formatting", [ruff, "format", "--check", "src", "tests"]),
    )
    return all([run_step(label, cmd, source, env, log)[0] == 0 for label, cmd in steps])


def check_frontend_lock(project):
    locked = json.loads((project / "package-lock.json").read_text())["packages"]
    node_modules = project / "node_modules"
    installed = json.loads((node_modules / ".package-lock.json").read_text())[
        "packages"
    ]
    for name in installed:
        actual = json.loads((project / name / "package.json").read_text())
        if locked.get(name, {}).get("version") != actual.get("version"):
            raise ValueError(f"{name} differs from package-lock.json. {PREPARE}")


def run_frontend(work, env, log, workers):
    project = ROOT / "frontend"
    node = shutil.which("node")
    npm = shutil.which("npm")
    if (
        not node
        or not npm
        or not (project / "node_modules" / ".bin" / "vitest").is_file()
    ):
        raise ValueError(f"Frontend tools or dependencies are missing. {PREPARE}")
    try:
        check_frontend_lock(project)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read frontend dependency state. {PREPARE}") from exc

    env = dict(
        env,
        npm_config_cache=str(work / "cache" / "npm"),
        npm_config_offline="true",
    )
    status, _ = run_step(
        "Check frontend dependencies",
        [npm, "ls", "--all", "--silent", "--include=dev", "--include=optional"],
        project,
        env,
        log,
    )
    if status:
        raise ValueError(PREPARE)

    source = work / "frontend"
    shutil.copytree(
        project,
        source,
        ignore=shutil.ignore_patterns(
            "node_modules",
            "dist",
            "dist-ssr",
            "coverage",
            ".env*",
            ".npmrc",
            "*.tsbuildinfo",
            "*.log",
            ".DS_Store",
        ),
    )
    (source / "node_modules").symlink_to(
        project / "node_modules", target_is_directory=True
    )
    status, output = run_step(
        "Check Node and available workers",
        [node, "-e", NODE_PREFLIGHT, str(Path(npm).resolve())],
        source,
        env,
        log,
        capture=True,
    )
    if status:
        raise ValueError(PREPARE)
    max_workers = int(output.rpartition("\n")[2])
    if not max_workers:
        raise ValueError(
            "Insufficient memory for one Vitest worker and the host reserve."
        )
    worker_count = min(4, max_workers) if workers == "auto" else max(1, int(workers))
    if worker_count > max_workers:
        raise ValueError(
            f"TEST_WORKERS={workers} exceeds the available "
            f"Vitest limit of {max_workers}."
        )
    print(f"  Vitest worker limit is {worker_count}.", flush=True)
    vitest = [
        npm,
        "run",
        "test",
        "--",
        "--maxWorkers",
        str(worker_count),
        "--no-cache",
        "--no-fs-module-cache",
        "--configLoader",
        "native",
    ]
    if worker_count == 1:
        vitest.extend(["--no-file-parallelism", "--maxConcurrency", "1"])

    steps = (
        ("TypeScript", [npm, "run", "typecheck"]),
        ("ESLint", [npm, "run", "lint"]),
        ("Vitest", vitest),
        (
            "Vite build",
            [
                npm,
                "run",
                "build",
                "--",
                "--configLoader",
                "native",
                "--outDir",
                work / "build",
                "--emptyOutDir",
            ],
        ),
    )
    return all([run_step(label, cmd, source, env, log)[0] == 0 for label, cmd in steps])


def main():
    suite = os.environ.get("TEST_SUITE", "all")
    workers = os.environ.get("TEST_WORKERS", "auto")
    if suite not in ("all", *SUITES):
        print("TEST_SUITE must be all, backend, or frontend.")
        return 2
    if workers != "auto" and not re.fullmatch(r"0|[1-9][0-9]*", workers):
        print("TEST_WORKERS must be auto, 0, or a positive integer.")
        return 2

    selected = SUITES if suite == "all" else (suite,)
    reports = Path(tempfile.mkdtemp(prefix="toolkit-test-"))
    print(f"Reports: {reports}", flush=True)
    if suite != "all":
        print(f"Partial run: {suite} only.", flush=True)
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    results = {}
    try:
        for name in selected:
            print(f"\n{name.capitalize()} checks", flush=True)
            log = reports / f"{name}.log"
            with tempfile.TemporaryDirectory(dir=reports, prefix=f"{name}-") as path:
                work = Path(path)
                env = test_environment(work)
                try:
                    if name == "backend":
                        results[name] = run_backend(work, env, log)
                    else:
                        results[name] = run_frontend(work, env, log, workers)
                except (OSError, ValueError) as exc:
                    with log.open("a") as report:
                        report.write(f"{exc}\n")
                    print(f"  {exc}", flush=True)
                    results[name] = False
    except TestInterruptedError as exc:
        print(f"\nTest run interrupted. Reports: {reports}", flush=True)
        return 128 + exc.signum
    except RuntimeError as exc:
        print(f"\n{exc} Reports: {reports}", flush=True)
        return 1

    print()
    for name, passed in results.items():
        result = "passed" if passed else "failed"
        print(f"{name}: {result} ({reports / f'{name}.log'})")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
