"""Embedded HTTP server that serves rendered subscriptions at /sub/{id}."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import core


def _make_handler(store, access_token: str):
    class SubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(
            self,
            status: int,
            body=b"",
            content_type="text/plain; charset=utf-8",
            extra=None,
        ) -> None:
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - required name
            parts = urlsplit(self.path)
            if not parts.path.startswith("/sub/"):
                self._send(404, "not found")
                return
            query = parse_qs(parts.query)
            if access_token and query.get("token", [""])[0] != access_token:
                self._send(403, "Forbidden: invalid token")
                return
            sub_id = parts.path.split("/")[-1]
            record = store.get_subscription(sub_id)
            if not record:
                self._send(404, "not found")
                return
            nodes = record["payload"].get("nodes", [])
            target = core.detect_target(
                self.headers.get("User-Agent", ""), query.get("target", [""])[0]
            )
            host = self.headers.get("Host", "")
            request_url = f"http://{host}{parts.path}" if host else parts.path
            try:
                body, content_type, filename = core.render_subscription(
                    target, nodes, request_url
                )
            except ValueError as exc:
                self._send(400, str(exc))
                return
            extra = {}
            if query.get("download", [""])[0] == "1":
                extra["Content-Disposition"] = f'attachment; filename="{filename}"'
            self._send(200, body, content_type, extra)

        def log_message(self, *args) -> None:  # silence console noise
            pass

    return SubHandler


def start_sub_server(
    store, host: str, port: int, access_token: str = ""
) -> ThreadingHTTPServer:
    """Start the /sub/{id} server in a daemon thread and return the server object."""
    httpd = ThreadingHTTPServer((host, port), _make_handler(store, access_token))
    thread = threading.Thread(target=httpd.serve_forever, name="sub-http", daemon=True)
    thread.start()
    return httpd
