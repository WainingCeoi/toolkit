"""In-process JSON-RPC server speaking aria2's dialect, for tests.

A real HTTP server on an ephemeral port rather than a monkeypatched requests
session: it exercises the actual wire format (token param, error envelope,
multicall shape), which is where the bugs live. No aria2 install needed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.37.0"


class FakeAria2:
    def __init__(self, secret: str = "s3cret") -> None:
        self.secret = secret
        self.calls: list[tuple[str, list]] = []
        self.downloads: dict[str, dict] = {}
        self._next_gid = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/jsonrpc"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add_download(self, *, infohash: str, status: str = "paused", **fields) -> str:
        self._next_gid += 1
        gid = f"gid{self._next_gid:04d}"
        self.downloads[gid] = {
            "gid": gid,
            "infoHash": infohash,
            "status": status,
            "totalLength": "0",
            "completedLength": "0",
            "downloadSpeed": "0",
            "files": [],
            **fields,
        }
        return gid

    # --- dispatch ---------------------------------------------------------
    def _dispatch(self, method: str, params: list):
        self.calls.append((method, params))
        name = method.removeprefix("aria2.")

        if name == "getVersion":
            return {"version": VERSION, "enabledFeatures": ["BitTorrent"]}
        if name in {"tellActive", "tellWaiting", "tellStopped"}:
            wanted = {
                "tellActive": {"active"},
                "tellWaiting": {"waiting", "paused"},
                "tellStopped": {"complete", "error", "removed"},
            }[name]
            return [d for d in self.downloads.values() if d["status"] in wanted]
        if name == "tellStatus":
            return self.downloads[params[0]]
        if name == "getFiles":
            return self.downloads[params[0]]["files"]
        if name == "addUri":
            gid = self.add_download(infohash="", status="waiting")
            self.downloads[gid]["uris"] = params[0]
            self.downloads[gid]["options"] = params[1] if len(params) > 1 else {}
            return gid
        if name == "addTorrent":
            gid = self.add_download(infohash="", status="paused")
            self.downloads[gid]["options"] = params[2] if len(params) > 2 else {}
            return gid
        if name in {"pause", "forcePause"}:
            self.downloads[params[0]]["status"] = "paused"
            return params[0]
        if name == "unpause":
            self.downloads[params[0]]["status"] = "active"
            return params[0]
        if name in {"remove", "forceRemove"}:
            self.downloads[params[0]]["status"] = "removed"
            return params[0]
        if name == "changeOption":
            self.downloads[params[0]].setdefault("options", {}).update(params[1])
            return "OK"
        if name in {"saveSession", "shutdown", "pauseAll", "forcePauseAll"}:
            return "OK"
        if method == "system.multicall":
            return [[self._dispatch(c["methodName"], c["params"])] for c in params[0]]
        raise KeyError(method)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's API
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                params = body.get("params", [])
                token = params[0] if params else None
                if token != f"token:{fake.secret}":
                    return self._send({"error": {"code": 1, "message": "Unauthorized"}})
                try:
                    result = fake._dispatch(body["method"], params[1:])
                except KeyError as exc:
                    return self._send(
                        {"error": {"code": 1, "message": f"No such method: {exc}"}}
                    )
                self._send({"result": result})

            def _send(self, payload):
                data = json.dumps({"jsonrpc": "2.0", "id": "t", **payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler
