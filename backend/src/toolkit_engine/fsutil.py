"""Filesystem-adjacent helpers shared by several tools."""

import re
from pathlib import Path


def natural_sort_key(name):
    """
    Human-friendly sort key: split a name into text/number chunks so digit
    runs compare numerically (ep2 < ep10) and text compares case-insensitively.
    """
    # `isdecimal()` (not `isdigit()`) matches exactly what int() accepts, so a
    # superscript/circled digit like "²" or "①" — isdigit True, int() raises —
    # sorts as text instead of crashing the key.
    return [
        int(chunk) if chunk.isdecimal() else chunk.lower()
        for chunk in re.split(r"(\d+)", name)
    ]


def dedupe_filenames(names):
    """Disambiguate duplicate basenames as ``stem (2).ext``, preserving order.

    Batch tools bundle each upload's output into one zip by filename, so two
    uploads sharing a name (``report.docx`` from two folders) would collide to a
    single archive entry and silently drop one result on extraction. Renaming
    the later duplicates keeps every output distinct — matching how the File
    Gatherer already auto-numbers colliding moves.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        base = Path(name or "").name
        count = seen.get(base, 0) + 1
        seen[base] = count
        if count == 1:
            out.append(base)
        else:
            p = Path(base)
            out.append(f"{p.stem} ({count}){p.suffix}")
    return out
