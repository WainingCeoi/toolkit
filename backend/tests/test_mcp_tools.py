"""Every toolbox tool through a real MCP Client to a captured HTTP transport."""

from __future__ import annotations

import asyncio
import json
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.server import MCPServer

from toolkit_mcp.client import Settings, ToolkitClient
from toolkit_mcp.tools import register_tools

# This table is intentionally independent of implementation, including defaults.
# Adding/removing a tool requires an explicit mapping expectation here.
CASES = [
    ("magnet_get_config", {}, "GET", "/magnet/config", None),
    (
        "magnet_set_config",
        {
            "website_url": "https://example.test",
            "cutoff_video": "https://example.test/1",
        },
        "PUT",
        "/magnet/config",
        {
            "website_url": "https://example.test",
            "cutoff_video": "https://example.test/1",
        },
    ),
    ("magnet_auto_start", {"start_page": 3}, "POST", "/magnet/auto", {"start_page": 3}),
    (
        "magnet_manual_start",
        {"urls": ["https://example.test/2"]},
        "POST",
        "/magnet/manual",
        {"urls": ["https://example.test/2"]},
    ),
    (
        "magnet_dedupe",
        {"links": ["magnet:a", "magnet:a"]},
        "POST",
        "/magnet/dedupe",
        {"links": ["magnet:a", "magnet:a"]},
    ),
    ("remux_scan", {"folder": "/videos"}, "POST", "/remux/scan", {"folder": "/videos"}),
    (
        "remux_match_subtitles",
        {"sub_folder": "/subs", "selected": ["/videos/a.mkv"]},
        "POST",
        "/remux/subtitles",
        {"sub_folder": "/subs", "selected": ["/videos/a.mkv"]},
    ),
    (
        "remux_start",
        {
            "selected": ["/videos/a.mkv"],
            "out_folder": "/output",
            "include_video": False,
            "video_index": 2,
            "multi_audio": True,
            "audio_value": "0,2",
            "include_subtitle": False,
            "subtitle_index": 3,
            "sub_lang": "eng",
            "use_external_sub": True,
            "external_sub_map": {"/videos/a.mkv": "/subs/a.srt"},
            "max_workers": 2,
        },
        "POST",
        "/remux/start",
        {
            "selected": ["/videos/a.mkv"],
            "out_folder": "/output",
            "include_video": False,
            "video_index": 2,
            "multi_audio": True,
            "audio_value": "0,2",
            "include_subtitle": False,
            "subtitle_index": 3,
            "sub_lang": "eng",
            "use_external_sub": True,
            "external_sub_map": {"/videos/a.mkv": "/subs/a.srt"},
            "max_workers": 2,
        },
    ),
    ("torrent_status", {}, "GET", "/torrent/status", None),
    ("torrent_devices", {}, "GET", "/torrent/devices", None),
    (
        "torrent_device_add",
        {
            "label": "test",
            "url": "http://device:8080",
            "username": "user",
            "password": "secret",
        },
        "POST",
        "/torrent/devices",
        {
            "label": "test",
            "url": "http://device:8080",
            "username": "user",
            "password": "secret",
        },
    ),
    (
        "torrent_device_update",
        {"device_id": "device_1", "password": "new"},
        "PATCH",
        "/torrent/devices/device_1",
        {"label": "", "url": "", "username": "", "password": "new"},
    ),
    (
        "torrent_device_delete",
        {"device_id": "device_1"},
        "DELETE",
        "/torrent/devices/device_1",
        None,
    ),
    (
        "torrent_device_select",
        {"device_id": "device_1"},
        "POST",
        "/torrent/devices/device_1/select",
        None,
    ),
    (
        "torrent_device_test",
        {"url": "http://device:8080", "device_id": "device_1"},
        "POST",
        "/torrent/devices/test",
        {"url": "http://device:8080", "username": "", "password": "", "id": "device_1"},
    ),
    (
        "torrent_resolve_status",
        {"infohash": "abc123"},
        "GET",
        "/torrent/resolve/abc123",
        None,
    ),
    (
        "torrent_send",
        {"infohash": "abc123", "selected": [0, 4]},
        "POST",
        "/torrent",
        {"infohash": "abc123", "selected": [0, 4]},
    ),
    ("torrent_discard", {"infohash": "abc123"}, "DELETE", "/torrent/abc123", None),
    (
        "webpdf_open",
        {"url": "https://example.test/images"},
        "POST",
        "/webpdf/open",
        {"url": "https://example.test/images"},
    ),
    ("webpdf_status", {}, "GET", "/webpdf/status", None),
    (
        "webpdf_scroll",
        {"steps": 2, "wait_ms": 0},
        "POST",
        "/webpdf/scroll",
        {"steps": 2, "wait_ms": 0},
    ),
    ("webpdf_close", {}, "POST", "/webpdf/close", None),
    ("webpdf_capture", {}, "POST", "/webpdf/capture", None),
    (
        "gather_start",
        {
            "source": "/source",
            "target": "/target",
            "categories": ["Image"],
            "custom": "txt *.log",
        },
        "POST",
        "/gather/start",
        {
            "source": "/source",
            "target": "/target",
            "categories": ["Image"],
            "custom": "txt *.log",
        },
    ),
    ("doc_to_markdown_health", {}, "GET", "/doc-to-markdown/health", None),
    (
        "purge_scan",
        {"folder": "/scratch", "patterns_raw": "tmp *.cache"},
        "POST",
        "/purge/scan",
        {"folder": "/scratch", "patterns_raw": "tmp *.cache"},
    ),
    (
        "purge_delete",
        {"scan_id": "scan_123"},
        "POST",
        "/purge/delete",
        {"scan_id": "scan_123"},
    ),
    (
        "photos_dry_run",
        {"source": "/source.photoslibrary", "dest": "/dest.photoslibrary"},
        "POST",
        "/photofilter/dry-run",
        {
            "source": "/source.photoslibrary",
            "dest": "/dest.photoslibrary",
            "rules": None,
        },
    ),
    (
        "photos_run",
        {"source": "/source.photoslibrary", "dest": "/dest.photoslibrary", "rules": ""},
        "POST",
        "/photofilter/run",
        {"source": "/source.photoslibrary", "dest": "/dest.photoslibrary", "rules": ""},
    ),
    (
        "dependencies_scan",
        {"folder": "/repo"},
        "POST",
        "/deps/scan",
        {"folder": "/repo"},
    ),
    (
        "dependencies_apply_start",
        {"folder": "/repo"},
        "POST",
        "/deps/apply/start",
        {"folder": "/repo", "commit": False, "message": None},
    ),
    (
        "subscription_generate",
        {
            "node_links": "vless://node",
            "preferred_ips": "1.1.1.1",
            "name_prefix": "test",
            "keep_original_host": False,
        },
        "POST",
        "/subs/generate",
        {
            "node_links": "vless://node",
            "preferred_ips": "1.1.1.1",
            "name_prefix": "test",
            "keep_original_host": False,
        },
    ),
    ("subscription_history", {}, "GET", "/subs/history", None),
    ("subscription_get", {"sub_id": "sub_1"}, "GET", "/subs/sub_1", None),
    ("subscription_urls", {"sub_id": "sub_1"}, "GET", "/subs/sub_1/urls", None),
    (
        "subscription_render",
        {"sub_id": "sub_1", "target": "clash"},
        "GET",
        "/subs/sub_1/render?target=clash",
        None,
    ),
    ("subscription_delete", {"sub_id": "sub_1"}, "DELETE", "/subs/sub_1", None),
]

