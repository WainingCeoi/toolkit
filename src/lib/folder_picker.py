"""Shared folder-selection widget for the tool pages.

Every tool that reads or writes a local folder renders the same editable path
field + native Browse dialog via folder_field().
"""

import subprocess
from pathlib import Path

import streamlit as st


def _applescript_str(value):
    """Quote a Python string as an AppleScript string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def pick_folder(start_dir=None):
    """Open the native macOS folder chooser and return the selected path.

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


def folder_field(label, state_key, default="", placeholder=None, start_dir=None):
    """Editable path field + Browse button whose value survives page switches.

    The value lives in the plain session key `state_key`, which is never bound
    to a widget: Streamlit deletes widget-bound state whenever its widget isn't
    rendered for one run (any page switch would do it), so binding the field
    directly would silently reset the path to its default. The text_input uses
    a shadow key instead, reseeded from `state_key` after every cleanup.

    Returns the stripped path string. `start_dir` is where the Browse dialog
    opens when the field itself is empty.
    """
    widget_key = f"{state_key}_field"
    # get() rather than `not in`: it also migrates a legacy None seed to a
    # string, which the editable text_input requires.
    if st.session_state.get(state_key) is None:
        st.session_state[state_key] = default
    if widget_key not in st.session_state:
        st.session_state[widget_key] = st.session_state[state_key]

    st.caption(label)
    # The Browse handler must run before the text_input is instantiated so the
    # picked path can be written to the widget's session-state key this run.
    field = st.container()
    if st.button("📂 Browse…", key=f"{state_key}_browse"):
        picked = pick_folder(st.session_state[state_key] or start_dir)
        if picked:
            st.session_state[state_key] = picked
            st.session_state[widget_key] = picked
    value = field.text_input(
        label,
        key=widget_key,
        label_visibility="collapsed",
        placeholder=placeholder,
    ).strip()
    st.session_state[state_key] = value
    return value
