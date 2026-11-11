"""Prepare, run, and rebuild the local Toolkit development environment."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

from process_groups import stop_groups

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
ENV_FILE = BACKEND / ".env"
TEMPLATE = BACKEND / ".env.example"
PYTHON = BACKEND / ".venv/bin/python"
DEFAULT_DB = BACKEND / "data/sub.db"
LOOPBACK = "127.0.0.1"

# ports.free_port only uses the standard library. Check ports before uv sync.
sys.path.insert(0, str(BACKEND / "src"))
from toolkit_api.ports import free_port  # noqa: E402


@contextmanager
def checkout_lock():
    # Lock the checkout directory itself so no removable lock file is needed.
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "This checkout already has an active make dev or make init."
            ) from None
        yield
    finally:
        os.close(fd)


def run(args, *, cwd=ROOT, env=None, capture=False, quiet=False, check=True):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        check=check,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )


def preparation_options():
    for command in ("uv", "node", "npm"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} is required. See README.md for setup.")

    for path in (
        TEMPLATE,
        BACKEND / ".python-version",
        BACKEND / "pyproject.toml",
        BACKEND / "uv.lock",
        FRONTEND / "package.json",
        FRONTEND / "package-lock.json",
    ):
        if not path.is_file():
            raise RuntimeError(f"Missing setup input {path.relative_to(ROOT)}.")

    # npm ships semver. Check the manifest range before sync or reset, even
    # when frontend/node_modules hasn't been installed yet.
    npm = Path(shutil.which("npm")).resolve()
    run(
        [
            "node",
            "-e",
            """
const [npm, manifest] = process.argv.slice(1)
const range = require(manifest).engines.node
const semver = require('node:module').createRequire(npm)('semver')
if (!semver.satisfies(process.version, range)) {
  console.error(`Node ${process.version} does not satisfy ${range}. See README.md.`)
  process.exit(1)
}
""",
            str(npm),
            str(FRONTEND / "package.json"),
        ],
    )
    pin = (BACKEND / ".python-version").read_text().strip()
    extras = os.environ["EXTRAS"].split()
    if set(extras) - {"docmd"}:
        raise RuntimeError("EXTRAS supports docmd or an empty value.")
    # Read the existing lock and project metadata before allowing a reset.
    run(["uv", "lock", "--check", "--offline"], cwd=BACKEND, env=uv_environment())
    return pin, extras


def uv_environment():
    env = os.environ.copy()
    for name in (
        "UV_CONFIG_FILE",
        "UV_FROZEN",
        "UV_NO_DEV",
        "UV_NO_EDITABLE",
        "UV_PROJECT",
        "UV_PYTHON",
        "UV_WORKING_DIRECTORY",
        "VIRTUAL_ENV",
    ):
        env.pop(name, None)
    env["UV_PROJECT_ENVIRONMENT"] = str(BACKEND / ".venv")
    return env


def require_local_path(path, *, directory=False):
    """Reject symlink traversal or a file shared with another directory."""
    relative = path.relative_to(ROOT)
    for parent in relative.parents:
        if (ROOT / parent).is_symlink():
            raise RuntimeError(f"{parent} is a symlink. Use checkout-owned paths.")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    valid = (
        stat.S_ISDIR(info.st_mode)
        if directory
        else (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    )
    if not valid:
        kind = "directory" if directory else "file"
        raise RuntimeError(f"{relative} is not a checkout-owned {kind}.")


def prepare(pin, extras):
    for directory in (BACKEND / ".venv", FRONTEND / "node_modules"):
        require_local_path(directory, directory=True)
    require_local_path(ENV_FILE)
    if not ENV_FILE.exists():
        with TEMPLATE.open("rb") as template, ENV_FILE.open("xb") as config:
            shutil.copyfileobj(template, config)

    if not extras:
        print(
            "MinerU is omitted. Set TOOLKIT_DISABLED_TOOLS=doc-to-markdown "
            "in backend/.env to hide its tool."
        )
    sync = ["uv", "sync", "--locked", "--dev", "--managed-python", "--python", pin]
    for extra in extras:
        sync.extend(["--extra", extra])
    uv_env = uv_environment()
    if run(
        [*sync, "--check"], cwd=BACKEND, env=uv_env, quiet=True, check=False
    ).returncode:
        run(sync, cwd=BACKEND, env=uv_env)
    else:
        print("Backend dependencies are up to date.")

    fingerprint = hashlib.sha256()
    for name in ("package.json", "package-lock.json", ".npmrc", "../.npmrc"):
        path = FRONTEND / name
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            fingerprint.update(f"{digest}  {name}\n".encode())
    for command in (
        [
            "node",
            "-p",
            "JSON.stringify([process.version, process.platform, process.arch, "
            "process.versions.modules])",
        ],
        ["npm", "--version"],
    ):
        fingerprint.update(run(command, cwd=FRONTEND, capture=True).stdout.encode())

    modules = FRONTEND / "node_modules"
    marker = modules / ".toolkit-install-state"
    key = fingerprint.hexdigest()
    if (
        marker.is_file()
        and marker.read_text().strip() == key
        and all(os.access(modules / ".bin" / tool, os.X_OK) for tool in ("vite", "tsc"))
        and not run(
            ["npm", "ls", "--all", "--silent", "--include=dev", "--include=optional"],
            cwd=FRONTEND,
            quiet=True,
            check=False,
        ).returncode
        and not run(
            [
                "node",
                "-e",
                """
