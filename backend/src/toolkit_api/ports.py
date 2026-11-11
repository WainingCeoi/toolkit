"""Port checks shared with the Python 3.9 Make bootstrap."""

import errno
import os
import socket

PORT_TRIES = 20
# Where to knock when probing a wildcard bind address.
_WILDCARD = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}


def _already_serving(family: int, host: str, port: int) -> bool:
    """True when something already accepts here; SO_REUSEADDR hides it from bind()."""
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            sock.connect((_WILDCARD.get(host, host), port))
        except OSError:
            return False
    return True


def free_port(host: str, base: int, tries: int = PORT_TRIES) -> int:
    """First port >= ``base`` that binds on ``host`` and has no listener already."""
    # Resolve the family first; an AF_INET-only probe fails for ::1.
    try:
        family = socket.getaddrinfo(host, base, type=socket.SOCK_STREAM)[0][0]
    except OSError:
        family = socket.AF_INET
    last_err: OSError | None = None
    for port in range(base, base + tries):
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Kept so a port left in TIME_WAIT is not skipped; uvicorn sets it too.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError as err:
                last_err = err
                continue
        if _already_serving(family, host, port):
            last_err = OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))
            continue
        return port
    raise SystemExit(
        f"No free port in {base}..{base + tries - 1} on {host} "
        f"(last error: {last_err}). Free one up or set PORT=<n>."
    )
