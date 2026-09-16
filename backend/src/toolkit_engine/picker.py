"""Native macOS folder chooser (AppleScript).

Only meaningful when the backend runs in the same macOS GUI session as the
user — which is this app's model (a local tool served to the local browser).
"""

import subprocess
from pathlib import Path


def _applescript_str(value):
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def pick_folder(start_dir=None, packages=False):
    """Open the native macOS folder chooser and return the selected path.

    Uses AppleScript (`osascript`) rather than tkinter, which isn't bundled
    with every Python build (e.g. Homebrew's). The dialog opens at start_dir
    when given. Returns "" if the user cancels.

    `packages` shows bundles (a *.photoslibrary, an .app) as folders, so one
    can be selected -- by default the chooser dims them as files, and a tool
    whose input IS a bundle would have no way to pick it.
    """
    prompt = "Select a folder"
    start = Path(start_dir).expanduser() if start_dir else None
    options = f'with prompt "{prompt}"'
    if start and start.is_dir():
        options += f" default location (POSIX file {_applescript_str(str(start))})"
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
