import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

# Project root — needed as the working directory when we have to fall back to
# `uv run mineru` (uv resolves the project venv relative to its cwd).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Files MinerU can parse (mirrors its CLI's accepted inputs).
ACCEPTED_TYPES = ["pdf", "png", "jpg", "jpeg", "docx", "pptx", "xlsx"]

# Per-file ceiling. Generous because the very first run downloads models and a
# long PDF can take minutes to parse on CPU/MPS.
PER_FILE_TIMEOUT = 1800


# =======================================================
# CONVERSION ENGINE (MinerU via subprocess)
# =======================================================
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


# =======================================================
# STREAMLIT UI SETUP
# =======================================================
st.title("📝 Doc to Markdown")
st.write(
    "Convert PDFs, Office documents, and images into clean Markdown with "
    "[MinerU](https://github.com/opendatalab/MinerU) — text, tables, formulas, "
    "and extracted images, bundled as a downloadable zip. The first run "
    "downloads MinerU's models, so give it a moment."
)

MINERU = find_mineru()
# The base `mineru` package ships the CLI but no ML backend — torch (and the
# pipeline/vlm deps) live in optional extras. Detect that up front so we warn
# before a conversion fails deep inside the subprocess.
BACKEND_READY = importlib.util.find_spec("torch") is not None
if MINERU is None:
    st.warning("Missing required tool: MinerU (`uv add mineru`).")
elif not BACKEND_READY:
    st.warning(
        "MinerU is installed but its conversion backend isn't. Install it with "
        "`uv add 'mineru[core]'` (all backends) or `uv add 'mineru[pipeline]'` "
        "(pipeline only), then reload."
    )

uploaded_files = st.file_uploader(
    "Select file(s)",
    type=ACCEPTED_TYPES,
    accept_multiple_files=True,
)

with st.expander("⚙️ Advanced options"):
    backend = st.selectbox(
        "Backend",
        ["pipeline", "hybrid-engine", "vlm-engine"],
        index=1,  # default: hybrid-engine
        help=(
            "pipeline: fast, general, lightest models. "
            "hybrid-engine / vlm-engine: higher accuracy on complex layouts, "
            "heavier and slower."
        ),
    )
    method, lang, effort, formula, table = "auto", "ch", "medium", True, True
    if backend == "pipeline":
        col1, col2 = st.columns(2)
        with col1:
            method = st.selectbox(
                "Parse method",
                ["auto", "txt", "ocr"],
                help="auto picks per file; txt for digital PDFs; ocr for scans.",
            )
        with col2:
            lang = st.selectbox(
                "OCR language",
                [
                    "ch",
                    "ch_server",
                    "korean",
                    "ta",
                    "te",
                    "ka",
                    "th",
                    "el",
                    "arabic",
                    "east_slavic",
                    "cyrillic",
                    "devanagari",
                ],
                help="`ch` handles Chinese + English. Only affects OCR accuracy.",
            )
        formula = st.checkbox("Parse formulas", value=True)
        table = st.checkbox("Parse tables", value=True)
    elif backend == "hybrid-engine":
        effort = st.selectbox(
            "Effort",
            ["medium", "high"],
            help="high enables image/chart analysis (slower, more accurate).",
        )

if st.button("Convert to Markdown", type="primary"):
    if not uploaded_files:
        st.error("❌ Please select at least one file first.")
    elif MINERU is None:
        st.error("❌ Install MinerU, then try again.")
    else:
        done, failed, zip_bytes = [], [], None
        total = len(uploaded_files)
        bar = st.progress(0, text=f"Converting 0/{total}…")
        with st.spinner(f"Converting {total} file(s) with MinerU…"):
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                buffer = io.BytesIO()
                archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED)

                for idx, upload in enumerate(uploaded_files):
                    # Show which file is in flight before its (long) MinerU run.
                    bar.progress(
                        int(idx / total * 100),
                        text=f"Converting {idx + 1}/{total} — {upload.name}…",
                    )
                    # Isolate each file's input and output so identical names
                    # and MinerU's whole-directory scanning can't collide.
                    src = tmp / f"in_{idx}" / upload.name
                    out_dir = tmp / f"out_{idx}"
                    src.parent.mkdir(parents=True, exist_ok=True)
                    out_dir.mkdir()
                    src.write_bytes(upload.getvalue())

                    cmd = build_mineru_cmd(
                        MINERU,
                        src,
                        out_dir,
                        backend,
                        method,
                        lang,
                        effort,
                        formula,
                        table,
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
                        failed.append((upload.name, "timed out"))
                        continue

                    if find_markdown(out_dir):
                        zip_tree(out_dir, archive)
                        done.append(upload.name)
                    else:
                        reason = (result.stderr or result.stdout or "").strip()
                        failed.append(
                            (upload.name, reason[-2000:] or "no Markdown produced")
                        )

                archive.close()
                if done:
                    zip_bytes = buffer.getvalue()

        bar.progress(100, text=f"Converted {total}/{total} file(s).")
        # Persist so the download button survives the rerun a click triggers.
        st.session_state["mineru_result"] = {
            "zip": zip_bytes,
            "done": done,
            "failed": failed,
        }
        st.toast(f"Doc to Markdown: converted {len(done)} file(s).", icon="📝")

# Last conversion — kept in session_state so the download persists across reruns.
result = st.session_state.get("mineru_result")
if result:
    if result["done"]:
        st.success(f"✅ Converted {len(result['done'])} file(s).")
        st.download_button(
            "⬇ Download Markdown (.zip)",
            data=result["zip"],
            file_name="markdown.zip",
            mime="application/zip",
            key="mineru_dl",
        )
    if result["failed"]:
        with st.expander(f"❌ {len(result['failed'])} failed"):
            for name, error in result["failed"]:
                st.text(f"{name}: {error}")
