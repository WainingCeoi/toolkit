"""Release catalogs for project interpreter pins."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from functools import cached_property
from pathlib import Path

import requests

_NODE_RELEASES = "https://nodejs.org/dist/index.json"
_LOOKUP_TIMEOUT = 15


class RuntimeReleases:
    """Release lookups for one scan or apply operation."""

    def __init__(self) -> None:
        self._node_lock = threading.Lock()

    def python(self, folder: Path, series: str) -> tuple[list[str], str | None]:
        """Read the locally available uv downloads catalog for this CPython series."""
        uv = shutil.which("uv")
        if uv is None:
            return [], "❌ uv is required to check for Python releases."
        try:
            proc = subprocess.run(
                [
                    uv,
                    "python",
                    "list",
                    f"cpython@{series}",
                    "--only-downloads",
                    "--all-versions",
                    "--output-format",
                    "json",
                    "--no-cache",
                ],
                cwd=folder,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=_LOOKUP_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return [], f"❌ uv python list timed out after {_LOOKUP_TIMEOUT}s."
        except OSError as exc:
            return [], f"❌ Could not run uv python list: {exc}"
        if proc.returncode:
            detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
            return [], f"❌ uv python list failed: {detail}"
        try:
            rows = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return [], f"❌ Could not read uv Python release data: {exc}"
        if not isinstance(rows, list):
            return [], "❌ Expected a list of Python releases from uv."
        versions = [
            row["version"]
            for row in rows
            if isinstance(row, dict)
            and row.get("implementation") == "cpython"
            and row.get("variant") == "default"
            and isinstance(row.get("version"), str)
        ]
        if rows and not versions:
            return [], f"❌ uv did not return standard CPython {series} releases."
        return versions, None

    @property
    def node(self) -> tuple[list[str], str | None]:
        """Read the Node.js distribution index once, without its version prefixes."""
        with self._node_lock:
            return self._node

    @cached_property
    def _node(self) -> tuple[list[str], str | None]:
        try:
            response = requests.get(_NODE_RELEASES, timeout=_LOOKUP_TIMEOUT)
            response.raise_for_status()
            rows = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            return [], f"❌ Could not read the Node.js release index: {exc}"
        except requests.RequestException as exc:
            return [], f"❌ Could not fetch the Node.js release index: {exc}"
        if not isinstance(rows, list):
            return [], "❌ Expected a list of releases from the Node.js release index."
        versions = [
            row["version"].removeprefix("v")
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("version"), str)
        ]
        if not versions:
            return [], "❌ The Node.js release index returned no versions."
        return versions, None
