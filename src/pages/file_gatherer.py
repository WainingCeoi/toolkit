import re
import shutil
import subprocess
from pathlib import Path

import streamlit as st
from scandir_rs import Scandir

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
# FOLDER PICKER
# =======================================================
def _applescript_str(value):
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def pick_folder(start_dir=None):
    """
    Open the native macOS folder chooser and return the selected path.

    Uses AppleScript (`osascript`) rather than tkinter, which isn't bundled
    with every Python build (e.g. Homebrew's). The dialog opens at start_dir
    when given. Returns "" if the user cancels.
    """
    prompt = "Select a folder"
    start = Path(start_dir).expanduser() if start_dir else None
    if start and start.is_dir():
        script = (
            f'POSIX path of (choose folder with prompt "{prompt}" '
            f"default location (POSIX file {_applescript_str(str(start))}))"
        )
    else:
        script = f'POSIX path of (choose folder with prompt "{prompt}")'

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    path = result.stdout.strip()
    return path.rstrip("/") if len(path) > 1 else path


def folder_selector(label, state_key, default, button_key):
    """Render a labelled Browse button + read-only path field on one line."""
    if state_key not in st.session_state:
        st.session_state[state_key] = default

    st.caption(label)

    value = st.session_state[state_key]
    st.text_input(label, value=value, disabled=True, label_visibility="collapsed")
    if st.button("📂 Browse…", key=button_key):
        picked = pick_folder(st.session_state[state_key])
        if picked:
            st.session_state[state_key] = picked
            st.rerun()

    return value


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
source_folder = folder_selector("Source folder", "move_source", desktop, "browse_src")
target_folder = folder_selector("Target folder", "move_target", desktop, "browse_tgt")

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
    src = Path(source_folder).expanduser().resolve()
    tgt = Path(target_folder).expanduser().resolve()

    if not src.is_dir():
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
            tgt.mkdir(parents=True, exist_ok=True)
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
