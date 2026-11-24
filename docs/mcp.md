# AI / MCP access

Toolkit also provides a local stdio MCP server covering twelve tools. Start
Toolkit with [`make dev`](getting-started.md), then point MCP at the backend
address printed in Terminal. The adapter shares backend jobs, browser
sessions, devices, and scan records with the web app.

```bash
backend/.venv/bin/python -m toolkit_mcp --api-base-url http://127.0.0.1:8000
```

Use the actual port above. For an AI client's stdio configuration, use the
absolute path to `backend/.venv/bin/python` as the command, with arguments
`["-m", "toolkit_mcp"]`, and set `TOOLKIT_API_BASE_URL` to the running backend's
address. Run `backend/.venv/bin/toolkit-mcp --help` for connection options.

Start with `toolkit_health`, then use the discovered tool descriptions for
parameters and operation effects. Upload paths and download destinations refer
to the MCP host; folders and project paths processed by the backend refer to the
API host. Downloads require an explicit output path and refuse overwriting by
default. Use `job_status` for progress, `job_cancel` for best-effort cancellation,
and `artifact_download` or `artifact_markdown` for results. Requests are not
automatically retried; after a timeout, check backend state before repeating an
operation. MCP-created jobs share backend state but do not automatically appear
in the current web task bar.

The MCP dependency stack has been tested on Apple silicon. Intel Mac installation
is unverified and may require native build tools for its cryptography dependency.
