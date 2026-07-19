"""Unit tests for the pure engine helpers (ported from the Streamlit repo)."""

import subprocess

from toolkit_engine import fsutil, picker


def test_natural_sort_orders_numbers_humanly():
    names = ["ep1", "ep10", "ep2", "Ep20", "ep3"]
    assert sorted(names, key=fsutil.natural_sort_key) == [
        "ep1",
        "ep2",
        "ep3",
        "ep10",
        "Ep20",
    ]


def test_natural_sort_key_survives_non_decimal_digits():
    # Superscript/circled digits are isdigit() but int() rejects them — the key
    # must sort them as text instead of raising ValueError.
    assert fsutil.natural_sort_key("page²") == ["page²"]
    assert fsutil.natural_sort_key("①") == ["①"]
    sorted(["a²", "a1", "a"], key=fsutil.natural_sort_key)  # no crash


def test_dedupe_filenames_disambiguates_collisions():
    assert fsutil.dedupe_filenames(["a.pdf", "a.pdf", "b.pdf", "a.pdf"]) == [
        "a.pdf",
        "a (2).pdf",
        "b.pdf",
        "a (3).pdf",
    ]
    # Basenames only; distinct names pass through untouched.
    assert fsutil.dedupe_filenames(["/x/a.docx", "/y/a.docx"]) == [
        "a.docx",
        "a (2).docx",
    ]


def test_applescript_str_escapes_quotes_and_backslashes():
    assert picker._applescript_str("/a/b") == '"/a/b"'
    assert picker._applescript_str('a"b') == '"a\\"b"'
    assert picker._applescript_str("a\\b") == '"a\\\\b"'


def test_pick_folder_returns_selection_and_embeds_start_dir(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["script"] = cmd[cmd.index("-e") + 1]
        return subprocess.CompletedProcess(
            cmd, 0, stdout="/Users/me/Movies/\n", stderr=""
        )

    monkeypatch.setattr(picker.subprocess, "run", fake_run)
    picked = picker.pick_folder(str(tmp_path))
    assert picked == "/Users/me/Movies"  # trailing slash trimmed
    assert str(tmp_path) in captured["script"]  # start dir embedded when it exists


def test_pick_folder_returns_empty_on_cancel(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="User canceled.")

    monkeypatch.setattr(picker.subprocess, "run", fake_run)
    assert picker.pick_folder(None) == ""
