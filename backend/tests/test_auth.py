"""Optional shared-secret gate (APP_AUTH_TOKEN) applied to the /api surface."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from toolkit_api.main import create_app


@pytest.fixture
def secured_client(app_state, monkeypatch):
    monkeypatch.setenv("APP_AUTH_TOKEN", "s3cret")
    app = create_app(state=app_state)
    with TestClient(app) as c:
        yield c


def test_no_token_env_means_open(client):
    # The default fixture sets no APP_AUTH_TOKEN — the gate is a no-op.
    assert client.get("/api/health").status_code == 200


def test_api_requires_token_when_set(secured_client):
    assert secured_client.get("/api/health").status_code == 401


def test_bearer_header_unlocks(secured_client):
    resp = secured_client.get(
        "/api/health", headers={"Authorization": "Bearer s3cret"}
    )
    assert resp.status_code == 200


def test_cookie_unlocks(secured_client):
    # EventSource can't set headers, so the cookie path must authenticate too.
    resp = secured_client.get(
        "/api/health", headers={"Cookie": "toolkit_auth=s3cret"}
    )
    assert resp.status_code == 200


def test_wrong_token_is_rejected(secured_client):
    resp = secured_client.get(
        "/api/health", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


def test_public_sub_route_is_not_gated_by_app_token(secured_client):
    # /sub/{id} carries its own SUB_ACCESS_TOKEN gate; the app token must not
    # shadow it (proxy clients can't hold the app cookie). Unknown id -> 404,
    # proving the request reached the handler rather than being 401'd.
    resp = secured_client.get("/sub/does-not-exist")
    assert resp.status_code == 404
