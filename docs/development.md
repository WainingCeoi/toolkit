# Development

## Commands

Run these commands from the project root after installing the
[prerequisites](getting-started.md).

| Command | When to use it |
| --- | --- |
| `make` or `make help` | Show the commands and options. |
| `make dev` | Prepare dependencies and start both services with automatic reload. |
| `make init` | Preview and confirm a destructive local reset, rebuild the environment, and exit. |
| `make test` | Run both test suites, code checks, and the frontend build. |

Use `make dev PORT=9000 FRONTEND_PORT=5174` to change either port. Both services
bind to loopback. If a requested port is occupied, choose another port or stop
the process using it. Vite's API proxy and the backend's development CORS origins
follow these settings. Use `EXTRAS=` with `make dev` or `make init` to omit MinerU.

With the API running, preview a frontend build locally by running
`pnpm run build` and then `pnpm run preview` from `frontend/`.
Set `API_PORT` on the preview command if the API uses a port other than 8000.

## Local reset

Run `make init` only when you want to reset this checkout. The preview uses
Git's ignore rules, including local and global exclusions. It can include
`backend/.env`, SQLite data and saved device settings, dependencies, build output,
caches, ignored local notes, and editor settings. Make a choice for any nested
repository in the preview, then type `reset` to approve the listed deletions.
The command rebuilds the dependencies, backend configuration, and local SQLite
stores.

## Checks

Run `make dev` and stop it with **Ctrl-C** to prepare a fresh checkout for testing.
For setup without launching services, run `(cd backend && uv sync --locked --dev)`
and `(cd frontend && pnpm install --frozen-lockfile)` from the project root.
`make test` prints its temporary run directory, which contains `backend.log` and
`frontend.log` for the selected suites, including the frontend build results.
Use `TEST_SUITE=backend` or `TEST_SUITE=frontend` for one suite and its checks.
`TEST_WORKERS=auto` sizes the Vitest pool using available CPU and memory. A
positive integer sets the worker limit, and `TEST_WORKERS=0` runs serially.
Pytest always runs serially.

To also exercise installed converters with temporary inputs, run the following
command from `backend/`.

```bash
TOOLKIT_TEST_EXTERNAL=1 uv run --no-sync pytest -q tests/test_mcp_e2e.py
```

Each integration test skips if its converter is unavailable. The web capture
test requires Google Chrome and a matching `chromedriver` on `PATH`. It uses a
temporary Chrome profile and a local fixture.

[GitHub Actions](../.github/workflows/ci.yml) runs `make test` on pull requests,
pushes to `main`, and manual runs from the repository's Actions tab.
