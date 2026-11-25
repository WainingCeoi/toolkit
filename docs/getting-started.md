# Getting started

Install the prerequisites before the first run.

- macOS Command Line Tools (`xcode-select --install`) for Git, Make, and `python3`.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs and manages Python for Toolkit.
- [Node.js](https://nodejs.org/) 22.22.2+ within 22.x, 24.15.0+ within 24.x, or 26+.
- [pnpm](https://pnpm.io/installation) 12.23+ within 12.x, available on `PATH` (for example, `brew install pnpm`).

uv manages Toolkit's Python runtime using the pin in
[backend/.python-version](../backend/.python-version).
CI and Node.js version managers such as fnm use the version in
[frontend/.node-version](../frontend/.node-version); any Node.js version listed above also works locally.
Run this command from the `toolkit` project folder in Terminal.

```bash
make dev
```

The first start installs dependencies from the lockfiles and creates
`backend/.env` from the example if needed. Later starts reuse current
dependencies. The FastAPI backend and Vite frontend reload on source changes.
After pulling the pnpm migration, `make dev` rebuilds the old frontend dependency
directory on its first run.

Open the frontend address printed in Terminal, normally
[http://127.0.0.1:5173](http://127.0.0.1:5173). The backend normally listens on
port 8000. Keep Terminal open while using the app and press **Ctrl-C** to stop
both services.

Choose a tool from the sidebar, or press **/** to search. Open tools appear as
tabs along the bottom, so you can switch tools while a task is running.

See the [tool guide](tools.md) for additional apps and the
[development guide](development.md) for command options.
