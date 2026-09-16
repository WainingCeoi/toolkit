"""Doc to Markdown engine: MinerU subprocess conversion, batched into a zip."""

import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# cwd for the `uv run mineru` fallback: uv resolves the venv relative to cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Files MinerU can parse (mirrors its CLI's accepted inputs).
ACCEPTED_TYPES = ["pdf", "png", "jpg", "jpeg", "docx", "pptx", "xlsx"]

# Per-file ceiling; the first run downloads models and CPU parsing takes minutes.
PER_FILE_TIMEOUT = 1800


def find_mineru():
    """Return the MinerU command prefix as a list, or None if unavailable."""
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
    """Assemble the `mineru` argv for converting one file to Markdown."""
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
    """Return the first Markdown file MinerU produced under out_dir, or None."""
    # Not a direct path: MinerU truncates long stems and the method folder varies.
    md_files = sorted(Path(out_dir).rglob("*.md"))
    return md_files[0] if md_files else None


def zip_tree(out_dir, archive):
    """Add every file under out_dir to the zip, preserving its relative path."""
    out_dir = Path(out_dir)
    for path in sorted(out_dir.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(out_dir))


def convert_batch(named_files, options, on_progress, mineru_cmd):
    """Convert (name, bytes) uploads to a zip of Markdown trees."""
    done, failed, zip_bytes = [], [], None
    total = len(named_files)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        buffer = io.BytesIO()
        archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED)

        for idx, (name, content) in enumerate(named_files):
            # Security: basename only, or "../x" would escape the temp dir below.
            safe = Path(name or "").name
            if not safe:
                failed.append((idx, name, "❌ Invalid filename."))
                continue
            on_progress(
                int(idx / total * 100),
                f"Converting {idx + 1}/{total} — {name}…",
            )
            # Per-file dirs: MinerU scans the whole input directory.
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
