"""Real SDK stdio -> real HTTP -> FastAPI tests with isolated backend state.

External converters are opt-in: TOOLKIT_TEST_EXTERNAL=1 pytest -q
 tests/test_mcp_e2e.py. These use synthetic documents and temporary profiles;
 absent dependencies skip explicitly and installed-but-broken ones fail.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from PIL import Image

from toolkit_api.main import create_app
from toolkit_engine import docmd, docpdf

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture
def tcp_api(app_state, monkeypatch, tmp_path):
    """Serve the real app on TCP, never build state from the user's databases."""
    monkeypatch.setenv("TOOLKIT_DISABLED_TOOLS", "")
    monkeypatch.setenv("SUB_PUBLIC_HOST", "127.0.0.1")
    monkeypatch.setenv("SUB_ACCESS_TOKEN", "")
    monkeypatch.setattr(docpdf, "LO_PROFILE", tmp_path / "libreoffice-profile")
    app = create_app(state=app_state)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
    except BaseException:
        sock.close()
        raise
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                pytest.fail("Isolated FastAPI TCP server did not start")
            time.sleep(0.02)
        with httpx2.Client(trust_env=False) as http:
            assert http.get(f"{base_url}/api/health").status_code == 200
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive(), "Test FastAPI server did not shut down"


def stdio_client(base_url, timeout=30):
    return Client(
        StdioServerParameters(
            command=sys.executable,
            args=["-m", "toolkit_mcp", "--api-base-url", base_url],
            cwd=BACKEND,
            env=dict(os.environ),
        ),
        read_timeout_seconds=timeout,
    )


async def call(client, tool_name, **arguments):
    result = await client.call_tool(tool_name, arguments)
    assert not result.is_error, "\n".join(
        getattr(item, "text", str(item)) for item in result.content
    )
    assert result.structured_content is not None
    return result.structured_content


