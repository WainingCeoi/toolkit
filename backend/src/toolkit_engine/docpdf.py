"""Doc to PDF engine: flatten .docx revisions and comments, render via LibreOffice."""

import io
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from lxml import etree

# Reused, isolated LibreOffice profile so headless runs even if the GUI is open.
LO_PROFILE = Path(tempfile.gettempdir()) / "toolkit_libreoffice_profile"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag):
    return f"{{{W}}}{tag}"


# Tracked insertions / moves-in: accept by unwrapping (keep the inner content).
_UNWRAP = {_w("ins"), _w("moveTo")}
# Dropped with their content; the *Change records would otherwise leave revision marks.
_DROP = {
    _w("del"),
    _w("moveFrom"),
    _w("moveFromRangeStart"),
    _w("moveFromRangeEnd"),
    _w("moveToRangeStart"),
    _w("moveToRangeEnd"),
    _w("commentRangeStart"),
    _w("commentRangeEnd"),
    _w("commentReference"),
    _w("rPrChange"),
    _w("pPrChange"),
    _w("tblPrChange"),
    _w("tcPrChange"),
    _w("trPrChange"),
    _w("sectPrChange"),
    _w("tblGridChange"),
}


def find_soffice():
    """Locate the LibreOffice `soffice` binary, or return None."""
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    app = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return app if Path(app).exists() else None


def _mark_deleted(para):
    """True when the paragraph's own mark is a tracked deletion."""
    rpr = para.find(_w("pPr") + "/" + _w("rPr"))
    return rpr is not None and rpr.find(_w("del")) is not None


def _merge_into_next(para):
    """Accepting a deleted paragraph mark joins the paragraph with the next one."""
    parent = para.getparent()
    following = para.getnext()
    if parent is None or following is None or following.tag != _w("p"):
        return
    # The surviving mark is the next paragraph's, so its pPr stays first.
    at = 1 if len(following) and following[0].tag == _w("pPr") else 0
    for offset, child in enumerate([el for el in para if el.tag != _w("pPr")]):
        following.insert(at + offset, child)
    parent.remove(para)


def _apply_structural_deletions(root):
    """Drop rows and merge paragraphs whose deletion markers Word would honour."""
    for trpr in [el for el in root.iter(_w("trPr")) if el.find(_w("del")) is not None]:
        row = trpr.getparent()
        if row is not None and row.getparent() is not None:
            row.getparent().remove(row)
    for para in [el for el in root.iter(_w("p")) if _mark_deleted(el)]:
        _merge_into_next(para)


def _flatten_revisions(root):
    """Accept all tracked changes in a parsed Word XML part, in place."""
    # Unwrap insertions repeatedly so nested ins/moveTo are fully resolved.
    while True:
        targets = [el for el in root.iter() if el.tag in _UNWRAP]
        if not targets:
            break
        for el in targets:
            parent = el.getparent()
            if parent is None:
                continue
            idx = parent.index(el)
            for child in reversed(list(el)):
                parent.insert(idx, child)
            parent.remove(el)
    # Before the generic pass, which would strip the markers off their containers.
    _apply_structural_deletions(root)
    for el in [el for el in root.iter() if el.tag in _DROP]:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _transform_part(name, data):
    """Return revision-cleaned XML bytes for a Word part, or None to copy as-is."""
    if not name.endswith(".xml"):
        return None
    if name == "word/settings.xml":
        root = etree.fromstring(data)
        for el in [el for el in root if el.tag == _w("trackChanges")]:
            root.remove(el)
        return etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    if (
        name == "word/document.xml"
        or name.startswith("word/header")
        or name.startswith("word/footer")
        or name in ("word/footnotes.xml", "word/endnotes.xml")
    ):
        root = etree.fromstring(data)
        _flatten_revisions(root)
        return etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    return None


def clean_docx(src_path, dst_path):
    """Accept every tracked change and strip comment markers via direct XML."""
    src_path, dst_path = Path(src_path), Path(dst_path)
    with (
        zipfile.ZipFile(src_path) as zin,
        zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for item in zin.infolist():
            data = zin.read(item.filename)
            transformed = _transform_part(item.filename, data)
            if transformed is not None:
                zout.writestr(item.filename, transformed)
            else:
                # Passing the ZipInfo keeps the original compress_type.
                zout.writestr(item, data)
    return dst_path


def _terminate(proc):
    # SIGTERM first: a killed soffice leaves a lock file in the shared profile.
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def batch_to_pdf(soffice, docx_paths, out_dir, is_cancelled=None, poll=0.5):
    """Render many .docx to PDF in a single LibreOffice run (one cold start)."""
    docx_paths = [str(p) for p in docx_paths]
    cmd = [
        soffice,
        f"-env:UserInstallation=file://{LO_PROFILE}",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        *docx_paths,
    ]
    timeout = max(120, 20 * len(docx_paths))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.monotonic() + timeout
    # The context manager closes the pipes however this returns.
    with proc:
        while True:
            try:
                # Retrying communicate() after a timeout keeps the output read so far.
                stdout, stderr = proc.communicate(timeout=poll)
            except subprocess.TimeoutExpired:
                pass
            else:
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            if is_cancelled is not None and is_cancelled():
                _terminate(proc)
                return None
            if time.monotonic() >= deadline:
                _terminate(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)


def convert_batch(named_files, on_progress, soffice, is_cancelled=None):
    """Clean and convert (name, bytes) uploads to a zip of PDFs."""
    done, failed, zip_bytes = [], [], None
    total = len(named_files)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clean_dir = tmp / "cleaned"
        out_dir = tmp / "out"
        clean_dir.mkdir()
        out_dir.mkdir()

        jobs = []  # (cleaned_path, arcname, original_name)
        for idx, (name, content) in enumerate(named_files):
            on_progress(
                int(idx / total * 50),
                f"Cleaning {idx + 1}/{total} — {name}…",
            )
            stem = Path(name).stem
            try:
                src = tmp / f"src_{idx}.docx"
                src.write_bytes(content)
                cleaned = clean_dir / f"{idx}_{stem}.docx"
                clean_docx(src, cleaned)
                jobs.append((cleaned, f"{stem}.pdf", name))
            except Exception as e:
                failed.append((idx, name, str(e)))

        if jobs:
            on_progress(50, f"Converting {len(jobs)} file(s) with LibreOffice…")
            # A LibreOffice timeout keeps whatever PDFs it produced; the rest fail.
            try:
                result = batch_to_pdf(
                    soffice, [job[0] for job in jobs], out_dir, is_cancelled
                )
                stderr = "" if result is None else result.stderr.strip()
            except subprocess.TimeoutExpired:
                stderr = "LibreOffice timed out"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for i, (cleaned, arcname, name) in enumerate(jobs):
                    on_progress(
                        50 + int((i + 1) / len(jobs) * 50),
                        f"Bundling {i + 1}/{len(jobs)} — {arcname}…",
                    )
                    produced = out_dir / f"{cleaned.stem}.pdf"
                    if produced.exists():
                        archive.write(produced, arcname)
                        done.append(arcname)
                    else:
                        # cleaned stem is "{idx}_{stem}"; recover the input index.
                        idx = int(cleaned.stem.split("_", 1)[0])
                        failed.append((idx, name, stderr or "no PDF produced"))
            if done:
                zip_bytes = buffer.getvalue()

    on_progress(100, f"Converted {len(done)}/{total} file(s).")
    return zip_bytes, done, failed
