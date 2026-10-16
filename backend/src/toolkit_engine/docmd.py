"""Doc to Markdown engine: MinerU subprocess conversion, batched into a zip."""

import io
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# A stable cwd for the MinerU subprocess, whatever the server was launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# File types exposed by this converter.
ACCEPTED_TYPES = ["pdf", "png", "jpg", "jpeg", "docx", "pptx", "xlsx"]

# Per-file ceiling; the first run downloads models and CPU parsing takes minutes.
PER_FILE_TIMEOUT = 1800


def find_mineru():
    """Return the stateless MinerU 4 command, or None if unavailable."""
    venv_bin = Path(sys.executable).with_name("mineru-kit")
    if venv_bin.exists():
        return [str(venv_bin)]
    on_path = shutil.which("mineru-kit")
    return [on_path] if on_path else None


def build_mineru_cmd(
    prefix,
    input_path,
    out_dir,
    tier="standard",
    ocr_mode="auto",
    image_analysis=True,
):
    """Build a whole-document export, including Markdown and image assets."""
    # Native Office documents only support Flash; PDF/images use the chosen tier.
    if Path(input_path).suffix.lower() in {".docx", ".pptx", ".xlsx"}:
        tier = "flash"
    cmd = [
        *prefix,
        "parse",
        str(input_path),
        "-o",
        str(out_dir),
        "--format",
        "zip",
        "--tier",
        tier,
        "--ocr-mode",
        ocr_mode,
    ]
    if not image_analysis:
        cmd.append("--disable-image-analysis")
    return cmd


def _terminate(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_mineru(cmd, is_cancelled=None, poll=0.5):
    """Run one MinerU conversion; None once ``is_cancelled()`` kills the child."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT,
    )
    deadline = time.monotonic() + PER_FILE_TIMEOUT
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
                raise subprocess.TimeoutExpired(cmd, PER_FILE_TIMEOUT)


def append_document_zip(out_dir, archive, prefix):
    """Copy a MinerU ZIP's full Markdown tree into the batch archive."""
    for output in sorted(Path(out_dir).glob("*.zip")):
        with zipfile.ZipFile(output) as document:
            members = [item for item in document.infolist() if not item.is_dir()]
            if not any(item.filename.endswith(".md") for item in members):
                continue
            # Do not propagate unsafe member paths into a downloadable archive.
            if any(
                Path(item.filename).is_absolute() or ".." in Path(item.filename).parts
                for item in members
            ):
                raise ValueError("unsafe path in MinerU output")
            # Validate every CRC before adding anything to the batch archive.
            # A bad later asset must not leave a failed document's Markdown behind.
            bad_member = document.testzip()
            if bad_member is not None:
                raise zipfile.BadZipFile(f"Bad CRC-32 for file {bad_member!r}")
            for item in members:
                archive.writestr(f"{prefix}/{item.filename}", document.read(item))
            return True
    return False


def convert_batch(named_files, options, on_progress, mineru_cmd, is_cancelled=None):
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
            # Separate workspaces also prevent same-stem output collisions.
            src = tmp / f"in_{idx}" / safe
            out_dir = tmp / f"out_{idx}"
            src.parent.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir()
            src.write_bytes(content)

            cmd = build_mineru_cmd(
                mineru_cmd,
                src,
                out_dir,
                options["tier"],
                options["ocr_mode"],
                options["image_analysis"],
            )
            try:
                result = run_mineru(cmd, is_cancelled)
            except subprocess.TimeoutExpired:
                failed.append((idx, name, "timed out"))
                continue
            if result is None:
                break

            try:
                converted = append_document_zip(out_dir, archive, safe)
            except (zipfile.BadZipFile, ValueError) as exc:
                failed.append((idx, name, str(exc)))
                continue
            if converted:
                done.append((idx, name))
            else:
                reason = (result.stderr or result.stdout or "").strip()
                failed.append((idx, name, reason[-2000:] or "no Markdown produced"))

        archive.close()
        if done:
            zip_bytes = buffer.getvalue()

    on_progress(100, f"Converted {len(done)}/{total} file(s).")
    return zip_bytes, done, failed
