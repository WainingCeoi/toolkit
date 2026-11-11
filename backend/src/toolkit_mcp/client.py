"""One HTTP client, bounded file transfers, and actionable MCP errors.

There are deliberately no imports from the API or business packages here.
No request is retried: even a lost response may follow a successful mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import tempfile
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx2 as httpx
from mcp.server.mcpserver.exceptions import ToolError


@dataclass(frozen=True)
class Settings:
    api_base_url: str = "http://127.0.0.1:8000"
    timeout: float = 120
    connect_timeout: float = 5
    max_upload_bytes: int = 512 * 1024 * 1024
    max_download_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self):
        parsed = urlsplit(self.api_base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("API base URL has an invalid port.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and port < 1)
            or any(c.isspace() for c in self.api_base_url)
        ):
            raise ValueError("Use an http(s) API base URL without credentials/query.")
        path = parsed.path.rstrip("/")
        if not path.endswith("/api"):
            path += "/api"
        object.__setattr__(
            self, "api_base_url", urlunsplit(parsed._replace(path=path)) + "/"
        )
        if any(
            not math.isfinite(x) or x <= 0 for x in (self.timeout, self.connect_timeout)
        ):
            raise ValueError("HTTP timeouts must be finite positive seconds.")
        if self.max_upload_bytes < 1 or self.max_download_bytes < 1:
            raise ValueError("File limits must be positive bytes.")


def local_path(value: str) -> Path:
    """Only explicit local absolute paths or local file: references."""
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise ToolError("Use a local file:///absolute/path reference.")
        value = unquote(parsed.path)
    path = Path(value).expanduser()
    if not path.is_absolute() or "\x00" in value:
        raise ToolError("Use an absolute local path, ~/path, or file:///path.")
    return path


class ToolkitClient:
    def __init__(self, settings: Settings, *, transport=None):
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(settings.timeout, connect=settings.connect_timeout),
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    @staticmethod
    def segment(value: str) -> str:
        # All backend identifiers are opaque, simple tokens. Reject URL delimiters.
        if not value or any(
            not (c.isascii() and (c.isalnum() or c in "_-")) for c in value
        ):
            raise ToolError("Invalid identifier: use the exact ID returned by Toolkit.")
        return value

    @staticmethod
    def _route(path: str) -> str:
        if not path.startswith("/") or any(x in path for x in ("..", ":", "?", "#")):
            raise ToolError("Invalid Toolkit API route.")
        return path.lstrip("/")

    @asynccontextmanager
    async def _response(self, method, path, **kwargs):
        try:
            async with self.http.stream(
                method, self._route(path), **kwargs
            ) as response:
                if not 200 <= response.status_code < 300:
                    raw = await self._read_bounded(response, 64 * 1024)
                    try:
                        detail = json.loads(raw).get(
                            "detail", raw.decode(errors="replace")
                        )
                    except ValueError, AttributeError:
                        detail = raw.decode(errors="replace")[:2000]
                    raise ToolError(
                        f"Toolkit API HTTP {response.status_code}: {detail}. "
                        "For 404 check base URL, disabled tools, and expired IDs. "
                        "No automatic retry was made."
                    )
                yield response
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"Toolkit API timed out at {self.settings.api_base_url}. "
                "Outcome may be unknown; the backend may still be running. "
                "Do not blindly repeat it. Use jobs_list/job_status or inspect the "
                "affected state first. No retry or automatic cancellation was made."
            ) from exc
        except httpx.RequestError as exc:
            raise ToolError(
                f"Cannot connect to Toolkit API at {self.settings.api_base_url} "
                f"({type(exc).__name__}). Start Toolkit with make dev. "
                "Set TOOLKIT_API_BASE_URL to the printed backend address. "
                "No retry was made; inspect state before repeating a sent request."
            ) from exc

    @staticmethod
    async def _read_bounded(response, limit):
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ToolError(f"API response exceeds the {limit}-byte safety limit.")
        return bytes(body)

    async def _decode(self, response):
        body = await self._read_bounded(response, 16 * 1024 * 1024)
        mime = response.headers.get("content-type", "").split(";")[0]
        if mime == "application/json":
            try:
                return json.loads(body)
            except ValueError as exc:
                raise ToolError(
                    "Toolkit returned invalid JSON; check the API base URL."
                ) from exc
        if mime.startswith("text/") and mime != "text/html":
            return {"text": body.decode("utf-8", errors="replace"), "media_type": mime}
        raise ToolError(
            "Expected Toolkit JSON/text but received another format. Check base URL; "
            "use a download tool for binary outputs."
        )

    async def request(self, method, path, *, json=None, params=None):
        async with self._response(method, path, json=json, params=params) as response:
            return await self._decode(response)

    @staticmethod
    def _destination(output_path, overwrite):
        path = local_path(output_path)
        if path.is_symlink() or (
            path.exists() and (not overwrite or not path.is_file())
        ):
            raise ToolError(
                "Output exists or is a symlink; choose a new path or set "
                "overwrite=true for a regular file."
            )
        if not path.parent.is_dir():
            raise ToolError("Output parent directory must already exist.")
        return path

    async def _save(
        self, response, path, overwrite, *, limit=None, expected_media_type=None
    ):
        mime = response.headers.get("content-type", "").split(";")[0]
        if mime in {"text/html", "application/json"}:
            raise ToolError(
                "Expected a file but received HTML/JSON; check API base URL."
            )
        if expected_media_type and mime != expected_media_type:
            raise ToolError(
                f"Expected {expected_media_type} but API returned {mime!r}."
            )
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".toolkit-", delete=False
            ) as file:
                temporary = Path(file.name)
                size, digest = 0, hashlib.sha256()
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > (limit or self.settings.max_download_bytes):
                        raise ToolError(
                            "Download exceeds size limit; destination was not replaced."
                        )
                    file.write(chunk)
                    digest.update(chunk)
            if overwrite:
                if path.is_symlink():
                    raise ToolError("Refusing to replace a symlink output.")
                os.replace(temporary, path)
            else:
                # Atomic link fails if a concurrent writer created the destination.
                os.link(temporary, path)
            return {
                "path": str(path.absolute()),
                "file_uri": path.absolute().as_uri(),
                "bytes": size,
                "sha256": digest.hexdigest(),
                "media_type": response.headers.get(
                    "content-type", "application/octet-stream"
                ),
                "overwrite_allowed": overwrite,
            }
        except OSError as exc:
            raise ToolError(
                f"Cannot save output: {exc}. Destination was not silently overwritten."
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def download(
        self, path, output_path, overwrite=False, params=None, *, limit=None
    ):
        destination = self._destination(output_path, overwrite)
        async with self._response("GET", path, params=params) as response:
            return await self._save(
                response,
                destination,
                overwrite,
                limit=limit,
                expected_media_type="image/png" if path.endswith("/qr.png") else None,
            )

    async def upload(
        self, path, paths, data=None, field="files", output_path=None, overwrite=False
    ):
        destination = self._destination(output_path, overwrite) if output_path else None
        with ExitStack() as stack:
            files, total = [], 0
            for reference in paths:
                file_path = local_path(reference)
                try:
                    if not file_path.is_file():
                        raise ToolError(
                            f"Upload file not found or not regular: {file_path}"
                        )
                    handle = stack.enter_context(file_path.open("rb"))
                    total += os.fstat(handle.fileno()).st_size
                except OSError as exc:
                    raise ToolError(f"Cannot read upload file: {exc}") from exc
                if total > self.settings.max_upload_bytes:
                    raise ToolError("Uploads exceed configured total byte limit.")
                files.append(
                    (
                        field,
                        (
                            file_path.name,
                            handle,
                            mimetypes.guess_type(file_path.name)[0]
                            or "application/octet-stream",
                        ),
                    )
                )
            form = {
                key: str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in (data or {}).items()
                if value is not None
            }
            async with self._response(
                "POST", path, files=files or None, data=form
            ) as response:
                if destination is not None:
                    return await self._save(
                        response,
                        destination,
                        overwrite,
                        expected_media_type="application/pdf"
                        if path == "/img-to-pdf"
                        else None,
                    )
                return await self._decode(response)
