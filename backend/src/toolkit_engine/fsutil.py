"""Filesystem-adjacent helpers shared by several tools."""

import re
from pathlib import Path


def natural_sort_key(name):
    """Sort key: digit runs compare numerically (2 < 10), text case-insensitively."""
    # isdecimal, not isdigit: "²" is isdigit but int() rejects it.
    return [
        int(chunk) if chunk.isdecimal() else chunk.lower()
        for chunk in re.split(r"(\d+)", name)
    ]


def dedupe_filenames(names):
    """Disambiguate duplicate basenames as ``stem (2).ext``, preserving order."""
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
