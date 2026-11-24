# Settings and hosting

## Settings and saved data

Copy [backend/.env.example](../backend/.env.example) to `backend/.env`, edit it,
and restart Toolkit. The example explains settings for subscription links,
hiding tools you do not use, and other optional customizations.

Saved subscriptions and BitComet connections are stored locally in `backend/data/`
by default. This folder can contain connection credentials, so keep it private.
For `make dev`, set a custom `SUB_DB_PATH` within this checkout in `backend/.env`.
Use the [native launcher](#native-hosting) for a data path outside the checkout.
Running tasks continue when you switch tools or reload the browser, but stopping
Toolkit ends them. Restarting does not resume unfinished tasks.

## Native hosting

Prepare the [dependencies](development.md#checks), then run these commands from
the project root to serve the built frontend and API on your network.

```bash
(cd frontend && pnpm run build)
(cd backend && uv run --no-sync python -m toolkit_api.host)
```

Open the printed address. Set `HOST=127.0.0.1` on the launcher to serve only on
this Mac. Use LAN hosting only on a trusted network because the app has no login
and other devices can run its tools, including those that move or delete files.
To hide WhatsApp Chat Exporter on a LAN host, include `whatsapp` in
`TOOLKIT_DISABLED_TOOLS` and restart Toolkit.
