"""In-process HTTP server speaking BitComet's WebUI dialect, for tests."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from toolkit_engine.bitcomet import decrypt
from toolkit_engine.torrent import parse_magnet, parse_torrent

SERVER_NAME = "BitComet-Test"
TORRENT_MAX_SIZE = 20 * 1024 * 1024


def _magnet_infohash(link: str) -> str:
    try:
        return parse_magnet(link)[0]
    except ValueError:
        return ""


def _file(index: int, path: str, size: int) -> dict:
    """One entry of files/get. `index` is 0-BASED, like the real API."""
    return {
        "index": index,
        "name": path,
        "size": size,
        "downloaded_size": 0,
        "priority": "normal",
        "ltseed": False,
        "error": "",
    }


class _Server(ThreadingHTTPServer):
    # A send window opens ten sockets at once; the default backlog of 5 resets them.
    request_queue_size = 64


class _DeniedError(Exception):
    """Answered as HTTP 401: the bearer token is missing, stale or revoked."""


class _RejectedError(Exception):
    """Answered as HTTP 200 carrying an error_code, exactly as BitComet does."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FakeBitComet:
    def __init__(
        self,
        username: str = "webui",
        password: str = "s3cret",
        save_folders: list[str] | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.save_folders = list(save_folders or ["/Users/test/Downloads"])
        self.calls: list[tuple[str, str, dict]] = []
        self.tasks: dict[str, dict] = {}
        self.deleted: list[tuple[str, bool]] = []
        self.logins = 0
        self.torrent_max_size = TORRENT_MAX_SIZE
        self.metadata: dict[str, list[tuple[str, int]]] = {}
        self.live_tokens: set[str] = set()
        self.invite_tokens: set[str] = set()
        self.reject_every_token = False

        self._next_task_id = 1001
        self._server = _Server(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            # shutdown() waits a full poll tick; the 0.5s default dominates the suite.
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def revoke_tokens(self) -> None:
        """Invalidate every issued device_token, as a BitComet restart does."""
        self.live_tokens.clear()

    # --- state ------------------------------------------------------------
    def add_task(
        self, name: str, files: list[tuple[str, int]], infohash: str = ""
    ) -> str:
        """Seed a task directly. `files` is [(path, size)] in torrent order."""
        task_id = str(self._next_task_id)
        self._next_task_id += 1
        self.tasks[task_id] = {
            "task_id": task_id,
            "task_guid": f"bt_{infohash or task_id}",
            "task_name": name,
            "status": "stopped",
            "files": [_file(n, path, size) for n, (path, size) in enumerate(files)],
        }
        return task_id

    def publish_metadata(self, infohash: str, files: list[tuple[str, int]]) -> None:
        """Put a magnet's file list in the swarm, for a running task to find."""
        self.metadata[infohash.lower()] = list(files)

    def _deliver_metadata(self, task: dict) -> None:
        """Give a magnet its files only while RUNNING; a stopped task never learns."""
        if task["files"] or task["status"] != "running":
            return
        infohash = task["task_guid"].removeprefix("bt_")
        task["files"] = [
            _file(n, path, size)
            for n, (path, size) in enumerate(self.metadata.get(infohash, []))
        ]

    def _summary(self, task: dict) -> dict:
        """A task as task_list/get reports it -- note task_id as an INT."""
        files = task["files"]
        selected = [f for f in files if f["priority"] != "disabled"]
        return {
            "task_id": int(task["task_id"]),
            "task_guid": task["task_guid"],
            "task_name": task["task_name"],
            "status": task["status"],
            "total_size": sum(f["size"] for f in files),
            "selected_size": sum(f["size"] for f in selected),
            "selected_downloaded_size": sum(f["downloaded_size"] for f in selected),
            "download_rate": task.get("download_rate", 0),
            "upload_rate": task.get("upload_rate", 0),
            "permillage": task.get("permillage", 0),
            "left_time": task.get("left_time", ""),
            "share_ratio": task.get("share_ratio", 0),
            "health": task.get("health", 0),
            "file_count": len(files),
            "error_code": "",
            "error_message": "",
        }

    def _task(self, payload: dict) -> dict:
        task_id = payload.get("task_id")
        # The real API refuses an int task_id too.
        if not isinstance(task_id, str):
            raise _RejectedError("INVALID_TASK_ID", "invalid task_id")
        if task_id not in self.tasks:
            raise _RejectedError("INVALID_TASK_ID", "invalid task_id")
        return self.tasks[task_id]

    def _selected_task_ids(self, payload: dict) -> list[str]:
        task_ids = payload.get("task_ids")
        if not isinstance(task_ids, list) or not all(
            isinstance(one, str) for one in task_ids
        ):
            raise _RejectedError("INVALID_TASK_IDS", "task_ids invalid")
        return task_ids

    def _check_save_folder(self, payload: dict) -> None:
        folder = str(payload.get("save_folder", "")).rstrip("/")
        if folder not in {f.rstrip("/") for f in self.save_folders}:
            raise _RejectedError("INVALID_SAVE_FOLDER", "save_folder invalid")

    # --- dispatch ---------------------------------------------------------
    def _dispatch(
        self, method: str, path: str, payload: dict, token: str | None
    ) -> dict:
        self.calls.append((method, path, payload))

        if path == "/api/webui/login":
            return self._login(payload)
        if path == "/api/device_token/get":
            return self._device_token(payload, token)

        self._authorise(token, self.live_tokens)

        if path == "/api/task/bt/add":
            return self._bt_add(payload)
        if path == "/api/task/torrent_links/add":
            return self._links_add(payload)
        if path == "/api/task/files/get":
            task = self._task(payload)
            self._deliver_metadata(task)
            return {
                "error_code": "OK",
                "files": [dict(f) for f in task["files"]],
                "task": self._summary(task),
            }
        if path == "/api/task/files/set_priority":
            return self._set_priority(payload)
        if path == "/api_v2/task_list/get":
            for task in self.tasks.values():
                self._deliver_metadata(task)
            return {"tasks": [self._summary(t) for t in self.tasks.values()]}
        if path == "/api_v2/tasks/action":
            return self._action(payload)
        if path == "/api_v2/tasks/delete":
            return self._delete(payload)
        if path == "/api/config/new_task/get":
            return {
                "save_folders": [
                    {"path": f, "display": f, "available_size": 100 * 1024 * 1024}
                    for f in self.save_folders
                ],
                "torrent_max_size": self.torrent_max_size,
            }
        if path == "/api/config/directories/get":
            return {"error_code": "OK", "directories": list(self.save_folders)}
        if path == "/api/config/directories/add":
            folder = str(payload.get("dir_path", ""))
            if not folder:
                raise _RejectedError("INVALID_DIR", "dir_path invalid")
            if folder not in self.save_folders:
                self.save_folders.append(folder)
            return {"error_code": "OK"}
        if path == "/api/config/directories/remove":
            folder = str(payload.get("dir_path", ""))
            self.save_folders = [f for f in self.save_folders if f != folder]
            return {"error_code": "OK"}

        raise _RejectedError("NOT_FOUND", f"no such endpoint: {path}")

    def _authorise(self, token: str | None, allowed: set[str]) -> None:
        if self.reject_every_token or token is None or token not in allowed:
            raise _DeniedError(token or "")

    def _login(self, payload: dict) -> dict:
        self.logins += 1
        client_id = payload.get("client_id") or ""
        try:
            creds = json.loads(decrypt(payload.get("authentication", ""), client_id))
        except (ValueError, IndexError) as exc:  # bad base64, HMAC, padding or JSON
            raise _RejectedError(
                "INVALID_AUTH", f"authentication invalid: {exc}"
            ) from exc

        if (creds.get("username"), creds.get("password")) != (
            self.username,
            self.password,
        ):
            raise _RejectedError("INVALID_USER", "invalid username or password")

        invite = f"invite-{self.logins}"
        self.invite_tokens.add(invite)
        return {"error_code": "OK", "invite_token": invite}

    def _device_token(self, payload: dict, token: str | None) -> dict:
        # The invite_token authorises exactly this one call, and only once.
        self._authorise(token, self.invite_tokens)
        invite = payload.get("invite_token")
        if invite not in self.invite_tokens:
            raise _DeniedError(str(invite))
        self.invite_tokens.discard(invite)

        device_token = f"device-{self.logins}"
        self.live_tokens.add(device_token)
        return {
            "error_code": "OK",
            "device_token": device_token,
            "server_id": "fake-server",
            "server_name": SERVER_NAME,
        }

    def _bt_add(self, payload: dict) -> dict:
        self._check_save_folder(payload)
        raw = base64.b64decode(payload.get("torrent_file", ""))
        if len(raw) > self.torrent_max_size:
            raise _RejectedError("TORRENT_TOO_LARGE", "torrent too large")
        try:
            info = parse_torrent(raw)
        except ValueError as exc:
            raise _RejectedError("INVALID_TORRENT", f"torrent invalid: {exc}") from exc

        task_id = self.add_task(
            info.name, [(f.path, f.size) for f in info.files], info.infohash
        )
        self.tasks[task_id]["status"] = (
            "stopped" if payload.get("start_later") else "running"
        )
        # Lowercase "ok" -- this endpoint alone disagrees with the rest.
        return {
            "error_code": "ok",
            "task_id": task_id,
            "task": self._summary(self.tasks[task_id]),
        }

    def _links_add(self, payload: dict) -> dict:
        self._check_save_folder(payload)
        # A newline-joined string; BitComet answers "missing" to a JSON list.
        raw = payload.get("torrent_links")
        if not isinstance(raw, str) or not raw.strip():
            raise _RejectedError("FATALL_ERROR", "torrent_links missing")
        links = [line.strip() for line in raw.split("\n") if line.strip()]

        for link in links:
            task_id = self.add_task(link, [], _magnet_infohash(link))
            self.tasks[task_id]["status"] = (
                "stopped" if payload.get("start_later") else "running"
            )
        # The real endpoint is asynchronous and answers with no task id at all.
        return {"error_code": "OK", "error_message": "adding task in batch started."}

    def _set_priority(self, payload: dict) -> dict:
        task = self._task(payload)
        priority = payload.get("priority")
        # "none" looks like the deselect value and is not: only "disabled" is.
        if priority not in {"very_high", "high", "normal", "disabled"}:
            raise _RejectedError("INVALID_PRIORITY", f"priority invalid: {priority}")

        indexes = payload.get("file_indexes")
        if not isinstance(indexes, list) or not all(
            isinstance(i, int) and 0 <= i < len(task["files"]) for i in indexes
        ):
            # Out of range is what a 1-based index looks like from here.
            raise _RejectedError("INVALID_FILE_INDEX", "file_indexes invalid")

        for index in indexes:
            task["files"][index]["priority"] = priority
        return {"error_code": "OK"}

    def _action(self, payload: dict) -> dict:
        verb = payload.get("action")
        if verb not in {"start", "stop", "hash_check", "tracker_update"}:
            raise _RejectedError("INVALID_ACTION", f"action invalid: {verb}")
        # BitComet answers "skipped" when every task already has the wanted state.
        changed = False
        for task_id in self._selected_task_ids(payload):
            if task_id in self.tasks and verb in {"start", "stop"}:
                wanted = "running" if verb == "start" else "stopped"
                if self.tasks[task_id]["status"] != wanted:
                    self.tasks[task_id]["status"] = wanted
                    changed = True
        if not changed:
            return {"error_code": "skipped", "error_message": "action skipped."}
        return {"error_code": "OK", "error_message": "action completed."}

    def _delete(self, payload: dict) -> dict:
        verb = payload.get("action")
        if verb not in {"delete_task", "delete_all"}:
            raise _RejectedError("INVALID_ACTION", f"action invalid: {verb}")
        for task_id in self._selected_task_ids(payload):
            self.tasks.pop(task_id, None)
            self.deleted.append((task_id, verb == "delete_all"))
        return {"error_code": "OK"}

    # --- wire -------------------------------------------------------------
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's API
                self._serve()

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's API
                self._serve()

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length)) if length else {}
                auth = self.headers.get("Authorization", "")
                token = auth.removeprefix("Bearer ") if auth else None
                try:
                    body = fake._dispatch(self.command, self.path, payload, token)
                except _DeniedError:
                    return self._send(401, {"error_code": "UNAUTHORIZED"})
                except _RejectedError as exc:
                    return self._send(
                        200, {"error_code": exc.code, "error_message": exc.message}
                    )
                self._send(200, body)

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler
