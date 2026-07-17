"""Filesystem-adjacent helpers shared by several tools."""

import re


def natural_sort_key(name):
    """
    Human-friendly sort key: split a name into text/number chunks so digit
    runs compare numerically (ep2 < ep10) and text compares case-insensitively.
    """
    return [
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.split(r"(\d+)", name)
    ]
