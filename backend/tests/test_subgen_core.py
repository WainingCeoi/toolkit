"""subgen.core node parsing + render targets (the previously-untested core)."""

from __future__ import annotations

import base64
import json

import pytest
import yaml

from subgen import core
from subgen.db import Store


def _vmess_link(**over):
    data = {
        "v": "2",
        "ps": "node-a",
        "add": "host.example.com",
        "port": "443",
        "id": "uuid-aaaa",
        "net": "ws",
        "path": "/ws",
        "host": "host.example.com",
        "tls": "tls",
    }
    data.update(over)
    return "vmess://" + base64.b64encode(json.dumps(data).encode()).decode()


def _decode_vmess(raw_sub):
    line = base64.b64decode(raw_sub).decode().strip()
    return json.loads(base64.b64decode(line[len("vmess://") :]).decode())


# --- parsing -----------------------------------------------------------------


def test_parse_mixed_valid_and_invalid_collects_warnings():
    text = "\n".join([_vmess_link(), "not-a-link", "trojan://pw@h.com:443#T"])
    parsed = core.parse_node_links(text)
    assert len(parsed["nodes"]) == 2
    assert len(parsed["warnings"]) == 1
    assert "Line 2" in parsed["warnings"][0]


def test_parse_base64_subscription_blob_expands():
    inner = "\n".join([_vmess_link(), "trojan://pw@h.com:443#T"])
    blob = base64.b64encode(inner.encode()).decode()
    parsed = core.parse_node_links(blob)
    assert len(parsed["nodes"]) == 2


# --- round-trips (parse -> render -> parse) ----------------------------------


def test_vmess_roundtrip_carries_allow_insecure():
    node = core.parse_node_links(_vmess_link(allowInsecure="1"))["nodes"][0]
    assert node["allow_insecure"] is True
    reparsed = _decode_vmess(core.render_raw_subscription([node]))
    assert reparsed.get("allowInsecure") == "1"


def test_vless_reality_renders_reality_opts_in_clash():
    link = (
        "vless://uuid-1@example.com:443?security=reality&pbk=PUBKEY&sid=ab12"
        "&sni=www.apple.com&type=tcp#reality-node"
    )
    nodes = core.parse_node_links(link)["nodes"]
    proxy = yaml.safe_load(core.render_clash_subscription(nodes))["proxies"][0]
    assert proxy["reality-opts"] == {"public-key": "PUBKEY", "short-id": "ab12"}
    # reality nodes must not carry skip-cert-verify (they authenticate by key).
    assert "skip-cert-verify" not in proxy


def test_trojan_renders_to_all_three_targets():
    nodes = core.parse_node_links("trojan://pw@h.example.com:443?sni=h.example.com#T")[
        "nodes"
    ]
    raw = base64.b64decode(core.render_raw_subscription(nodes)).decode()
    assert raw.startswith("trojan://")
    clash = yaml.safe_load(core.render_clash_subscription(nodes))
    assert clash["proxies"][0]["type"] == "trojan"
    _, _, filename = core.render_subscription("surge", nodes, "")
    assert filename


def test_expand_nodes_crosses_every_node_with_every_endpoint():
    nodes = core.parse_node_links("\n".join([_vmess_link(), _vmess_link(ps="b")]))[
        "nodes"
    ]
    eps = core.parse_preferred_endpoints("1.1.1.1#EP1, 2.2.2.2#EP2")["endpoints"]
    expanded = core.expand_nodes(nodes, eps, {"name_prefix": ""})
    assert len(expanded["nodes"]) == 4  # 2 nodes x 2 endpoints
    assert len({n["name"] for n in expanded["nodes"]}) == 4  # unique names


# --- store -------------------------------------------------------------------


@pytest.fixture
def memory_store():
    store = Store(":memory:")
    yield store
    store.close()


def test_memory_store_persists_across_calls(memory_store):
    memory_store.save_subscription(
        id="abc",
        source_hash="h1",
        payload=json.dumps({"nodes": []}),
        name_prefix="",
        keep_original_host=True,
        node_count=0,
        created_at="2026-01-01",
    )
    assert memory_store.get_subscription("abc")["id"] == "abc"


def test_save_subscription_returns_existing_id_on_hash_conflict(memory_store):
    common = dict(
        source_hash="dup",
        payload="{}",
        name_prefix="",
        keep_original_host=True,
        node_count=0,
        created_at="2026-01-01",
    )
    first = memory_store.save_subscription(id="first", **common)
    second = memory_store.save_subscription(id="second", **common)
    assert first == "first"
    assert second == "first"  # the loser gets the winner's stored id
