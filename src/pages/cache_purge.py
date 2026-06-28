import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import streamlit as st
from scandir_rs import Scandir

DEFAULT_CACHE_TYPES = ["*.dwl", "*.dwl2", "*.bak", "*.log", "*.db", "*.tmp", "*.err"]


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
    """Turn a user token into a glob: 'bak'/'.bak' -> '*.bak'; keep real globs.

    Catch-all patterns that would match every file are rejected (return None)
    so a stray '*' can't wipe out the whole folder.
    """
    token = token.strip()
    if not token or token in {"*", "*.*", "**", "*.", ".*", "?"}:
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


def delete_file(file_path):
    """Delete one file; return (path, None) on success or (path, error)."""
    try:
        Path(file_path).unlink()
        return (file_path, None)
    except Exception as e:
        return (file_path, str(e))


# =======================================================
# STREAMLIT UI SETUP
# =======================================================
st.title("🧹 Cache Purge")
st.write(
    "Recursively find and delete cache / junk files (logs, backups, temp "
    "files) from a folder. Scan to preview first — deletion is permanent."
)

# --- 1. FOLDER ---
st.write("## 1. Folder")
if "purge_source" not in st.session_state:
    st.session_state.purge_source = str(Path("~/Desktop").expanduser())

st.caption("Folder to clean")
source_folder = st.session_state.purge_source
st.text_input(
    "Folder to clean",
    value=source_folder,
    disabled=True,
    label_visibility="collapsed",
)
if st.button("📂 Browse…", key="browse_purge"):
    picked = pick_folder(st.session_state.purge_source)
    if picked:
        st.session_state.purge_source = picked
        st.rerun()

# --- 2. FILE TYPES ---
st.write("## 2. File Types")
raw = st.text_input(
    "Cache extensions / patterns",
    value=" ".join(DEFAULT_CACHE_TYPES),
    help="Space- or comma-separated globs, e.g. *.bak *.log tmp",
)
patterns = []
rejected = []
for token in raw.replace(",", " ").split():
    pattern = normalize_pattern(token)
    if pattern:
        patterns.append(pattern)
    else:
        rejected.append(token)
patterns = sorted(set(patterns))
if rejected:
    st.warning(
        "Ignored catch-all pattern(s) that would match every file: "
        + ", ".join(f"`{t}`" for t in rejected)
    )
if patterns:
    st.caption("Matching: " + ", ".join(patterns))

# --- 3. SCAN & DELETE ---
st.write("## 3. Scan & Delete")
if st.button("🔍 Scan folder", key="scan_btn"):
    src = Path(source_folder).expanduser()
    if not src.is_dir():
        st.error("❌ Folder not found.")
    elif not patterns:
        st.error("❌ Enter at least one extension / pattern.")
    else:
        scanner, errors = Scandir(str(src), file_include=patterns).collect()
        found = [str(src / entry.path) for entry in scanner if entry.is_file]
        found.sort(key=lambda p: natural_sort_key(Path(p).name))
        st.session_state.purge_files = found
        st.session_state.purge_errors = list(errors) if errors else []
        st.session_state.purge_scanned = str(src)

# Show scan results only while they match the current folder
files = st.session_state.get("purge_files", [])
src_now = str(Path(source_folder).expanduser())
scanned_here = st.session_state.get("purge_scanned") == src_now
if scanned_here and not files:
    st.info("No matching cache files found in this folder. ✨")
elif files and scanned_here:
    total_size = 0
    for f in files:
        try:
            total_size += Path(f).stat().st_size
        except OSError:
            pass
    st.warning(
        f"Found **{len(files)}** file(s) (~{total_size / 1_048_576:.1f} MB). "
        "Deleting them cannot be undone."
    )

    if st.session_state.get("purge_errors"):
        with st.expander("⚠️ Scan warnings"):
            for error in st.session_state.purge_errors:
                st.text(str(error))

    preview = files[:200]
    st.dataframe(
        [
            {
                "File": Path(f).name,
                "Subfolder": str(Path(f).parent.relative_to(src_now)) or ".",
            }
            for f in preview
        ],
        hide_index=True,
    )
    if len(files) > len(preview):
        st.caption(f"Showing first {len(preview)} of {len(files)}.")

    confirm = st.checkbox(
        "I understand this permanently deletes the files listed above.",
        key="purge_confirm",
    )
    if st.button(
        f"🗑️ Delete {len(files)} file(s)", type="primary", disabled=not confirm
    ):
        bar = st.progress(0, text="Deleting…")
        deleted, failed = [], []
        total = len(files)
        with ThreadPoolExecutor() as executor:
            for idx, (path, error) in enumerate(
                executor.map(delete_file, files), start=1
            ):
                if error is None:
                    deleted.append(path)
                else:
                    failed.append((path, error))
                bar.progress(int(idx / total * 100), text=f"Deleting… {idx}/{total}")

        st.session_state.pop("purge_files", None)
        st.session_state.pop("purge_scanned", None)
        st.success(f"Deleted {len(deleted)} file(s).")
        if failed:
            with st.expander(f"❌ {len(failed)} failed"):
                for path, error in failed:
                    st.text(f"{Path(path).name}: {error}")
        st.toast(f"Cache Purge: deleted {len(deleted)} file(s).", icon="🧹")
