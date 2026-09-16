"""Doc to PDF + Doc to Markdown: engine units and router validation/job flow."""

from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lxml import etree

from toolkit_api.jobs import FINISHED_STATES
from toolkit_api.main import create_app
from toolkit_engine import docmd, docpdf


@pytest.fixture
def tool_client(app_state):
    app = create_app(state=app_state)
    with TestClient(app) as c:
        yield c


def wait_for_job(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in FINISHED_STATES:
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# --- Doc to Markdown engine units ---
def test_build_mineru_cmd_pipeline_includes_method_lang_and_toggles():
    cmd = docmd.build_mineru_cmd(
        ["mineru"],
        "in.pdf",
        "out",
        backend="pipeline",
        method="ocr",
        lang="ch",
        formula=True,
        table=False,
    )
    assert cmd[:7] == ["mineru", "-p", "in.pdf", "-o", "out", "-b", "pipeline"]
    joined = " ".join(cmd)
    assert "-m ocr" in joined
    assert "-l ch" in joined
    assert "-f true" in joined
    assert "-t false" in joined
    assert "--effort" not in joined


def test_build_mineru_cmd_hybrid_uses_effort_not_pipeline_flags():
    cmd = docmd.build_mineru_cmd(
        ["mineru"],
        "in.pdf",
        "out",
        backend="hybrid-engine",
        effort="high",
    )
    joined = " ".join(cmd)
    assert "--effort high" in joined
    assert "-m " not in joined
    assert "-l " not in joined


def test_find_mineru_is_none_when_only_uv_is_installed(monkeypatch, tmp_path):
    # `uv run mineru` cannot install the optional docmd extra, so it is not a fallback.
    monkeypatch.setattr(docmd.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(
        docmd.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    assert docmd.find_mineru() is None


def test_run_mineru_kills_the_child_on_cancel():
    started = time.monotonic()
    result = docmd.run_mineru(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        is_cancelled=lambda: True,
        poll=0.05,
    )
    assert result is None
    assert time.monotonic() - started < 10


def test_docmd_convert_batch_stops_at_the_cancelled_file(monkeypatch):
    calls = []

    def fake_run(cmd, *_args, **_kwargs):
        calls.append(cmd)
        return None  # the child was killed mid-file

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    options = {
        "backend": "pipeline",
        "method": "auto",
        "lang": "ch",
        "effort": "medium",
        "formula": True,
        "table": True,
    }
    zip_bytes, done, failed = docmd.convert_batch(
        [("a.pdf", b"one"), ("b.pdf", b"two")],
        options,
        lambda pct, text: None,
        ["mineru"],
        lambda: True,
    )
    assert len(calls) == 1
    assert (zip_bytes, done, failed) == (None, [], [])


def test_docmd_convert_batch_final_message_counts_only_successes(monkeypatch):
    messages = []

    def fake_run(cmd, *_args, **_kwargs):
        in_path = Path(cmd[cmd.index("-p") + 1])
        if in_path.parent.name == "in_0":
            md_dir = Path(cmd[cmd.index("-o") + 1]) / "a" / "auto"
            md_dir.mkdir(parents=True)
            (md_dir / "a.md").write_text("# hi")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="boom")

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    options = {
        "backend": "pipeline",
        "method": "auto",
        "lang": "ch",
        "effort": "medium",
        "formula": True,
        "table": True,
    }
    docmd.convert_batch(
        [("a.pdf", b"one"), ("b.pdf", b"two")],
        options,
        lambda pct, text: messages.append(text),
        ["mineru"],
    )
    assert messages[-1] == "Converted 1/2 file(s)."


# --- Doc to PDF engine units ---
DOCUMENT_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:ins w:id="1" w:author="a">
        <w:r><w:t>inserted text</w:t></w:r>
      </w:ins>
      <w:del w:id="2" w:author="a">
        <w:r><w:delText>deleted text</w:delText></w:r>
      </w:del>
      <w:r><w:t>plain text</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

SETTINGS_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:trackChanges/>
</w:settings>
"""


def make_docx(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", DOCUMENT_XML)
        z.writestr("word/settings.xml", SETTINGS_XML)
    return path


def test_clean_docx_accepts_insertions_drops_deletions_and_trackchanges(tmp_path):
    src = make_docx(tmp_path / "src.docx")
    dst = tmp_path / "clean.docx"
    docpdf.clean_docx(src, dst)

    with zipfile.ZipFile(dst) as z:
        doc = etree.fromstring(z.read("word/document.xml"))
        settings = etree.fromstring(z.read("word/settings.xml"))

    tags = {el.tag for el in doc.iter()}
    assert docpdf._w("ins") not in tags
    assert docpdf._w("del") not in tags
    texts = [el.text for el in doc.iter(docpdf._w("t"))]
    assert "inserted text" in texts
    assert "plain text" in texts
    assert b"deleted text" not in etree.tostring(doc)
    assert docpdf._w("trackChanges") not in {el.tag for el in settings.iter()}


STRUCTURAL_DELETIONS_XML = f"""<w:document xmlns:w="{docpdf.W}"><w:body>
  <w:p>
    <w:pPr><w:rPr><w:del w:id="1"/></w:rPr></w:pPr>
    <w:r><w:t>first</w:t></w:r>
  </w:p>
  <w:p><w:r><w:t>second</w:t></w:r></w:p>
  <w:tbl>
    <w:tr>
      <w:trPr><w:del w:id="2"/></w:trPr>
      <w:tc><w:p><w:r><w:t>gone</w:t></w:r></w:p></w:tc>
    </w:tr>
    <w:tr><w:tc><w:p><w:r><w:t>kept</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
</w:body></w:document>""".encode()


def test_flatten_revisions_merges_deleted_marks_and_drops_deleted_rows():
    root = etree.fromstring(STRUCTURAL_DELETIONS_XML)
    docpdf._flatten_revisions(root)

    paragraphs = [
        "".join(t.text or "" for t in p.iter(docpdf._w("t")))
        for p in root.iter(docpdf._w("p"))
    ]
    assert paragraphs == ["firstsecond", "kept"]
    assert len(list(root.iter(docpdf._w("tr")))) == 1


MOVED_PARAGRAPH_XML = f"""<w:document xmlns:w="{docpdf.W}"><w:body>
  <w:p>
    <w:pPr><w:rPr><w:moveFrom w:id="1"/></w:rPr></w:pPr>
    <w:moveFrom w:id="2"><w:r><w:t>moved away</w:t></w:r></w:moveFrom>
  </w:p>
  <w:p><w:r><w:t>next</w:t></w:r></w:p>
</w:body></w:document>""".encode()


def test_flatten_revisions_merges_move_source_paragraph_marks():
    # Word marks a moved-out paragraph mark with w:moveFrom, not w:del.
    root = etree.fromstring(MOVED_PARAGRAPH_XML)
    docpdf._flatten_revisions(root)

    paragraphs = [
        "".join(t.text or "" for t in p.iter(docpdf._w("t")))
        for p in root.iter(docpdf._w("p"))
    ]
    assert paragraphs == ["next"]


def test_batch_to_pdf_kills_soffice_on_cancel(tmp_path):
    fake_soffice = tmp_path / "soffice"
    fake_soffice.write_text("#!/bin/sh\nsleep 30\n")
    fake_soffice.chmod(0o755)

    started = time.monotonic()
    result = docpdf.batch_to_pdf(
        str(fake_soffice), [tmp_path / "a.docx"], tmp_path, lambda: True, 0.05
    )
    assert result is None
    assert time.monotonic() - started < 10


# --- Doc to PDF router ---
def test_docpdf_post_without_files_is_400(tool_client):
    resp = tool_client.post("/api/doc-to-pdf")
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Please select at least one Word (.docx) file first."
    )


def test_docpdf_post_without_libreoffice_is_400(tool_client, monkeypatch):
    monkeypatch.setattr(docpdf, "find_soffice", lambda: None)
    resp = tool_client.post(
        "/api/doc-to-pdf",
        files={"files": ("a.docx", b"stub", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Missing required tool: LibreOffice (`brew install --cask libreoffice`)"
    )


def test_docpdf_post_rejects_non_docx(tool_client):
    resp = tool_client.post(
        "/api/doc-to-pdf",
        files={"files": ("a.txt", b"stub", "text/plain")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Only Word (.docx) files are supported."


def test_docpdf_job_bundles_pdfs_into_zip_artifact(tool_client, monkeypatch, tmp_path):
    monkeypatch.setattr(docpdf, "find_soffice", lambda: "/stub/soffice")

    def fake_batch_to_pdf(soffice, docx_paths, out_dir, *_args):
        for p in docx_paths:
            (Path(out_dir) / f"{Path(p).stem}.pdf").write_bytes(b"%PDF-1.4 stub")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(docpdf, "batch_to_pdf", fake_batch_to_pdf)

    src = make_docx(tmp_path / "report.docx")
    resp = tool_client.post(
        "/api/doc-to-pdf",
        files={"files": ("report.docx", src.read_bytes(), "application/octet-stream")},
    )
    assert resp.status_code == 200

    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["done"] == ["report.pdf"]
    assert snap["result"]["failed"] == []
    assert snap["result"]["filename"] == "converted_pdfs.zip"
    assert snap["items"][0]["state"] == "done"

    download = tool_client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as z:
        assert z.namelist() == ["report.pdf"]


def test_docpdf_job_cancels_while_the_soffice_lock_is_held(
    tool_client, app_state, monkeypatch, tmp_path
):
    monkeypatch.setattr(docpdf, "find_soffice", lambda: "/stub/soffice")
    src = make_docx(tmp_path / "a.docx").read_bytes()

    app_state.soffice_lock.acquire()
    try:
        resp = tool_client.post(
            "/api/doc-to-pdf",
            files={"files": ("a.docx", src, "application/octet-stream")},
        )
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]
        assert tool_client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        snap = wait_for_job(tool_client, job_id)
    finally:
        app_state.soffice_lock.release()

    assert snap["state"] == "cancelled"


# --- Doc to Markdown router ---
def test_docmd_post_without_files_is_400(tool_client):
    resp = tool_client.post("/api/doc-to-markdown")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "❌ Please select at least one file first."


def test_docmd_health_reports_booleans(tool_client):
    body = tool_client.get("/api/doc-to-markdown/health").json()
    assert set(body) == {"mineru", "backend_ready"}
    assert all(isinstance(v, bool) for v in body.values())


def test_docmd_job_zips_markdown_artifact(tool_client, monkeypatch):
    monkeypatch.setattr(docmd, "find_mineru", lambda: ["mineru"])

    def fake_run(cmd, *_args, **_kwargs):
        out_dir = Path(cmd[cmd.index("-o") + 1])
        md_dir = out_dir / "notes" / "auto"
        md_dir.mkdir(parents=True)
        (md_dir / "notes.md").write_text("# hi")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    resp = tool_client.post(
        "/api/doc-to-markdown",
        files={"files": ("notes.pdf", b"%PDF-1.4 stub", "application/pdf")},
        data={"backend": "pipeline", "method": "txt", "formula": "false"},
    )
    assert resp.status_code == 200

    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert snap["result"]["done"] == ["notes.pdf"]
    assert snap["result"]["failed"] == []
    assert snap["result"]["filename"] == "markdown.zip"
    assert snap["items"][0]["state"] == "done"

    download = tool_client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as z:
        assert "notes.pdf/notes/auto/notes.md" in z.namelist()


def test_docmd_post_rejects_unsupported_type(tool_client):
    resp = tool_client.post(
        "/api/doc-to-markdown",
        files={"files": ("evil.exe", b"stub", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "❌ Unsupported file type: evil.exe. Accepted: "
        + ", ".join(docmd.ACCEPTED_TYPES)
    )


def test_docmd_convert_batch_sanitizes_traversal_filename(monkeypatch):
    captured = {}

    def fake_run(cmd, *_args, **_kwargs):
        in_path = Path(cmd[cmd.index("-p") + 1])
        captured["in_path"] = in_path
        out_dir = Path(cmd[cmd.index("-o") + 1])
        md_dir = out_dir / "x" / "auto"
        md_dir.mkdir(parents=True)
        (md_dir / "x.md").write_text("# hi")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    options = {
        "backend": "pipeline",
        "method": "auto",
        "lang": "ch",
        "effort": "medium",
        "formula": True,
        "table": True,
    }
    docmd.convert_batch(
        [("../../pwned.pdf", b"stub")],
        options,
        lambda pct, text: None,
        ["mineru"],
    )

    in_path = captured["in_path"]
    assert in_path.name == "pwned.pdf"
    assert ".." not in in_path.parts


def test_docmd_same_stem_different_extension_keep_separate_trees(
    tool_client, monkeypatch
):
    monkeypatch.setattr(docmd, "find_mineru", lambda: ["mineru"])

    def fake_run(cmd, *_args, **_kwargs):
        # MinerU names its output tree after the input stem, so both are "report".
        md_dir = Path(cmd[cmd.index("-o") + 1]) / "report" / "auto"
        md_dir.mkdir(parents=True)
        (md_dir / "report.md").write_text(Path(cmd[cmd.index("-p") + 1]).suffix)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    resp = tool_client.post(
        "/api/doc-to-markdown",
        files=[
            ("files", ("report.pdf", b"%PDF-1.4 one", "application/pdf")),
            ("files", ("report.docx", b"two", "application/octet-stream")),
        ],
        data={"backend": "pipeline"},
    )
    assert resp.status_code == 200

    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    download = tool_client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    with zipfile.ZipFile(io.BytesIO(download.content)) as z:
        assert sorted(z.namelist()) == [
            "report.docx/report/auto/report.md",
            "report.pdf/report/auto/report.md",
        ]


def test_docmd_duplicate_names_get_index_correct_states(tool_client, monkeypatch):
    monkeypatch.setattr(docmd, "find_mineru", lambda: ["mineru"])

    def fake_run(cmd, *_args, **_kwargs):
        in_path = Path(cmd[cmd.index("-p") + 1])
        out_dir = Path(cmd[cmd.index("-o") + 1])
        if in_path.parent.name == "in_1":  # only the second upload produces md
            md_dir = out_dir / "a" / "auto"
            md_dir.mkdir(parents=True)
            (md_dir / "a.md").write_text("# hi")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="boom")

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    resp = tool_client.post(
        "/api/doc-to-markdown",
        files=[
            ("files", ("a.pdf", b"%PDF-1.4 one", "application/pdf")),
            ("files", ("a.pdf", b"%PDF-1.4 two", "application/pdf")),
        ],
        data={"backend": "pipeline"},
    )
    assert resp.status_code == 200

    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert [item["state"] for item in snap["items"]] == ["failed", "done"]


def test_docmd_cancel_publishes_the_files_already_converted(tool_client, monkeypatch):
    monkeypatch.setattr(docmd, "find_mineru", lambda: ["mineru"])
    reached_second = threading.Event()
    cancel_landed = threading.Event()

    def fake_run(cmd, *_args, **_kwargs):
        in_path = Path(cmd[cmd.index("-p") + 1])
        if in_path.parent.name == "in_1":
            reached_second.set()
            cancel_landed.wait(5)
            return None  # the child was killed mid-file
        md_dir = Path(cmd[cmd.index("-o") + 1]) / "a" / "auto"
        md_dir.mkdir(parents=True)
        (md_dir / "a.md").write_text("# one")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docmd, "run_mineru", fake_run)

    resp = tool_client.post(
        "/api/doc-to-markdown",
        files=[
            ("files", ("a.pdf", b"%PDF-1.4 one", "application/pdf")),
            ("files", ("b.pdf", b"%PDF-1.4 two", "application/pdf")),
        ],
        data={"backend": "pipeline"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert reached_second.wait(5)
    assert tool_client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    cancel_landed.set()

    snap = wait_for_job(tool_client, job_id)
    assert snap["state"] == "cancelled"
    assert snap["result"]["done"] == ["a.pdf"]
    assert snap["result"]["failed"] == []
    assert [item["state"] for item in snap["items"]] == ["done", "pending"]

    download = tool_client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as z:
        assert z.namelist() == ["a.pdf/a/auto/a.md"]


def test_docpdf_cancel_publishes_the_pdfs_already_rendered(
    tool_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(docpdf, "find_soffice", lambda: "/stub/soffice")
    rendering = threading.Event()
    cancel_landed = threading.Event()

    def fake_batch_to_pdf(soffice, docx_paths, out_dir, *_args):
        # soffice is killed mid-batch, with the first file's PDF already on disk.
        (Path(out_dir) / f"{Path(docx_paths[0]).stem}.pdf").write_bytes(b"%PDF stub")
        rendering.set()
        cancel_landed.wait(5)
        return None

    monkeypatch.setattr(docpdf, "batch_to_pdf", fake_batch_to_pdf)

    src = make_docx(tmp_path / "x.docx").read_bytes()
    resp = tool_client.post(
        "/api/doc-to-pdf",
        files=[
            ("files", ("a.docx", src, "application/octet-stream")),
            ("files", ("b.docx", src, "application/octet-stream")),
        ],
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert rendering.wait(5)
    assert tool_client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    cancel_landed.set()

    snap = wait_for_job(tool_client, job_id)
    assert snap["state"] == "cancelled"
    assert snap["result"]["done"] == ["a.pdf"]
    assert snap["result"]["failed"] == [["b.docx", "cancelled"]]
    assert [item["state"] for item in snap["items"]] == ["done", "failed"]

    download = tool_client.get(f"/api/artifacts/{snap['result']['artifact_id']}")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as z:
        assert z.namelist() == ["a.pdf"]


def test_docpdf_duplicate_names_get_index_correct_states(
    tool_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(docpdf, "find_soffice", lambda: "/stub/soffice")

    def fake_batch_to_pdf(soffice, docx_paths, out_dir, *_args):
        for p in docx_paths:
            # Cleaned files are named "{idx}_{stem}" — render only index 1.
            if Path(p).stem.startswith("1_"):
                (Path(out_dir) / f"{Path(p).stem}.pdf").write_bytes(b"%PDF stub")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="boom")

    monkeypatch.setattr(docpdf, "batch_to_pdf", fake_batch_to_pdf)

    src = make_docx(tmp_path / "a.docx").read_bytes()
    resp = tool_client.post(
        "/api/doc-to-pdf",
        files=[
            ("files", ("a.docx", src, "application/octet-stream")),
            ("files", ("a.docx", src, "application/octet-stream")),
        ],
    )
    assert resp.status_code == 200

    snap = wait_for_job(tool_client, resp.json()["job_id"])
    assert snap["state"] == "done"
    assert [item["state"] for item in snap["items"]] == ["failed", "done"]
