"""Optimized-IP Subscription API (/api/subs) + the public /sub/{id} route."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from toolkit_api.main import create_app

TROJAN_LINK = "trojan://s3cret-pass@origin.example.com:443#US"


@pytest.fixture
def tool_client(app_state, monkeypatch):
    # Pin the URL host so no mDNS/LAN discovery runs, and start token-free
    # (individual tests opt in to SUB_ACCESS_TOKEN). create_app already wires
    # both the /api and public /sub routers — don't re-include them here.
    monkeypatch.setenv("SUB_PUBLIC_HOST", "mac.local")
    monkeypatch.delenv("SUB_ACCESS_TOKEN", raising=False)
    app = create_app(state=app_state)
    with TestClient(app) as c:
        yield c


def _generate(client, **overrides):
    payload = {
        "node_links": TROJAN_LINK,
        "preferred_ips": "1.2.3.4",
        "name_prefix": "",
        "keep_original_host": True,
    }
    payload.update(overrides)
    return client.post("/api/subs/generate", json=payload)


def test_generate_returns_sub_id_and_counts(tool_client):
    resp = _generate(tool_client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sub_id"]
    assert body["dedup"] is False
    assert body["counts"] == {"input_nodes": 1, "endpoints": 1, "output_nodes": 1}
    assert body["preview"][0]["server"] == "1.2.3.4"
    assert body["preview"][0]["name"] == "US | 01"
    assert body["urls"]["auto"] == f"http://mac.local:80/sub/{body['sub_id']}"


def test_identical_generate_dedups_to_same_id(tool_client):
    first = _generate(tool_client).json()
    second = _generate(tool_client).json()
    assert second["dedup"] is True
    assert second["sub_id"] == first["sub_id"]


def test_generate_rejects_empty_node_links(tool_client):
    resp = _generate(tool_client, node_links="")
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Paste at least one vmess:// / vless:// / trojan:// node link."
    )


def test_history_lists_generated_subscription(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    items = tool_client.get("/api/subs/history").json()
    assert [item["id"] for item in items] == [sub_id]
    assert items[0]["node_count"] == 1


def test_get_by_id_returns_loaded_result(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    body = tool_client.get(f"/api/subs/{sub_id}").json()
    assert body["sub_id"] == sub_id
    assert body["loaded"] is True
    assert body["dedup"] is False
    assert body["counts"] == {"input_nodes": 1, "endpoints": 1, "output_nodes": 1}
    assert body["preview"][0]["type"] == "trojan"


def test_get_unknown_id_is_404(tool_client):
    resp = tool_client.get("/api/subs/nope")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "That subscription no longer exists."


def test_delete_removes_subscription(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    assert tool_client.delete(f"/api/subs/{sub_id}").status_code == 200
    assert tool_client.get(f"/api/subs/{sub_id}").status_code == 404


def test_render_clash_yaml_contains_proxy_name(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    resp = tool_client.get(f"/api/subs/{sub_id}/render", params={"target": "clash"})
    assert resp.status_code == 200
    assert "US | 01" in resp.text
    assert "subscription-clash.yaml" in resp.headers["content-disposition"]


def test_render_unknown_target_is_400(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    resp = tool_client.get(f"/api/subs/{sub_id}/render", params={"target": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Unsupported subscription output format: bogus"


def test_qr_png_returns_png_bytes(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    resp = tool_client.get(f"/api/subs/{sub_id}/qr.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_urls_include_token_when_access_token_set(tool_client, monkeypatch):
    sub_id = _generate(tool_client).json()["sub_id"]
    monkeypatch.setenv("SUB_ACCESS_TOKEN", "hunter2")
    urls = tool_client.get(f"/api/subs/{sub_id}/urls").json()
    assert set(urls) == {"auto", "raw (Shadowrocket / V2rayN)", "clash", "surge"}
    assert urls["auto"] == f"http://mac.local:80/sub/{sub_id}?token=hunter2"
    assert urls["clash"].endswith(f"/sub/{sub_id}?target=clash&token=hunter2")


def test_public_sub_raw_returns_base64_body(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    resp = tool_client.get(f"/sub/{sub_id}", params={"target": "raw"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    decoded = base64.b64decode(resp.text).decode("utf-8")
    assert decoded.startswith("trojan://")


def test_public_sub_download_sets_attachment(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    resp = tool_client.get(f"/sub/{sub_id}", params={"target": "raw", "download": "1"})
    assert resp.headers["content-disposition"] == (
        'attachment; filename="subscription.txt"'
    )


def test_public_sub_surge_embeds_full_request_url(tool_client):
    sub_id = _generate(tool_client).json()["sub_id"]
    resp = tool_client.get(f"/sub/{sub_id}", params={"target": "surge"})
    assert resp.status_code == 200
    assert resp.text.startswith(
        f"#!MANAGED-CONFIG http://testserver/sub/{sub_id}?target=surge "
    )


def test_public_sub_unknown_id_is_404(tool_client):
    resp = tool_client.get("/sub/nope")
    assert resp.status_code == 404
    assert resp.text == "not found"


def test_public_sub_checks_token_before_id_lookup(tool_client, monkeypatch):
    monkeypatch.setenv("SUB_ACCESS_TOKEN", "hunter2")
    missing = tool_client.get("/sub/nope")
    assert missing.status_code == 403
    assert missing.text == "Forbidden: invalid token"
    assert tool_client.get("/sub/nope", params={"token": "wrong"}).status_code == 403
    assert tool_client.get("/sub/nope", params={"token": "hunter2"}).status_code == 404


def test_public_sub_valid_token_serves_subscription(tool_client, monkeypatch):
    sub_id = _generate(tool_client).json()["sub_id"]
    monkeypatch.setenv("SUB_ACCESS_TOKEN", "hunter2")
    assert tool_client.get(f"/sub/{sub_id}").status_code == 403
    resp = tool_client.get(f"/sub/{sub_id}", params={"token": "hunter2"})
    assert resp.status_code == 200