const fs = require('node:fs')
const lock = JSON.parse(fs.readFileSync('node_modules/.package-lock.json'))
const complete = Object.keys(lock.packages).every(
  path => !path || fs.existsSync(path + '/package.json')
)
process.exit(complete ? 0 : 1)
""",
            ],
            cwd=FRONTEND,
            quiet=True,
            check=False,
        ).returncode
    ):
        print("Frontend dependencies are up to date.")
    else:
        run(
            ["npm", "ci", "--include=dev", "--include=optional", "--engine-strict"],
            cwd=FRONTEND,
        )
        marker.write_text(key + "\n")


def port_from_env(name):
    try:
        port = int(os.environ.get(name, ""))
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    raise RuntimeError(f"{name} must be a port number from 1 to 65535.")


def check_port(name, port):
    try:
        free_port(LOOPBACK, port, tries=1)
    except SystemExit as exc:
        raise RuntimeError(
            f"{LOOPBACK}:{port} is unavailable. Choose another {name}."
        ) from exc


def check_idle():
    """Find manually launched Toolkit processes, including custom-port hosts."""
    lsof = shutil.which("lsof")
    if not lsof:
        raise RuntimeError(
            "lsof is required to check that Toolkit is stopped before reset."
        )
    result = run(
        [lsof, "-nP", "-u", str(os.getuid()), "-F0pcfn"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not check running processes with lsof.")
    pid, command, descriptor = "", "", ""
    for item in result.stdout.replace("\0\n", "\0").split("\0"):
        if item.startswith("p"):
            pid = item[1:]
        elif item.startswith("c"):
            command = item[1:]
        elif item.startswith("f"):
            descriptor = item[1:]
        elif item.startswith("n") and pid != str(os.getpid()):
            path = Path(item[1:])
            if path != ROOT and ROOT not in path.parents:
                continue
            app = command.lower().startswith(("python", "node", "npm", "uv", "vite"))
            database = path.name.endswith((".db", ".db-wal", ".db-shm", ".db-journal"))
            if (app and descriptor in {"cwd", "txt"}) or database:
                raise RuntimeError(
                    f"Stop {command} (PID {pid}) before resetting this checkout."
                )


def development_env(frontend_port):
    env = os.environ.copy()
    env.pop("SUB_DB_PATH", None)
    settings = run(
        [
            PYTHON,
            "-I",
            "-c",
            "import json; from dotenv import dotenv_values; "
            "settings = dotenv_values('.env'); "
            "print(json.dumps({k: settings.get(k) "
            "for k in ('SUB_DB_PATH', 'APP_CORS_ORIGINS')}))",
        ],
        cwd=BACKEND,
        env=env,
        capture=True,
    )
    config = json.loads(settings.stdout)
    raw_path = config.get("SUB_DB_PATH") or str(DEFAULT_DB)
    # Resolve relative paths from backend/, matching the native launcher.
    database = Path(os.path.abspath(BACKEND / raw_path))
    if (
        raw_path == ":memory:"
        or not database.is_relative_to(ROOT)
        or any(part.casefold() == ".git" for part in database.relative_to(ROOT).parts)
    ):
        raise RuntimeError(
            "backend/.env SUB_DB_PATH must name a SQLite file inside the checkout. "
            "See README.md for native hosting with an external data path."
        )
    check_databases(database)
    env["SUB_DB_PATH"] = str(database)
    origins = [f"http://{host}:{frontend_port}" for host in (LOOPBACK, "localhost")]
    custom_origins = (config.get("APP_CORS_ORIGINS") or "").split(",")
    env["APP_CORS_ORIGINS"] = ",".join(
        dict.fromkeys([*origins, *filter(None, map(str.strip, custom_origins))])
    )
    return env


def check_databases(database):
    require_local_path(database.parent, directory=True)
    for path in {database, database.with_name("torrents.db")}:
        require_local_path(path)
        for suffix in ("-wal", "-shm", "-journal"):
            require_local_path(path.with_name(path.name + suffix))
        if path.exists() and path.stat().st_size:
            try:
                with closing(
                    sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
                ) as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    f"Cannot read SQLite data in {path.relative_to(ROOT)}: {exc}"
                ) from exc
            if version != 0:
                raise RuntimeError(
                    f"Unsupported SQLite schema version in {path.relative_to(ROOT)}."
                )
    for name in ("bitcomet-device-id", "bitcomet-devices.json"):
        require_local_path(database.with_name(name))


def initialize_databases():
    check_databases(DEFAULT_DB)
    run(
        [
            PYTHON,
            "-I",
            "-c",
            "import sys; from pathlib import Path; from subgen.db import Store; "
            "from toolkit_api.devices import DeviceBook; "
            "db = Path(sys.argv[1]); Store(db).close(); "
            "DeviceBook(db.with_name('torrents.db'))",
            DEFAULT_DB,
        ],
        cwd=BACKEND,
    )


@contextmanager
def services():
    children = {}

    def start(name, args, cwd, env):
        children[name] = subprocess.Popen(
            args, cwd=cwd, env=env, start_new_session=True
        )

    try:
        yield children, start
    finally:
        stop_groups(children.values(), grace=8)


def check_services(children):
    for name, process in children.items():
        if process.poll() is not None:
            raise RuntimeError(
                f"{name} exited unexpectedly (status {process.returncode})."
            )


def wait_ready(children, port, path, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_services(children)
        with closing(
            http.client.HTTPConnection(LOOPBACK, port, timeout=1)
        ) as connection:
            try:
                connection.request("GET", path)
                if connection.getresponse().status == 200:
                    check_services(children)
                    return
            except (OSError, http.client.HTTPException):
                pass
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {LOOPBACK}:{port}{path}.")


def dev():
    api_port = port_from_env("PORT")
    frontend_port = port_from_env("FRONTEND_PORT")
    if api_port == frontend_port:
        raise RuntimeError("PORT and FRONTEND_PORT must be different.")
    check_port("PORT", api_port)
    check_port("FRONTEND_PORT", frontend_port)
    prepare(*preparation_options())
    env = development_env(frontend_port)
    # Recheck after dependency preparation, which can take a while on first use.
    check_port("PORT", api_port)
    check_port("FRONTEND_PORT", frontend_port)
    with services() as (children, start):
        start(
            "FastAPI",
            [
                PYTHON,
                "-I",
                "-m",
                "uvicorn",
                "toolkit_api.main:app",
                "--reload",
                "--host",
                LOOPBACK,
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            BACKEND,
            env,
        )
        wait_ready(children, api_port, "/api/health")
        start(
            "Vite",
            [
                "npm",
                "run",
                "dev",
                "--",
                "--port",
                str(frontend_port),
                "--strictPort",
                "--logLevel",
                "warn",
            ],
            FRONTEND,
            {**env, "API_PORT": str(api_port)},
        )
        wait_ready(children, frontend_port, "/")
        wait_ready(children, frontend_port, "/api/health")
        print(f"Toolkit UI  http://{LOOPBACK}:{frontend_port}", flush=True)
        print(f"Toolkit API http://{LOOPBACK}:{api_port}/docs", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        while True:
            time.sleep(1)
            # The reload supervisor can stay alive after its API child exits.
            # Allow a normal reload to finish before failing the whole session.
            wait_ready(children, frontend_port, "/api/health", timeout=15)


def init():
    from reset_local import reset_checkout

    check_idle()
    options = preparation_options()

    def ready_to_reset():
        pin, _ = options
        run(["uv", "python", "install", pin], cwd=BACKEND, env=uv_environment())
        check_idle()

    if reset_checkout(ROOT, ready_to_reset):
        prepare(*options)
        initialize_databases()
        print("Local environment rebuilt. Run make dev to start Toolkit.")


def interrupt(_signum, _frame):
    # Let the first signal unwind through owned-process cleanup.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"dev", "init"}:
        print("Use make dev or make init.", file=sys.stderr)
        return 2
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    command = sys.argv[1]
    try:
        with checkout_lock():
            if command == "dev":
                dev()
            else:
                init()
    except KeyboardInterrupt:
        print(f"make {command} interrupted.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"make {command}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