FILE_TOOLS = {
    "torrent_resolve",
    "image_to_pdf",
    "doc_to_pdf_start",
    "doc_to_markdown_start",
    "subscription_download",
    "subscription_qr_download",
}


def run_with_transport(handler, action):
    async def run():
        api = ToolkitClient(
            Settings(api_base_url="http://backend:9876"),
            transport=httpx.MockTransport(handler),
        )
        server = MCPServer("mapping-test", log_level="ERROR")
        register_tools(server, api)
        try:
            async with Client(server) as client:
                return await action(client)
        finally:
            await api.close()

    return asyncio.run(run())


@pytest.mark.parametrize(
    "name,args,method,path,payload", CASES, ids=[case[0] for case in CASES]
)
def test_every_json_http_mapping(name, args, method, path, payload):
    requests = []
    reply = (
        [{"id": "sub_1"}]
        if name == "subscription_history"
        else {
            "from_backend": True,
            "job_id": "j123",
            "scan_id": "s123",
            "artifact_id": "a123",
        }
    )

    def handler(request):
        requests.append(request)
        if name == "subscription_render":
            return httpx.Response(
                200, text="proxies: []\n", headers={"content-type": "text/yaml"}
            )
        return httpx.Response(200, json=reply)

    async def call(client):
        result = await client.call_tool(name, args)
        assert not result.is_error, result.content
        if name == "subscription_history":
            assert result.structured_content == {"subscriptions": reply}
        elif name == "subscription_render":
            assert result.structured_content["text"] == "proxies: []\n"
        else:
            assert result.structured_content == reply

    run_with_transport(handler, call)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == method
    assert str(request.url) == "http://backend:9876/api" + path
    assert (json.loads(request.content) if request.content else None) == payload


