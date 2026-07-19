"""Optional shared-secret gate for the /api surface.

Off by default: when ``APP_AUTH_TOKEN`` is empty — ``make dev`` / ``make start``,
the test suite, any loopback run — :func:`require_auth` is a no-op, so the local
single-user flow needs no token and nothing changes.

``make host`` sets ``APP_AUTH_TOKEN`` (generating a random one if the user hasn't
pinned it in the env) *before* it binds ``0.0.0.0``, so every ``/api`` request
from the LAN must present the token: either the ``Authorization: Bearer <token>``
header (what the fetch wrapper sends) or the ``toolkit_auth`` cookie (which the
job-progress ``EventSource`` relies on, since it can't set headers). Comparison is
constant-time.

The public ``/sub/{id}`` route keeps its own ``SUB_ACCESS_TOKEN`` gate and is
deliberately NOT covered here — proxy clients fetch it directly and can't hold
the app cookie.
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request

COOKIE_NAME = "toolkit_auth"


def auth_token() -> str:
    """The configured shared secret, or "" when the gate is disabled."""
    return os.environ.get("APP_AUTH_TOKEN", "").strip()


def _bearer(header: str) -> str:
    prefix = "bearer "
    return header[len(prefix) :].strip() if header.lower().startswith(prefix) else ""


async def require_auth(request: Request) -> None:
    """Router dependency: 401 unless the request carries the token (when set)."""
    token = auth_token()
    if not token:
        return  # gate disabled — loopback / dev / tests
    supplied = _bearer(
        request.headers.get("Authorization", "")
    ) or request.cookies.get(COOKIE_NAME, "")
    if not (supplied and secrets.compare_digest(supplied, token)):
        raise HTTPException(status_code=401, detail="Authentication required.")