async def finished(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await call(client, "job_status", job_id=job_id)
        if status["state"] in {"done", "failed", "cancelled"}:
            return status
        await asyncio.sleep(0.05)
    await call(client, "job_cancel", job_id=job_id)
    pytest.fail(f"Job {job_id} did not finish within {timeout}s")


def inspect_pdf(path):
    # Native PyMuPDF teardown warnings must not leak into warning-as-error pytest.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys,warnings\n"
            "with warnings.catch_warnings():\n"
            " warnings.simplefilter('ignore')\n"
            " import pymupdf\n"
            "with pymupdf.open(sys.argv[1]) as doc:\n"
            " print(json.dumps([{'size': [p.rect.width,p.rect.height], "
            "'text':p.get_text()} for p in doc]))\n",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_stdio_discovery_image_pdf_and_errors(tcp_api, tmp_path):
    first = tmp_path / "page2.png"
    second = tmp_path / "page10.png"
    Image.new("RGB", (20, 30), "red").save(first)
    Image.new("RGB", (40, 50), "blue").save(second)
    output = tmp_path / "combined.pdf"

    async def exercise():
        async with stdio_client(tcp_api) as client:
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            assert len(names) >= 40
            assert {"image_to_pdf", "magnet_dedupe", "job_status"} <= names
            result = await call(
                client,
                "image_to_pdf",
                files=[second.as_uri(), str(first)],
                name="combined.pdf",
                output_path=str(output),
            )
            assert result["path"] == str(output)
            assert output.read_bytes().startswith(b"%PDF")
            assert [p["size"] for p in inspect_pdf(output)] == [
                [20.0, 30.0],
                [40.0, 50.0],
            ]
            before = output.read_bytes()
            refused = await client.call_tool(
                "image_to_pdf",
                {"files": [str(first)], "output_path": str(output)},
            )
            assert refused.is_error
            assert output.read_bytes() == before
            await call(
                client,
                "image_to_pdf",
                files=[str(first)],
                output_path=str(output),
                overwrite=True,
            )
            assert len(inspect_pdf(output)) == 1
            invalid_image = tmp_path / "broken.png"
            invalid_image.write_text("not an image")
            failed = await client.call_tool(
                "image_to_pdf",
                {
                    "files": [str(invalid_image)],
                    "output_path": str(tmp_path / "failed.pdf"),
                },
            )
            assert failed.is_error
            assert not (tmp_path / "failed.pdf").exists()
            absent = await client.call_tool("job_status", {"job_id": "unknown"})
            assert absent.is_error
            deduped = await call(client, "magnet_dedupe", links=["a", "a", "b"])
            assert deduped == {"unique": ["a", "b"], "count": 2}

    asyncio.run(exercise())


def test_stdio_gather_purge_and_shared_job_state(tcp_api, tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "sample.txt").write_text("isolated move input")
    (nested / "keep.bin").write_bytes(b"keep")
    target = tmp_path / "target"

    async def exercise():
        async with stdio_client(tcp_api) as client:
            started = await call(
                client,
                "gather_start",
                source=str(source),
                target=str(target),
                custom="txt",
            )
            job_id = started["job_id"]
            status = await finished(client, job_id)
            assert status["state"] == "done", status
            assert status["result"]["moved"] == ["sample.txt"]
            assert (target / "sample.txt").read_text() == "isolated move input"
            assert not (nested / "sample.txt").exists()
            assert (nested / "keep.bin").exists()
            # The HTTP client observes exactly the job created by the MCP client.
            with httpx2.Client(trust_env=False) as http:
                shared = http.get(f"{tcp_api}/api/jobs/{job_id}").json()
            assert shared == status
            listing = await call(client, "jobs_list", tool="file-gatherer")
            assert job_id in {job["id"] for job in listing["jobs"]}
            scan = await call(
                client, "purge_scan", folder=str(target), patterns_raw="txt"
            )
            assert scan["files"] == [str(target / "sample.txt")]
            assert (target / "sample.txt").exists()
            deletion = await call(client, "purge_delete", scan_id=scan["scan_id"])
            status = await finished(client, deletion["job_id"])
            assert status["state"] == "done", status
            assert not (target / "sample.txt").exists()
            assert (nested / "keep.bin").exists()
            duplicate = await client.call_tool(
                "purge_delete", {"scan_id": scan["scan_id"]}
            )
            assert duplicate.is_error  # Single-use server scan cannot be reused.

    asyncio.run(exercise())


def test_stdio_subscriptions_and_file_downloads(tcp_api, tmp_path):
    async def exercise():
        async with stdio_client(tcp_api) as client:
            generated = await call(
                client,
                "subscription_generate",
                node_links="trojan://synthetic-secret@origin.example.com:443#Test",
                preferred_ips="192.0.2.10",
            )
            sub_id = generated["sub_id"]
            assert generated["counts"]["output_nodes"] == 1
            rendered = await call(
                client, "subscription_render", sub_id=sub_id, target="clash"
            )
            assert "192.0.2.10" in rendered["text"]
            config_path = tmp_path / "subscription.yaml"
            await call(
                client,
                "subscription_download",
                sub_id=sub_id,
                target="clash",
                output_path=str(config_path),
            )
            assert "192.0.2.10" in config_path.read_text()
            qr_path = tmp_path / "subscription.png"
            await call(
                client,
                "subscription_qr_download",
                sub_id=sub_id,
                output_path=str(qr_path),
            )
            with Image.open(qr_path) as qr:
                assert qr.format == "PNG"
                qr.verify()

    asyncio.run(exercise())


def test_stdio_cancellation_uses_existing_backend_registry(tcp_api, app_state):
    # A deterministic cancellable worker is a fixture, not a production job manager.
    entered = threading.Event()

    def worker(job):
        entered.set()
        while not job.cancelled:
            time.sleep(0.01)
        return {"stopped_cooperatively": True}

    job = app_state.jobs.submit("mcp-cancel-test", [], worker)
    assert entered.wait(timeout=5)

    async def exercise():
        async with stdio_client(tcp_api) as client:
            initial = await call(client, "job_status", job_id=job.id)
            assert initial["state"] == "running"
            cancelled = await call(client, "job_cancel", job_id=job.id)
            assert cancelled["cancelling"] is True
            terminal = await finished(client, job.id)
            assert terminal["state"] == "cancelled"
            assert terminal["result"]["stopped_cooperatively"] is True

    asyncio.run(exercise())


def test_stdio_backend_unavailable_is_tool_error():
    # Bind without listening to reserve a guaranteed unavailable local endpoint.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"

        async def exercise():
            async with stdio_client(url) as client:
                assert (await client.list_tools()).tools
                response = await client.call_tool("toolkit_health", {})
                assert response.is_error
                text = " ".join(item.text for item in response.content)
                assert "connect" in text.lower() or "backend" in text.lower()

        asyncio.run(exercise())


def make_docx(path):
    """Minimal valid Office document with unique plain text and no external assets."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats'
            '.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
            '2006/main"><w:body><w:p><w:r><w:t>Toolkit MCP synthetic document 8427'
            "</w:t></w:r></w:p></w:body></w:document>",
        )


def require_external(binary, name):
    if os.environ.get("TOOLKIT_TEST_EXTERNAL") != "1":
        pytest.skip("Set TOOLKIT_TEST_EXTERNAL=1 to run installed external converters")
    if not binary:
        pytest.skip(f"{name} is not installed")


def test_stdio_real_libreoffice_document_to_pdf(tcp_api, tmp_path):
    require_external(docpdf.find_soffice(), "LibreOffice")
    source = tmp_path / "synthetic.docx"
    make_docx(source)

    async def exercise():
        async with stdio_client(tcp_api) as client:
            started = await call(client, "doc_to_pdf_start", files=[str(source)])
            status = await finished(client, started["job_id"], timeout=90)
            assert status["state"] == "done", status
            assert not status["result"]["failed"], status
            archive_path = tmp_path / "pdfs.zip"
            await call(
                client,
                "artifact_download",
                artifact_id=status["result"]["artifact_id"],
                output_path=str(archive_path),
            )
            with zipfile.ZipFile(archive_path) as archive:
                assert archive.testzip() is None
                pdfs = [n for n in archive.namelist() if n.endswith(".pdf")]
                assert len(pdfs) == 1
                pdf_path = tmp_path / "converted.pdf"
                pdf_path.write_bytes(archive.read(pdfs[0]))
            assert "8427" in "".join(page["text"] for page in inspect_pdf(pdf_path))

    asyncio.run(exercise())


def test_stdio_real_mineru_office_markdown(tcp_api, tmp_path, monkeypatch):
    require_external(docmd.find_mineru(), "MinerU")
    # Native Office Flash requires no model downloads. Fail if it unexpectedly does.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-cache"))
    source = tmp_path / "synthetic.docx"
    make_docx(source)

    async def exercise():
        async with stdio_client(tcp_api) as client:
            started = await call(
                client,
                "doc_to_markdown_start",
                files=[str(source)],
                tier="flash",
                image_analysis=False,
            )
            status = await finished(client, started["job_id"], timeout=90)
            assert status["state"] == "done", status
            assert not status["result"]["failed"], status
            body = await call(
                client,
                "artifact_markdown",
                artifact_id=status["result"]["artifact_id"],
            )
            assert "Toolkit MCP synthetic document 8427" in json.dumps(body)

    asyncio.run(exercise())


def test_stdio_real_ffmpeg_remux(tcp_api, tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    require_external(ffmpeg, "FFmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffprobe, "FFmpeg is installed, but ffprobe is unavailable"
    source = tmp_path / "synthetic.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=0.2",
            "-c:v",
            "ffv1",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    destination = tmp_path / "remuxed"

    async def exercise():
        async with stdio_client(tcp_api) as client:
            scanned = await call(client, "remux_scan", folder=str(tmp_path))
            assert str(source) in {video["path"] for video in scanned["videos"]}
            started = await call(
                client,
                "remux_start",
                selected=[str(source)],
                out_folder=str(destination),
                multi_audio=True,
                audio_value="",
                include_subtitle=False,
            )
            status = await finished(client, started["job_id"], timeout=30)
            assert status["state"] == "done", status
            assert status["result"]["successful"] == 1, status
            assert (destination / source.name).stat().st_size > 0

    asyncio.run(exercise())
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,codec_type,width,height",
            "-of",
            "json",
            str(destination / source.name),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(probe.stdout)["streams"] == [
        {"codec_name": "ffv1", "codec_type": "video", "width": 64, "height": 64}
    ]


def test_stdio_real_chrome_web_capture(tcp_api, app_state, tmp_path, monkeypatch):
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    require_external(chrome.is_file(), "Google Chrome")
    # Keep real driver downloads bounded. WDM 4.1 otherwise uses no HTTP timeout;
    # this changes only waiting limits, never the response or browser behavior.
    import webdriver_manager.chrome
    from webdriver_manager.core.driver_cache import DriverCacheManager
    from webdriver_manager.core.http import WDMHttpClient

    real_manager = webdriver_manager.chrome.ChromeDriverManager

    def isolated_manager():
        return real_manager(cache_manager=DriverCacheManager(root_dir=str(tmp_path)))

    monkeypatch.setattr(
        webdriver_manager.chrome, "ChromeDriverManager", isolated_manager
    )
    real_get = WDMHttpClient.get

    def bounded_get(self, url, **kwargs):
        kwargs.setdefault("timeout", (3, 5))
        return real_get(self, url, **kwargs)

    monkeypatch.setattr(WDMHttpClient, "get", bounded_get)
    image_bytes = BytesIO()
    Image.new("RGB", (32, 48), "green").save(image_bytes, format="PNG")
    encoded = base64.b64encode(image_bytes.getvalue()).decode("ascii")
    html = (
        "<html><head><title>Toolkit synthetic web images</title></head><body>"
        f'<img class="bi" src="data:image/png;base64,{encoded}">'
        '<div style="height:2000px">Synthetic scrolling fixture</div>'
        f'<img class="bi" src="data:image/png;base64,{encoded}">'
        "</body></html>"
    )
    url = "data:text/html," + quote(html)
    output = tmp_path / "web-capture.pdf"

    async def exercise():
        async with stdio_client(tcp_api, timeout=120) as client:
            opened = await call(client, "webpdf_open", url=url)
            assert opened["open"] is True
            # Selenium launches a new temporary profile, never the user's profile.
            chrome_info = app_state.browser._driver.capabilities.get("chrome", {})
            profile = chrome_info.get("userDataDir", "")
            assert profile
            assert "Library/Application Support/Google/Chrome" not in profile
            status = await call(client, "webpdf_status")
            assert status["open"] is True
            scrolled = await call(client, "webpdf_scroll", steps=2, wait_ms=0)
            assert scrolled
            captured = await call(client, "webpdf_capture")
            assert captured["pages"] == 2
            assert captured["skipped"] == 0
            await call(
                client,
                "artifact_download",
                artifact_id=captured["artifact_id"],
                output_path=str(output),
            )
            assert len(inspect_pdf(output)) == 2
            assert (await call(client, "webpdf_status"))["open"] is False

    asyncio.run(exercise())
