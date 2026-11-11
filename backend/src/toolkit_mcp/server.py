"""Stdio MCP server; lifespan owns only an HTTP client, never backend state."""

from __future__ import annotations

import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .client import Settings, ToolkitClient
from .common import annotations


def create_server(settings: Settings | None = None, *, api=None) -> MCPServer:
    client = api or ToolkitClient(settings or Settings())

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield client
        finally:
            await client.close()

    server = MCPServer(
        "Toolkit",
        version="0.1.0",
        instructions=(
            "All business operations run through one existing Toolkit FastAPI backend. "
            "Start with toolkit_health. Backend paths refer to the API host; uploaded "
            "files and download destinations refer to the MCP host. Retain returned "
            "job_id, scan_id, infohash and artifact_id. Poll job_status and explicitly "
            "job_cancel when needed. Cancellation does not roll back changes. "
            "Never blindly retry a timed-out mutation. Use jobs_list to recover IDs. "
            "Use artifact_markdown for actual Markdown text, artifact_download for "
            "binary files. API/browser/scan state is shared with the web UI, but "
            "MCP-created jobs do not automatically appear in the web task bar."
        ),
        lifespan=lifespan,
        log_level="WARNING",
    )

    @server.tool(annotations=annotations(read_only=True, idempotent=True))
    async def toolkit_health() -> dict[str, Any]:
        """Check backend connection and FFmpeg/LibreOffice/MinerU/BitComet availability.

        Also lists enabled web tools. A tool may remain discoverable in MCP while
        disabled on this backend; calling it returns HTTP 404.
        """
        return {
            "api_base_url": client.settings.api_base_url,
            "health": await client.request("GET", "/health"),
            "tools": await client.request("GET", "/tools"),
        }

    @server.tool(annotations=annotations(read_only=True, idempotent=True))
    async def jobs_list(
        tool: str | None = None,
        state: Literal["running", "done", "failed", "cancelled"] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        """Find retained backend jobs, newest first, including web and MCP tasks.

        Use tool slug/state filters or recover a job after losing a start response.
        Jobs are in-memory and normally retain the last 50; restart loses all IDs.
        """
        params = {"limit": limit}
        if tool is not None:
            params["tool"] = tool
        if state is not None:
            params["state"] = state
        return {"jobs": await client.request("GET", "/jobs", params=params)}

    @server.tool(annotations=annotations(read_only=True, idempotent=True))
    async def job_status(job_id: str) -> dict[str, Any]:
        """Get progress, item failures and full result for a backend job.

        Terminal states: done, failed, cancelled. A done batch may contain individual
        failures; inspect result/items/error. Use artifact_download with artifact_id.
        """
        return await client.request("GET", f"/jobs/{client.segment(job_id)}")

    @server.tool(annotations=annotations(destructive=True, idempotent=True))
    async def job_cancel(job_id: str) -> dict[str, Any]:
        """Request best-effort cancellation of a backend job; then poll job_status.

        Does not undo moved/deleted files, output writes, lock updates or commits.
        Some subprocesses terminate; other workers stop between items. A cancelling
        response is not proof the worker stopped. Synchronous image/web capture and
        BitComet downloads after send are outside this job cancellation API.
        """
        return await client.request("POST", f"/jobs/{client.segment(job_id)}/cancel")

    @server.tool(annotations=annotations(destructive=True))
    async def job_result(
        job_id: str, output_path: str | None = None, overwrite: bool = False
    ) -> dict[str, Any]:
        """Get a job snapshot and optionally download its artifact to an absolute path.

        With output_path, requires terminal job state and result.artifact_id. Cancelled
        jobs may have partial artifacts. Default refuses an existing output; parent
        directory must exist. Return includes IDs, full result and local file metadata.
        Use job_status for read-only result inspection without writing a local file.
        """
        result = await job_status(job_id)
        if output_path is not None:
            if result.get("state") not in {"done", "failed", "cancelled"}:
                raise ToolError(
                    "Job is still running; poll job_status before downloading."
                )
            artifact_id = (result.get("result") or {}).get("artifact_id")
            if not artifact_id:
                raise ToolError(
                    "No downloadable artifact; inspect job_status result/error."
                )
            result["download"] = await artifact_download(
                artifact_id, output_path, overwrite
            )
        return result

    @server.tool(annotations=annotations(destructive=True))
    async def artifact_download(
        artifact_id: str, output_path: str, overwrite: bool = False
    ) -> dict[str, Any]:
        """Stream an artifact to an absolute local path; return file metadata.

        Use result.artifact_id from a job or webpdf_capture. Parent directory must
        exist; overwrite=false refuses existing files, true atomically replaces a
        regular file. Returns path/file_uri, bytes, SHA256 and media type. Artifacts
        expire after roughly six idle hours and are lost on backend shutdown.
        """
        saved = await client.download(
            f"/artifacts/{client.segment(artifact_id)}", output_path, overwrite
        )
        return {"artifact_id": artifact_id, **saved}

    @server.tool(annotations=annotations(read_only=True, idempotent=True))
    async def artifact_markdown(
        artifact_id: str,
        member: str | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        max_chars: Annotated[int, Field(ge=1, le=50000)] = 12000,
    ) -> dict[str, Any]:
        """Read actual Markdown text from a Doc to Markdown ZIP artifact over HTTP.

        Returns Markdown members and one member's text. With no member, reads the
        first Markdown file; select another exact member name for subsequent files.
        offset/next_offset are Unicode character positions; repeat until next_offset
        is null. Bounded to 64 MiB ZIP, 16 MiB per Markdown member, 1000 ZIP entries.
        No archive extraction, image embedding or ZIP bytes in model context.
        """
        route = f"/artifacts/{client.segment(artifact_id)}"
        with tempfile.TemporaryDirectory(prefix="toolkit_mcp_markdown_") as directory:
            path = Path(directory) / "markdown.zip"
            await client.download(route, str(path), limit=64 * 1024 * 1024)
            try:
                with zipfile.ZipFile(path) as archive:
                    infos = archive.infolist()
                    if len(infos) > 1000:
                        raise ToolError(
                            "Too many ZIP members; download it for local inspection."
                        )
                    members = [
                        i.filename
                        for i in infos
                        if not i.is_dir()
                        and i.filename.lower().endswith((".md", ".markdown"))
                    ]
                    if not members:
                        raise ToolError("Artifact has no Markdown members.")
                    selected = member if member is not None else members[0]
                    if selected not in members:
                        raise ToolError(
                            f"Unknown Markdown member. Available members: {members}"
                        )
                    info = archive.getinfo(selected)
                    if info.file_size > 16 * 1024 * 1024:
                        raise ToolError(
                            "Markdown exceeds 16 MiB; download the artifact locally."
                        )
                    with archive.open(info) as stream:
                        body = stream.read(16 * 1024 * 1024 + 1)
                    if len(body) > 16 * 1024 * 1024:
                        raise ToolError("Markdown member exceeds the read limit.")
                    text = body.decode("utf-8-sig")
            except (
                zipfile.BadZipFile,
                UnicodeError,
                RuntimeError,
                NotImplementedError,
                OSError,
            ) as exc:
                raise ToolError(f"Cannot read Markdown ZIP: {exc}") from exc
        if offset > len(text):
            raise ToolError(f"offset exceeds this member's {len(text)} characters.")
        end = min(offset + max_chars, len(text))
        return {
            "artifact_id": artifact_id,
            "members": members,
            "member": selected,
            "text": text[offset:end],
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
        }

    from .tools import register_tools

    register_tools(server, client)
    return server
