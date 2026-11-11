"""Adapter transport and shared tools: controlled HTTP responses, no business fakes."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from toolkit_mcp.client import Settings, ToolkitClient, local_path
from toolkit_mcp.server import create_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def adapter(handler, **settings):
    return ToolkitClient(
        Settings(api_base_url="http://toolkit.test:9123", **settings),
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "base", ["http://localhost:8001", "http://localhost:8001/api/"]
)
def test_settings_normalize(base):
    assert Settings(base).api_base_url == "http://localhost:8001/api/"


@pytest.mark.parametrize(
    "base",
    [
        "file:///tmp/x",
        "oops",
        "http://x:bad",
        "http://u:p@x",
        "http://x?a=1",
        "http://x#f",
    ],
)
def test_settings_reject(base):
    with pytest.raises(ValueError):
        Settings(base)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_timeout_reject(value):
    with pytest.raises(ValueError):
        Settings(timeout=value)


def test_path_references(tmp_path):
    file = tmp_path / "空 格.png"
    assert local_path(file.as_uri()) == file
    assert local_path("~/a").is_absolute()
    for invalid in ["relative.png", "file://remote/tmp/file", "file:///tmp/a?secret=x"]:
        with pytest.raises(ToolError):
            local_path(invalid)
    for invalid in ["../x", "abc/def", "a?b", "a%2fb", ""]:
        with pytest.raises(ToolError):
            ToolkitClient.segment(invalid)


@pytest.mark.anyio
async def test_http_errors_preserve_detail_no_retry():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(409, json={"detail": "scan expired; scan again"})

    api = adapter(respond)
    try:
        with pytest.raises(ToolError, match="HTTP 409.*scan expired"):
            await api.request("POST", "/purge/delete", json={"scan_id": "s"})
        assert len(calls) == 1
        assert calls[0].url.path == "/api/purge/delete"
    finally:
        await api.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout]
)
async def test_connection_and_timeout(error):
    calls = []

    def fail(request):
        calls.append(request)
        raise error("bad", request=request)

    api = adapter(fail)
    try:
        with pytest.raises(ToolError) as caught:
            await api.request("POST", "/gather/start", json={"source": "/x"})
        assert "retry" in str(caught.value).lower()
        assert "http://toolkit.test:9123/api/" in str(caught.value)
        if error is httpx.ReadTimeout:
            assert "Outcome may be unknown" in str(caught.value)
        else:
            assert "TOOLKIT_API_BASE_URL" in str(caught.value)
        assert len(calls) == 1
    finally:
        await api.close()


@pytest.mark.anyio
async def test_upload_and_atomic_download(tmp_path):
    source = tmp_path / "图.png"
    source.write_bytes(b"image fixture")
    output = tmp_path / "result.pdf"
    requests = []

    async def respond(request):
        requests.append((request, await request.aread()))
        return httpx.Response(
            200, content=b"%PDF-output", headers={"content-type": "application/pdf"}
        )

    api = adapter(respond)
    try:
        result = await api.upload(
            "/img-to-pdf",
            [source.as_uri()],
            {"name": "图", "flag": False},
            output_path=str(output),
        )
        assert output.read_bytes() == b"%PDF-output"
        assert result["bytes"] == 11 and len(result["sha256"]) == 64
        assert result["file_uri"] == output.as_uri()
        request, body = requests[0]
        assert "multipart/form-data" in request.headers["content-type"]
        assert b'name="files"' in body and b"image fixture" in body
        assert b"false" in body and "图.png".encode() in body
        with pytest.raises(ToolError, match="Output exists"):
            await api.upload("/img-to-pdf", [str(source)], output_path=str(output))
        assert (
            len(requests) == 1
        )  # preflight prevents conversion before overwrite error
        await api.download("/artifacts/a", str(output), overwrite=True)
        assert len(requests) == 2
        assert not list(tmp_path.glob(".toolkit-*"))
    finally:
        await api.close()


@pytest.mark.anyio
async def test_size_limits_and_failed_download_keep_original(tmp_path):
    source = tmp_path / "input"
    source.write_bytes(b"123456")
    dest = tmp_path / "out"
    dest.write_bytes(b"original")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"too big")

    api = adapter(respond, max_upload_bytes=5, max_download_bytes=5)
    try:
        with pytest.raises(ToolError, match="Uploads exceed"):
            await api.upload("/doc-to-pdf", [str(source)])
        assert not calls
        with pytest.raises(ToolError, match="Download exceeds"):
            await api.download("/artifacts/a", str(dest), True)
        assert dest.read_bytes() == b"original"
        assert not list(tmp_path.glob(".toolkit-*"))
        with pytest.raises(ToolError, match="not found"):
            await api.upload("/doc-to-pdf", [str(tmp_path / "missing")])
    finally:
        await api.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mime,body",
    [
        ("text/html", b"<html>wrong app</html>"),
        ("application/json", b"not json"),
        ("application/pdf", b"%PDF"),
    ],
)
async def test_wrong_backend_and_invalid_responses(mime, body):
    api = adapter(
        lambda r: httpx.Response(200, content=body, headers={"content-type": mime})
    )
    try:
        with pytest.raises(ToolError):
            await api.request("GET", "/health")
    finally:
        await api.close()


@pytest.mark.anyio
async def test_redirect_not_followed():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "http://elsewhere.test/upload"})

    api = adapter(respond)
    try:
        with pytest.raises(ToolError, match="HTTP 307"):
            await api.request("POST", "/gather/start", json={"source": "/private"})
        assert len(calls) == 1
    finally:
        await api.close()


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return output.getvalue()


@pytest.mark.anyio
async def test_shared_tools_mapping_and_markdown(tmp_path):
    calls = []
    archive = zip_bytes(
        {
            "one/readme.md": "# 正文\nHello world",
            "two.md": "second",
            "../escape.txt": "never extracted",
        }
    )

    def respond(request):
        calls.append(request)
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if path in {"/api/tools", "/api/jobs"}:
            return httpx.Response(200, json=[])
        if path == "/api/jobs/job1/cancel":
            return httpx.Response(200, json={"cancelling": True})
        if path == "/api/jobs/job1":
            return httpx.Response(
                200,
                json={"id": "job1", "state": "done", "result": {"artifact_id": "a1"}},
            )
        if path == "/api/artifacts/a1":
            return httpx.Response(
                200, content=archive, headers={"content-type": "application/zip"}
            )
        return httpx.Response(404, json={"detail": "unknown"})

    api = adapter(respond)
    async with Client(create_server(api=api)) as client:

        async def call(name, **args):
            result = await client.call_tool(name, args)
            assert not result.is_error, result
            return result.structured_content

        assert (await call("toolkit_health"))["health"]["ok"]
        await call("jobs_list", tool="doc-to-markdown", state="done", limit=3)
        assert calls[-1].url.params["tool"] == "doc-to-markdown"
        assert calls[-1].url.params["state"] == "done"
        assert calls[-1].url.params["limit"] == "3"
        assert (await call("job_status", job_id="job1"))["id"] == "job1"
        assert (await call("job_cancel", job_id="job1"))["cancelling"]
        assert calls[-1].method == "POST"
        saved = await call(
            "job_result", job_id="job1", output_path=str(tmp_path / "out.zip")
        )
        assert saved["download"]["artifact_id"] == "a1"
        assert Path(saved["download"]["path"]).read_bytes() == archive
        page = await call("artifact_markdown", artifact_id="a1", max_chars=5)
        assert page["text"] == "# 正文\n" and page["next_offset"] == 5
        page = await call("artifact_markdown", artifact_id="a1", offset=5)
        assert page["text"] == "Hello world" and page["next_offset"] is None
        page = await call("artifact_markdown", artifact_id="a1", member="two.md")
        assert page["text"] == "second" and len(page["members"]) == 2
        assert not (tmp_path / "escape.txt").exists()
        for args in [{"member": "absent.md"}, {"offset": 999}, {"max_chars": 0}]:
            result = await client.call_tool(
                "artifact_markdown", {"artifact_id": "a1", **args}
            )
            assert result.is_error
        result = await client.call_tool("job_status", {"job_id": "unknown"})
        assert result.is_error and "HTTP 404" in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [b"invalid ZIP", zip_bytes({"image.png": b"x"}), zip_bytes({"x.md": b"\xff"})],
)
async def test_invalid_markdown_artifacts(body):
    api = adapter(lambda r: httpx.Response(200, content=body))
    async with Client(create_server(api=api)) as client:
        result = await client.call_tool("artifact_markdown", {"artifact_id": "a"})
        assert result.is_error


def test_adapter_has_no_business_imports():
    import ast

    import toolkit_mcp

    for path in Path(toolkit_mcp.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(
                n.startswith(("toolkit_api", "toolkit_engine", "subgen")) for n in names
            ), path


@pytest.mark.anyio
@pytest.mark.parametrize("mime", ["text/html", "application/json", "text/plain"])
async def test_wrong_address_cannot_be_saved_as_pdf(tmp_path, mime):
    source = tmp_path / "image.png"
    source.write_bytes(b"image")
    dest = tmp_path / "output.pdf"
    api = adapter(
        lambda r: httpx.Response(200, content=b"wrong", headers={"content-type": mime})
    )
    try:
        with pytest.raises(ToolError):
            await api.upload("/img-to-pdf", [str(source)], output_path=str(dest))
        assert not dest.exists()
    finally:
        await api.close()


@pytest.mark.anyio
async def test_mid_download_timeout_cleans_partial_file(tmp_path):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial bytes"
            raise httpx.ReadTimeout("connection stalled")

    destination = tmp_path / "output.zip"
    destination.write_bytes(b"original")
    api = adapter(lambda r: httpx.Response(200, stream=BrokenStream()))
    try:
        with pytest.raises(ToolError, match="timed out"):
            await api.download("/artifacts/a", str(destination), overwrite=True)
        assert destination.read_bytes() == b"original"
        assert not list(tmp_path.glob(".toolkit-*"))
    finally:
        await api.close()
