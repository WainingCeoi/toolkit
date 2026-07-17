"""Doc to PDF engine: accept tracked changes, strip comments, render to PDF
with LibreOffice. Lifted from the old Doc to PDF page; the page's button flow
is re-expressed as convert_batch()."""

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
# Accept by dropping the element and its content: deletions, moves-out, comment
# markers, and format-change records (which would otherwise leave revision marks).
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
    """Accept all tracked changes in a parsed Word XML part, in place.

    Insertions/moves-in are unwrapped (content kept); deletions, moves-out,
    comment markers, and format-change records are removed outright — so nothing
    is left for a renderer to mark up.
    """
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
    # Drop deletions, comment markers, and format-change records.
    for el in [el for el in root.iter() if el.tag in _DROP]:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def clean_docx(src_path, dst_path):
    """Accept every tracked change and strip comment markers via direct XML.

    Operates on the .docx parts (document body, headers/footers, notes) and
    turns off change recording in settings.xml, so the result carries no
    revision markup and renders to PDF without any marks or comments.
    """
    src_path, dst_path = Path(src_path), Path(dst_path)
    with zipfile.ZipFile(src_path) as zin:
        names = zin.namelist()
        parts = {name: zin.read(name) for name in names}

    for name in names:
        if not name.endswith(".xml"):
            continue
        if name == "word/settings.xml":
            root = etree.fromstring(parts[name])
            for el in [el for el in root if el.tag == _w("trackChanges")]:
                root.remove(el)
            parts[name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
        elif (
            name == "word/document.xml"
            or name.startswith("word/header")
            or name.startswith("word/footer")
            or name in ("word/footnotes.xml", "word/endnotes.xml")
        ):
            root = etree.fromstring(parts[name])
            _flatten_revisions(root)
            parts[name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )

    with zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            zout.writestr(name, parts[name])
    return dst_path


def batch_to_pdf(soffice, docx_paths, out_dir):
    """Render many .docx to PDF in a single LibreOffice run (one cold start).

    LibreOffice starts once and converts every file in that process, which is
    much faster than one invocation per file. Each PDF lands in out_dir named
    after its input stem. Returns the completed subprocess so the caller can
    surface a failure reason; the caller decides per-file success by checking
    which expected PDFs were actually produced.
    """
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
    """Clean and convert a batch of (name, bytes) uploads to a zip of PDFs.

    The old page's button flow: clean each upload into a temp dir (per-file
    failure capture), render everything in one batched LibreOffice run, then
    bundle the produced PDFs into an in-memory zip. Cleaning spans the first
    half of the progress range; the batched conversion sits at 50; bundling
    fills the second half. Returns (zip_bytes, done, failed) — zip_bytes is
    None when nothing converted.
    """
    done, failed, zip_bytes = [], [], None
    total = len(named_files)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clean_dir = tmp / "cleaned"
        out_dir = tmp / "out"
        clean_dir.mkdir()
        out_dir.mkdir()

        # Clean each upload (fast, in-memory XML) into one temp dir.
        # Cleaning spans the first half of the bar; the conversion below
        # is one batched LibreOffice run with no per-file progress.
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
                failed.append((name, str(e)))

        # Convert every cleaned file in a single LibreOffice run, then
        # bundle the produced PDFs into an in-memory zip.
        if jobs:
            on_progress(50, f"Converting {len(jobs)} file(s) with LibreOffice…")
            result = batch_to_pdf(soffice, [job[0] for job in jobs], out_dir)
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
                        failed.append(
                            (name, result.stderr.strip() or "no PDF produced")
                        )
            if done:
                zip_bytes = buffer.getvalue()

    on_progress(100, f"Converted {len(done)}/{total} file(s).")
    return zip_bytes, done, failed
