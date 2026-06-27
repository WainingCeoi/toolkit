"""Conversion engine: parse vmess/vless/trojan links, swap in optimized IP
endpoints, and render Raw / Clash / Surge subscriptions.

Node objects are plain dicts with snake_case keys so they serialize straight to
JSON for storage. This module is framework-agnostic and is the single source of
truth for all parsing and rendering.
"""

from __future__ import annotations

import base64
import json
import re
from copy import deepcopy
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import yaml

SUPPORTED_PROTOCOLS = ("vmess", "vless", "trojan")
DEFAULT_TEST_URL = "http://cp.cloudflare.com/generate_204"


# ---------------------------------------------------------------- base64 / text
def _b64encode_utf8(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _b64decode_utf8(value: str) -> str:
    cleaned = "".join((value or "").split()).replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    return base64.b64decode(cleaned).decode("utf-8")


def normalize_text(value: str) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def split_csv_like(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[\n,;]+", normalize_text(text)) if p.strip()]


# ------------------------------------------------------------------- primitives
def _to_int(value, fallback: int = 0) -> int:
    try:
        return int(str(value).strip())
    except ValueError, TypeError:
        return fallback


def _normalize_port(value, fallback: int | None = None) -> int:
    n = _to_int(value, -1)
    if 1 <= n <= 65535:
        return n
    if fallback is not None:
        return fallback
    raise ValueError(f"Invalid port: {value}")


def _normalize_path(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "/"
    return text if text.startswith("/") else "/" + text


def _split_list_value(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in re.split(r"[\n,]+", str(value or "")) if x.strip()]


def _to_bool(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _is_tls_enabled(value) -> bool:
    return str(value or "").strip().lower() in ("tls", "xtls", "reality")


def _decode_hash_name(fragment: str) -> str:
    raw = (fragment or "").lstrip("#")
    if not raw:
        return ""
    try:
        return unquote(raw)
    except Exception:
        return raw


def _format_host_for_url(host: str) -> str:
    h = str(host)
    if ":" in h and not h.startswith("["):
        return f"[{h}]"
    return h


def _get_effective_tls_host(node: dict) -> str:
    return str(
        node.get("sni") or node.get("host_header") or node.get("original_server") or ""
    ).strip()


# ----------------------------------------------------------------- target / urls
def detect_target(user_agent: str = "", explicit_target: str = "") -> str:
    """Resolve the output format from an explicit target or the client User-Agent."""
    target = (explicit_target or "").strip().lower()
    if target and target != "auto":
        return target
    ua = (user_agent or "").lower()
    if re.search(r"clash|mihomo|stash|nekobox|meta", ua):
        return "clash"
    if "surge" in ua:
        return "surge"
    return "raw"


# ---------------------------------------------------------------------- parsing
def _maybe_expand_raw_subscription(input_text: str) -> str:
    text = normalize_text(input_text)
    if not text or "://" in text:
        return text
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-\s]+", text):
        return text
    try:
        decoded = _b64decode_utf8(text)
        if "://" in decoded:
            return decoded
    except Exception:
        pass
    return text


def _parse_vmess_uri(uri: str) -> dict:
    data = json.loads(_b64decode_utf8(uri[len("vmess://") :].strip()))
    server = str(data.get("add", "")).strip()
    uuid = str(data.get("id", "")).strip()
    if not server or not uuid:
        raise ValueError("VMess link is missing 'add' or 'id'")
    return {
        "type": "vmess",
        "name": str(data.get("ps", "")).strip() or "vmess",
        "server": server,
        "original_server": server,
        "port": _normalize_port(data.get("port"), 443),
        "uuid": uuid,
        "password": "",
        "alter_id": _to_int(data.get("aid"), 0),
        "cipher": str(data.get("scy") or data.get("cipher") or "auto").strip()
        or "auto",
        "network": str(data.get("net", "ws")).strip() or "ws",
        "path": _normalize_path(data.get("path", "/")),
        "host_header": str(data.get("host", "")).strip(),
        "sni": str(data.get("sni", "")).strip(),
        "tls": _is_tls_enabled(data.get("tls")),
        "security": str(data.get("tls", "")).strip(),
        "alpn": _split_list_value(data.get("alpn")),
        "fp": str(data.get("fp", "")).strip(),
        "header_type": str(data.get("type", "")).strip(),
        "allow_insecure": _to_bool(data.get("allowInsecure")),
        "flow": "",
        "service_name": "",
        "authority": "",
        "encryption": "none",
        "endpoint_label": "",
        "endpoint_source": "",
        "params": {},
    }


def _parse_vless_uri(uri: str) -> dict:
    parts = urlsplit(uri)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    server = parts.hostname or ""
    uuid = unquote(parts.username or "").strip()
    if not server or not uuid:
        raise ValueError("VLESS link is missing host or UUID")
    security = str(params.get("security", "")).strip()
    return {
        "type": "vless",
        "name": _decode_hash_name(parts.fragment) or "vless",
        "server": server,
        "original_server": server,
        "port": _normalize_port(parts.port or params.get("port"), 443),
        "uuid": uuid,
        "password": "",
        "alter_id": 0,
        "cipher": "auto",
        "network": str(params.get("type", "tcp")).strip() or "tcp",
        "path": _normalize_path(params.get("path", "")),
        "host_header": str(params.get("host", "")).strip(),
        "sni": str(params.get("sni") or params.get("peer") or "").strip(),
        "tls": security in ("tls", "reality"),
        "security": security,
        "alpn": _split_list_value(params.get("alpn")),
        "fp": str(params.get("fp", "")).strip(),
        "header_type": "",
        "allow_insecure": _to_bool(
            params.get("allowInsecure") or params.get("insecure")
        ),
        "flow": str(params.get("flow", "")).strip(),
        "service_name": str(params.get("serviceName", "")).strip(),
        "authority": str(params.get("authority", "")).strip(),
        "encryption": str(params.get("encryption", "none")).strip() or "none",
        "endpoint_label": "",
        "endpoint_source": "",
        "params": params,
    }


def _parse_trojan_uri(uri: str) -> dict:
    parts = urlsplit(uri)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    server = parts.hostname or ""
    password = unquote(parts.username or "").strip()
    if not server or not password:
        raise ValueError("Trojan link is missing host or password")
    security = str(params.get("security", "tls")).strip() or "tls"
    return {
        "type": "trojan",
        "name": _decode_hash_name(parts.fragment) or "trojan",
        "server": server,
        "original_server": server,
        "port": _normalize_port(parts.port or params.get("port"), 443),
        "uuid": "",
        "password": password,
        "alter_id": 0,
        "cipher": "auto",
        "network": str(params.get("type", "tcp")).strip() or "tcp",
        "path": _normalize_path(params.get("path", "")),
        "host_header": str(params.get("host", "")).strip(),
        "sni": str(params.get("sni") or params.get("peer") or "").strip(),
        "tls": security == "tls",
        "security": security,
        "alpn": _split_list_value(params.get("alpn")),
        "fp": str(params.get("fp", "")).strip(),
        "header_type": "",
        "allow_insecure": _to_bool(
            params.get("allowInsecure") or params.get("insecure")
        ),
        "flow": "",
        "service_name": str(params.get("serviceName", "")).strip(),
        "authority": str(params.get("authority", "")).strip(),
        "encryption": "none",
        "endpoint_label": "",
        "endpoint_source": "",
        "params": params,
    }


def _parse_single_node(uri: str) -> dict:
    lower = uri.lower()
    if lower.startswith("vmess://"):
        return _parse_vmess_uri(uri)
    if lower.startswith("vless://"):
        return _parse_vless_uri(uri)
    if lower.startswith("trojan://"):
        return _parse_trojan_uri(uri)
    raise ValueError("Only vmess://, vless://, and trojan:// links are supported")


def parse_node_links(input_text: str) -> dict:
    """Parse pasted node links (or a base64 subscription blob) into node dicts."""
    text = _maybe_expand_raw_subscription(input_text)
    lines = [ln.strip() for ln in normalize_text(text).split("\n") if ln.strip()]
    if not lines:
        raise ValueError(
            "Paste at least one vmess:// / vless:// / trojan:// node link."
        )
    nodes: list[dict] = []
    warnings: list[str] = []
    for index, line in enumerate(lines):
        try:
            nodes.append(_parse_single_node(line))
        except Exception as exc:  # noqa: BLE001 - collect per-line failures
            warnings.append(f"Line {index + 1} failed to parse: {exc}")
    if not nodes:
        raise ValueError(warnings[0] if warnings else "No usable nodes were parsed.")
    return {"nodes": nodes, "warnings": warnings, "normalized_input": text}


def _split_host_and_port(value: str) -> tuple[str, int | None]:
    value = (value or "").strip()
    if not value:
        return "", None
    if value.startswith("["):
        match = re.match(r"^\[([^\]]+)\](?::(\d+))?$", value)
        if not match:
            raise ValueError(f"Invalid IPv6 address format: {value}")
        return match.group(1), (
            _normalize_port(match.group(2)) if match.group(2) else None
        )
    if value.count(":") > 1:
        return value, None  # bare IPv6
    if ":" in value:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, _normalize_port(port)
    return value, None


def _parse_endpoint(raw_line: str) -> dict:
    raw = (raw_line or "").strip()
    if not raw:
        raise ValueError("Optimized address is empty")
    if "#" in raw:
        host_part, _, label = raw.partition("#")
        host_part, label = host_part.strip(), label.strip()
    else:
        host_part, label = raw, ""
    host, port = _split_host_and_port(host_part)
    if not host:
        raise ValueError(f"Invalid address: {raw}")
    return {"host": host, "port": port, "label": label}


def parse_preferred_endpoints(input_text: str) -> dict:
    """Parse optimized addresses (``host[:port][#remark]``), de-duped by host:port."""
    items = split_csv_like(input_text)
    if not items:
        raise ValueError("Enter at least one optimized IP or domain.")
    endpoints: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(items):
        try:
            endpoint = _parse_endpoint(raw)
            key = f"{endpoint['host']}:{endpoint['port'] or ''}"
            if key in seen:
                continue
            seen.add(key)
            endpoints.append(endpoint)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Optimized address {index + 1} failed to parse: {exc}")
    if not endpoints:
        raise ValueError(
            warnings[0] if warnings else "No usable optimized addresses were parsed."
        )
    return {"endpoints": endpoints, "warnings": warnings}


# --------------------------------------------------------------------- expansion
def _build_node_name(base_name: str, suffix: str) -> str:
    clean_base = str(base_name or "").strip() or "node"
    clean_suffix = str(suffix or "").strip()
    return f"{clean_base} | {clean_suffix}" if clean_suffix else clean_base


def expand_nodes(
    base_nodes: list[dict], endpoints: list[dict], options: dict | None = None
) -> dict:
    """Cross every base node with every endpoint, swapping in the optimized server.

    With ``keep_original_host`` (default), the original SNI/Host headers are kept so
    TLS still validates against the real domain while traffic goes to the optimized IP.

    Output nodes are named ``"<left> | NN"`` with a running, zero-padded counter
    (e.g. ``US | 01``, ``US | 02``). ``<left>`` is the name prefix when one is given,
    otherwise the original node name. The counter is global across the whole expansion
    so every generated node keeps a unique name.
    """
    options = options or {}
    keep_original_host = options.get("keep_original_host", True) is not False
    name_prefix = str(options.get("name_prefix") or "").strip()
    warnings: list[str] = []
    expanded: list[dict] = []
    seq = 0
    for base_node in base_nodes:
        if keep_original_host and not _get_effective_tls_host(base_node):
            warnings.append(
                f"Node '{base_node.get('name')}' has no Host/SNI/original domain; "
                "the TLS handshake may fail after swapping in the optimized IP."
            )
        for endpoint in endpoints:
            seq += 1
            port = endpoint.get("port") or base_node["port"]
            clone = deepcopy(base_node)
            clone["server"] = endpoint["host"]
            clone["port"] = port
            clone["name"] = _build_node_name(
                name_prefix or base_node.get("name"), f"{seq:02d}"
            )
            clone["endpoint_label"] = endpoint.get("label") or ""
            clone["endpoint_source"] = f"{endpoint['host']}:{port}"
            if keep_original_host:
                clone["sni"] = (
                    base_node.get("sni")
                    or base_node.get("host_header")
                    or base_node.get("original_server")
                    or ""
                )
                clone["host_header"] = (
                    base_node.get("host_header")
                    or base_node.get("sni")
                    or base_node.get("original_server")
                    or ""
                )
            else:
                if not base_node.get("sni") or base_node.get("sni") == base_node.get(
                    "original_server"
                ):
                    clone["sni"] = endpoint["host"]
                if not base_node.get("host_header") or base_node.get(
                    "host_header"
                ) == base_node.get("original_server"):
                    clone["host_header"] = endpoint["host"]
            expanded.append(clone)
    return {"nodes": expanded, "warnings": warnings}


def summarize_nodes(nodes: list[dict], limit: int = 20) -> list[dict]:
    return [
        {
            "name": node.get("name"),
            "type": node.get("type"),
            "server": node.get("server"),
            "port": node.get("port"),
            "host": node.get("host_header") or "",
            "sni": node.get("sni") or "",
            "network": node.get("network") or "tcp",
            "tls": bool(node.get("tls")),
        }
        for node in nodes[:limit]
    ]


# ------------------------------------------------------------------- node URIs
def _render_vmess_uri(node: dict) -> str:
    payload = {
        "v": "2",
        "ps": node.get("name"),
        "add": node.get("server"),
        "port": str(node.get("port")),
        "id": node.get("uuid"),
        "aid": str(node.get("alter_id", 0)),
        "scy": node.get("cipher") or "auto",
        "net": node.get("network") or "ws",
        "type": node.get("header_type") or "",
        "host": node.get("host_header") or "",
        "path": node.get("path") or "/",
        "tls": (node.get("security") or "tls") if node.get("tls") else "",
        "sni": node.get("sni") or "",
        "fp": node.get("fp") or "",
        "alpn": ",".join(node.get("alpn") or []),
    }
    return "vmess://" + _b64encode_utf8(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _set_query_param(params: dict, key: str, value: str) -> None:
    v = str(value or "").strip()
    if v:
        params[key] = v
    else:
        params.pop(key, None)


def _render_vless_uri(node: dict) -> str:
    params = dict(node.get("params") or {})
    params["type"] = node.get("network") or "ws"
    params["encryption"] = node.get("encryption") or "none"
    if node.get("security"):
        params["security"] = node["security"]
    elif node.get("tls"):
        params["security"] = "tls"
    else:
        params.pop("security", None)
    _set_query_param(params, "path", node.get("path") or "")
    _set_query_param(params, "host", node.get("host_header") or "")
    _set_query_param(params, "sni", node.get("sni") or "")
    _set_query_param(
        params, "alpn", ",".join(node.get("alpn") or []) if node.get("alpn") else ""
    )
    _set_query_param(params, "fp", node.get("fp") or "")
    _set_query_param(params, "flow", node.get("flow") or "")
    _set_query_param(params, "serviceName", node.get("service_name") or "")
    _set_query_param(params, "authority", node.get("authority") or "")
    frag = f"#{quote(node.get('name') or '')}" if node.get("name") else ""
    return (
        f"vless://{quote(str(node.get('uuid')), safe='')}@"
        f"{_format_host_for_url(node.get('server'))}:{node.get('port')}?{urlencode(params)}{frag}"
    )


def _render_trojan_uri(node: dict) -> str:
    params = dict(node.get("params") or {})
    params["type"] = node.get("network") or "ws"
    params["security"] = node.get("security") or "tls"
    _set_query_param(params, "path", node.get("path") or "")
    _set_query_param(params, "host", node.get("host_header") or "")
    _set_query_param(params, "sni", node.get("sni") or "")
    _set_query_param(
        params, "alpn", ",".join(node.get("alpn") or []) if node.get("alpn") else ""
    )
    _set_query_param(params, "fp", node.get("fp") or "")
    _set_query_param(params, "serviceName", node.get("service_name") or "")
    _set_query_param(params, "authority", node.get("authority") or "")
    frag = f"#{quote(node.get('name') or '')}" if node.get("name") else ""
    return (
        f"trojan://{quote(str(node.get('password')), safe='')}@"
        f"{_format_host_for_url(node.get('server'))}:{node.get('port')}?{urlencode(params)}{frag}"
    )


def render_node_uri(node: dict) -> str:
    kind = node.get("type")
    if kind == "vmess":
        return _render_vmess_uri(node)
    if kind == "vless":
        return _render_vless_uri(node)
    if kind == "trojan":
        return _render_trojan_uri(node)
    raise ValueError(f"Unknown node type: {kind}")


# ------------------------------------------------------------------- renderers
def render_raw_subscription(nodes: list[dict]) -> str:
    return _b64encode_utf8("\n".join(render_node_uri(node) for node in nodes))


def _clash_proxy(node: dict) -> dict:
    proxy: dict = {
        "name": node.get("name"),
        "type": node.get("type"),
        "server": node.get("server"),
        "port": node.get("port"),
        "udp": True,
    }
    if node["type"] == "vmess":
        proxy["uuid"] = node.get("uuid")
        proxy["alterId"] = node.get("alter_id", 0)
        proxy["cipher"] = node.get("cipher") or "auto"
    elif node["type"] == "vless":
        proxy["uuid"] = node.get("uuid")
        if node.get("flow"):
            proxy["flow"] = node["flow"]
    elif node["type"] == "trojan":
        proxy["password"] = node.get("password")
    if node.get("tls"):
        proxy["tls"] = True
        servername = _get_effective_tls_host(node)
        if servername:
            proxy["servername"] = servername
        if node.get("alpn"):
            proxy["alpn"] = list(node["alpn"])
        if node.get("fp"):
            proxy["client-fingerprint"] = node["fp"]
        proxy["skip-cert-verify"] = bool(node.get("allow_insecure"))
    proxy["network"] = node.get("network") or "tcp"
    net = node.get("network")
    if net == "ws":
        ws_opts: dict = {"path": node.get("path") or "/"}
        if node.get("host_header"):
            ws_opts["headers"] = {"Host": node["host_header"]}
        proxy["ws-opts"] = ws_opts
    elif net == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": node.get("service_name") or ""}
    elif net in ("http", "h2"):
        http_opts: dict = {"path": [node.get("path") or "/"]}
        if node.get("host_header"):
            http_opts["headers"] = {"Host": [node["host_header"]]}
        proxy["http-opts"] = http_opts
    return proxy


def render_clash_subscription(nodes: list[dict]) -> str:
    supported = [n for n in nodes if n.get("type") in SUPPORTED_PROTOCOLS]
    if not supported:
        raise ValueError("No nodes can be exported to Clash.")
    proxy_names = [n["name"] for n in supported]
    document = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "proxies": [_clash_proxy(n) for n in supported],
        "proxy-groups": [
            {
                "name": "🚀 Proxy Selection",
                "type": "select",
                "proxies": ["♻️ Auto Select", *proxy_names],
            },
            {
                "name": "♻️ Auto Select",
                "type": "url-test",
                "url": DEFAULT_TEST_URL,
                "interval": 300,
                "tolerance": 50,
                "proxies": list(proxy_names),
            },
        ],
        "rules": ["MATCH,🚀 Proxy Selection"],
    }
    return yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def _sanitize_surge_name(name: str) -> str:
    # Surge uses "," and "=" as field delimiters, so any present in a node name are
    # swapped for their full-width equivalents to avoid corrupting the proxy line.
    return (
        str(name or "proxy")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace(",", "，")
        .replace("=", "＝")
        .strip()
    )


def _escape_surge_header(value: str) -> str:
    return str(value or "").replace('"', '\\"')


def _render_surge_proxy(node: dict) -> str:
    name = _sanitize_surge_name(node.get("name"))
    host = _format_host_for_url(node.get("server"))
    sni = _get_effective_tls_host(node)
    if node["type"] == "vmess":
        params = [
            f"username={node.get('uuid')}",
            "vmess-aead=true",
            f"tls={'true' if node.get('tls') else 'false'}",
            f"skip-cert-verify={'true' if node.get('allow_insecure') else 'false'}",
        ]
        if sni:
            params.append(f"sni={sni}")
        if node.get("network") == "ws":
            params.append("ws=true")
            params.append(f"ws-path={node.get('path') or '/'}")
            if node.get("host_header"):
                params.append(
                    f'ws-headers=Host:"{_escape_surge_header(node["host_header"])}"'
                )
        return f"{name} = vmess, {host}, {node.get('port')}, {', '.join(params)}"

    params = [
        f"password={node.get('password')}",
        f"skip-cert-verify={'true' if node.get('allow_insecure') else 'false'}",
    ]
    if sni:
        params.append(f"sni={sni}")
    if node.get("network") == "ws":
        params.append("ws=true")
        params.append(f"ws-path={node.get('path') or '/'}")
        if node.get("host_header"):
            params.append(
                f'ws-headers=Host:"{_escape_surge_header(node["host_header"])}"'
            )
    return f"{name} = trojan, {host}, {node.get('port')}, {', '.join(params)}"


def render_surge_subscription(nodes: list[dict], request_url: str) -> str:
    supported = [n for n in nodes if n.get("type") in ("vmess", "trojan")]
    if not supported:
        raise ValueError("Surge export currently supports VMess / Trojan nodes only.")
    proxy_names = [_sanitize_surge_name(n["name"]) for n in supported]
    lines = [
        f"#!MANAGED-CONFIG {request_url} interval=86400 strict=false",
        "",
        "[General]",
        "loglevel = notify",
        f"internet-test-url = {DEFAULT_TEST_URL}",
        f"proxy-test-url = {DEFAULT_TEST_URL}",
        "ipv6 = false",
        "",
        "[Proxy]",
        *[_render_surge_proxy(n) for n in supported],
        "",
        "[Proxy Group]",
        f"🚀 Proxy Selection = select, ♻️ Auto Select, {', '.join(proxy_names)}",
        f"♻️ Auto Select = url-test, {', '.join(proxy_names)}, "
        f"url={DEFAULT_TEST_URL}, interval=600, tolerance=50",
        "",
        "[Rule]",
        "FINAL, 🚀 Proxy Selection",
        "",
    ]
    return "\n".join(lines)


def render_subscription(
    target: str, nodes: list[dict], request_url: str = ""
) -> tuple[str, str, str]:
    """Render nodes for a target, returning ``(body, content_type, filename)``."""
    if target in ("raw", "base64", "v2rayn", "shadowrocket"):
        return (
            render_raw_subscription(nodes),
            "text/plain; charset=utf-8",
            "subscription.txt",
        )
    if target == "clash":
        return (
            render_clash_subscription(nodes),
            "text/yaml; charset=utf-8",
            "subscription-clash.yaml",
        )
    if target == "surge":
        return (
            render_surge_subscription(nodes, request_url),
            "text/plain; charset=utf-8",
            "subscription-surge.conf",
        )
    if target == "json":
        return (
            json.dumps(nodes, ensure_ascii=False, indent=2),
            "application/json; charset=utf-8",
            "subscription.json",
        )
    raise ValueError(f"Unsupported subscription output format: {target}")
