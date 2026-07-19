"""LAN host launcher — serve the built UI + API from ONE process on the local
network.

``make host`` (or ``uv run --frozen python -m toolkit_api.host``) binds
``0.0.0.0`` so every device on the same Wi-Fi can reach the app at
``http://<this-machine>.local:<port>``. It reads this host's own mDNS/Bonjour
name from the OS (never hardcoded), auto-advances past a busy port, and prints
a loud security notice because 0.0.0.0 exposes the app to everyone on the
network — and this app has no authentication while its tools move and delete
files on this Mac.

The plain ``make dev`` / ``make start`` commands stay on loopback — a
hot-reload dev server is never exposed on the LAN. Set ``HOST=127.0.0.1`` to
keep this launcher local-only; ``PORT`` sets the base port.

Single worker on purpose: the job registry, the live browser session, and the
LibreOffice profile lock are in-process state — multiple workers would break
them.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
import subprocess
import sys

APP = "toolkit_api.main:app"
BASE_PORT = 8000
PORT_TRIES = 20
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_BAR = "─" * 64


def _run(cmd: list[str]) -> str | None:
    """Run a short command and return its trimmed stdout, or None on failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
    except OSError, subprocess.SubprocessError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def mdns_name() -> str | None:
    """This host's mDNS name (e.g. ``my-mac.local``), read from the OS.

    macOS: the Bonjour ``LocalHostName`` (``scutil --get LocalHostName``),
    lowercased with ``.local`` appended; falls back to ``hostname -s``.
    Returns None if no name can be resolved.
    """
    if sys.platform == "darwin":
        candidates = [["scutil", "--get", "LocalHostName"], ["hostname", "-s"]]
    else:
        candidates = [["hostname", "-s"], ["hostname"]]
    for cmd in candidates:
        raw = _run(cmd)
        if not raw:
            continue
        base = raw.strip().rstrip(".").split(".")[0].lower()
        if base:
            return f"{base}.local"
    return None


def _is_private_lan(ip: str) -> bool:
    """True for a real IPv4 LAN address (RFC1918), excluding loopback/link-local."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.version == 4
        and addr.is_private
        and not addr.is_loopback
        and not addr.is_link_local
    )


def _route_hint_ip() -> str | None:
    """The egress interface's IPv4 for public traffic (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a UDP socket only selects the egress interface.
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def lan_ip() -> str | None:
    """Best-effort primary LAN IPv4 that peers on the Wi-Fi would actually use.

    Prefers a real private-LAN interface address (so a VPN's egress address
    doesn't hide the Wi-Fi IP), falling back to the public-egress routing
    hint. Returns None if nothing usable.
    """
    candidates: list[str] = []
    if sys.platform == "darwin":
        for iface in ("en0", "en1", "en2"):  # Wi-Fi / Ethernet on most Macs
            got = _run(["ipconfig", "getifaddr", iface])
            if got:
                candidates.append(got)
    else:
        got = _run(["hostname", "-I"])  # Linux: space-separated addresses
        if got:
            candidates.extend(got.split())
    hint = _route_hint_ip()
    if hint:
        candidates.append(hint)
    for ip in candidates:
        if _is_private_lan(ip):
            return ip
    return candidates[0] if candidates else None


def free_port(host: str, base: int, tries: int = PORT_TRIES) -> int:
    """First port >= ``base`` that actually binds on ``host`` (real bind test)."""
    # Bind with the family the host actually resolves to — an AF_INET-only probe
    # aborts for an IPv6 host like ::1 (which _LOOPBACK explicitly supports).
    try:
        family = socket.getaddrinfo(host, base, type=socket.SOCK_STREAM)[0][0]
    except OSError:
        family = socket.AF_INET
    last_err: OSError | None = None
    for port in range(base, base + tries):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError as err:
                last_err = err
    raise SystemExit(
        f"No free port in {base}..{base + tries - 1} on {host} "
        f"(last error: {last_err}). Free one up or set PORT=<n>."
    )


def _ensure_auth_token(local_only: bool) -> str | None:
    """Ensure APP_AUTH_TOKEN is set for a LAN bind; return it (None if local-only).

    A user-pinned APP_AUTH_TOKEN wins (stable across restarts); otherwise a fresh
    random token is minted and exported so the app process (same process, one
    worker, no reload) reads it per request. Loopback binds need no token.
    """
    if local_only:
        return None
    token = os.environ.get("APP_AUTH_TOKEN", "").strip()
    if not token:
        token = secrets.token_urlsafe(9)
        os.environ["APP_AUTH_TOKEN"] = token
    return token


def _print_banner(
    host: str,
    port: int,
    base: int,
    name: str | None,
    ip: str | None,
    token: str | None,
) -> None:
    local_only = host in _LOOPBACK
    lines = [_BAR, "  🧰 Toolkit — one-command host", _BAR]
    if port != base:
        lines.append(f"  ⚠  port {base} was busy → serving on {port} instead")
    lines.append(f"  This machine : http://localhost:{port}")
    if local_only:
        lines.append("  Scope        : local-only (HOST is loopback; not on the LAN)")
    elif name or ip:
        if name:
            lines.append(f"  On the Wi-Fi : http://{name}:{port}")
        if ip:
            lines.append(f"               : http://{ip}:{port}")
    else:
        lines.append("  On the Wi-Fi : could not resolve this host's name/IP")
    lines.append(_BAR)

    if not local_only:
        lines += [
            "  ⚠  EXPOSED ON THE LAN (bound to 0.0.0.0 — every device on this Wi-Fi):",
            "     • Its tools move and PERMANENTLY DELETE files on this Mac.",
            "     • Plain HTTP (no TLS) — appropriate for a trusted LAN only.",
            "     • Restrict to this machine with:  HOST=127.0.0.1 make host",
        ]
        if token:
            lines += [
                _BAR,
                "  🔑 Access token (enter once in the browser to unlock the tools):",
                f"       {token}",
                "     Pin your own with:  APP_AUTH_TOKEN=<secret> make host",
            ]
        lines.append(_BAR)
    print("\n".join(lines), flush=True)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        base = int(os.environ.get("PORT", str(BASE_PORT)))
    except ValueError:
        raise SystemExit(
            f"PORT must be an integer, got {os.environ.get('PORT')!r}"
        ) from None

    port = free_port(host, base)
    local_only = host in _LOOPBACK
    name = None if local_only else mdns_name()
    ip = None if local_only else lan_ip()
    token = _ensure_auth_token(local_only)
    _print_banner(host, port, base, name, ip, token)

    # Import here so the banner (and any port error) prints before the heavy
    # app import builds the shared state. workers=1 / no reload: one process,
    # as this app requires.
    import uvicorn

    uvicorn.run(APP, host=host, port=port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
