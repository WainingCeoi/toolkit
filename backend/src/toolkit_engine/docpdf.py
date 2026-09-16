"""Doc to PDF engine: flatten .docx revisions and comments, render via LibreOffice."""

import io
import shutil
import subprocess
import tempfile
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


def batch_to_pdf(soffice, docx_paths, out_dir):
    """Render many .docx to PDF in a single LibreOffice run (one cold start)."""
    docx_paths = [str(p) for p in docx_paths]
    return subprocess.run(
        [
            soffice,
            f"-env:UserInstallation=file://{LO_PROFILE}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            *docx_paths,
        ],
        capture_output=True,
        text=True,
        timeout=max(120, 20 * len(docx_paths)),
    )


def convert_batch(named_files, on_progress, soffice):
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
                result = batch_to_pdf(soffice, [job[0] for job in jobs], out_dir)
                stderr = result.stderr.strip()
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
