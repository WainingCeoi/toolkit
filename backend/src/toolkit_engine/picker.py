"""Native macOS folder chooser (AppleScript); needs the user's GUI session."""

import subprocess
from pathlib import Path


def _applescript_str(value):
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def pick_folder(start_dir=None, packages=False):
    """Open the native folder chooser; returns "" if the user cancels."""
    prompt = "Select a folder"
    start = Path(start_dir).expanduser() if start_dir else None
    options = f'with prompt "{prompt}"'
    if start and start.is_dir():
        options += f" default location (POSIX file {_applescript_str(str(start))})"
    # Without this, bundles (*.photoslibrary, .app) are dimmed as files.
    if packages:
        options += " showing package contents true"
    script = f"POSIX path of (choose folder {options})"

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    path = result.stdout.strip()
    return path.rstrip("/") if len(path) > 1 else path