@pytest.mark.parametrize("name", sorted(FILE_TOOLS))
def test_every_file_http_mapping(name, tmp_path):
    source = tmp_path / "测试 input.png"
    source.write_bytes(b"uploaded-content")
    output = tmp_path / "result.bin"
    requests = []
    args = {"files": [source.as_uri()]}
    route = {
        "image_to_pdf": "/img-to-pdf",
        "doc_to_pdf_start": "/doc-to-pdf",
        "doc_to_markdown_start": "/doc-to-markdown",
        "torrent_resolve": "/torrent/resolve",
        "subscription_download": "/subs/sub_1/render?target=surge",
        "subscription_qr_download": "/subs/sub_1/qr.png",
    }[name]
    binary = name in {
        "image_to_pdf",
        "subscription_download",
        "subscription_qr_download",
    }
    if name == "torrent_resolve":
        args = {"torrent_file": str(source), "save_dir": "/remote/downloads"}
    elif name.startswith("subscription_"):
        args = {"sub_id": "sub_1", "output_path": str(output)}
        if name == "subscription_download":
            args["target"] = "surge"
    elif name == "image_to_pdf":
        args.update(output_path=str(output), name="chosen.pdf")
    elif name == "doc_to_markdown_start":
        args.update(tier="basic", ocr_mode="ocr", image_analysis=False)

    def handler(request):
        requests.append(request)
        if binary:
            content_type = {
                "image_to_pdf": "application/pdf",
                "subscription_download": "text/plain",
                "subscription_qr_download": "image/png",
            }[name]
            return httpx.Response(
                200,
                content=b"binary-result",
                headers={"content-type": content_type},
            )
        return httpx.Response(200, json={"job_id": "j1", "infohash": "abc"})

    async def call(client):
        result = await client.call_tool(name, args)
        assert not result.is_error, result.content
        if binary:
            assert output.read_bytes() == b"binary-result"
            assert result.structured_content["path"] == str(output)
        else:
            assert result.structured_content == {"job_id": "j1", "infohash": "abc"}

    run_with_transport(handler, call)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://backend:9876/api" + route
    assert request.method == ("GET" if name.startswith("subscription_") else "POST")
    if request.method == "POST":
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        parts = list(message.iter_parts())
        files = [part for part in parts if part.get_filename()]
        assert len(files) == 1
        assert files[0].get_param("name", header="content-disposition") == (
            "file" if name == "torrent_resolve" else "files"
        )
        assert files[0].get_payload(decode=True) == b"uploaded-content"
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(
                decode=True
            ).decode()
            for part in parts
            if not part.get_filename()
        }
        expected = {
            "image_to_pdf": {"name": "chosen.pdf"},
            "torrent_resolve": {"save_dir": "/remote/downloads"},
            "doc_to_pdf_start": {},
            "doc_to_markdown_start": {
                "tier": "basic",
                "ocr_mode": "ocr",
                "image_analysis": "false",
            },
        }
        assert fields == expected[name]


def test_torrent_magnet_form_without_upload():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"infohash": "abc"})

    async def call(client):
        result = await client.call_tool(
            "torrent_resolve",
            {"magnet": "magnet:?xt=urn:btih:abc", "save_dir": "/remote"},
        )
        assert not result.is_error
        assert result.structured_content == {"infohash": "abc"}

    run_with_transport(handler, call)
    assert len(requests) == 1
    assert parse_qs(requests[0].content.decode()) == {
        "magnet": ["magnet:?xt=urn:btih:abc"],
        "save_dir": ["/remote"],
    }


def test_complete_tool_inventory_and_side_effect_annotations():
    async def inspect(client):
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert set(tools) == {case[0] for case in CASES} | FILE_TOOLS
        for tool in tools.values():
            assert tool.description
            assert tool.input_schema["type"] == "object"
            assert tool.output_schema is not None
        for name in (
            "torrent_status",
            "torrent_device_test",
            "torrent_resolve_status",
            "purge_scan",
            "dependencies_scan",
            "photos_dry_run",
            "image_to_pdf",
        ):
            assert tools[name].annotations.read_only_hint is False
        for name in (
            "purge_delete",
            "gather_start",
            "photos_run",
            "dependencies_apply_start",
            "remux_start",
            "subscription_delete",
        ):
            assert tools[name].annotations.destructive_hint is True
        assert tools["magnet_dedupe"].annotations.read_only_hint is True

    run_with_transport(lambda _: httpx.Response(200, json={}), inspect)


@pytest.mark.parametrize(
    "name,args",
    [
        ("torrent_resolve", {}),
        ("torrent_resolve", {"magnet": "magnet:x", "torrent_file": "/tmp/x"}),
        ("torrent_device_delete", {"device_id": "../other"}),
        ("subscription_get", {"sub_id": "x?target=raw"}),
        ("remux_start", {"selected": [], "out_folder": "/tmp", "max_workers": 9}),
        ("webpdf_scroll", {"steps": 21}),
        ("doc_to_markdown_start", {"files": [], "tier": "invalid"}),
        ("subscription_render", {"sub_id": "sub1", "target": "invalid"}),
    ],
)
def test_parameter_validation_before_http(name, args):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async def call(client):
        result = await client.call_tool(name, args)
        assert result.is_error

    run_with_transport(handler, call)
    assert requests == []


def test_explicit_git_commit_parameters():
    def handler(request):
        assert json.loads(request.content) == {
            "folder": "/isolated",
            "commit": True,
            "message": "test commit",
        }
        return httpx.Response(200, json={"job_id": "commit_job"})

    async def call(client):
        result = await client.call_tool(
            "dependencies_apply_start",
            {"folder": "/isolated", "commit": True, "message": "test commit"},
        )
        assert not result.is_error
        assert result.structured_content == {"job_id": "commit_job"}

    run_with_transport(handler, call)
