"""Unit tests for the pure engine helpers (ported from the Streamlit repo)."""

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


def test_applescript_str_escapes_quotes_and_backslashes():
    assert picker._applescript_str("/a/b") == '"/a/b"'
    assert picker._applescript_str('a"b') == '"a\\"b"'
    assert picker._applescript_str("a\\b") == '"a\\\\b"'
