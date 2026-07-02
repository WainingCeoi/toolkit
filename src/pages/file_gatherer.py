import re
import shutil
from pathlib import Path

import streamlit as st
from scandir_rs import Scandir

from lib.folder_picker import folder_field

# File-type presets -> scandir_rs file_include glob patterns
FILE_TYPE_PRESETS = {
    "Video": [
        "*.mkv",
        "*.mp4",
        "*.mov",
        "*.ts",
        "*.flv",
        "*.avi",
        "*.webm",
        "*.m4v",
        "*.wmv",
        "*.mpg",
        "*.mpeg",
    ],
    "Audio": ["*.mp3", "*.flac", "*.aac", "*.wav", "*.m4a", "*.ogg", "*.opus", "*.wma"],
    "Image": [
        "*.jpg",
        "*.jpeg",
        "*.png",
        "*.gif",
        "*.heic",
        "*.webp",
        "*.bmp",
        "*.tiff",
    ],
    "Subtitle": ["*.srt", "*.ass", "*.ssa", "*.sub", "*.vtt"],
    "Document": ["*.pdf", "*.docx", "*.doc", "*.txt", "*.epub", "*.pptx", "*.xlsx"],
    "Archive": ["*.zip", "*.rar", "*.7z", "*.tar", "*.gz"],
}


def natural_sort_key(name):
    """
    Human-friendly sort key: split a name into text/number chunks so digit
    runs compare numerically (ep2 < ep10) and text compares case-insensitively.
    """
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", name)
    ]


def normalize_pattern(token):
    """Turn a user token into a glob: 'srt'/'.srt' -> '*.srt'; keep real globs."""
    token = token.strip()
    if not token:
        return None
    if "*" in token or "?" in token:
        return token
    return f"*.{token.lstrip('.')}"


# =======================================================
# STREAMLIT UI SETUP
# =======================================================
st.title("📦 File Gatherer")
st.write(
    "Recursively gather files by type from a source folder and move them "
    "into a single target folder (duplicate names are auto-numbered)."
)

# --- 1. FOLDERS ---
st.write("## 1. Folders")
desktop = str(Path("~/Desktop").expanduser())
source_folder = folder_field("Source folder", "move_source", desktop)
target_folder = folder_field("Target folder", "move_target", desktop)

# --- 2. FILE TYPES ---
st.write("## 2. File Types")
selected_categories = st.multiselect(
    "Categories",
    options=list(FILE_TYPE_PRESETS.keys()),
    default=["Video"],
)
custom_raw = st.text_input(
    "Custom patterns / extensions (optional)",
    value="",
    help="Comma- or space-separated, e.g. srt, *.nfo, report*.pdf",
)

patterns = []
for category in selected_categories:
    patterns.extend(FILE_TYPE_PRESETS[category])
for token in custom_raw.replace(",", " ").split():
    pattern = normalize_pattern(token)
    if pattern:
        patterns.append(pattern)
patterns = sorted(set(patterns))

if patterns:
    st.caption("Matching: " + ", ".join(patterns))

# --- 3. SCAN & MOVE ---
st.write("## 3. Scan & Move")
if st.button("🚚 Scan & Move", type="primary", key="scan_move_btn"):
    src_raw = Path(source_folder).expanduser()
    tgt_raw = Path(target_folder).expanduser()
    src, tgt = src_raw.resolve(), tgt_raw.resolve()

    # A relative (or empty) typed path would resolve against the app's CWD —
    # refuse it before it can target the wrong tree.
    if not (src_raw.is_absolute() and tgt_raw.is_absolute()):
        st.error("❌ Use absolute folder paths (e.g. ~/Movies or /Volumes/T7).")
    elif not src.is_dir():
        st.error("❌ Source folder not found.")
    elif not patterns:
        st.error("❌ Select at least one file type.")
    elif tgt == src or src in tgt.parents:
        st.error("❌ Target must be a different folder, outside the source.")
    else:
        with st.spinner("Scanning source folder…"):
            scanner, errors = Scandir(str(src), file_include=patterns).collect()
            files = [str(src / entry.path) for entry in scanner if entry.is_file]
            files.sort(key=lambda p: natural_sort_key(Path(p).name))

        # A scan error means part of the tree was unreadable, so the gather may
        # be incomplete — surface the count here and in the final status below.
        if errors:
            with st.expander(f"⚠️ {len(errors)} scan warning(s)"):
                for error in errors:
                    st.text(str(error))

        if not files:
            st.info("No matching files found.")
        else:
            try:
                tgt.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                # e.g. the typed target (or one of its parents) is a file
                st.error(f"❌ Cannot create the target folder: {e}")
                st.stop()
            total = len(files)
            bar = st.progress(0, text=f"Moving… 0/{total}")
            moved, failed = [], []

            for idx, file_path in enumerate(files, start=1):
                file = Path(file_path)
                try:
                    target_path = tgt / file.name
                    # Handle duplicated files
                    counter = 1
                    while target_path.exists():
                        target_path = tgt / f"{file.stem}_{counter}{file.suffix}"
                        counter += 1
                    shutil.move(str(file), str(target_path))
                    moved.append(file.name)
                except Exception as e:
                    failed.append((file.name, str(e)))
                bar.progress(int(idx / total * 100), text=f"Moving… {idx}/{total}")

            # Summary
            c1, c2 = st.columns(2)
            c1.metric("Moved ✅", len(moved))
            c2.metric("Failed ❌", len(failed))
            if failed:
                st.write("### ⚠️ Failures")
                for name, err in failed:
                    st.error(f"🔴 {name}: {err}")

            if errors:
                st.warning(
                    f"Moved to {tgt}, but {len(errors)} location(s) couldn't be "
                    "scanned — matching files may remain in the source."
                )
            else:
                st.success(f"Done! Moved to: {tgt}")
            st.toast(f"File Gatherer: moved {len(moved)} file(s).", icon="📦")
