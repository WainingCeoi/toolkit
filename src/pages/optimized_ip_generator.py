"""Streamlit UI for the local optimized-IP subscription generator."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import segno
import streamlit as st

from lib.subgen import config, core, netutil, subserver
from lib.subgen.db import Store

# Page config is set once by the Toolkit entry point (src/app.py).


@st.cache_resource
def get_runtime():
    """Open the SQLite store and start the embedded sub-server once per process."""
    store = Store(config.DB_PATH)
    httpd = None
    if not config.DISABLE_HTTP:
        try:
            httpd = subserver.start_sub_server(
                store, config.SUB_HOST, config.SUB_PORT, config.ACCESS_TOKEN
            )
        except OSError:
            httpd = None  # port already bound (e.g. after a hot reload)
    return store, httpd


store, _httpd = get_runtime()

for _key, _default in (
    ("node_links", ""),
    ("preferred_ips", ""),
    ("name_prefix", ""),
    ("keep_original_host", True),
):
    st.session_state.setdefault(_key, _default)


def _sub_base_url() -> str:
    # Prefer an explicit override, then the Mac's stable .local hostname, then a LAN IP.
    host = config.PUBLIC_HOST or netutil.get_local_hostname()
    if not host:
        ips = netutil.get_lan_ips()
        host = ips[0] if ips else "localhost"
    return f"http://{host}:{config.SUB_PORT}"


def _build_urls(sub_id: str) -> dict:
    base = f"{_sub_base_url()}/sub/{sub_id}"
    tok = f"&token={config.ACCESS_TOKEN}" if config.ACCESS_TOKEN else ""
    tok_only = f"?token={config.ACCESS_TOKEN}" if config.ACCESS_TOKEN else ""
    return {
        "auto": f"{base}{tok_only}",
        "raw (Shadowrocket / V2rayN)": f"{base}?target=raw{tok}",
        "clash": f"{base}?target=clash{tok}",
        "surge": f"{base}?target=surge{tok}",
    }


def _qr_png(text: str) -> bytes:
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=4)
    return buf.getvalue()


def _do_generate() -> None:
    st.session_state.pop("gen_error", None)
    try:
        parsed_nodes = core.parse_node_links(st.session_state["node_links"])
        parsed_eps = core.parse_preferred_endpoints(st.session_state["preferred_ips"])
    except ValueError as exc:
        st.session_state["gen_error"] = str(exc)
        return
    options = {
        "name_prefix": st.session_state["name_prefix"],
        "keep_original_host": st.session_state["keep_original_host"],
    }
    expanded = core.expand_nodes(
        parsed_nodes["nodes"], parsed_eps["endpoints"], options
    )
    counts = {
        "inputNodes": len(parsed_nodes["nodes"]),
        "preferredEndpoints": len(parsed_eps["endpoints"]),
        "outputNodes": len(expanded["nodes"]),
    }
    source_hash = netutil.build_source_hash(
        st.session_state["node_links"],
        st.session_state["preferred_ips"],
        st.session_state["name_prefix"],
        st.session_state["keep_original_host"],
    )
    existing = store.find_subscription_by_hash(source_hash)
    if existing:
        sub_id, dedup = existing["id"], True
    else:
        sub_id, dedup = netutil.create_short_id(), False
        created_at = datetime.now(UTC).isoformat()
        payload = {
            "version": 1,
            "created_at": created_at,
            "options": options,
            "counts": counts,
            "nodes": expanded["nodes"],
        }
        store.save_subscription(
            id=sub_id,
            source_hash=source_hash,
            payload=json.dumps(payload, ensure_ascii=False),
            name_prefix=options["name_prefix"],
            keep_original_host=options["keep_original_host"],
            node_count=len(expanded["nodes"]),
            created_at=created_at,
        )
    st.session_state["last_result"] = {
        "sub_id": sub_id,
        "dedup": dedup,
        "counts": counts,
        "preview": core.summarize_nodes(expanded["nodes"]),
        "warnings": parsed_nodes["warnings"]
        + parsed_eps["warnings"]
        + expanded["warnings"],
        "nodes": expanded["nodes"],
    }


def _delete_subscription(sub_id: str) -> None:
    store.delete_subscription(sub_id)
    last = st.session_state.get("last_result")
    if last and last.get("sub_id") == sub_id:
        st.session_state.pop("last_result", None)


def _load_subscription(sub_id: str) -> None:
    """Pull a stored subscription back into the result panel (top-right)."""
    record = store.get_subscription(sub_id)
    if not record:
        st.session_state["gen_error"] = "That subscription no longer exists."
        return
    nodes = record["payload"].get("nodes", [])
    counts = record["payload"].get("counts") or {
        "inputNodes": "—",
        "preferredEndpoints": "—",
        "outputNodes": len(nodes),
    }
    st.session_state.pop("gen_error", None)
    st.session_state["last_result"] = {
        "sub_id": sub_id,
        "dedup": False,
        "loaded": True,
        "counts": counts,
        "preview": core.summarize_nodes(nodes),
        "warnings": [],
        "nodes": nodes,
    }


# ------------------------------------------------------------------ layout
st.title("Optimized-IP Subscription Generator")
st.caption(
    "Paste your self-built nodes + optimized IPs to generate Shadowrocket / "
    "Clash / Surge subscription links and files. Data is stored in a local "
    "SQLite database."
)

left, right = st.columns(2, gap="large")

with left:
    st.text_area(
        "1. Paste Original Node Kinks",
        key="node_links",
        height=120,
        placeholder="Paste Original Node Links Here",
        label_visibility="collapsed",
    )

    st.text_area(
        "2. Paste Optimized IPs / Domains",
        key="preferred_ips",
        height=120,
        placeholder="Paste Optimized IPs / Domains Here, One Per Line",
        label_visibility="collapsed",
    )

    st.text_area(
        "3. Node Name Prefix (optional)",
        key="name_prefix",
        placeholder="Node Name Prefix (Optional)",
        label_visibility="collapsed",
    )
    st.checkbox("Keep original Host / SNI (recommended)", key="keep_original_host")
    st.button(
        "Generate subscription", type="primary", width="stretch", on_click=_do_generate
    )


with right:
    if st.session_state.get("gen_error"):
        st.error(st.session_state["gen_error"])
    result = st.session_state.get("last_result")
    if not result:
        st.info(
            "After generating, your subscription links, QR code, preview, "
            "and download buttons appear here."
        )
    else:
        if result.get("loaded"):
            st.success(f"Loaded `{result['sub_id']}` from history.")
        elif result["dedup"]:
            st.success(
                f"Identical input already exists; reusing short link "
                f"`{result['sub_id']}`."
            )
        else:
            st.success(f"Generated short link `{result['sub_id']}`.")
        counts = result["counts"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Input nodes", counts["inputNodes"])
        c2.metric("Optimized addresses", counts["preferredEndpoints"])
        c3.metric("Output nodes", counts["outputNodes"])

        urls = _build_urls(result["sub_id"])

        with st.expander("Node preview"):
            st.dataframe(result["preview"], width="stretch", hide_index=True)

        with st.expander(
            "Subscription links (import directly from a phone on the same Wi-Fi)"
        ):
            for label, url in urls.items():
                st.text_input(label, value=url)
            st.write("**Download subscription files (import without a server)**")
            d1, d2, d3 = st.columns(3)
            raw_body, _, raw_name = core.render_subscription("raw", result["nodes"])
            d1.download_button(
                "raw .txt", raw_body, file_name=raw_name, width="stretch"
            )
            try:
                clash_body, _, clash_name = core.render_subscription(
                    "clash", result["nodes"]
                )
                d2.download_button(
                    "clash .yaml", clash_body, file_name=clash_name, width="stretch"
                )
            except ValueError as exc:
                d2.button(
                    "clash unavailable", disabled=True, help=str(exc), width="stretch"
                )
            try:
                surge_body, _, surge_name = core.render_subscription(
                    "surge", result["nodes"], urls["surge"]
                )
                d3.download_button(
                    "surge .conf", surge_body, file_name=surge_name, width="stretch"
                )
            except ValueError as exc:
                d3.button(
                    "surge unavailable", disabled=True, help=str(exc), width="stretch"
                )

        with st.expander("Subscription QR code (raw / Shadowrocket)"):
            st.image(
                _qr_png(urls["raw (Shadowrocket / V2rayN)"]),
                caption="Scan to import",
                width=220,
            )

        if result["warnings"]:
            st.warning("\n".join(result["warnings"]))


st.divider()
st.subheader("Subscription history")
history = store.list_subscriptions(50)
if not history:
    st.caption("No subscriptions generated yet.")
for item in history:
    info_col, load_col, del_col = st.columns([4, 1, 1])
    info_col.markdown(
        f"`{item['id']}` · {item['node_count']} nodes · {item['created_at'][:19]}"
    )
    load_col.button(
        "Load",
        key=f"loadsub_{item['id']}",
        on_click=_load_subscription,
        args=(item["id"],),
        width="stretch",
    )
    del_col.button(
        "Delete",
        key=f"delsub_{item['id']}",
        on_click=_delete_subscription,
        args=(item["id"],),
        width="stretch",
    )
