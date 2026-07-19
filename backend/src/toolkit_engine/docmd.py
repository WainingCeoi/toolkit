"""Doc to Markdown engine: convert PDFs, Office docs, and images to Markdown
with MinerU (subprocess). Lifted from the old Doc to Markdown page; the page's
button flow is re-expressed as convert_batch()."""

import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Project root — needed as the working directory when we have to fall back to
# `uv run mineru` (uv resolves the project venv relative to its cwd).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Files MinerU can parse (mirrors its CLI's accepted inputs).
ACCEPTED_TYPES = ["pdf", "png", "jpg", "jpeg", "docx", "pptx", "xlsx"]

# Per-file ceiling. Generous because the very first run downloads models and a
# long PDF can take minutes to parse on CPU/MPS.
PER_FILE_TIMEOUT = 1800


def find_mineru():
    """Return the MinerU command prefix as a list, or None if unavailable.

    Prefers the console script sitting next to the running interpreter (the
    project venv), then a `mineru` on PATH, then `uv run mineru` as a fallback.
    """
    venv_bin = Path(sys.executable).with_name("mineru")
    if venv_bin.exists():
        return [str(venv_bin)]
    on_path = shutil.which("mineru")
    if on_path:
        return [on_path]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "mineru"]
    return None


def build_mineru_cmd(
    prefix,
    input_path,
    out_dir,
    backend="pipeline",
    method="auto",
    lang="ch",
    effort="medium",
    formula=True,
    table=True,
):
    """Assemble the `mineru` argv for converting one file to Markdown.

    Only flags the chosen backend actually honours are appended: method/lang
    and the formula/table toggles are pipeline-only; effort is hybrid-only.
    """
    cmd = [
        *prefix,
        "-p",
        str(input_path),
        "-o",
        str(out_dir),
        "-b",
        backend,
    ]
    if backend == "pipeline":
        cmd += [
            "-m",
            method,
            "-l",
            lang,
            "-f",
            "true" if formula else "false",
            "-t",
            "true" if table else "false",
        ]
    elif backend.startswith("hybrid"):
        cmd += ["--effort", effort]
    return cmd


def find_markdown(out_dir):
    """Return the first Markdown file MinerU produced under out_dir, or None.

    MinerU writes to {out_dir}/{stem}/{method}/{stem}.md, but the stem folder can
    be truncated for long names and the method folder varies by backend
    (auto/ocr/vlm/hybrid_*) — so we just search the (per-file isolated) tree.
    """
    md_files = sorted(Path(out_dir).rglob("*.md"))
    return md_files[0] if md_files else None


def zip_tree(out_dir, archive):
    """Add every file under out_dir to the zip, preserving its relative path.

    out_dir holds exactly one file's output (its named stem folder), so the
    paths land under that folder in the archive.
    """
    out_dir = Path(out_dir)
    for path in sorted(out_dir.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(out_dir))


def convert_batch(named_files, options, on_progress, mineru_cmd):
    """Convert a batch of (name, bytes) uploads to a zip of Markdown trees.

    The old page's button flow: each file gets an isolated in_{idx}/out_{idx}
    pair (identical names and MinerU's whole-directory scanning can't collide),
    one MinerU subprocess per file with the per-file timeout, and every
    successful output tree is bundled into one in-memory zip. `options` carries
    backend/method/lang/effort/formula/table. Returns (zip_bytes, done, failed)
    — failed entries are (idx, name, error); zip_bytes is None when nothing
    converted.
    """
    done, failed, zip_bytes = [], [], None
    total = len(named_files)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        buffer = io.BytesIO()
        archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED)

        for idx, (name, content) in enumerate(named_files):
            # Sanitize the client-supplied filename to a bare basename before
            # ANY path use — a name like "../../x" or an absolute path would
            # otherwise escape the temp dir on the join below. Path().name
            # already strips every directory/traversal segment (and reduces
            # "." / ".." to ""), so an empty result is the only unsafe case;
            # a legitimate dotfile like ".hidden.pdf" is kept.
            safe = Path(name or "").name
            if not safe:
                failed.append((idx, name, "❌ Invalid filename."))
                continue
            # Show which file is in flight before its (long) MinerU run.
            on_progress(
                int(idx / total * 100),
                f"Converting {idx + 1}/{total} — {name}…",
            )
            # Isolate each file's input and output so identical names
            # and MinerU's whole-directory scanning can't collide.
            src = tmp / f"in_{idx}" / safe
            out_dir = tmp / f"out_{idx}"
            src.parent.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir()
            src.write_bytes(content)

            cmd = build_mineru_cmd(
                mineru_cmd,
                src,
                out_dir,
                options["backend"],
                options["method"],
                options["lang"],
                options["effort"],
                options["formula"],
                options["table"],
            )
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=PROJECT_ROOT,
                    timeout=PER_FILE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                failed.append((idx, name, "timed out"))
                continue

            if find_markdown(out_dir):
                zip_tree(out_dir, archive)
                done.append(name)
            else:
                reason = (result.stderr or result.stdout or "").strip()
                failed.append((idx, name, reason[-2000:] or "no Markdown produced"))

        archive.close()
        if done:
            zip_bytes = buffer.getvalue()

    on_progress(100, f"Converted {total}/{total} file(s).")
    return zip_bytes, done, failed
