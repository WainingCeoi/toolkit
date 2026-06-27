"""Helpers: short-id generation, dedup hashing, and LAN IP discovery."""

from __future__ import annotations

import hashlib
import json
import platform
import secrets
import socket

ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def create_short_id(length: int = 10) -> str:
    """Return a random, confusable-free short id of the given length."""
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(length))


def _normalize_lines(value: str) -> str:
    lines = [
        ln.strip()
        for ln in (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    lines = sorted(ln for ln in lines if ln)
    return "\n".join(lines)


def build_source_hash(
    node_links: str, preferred_ips: str, name_prefix: str, keep_original_host: bool
) -> str:
    """Return a stable SHA-256 of the inputs, order- and whitespace-insensitive."""
    normalized = json.dumps(
        {
            "nodeLinks": _normalize_lines(node_links),
            "preferredIps": _normalize_lines(preferred_ips),
            "namePrefix": (name_prefix or "").strip(),
            "keepOriginalHost": keep_original_host is not False,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get_local_hostname() -> str:
    """Return the host's ``.local`` (mDNS / Bonjour) name, e.g. ``Weining.local``.

    macOS resolves ``<name>.local`` across the LAN no matter what IP the router
    hands out, so it makes a more stable subscription host than the raw address.
    Returns ``""`` when no usable name is found.
    """
    try:
        name = socket.gethostname().strip().rstrip(".")
    except OSError:
        return ""
    if not name or name.lower() in ("localhost", "localhost.localdomain"):
        return ""
    if "." not in name and platform.system() == "Darwin":
        name = f"{name}.local"
    return name


def get_lan_ips() -> list[str]:
    """Return the host's non-loopback IPv4 addresses, primary first."""
    ips: list[str] = []
    # Primary outbound interface (no packets are actually sent).
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        primary = sock.getsockname()[0]
        sock.close()
        if primary and not primary.startswith("127."):
            ips.append(primary)
    except OSError:
        pass
    # Any other IPv4 the host advertises.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips
