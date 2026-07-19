"""Optimized-IP Subscription: /api/subs management + the public /sub route.

Re-expresses src/pages/optimized_ip_generator.py over the lifted subgen
engine. The old embedded :8765 side-server (subgen.subserver) is retired:
``public_router`` serves GET /sub/{sub_id} natively with the same semantics
(token before id lookup, User-Agent target auto-detection, ?download=1,
CORS *). The integrator mounts ``router`` under /api and ``public_router``
without a prefix.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import segno
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from subgen import config, core, netutil

from ..deps import StoreDep

router = APIRouter(prefix="/subs", tags=["subscription"])
public_router = APIRouter(tags=["subscription"])


# ------------------------------------------------------------------- schemas
class GenerateIn(BaseModel):
    node_links: str
    preferred_ips: str
    name_prefix: str = ""
    keep_original_host: bool = True


class CountsOut(BaseModel):
    # None when a legacy payload predates stored counts (the page showed "—").
    input_nodes: int | None = None
    endpoints: int | None = None
    output_nodes: int


class SubscriptionOut(BaseModel):
    sub_id: str
    dedup: bool
    loaded: bool = False
    counts: CountsOut
    warnings: list[str]
    preview: list[dict]
    urls: dict[str, str]


class HistoryItemOut(BaseModel):
    id: str
    node_count: int
    name_prefix: str
    created_at: str


# --------------------------------------------------------------------- urls
def _sub_base_url(request: Request) -> str:
    # Host preference matches the old page: explicit override, then the Mac's
    # stable .local hostname, then a LAN IP. The port follows the incoming
    # request because /sub is now served natively by this app rather than the
    # retired :8765 side-server.
    host = config.PUBLIC_HOST or netutil.get_local_hostname()
    if not host:
        ips = netutil.get_lan_ips()
        host = ips[0] if ips else "localhost"
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    return f"{request.url.scheme}://{host}:{port}"


def _build_urls(sub_id: str, request: Request) -> dict:
    base = f"{_sub_base_url(request)}/sub/{sub_id}"
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


# ------------------------------------------------------------- /api/subs/...
@router.post("/generate", response_model=SubscriptionOut)
def generate(req: GenerateIn, request: Request, store: StoreDep) -> SubscriptionOut:
    try:
        parsed_nodes = core.parse_node_links(req.node_links)
        parsed_eps = core.parse_preferred_endpoints(req.preferred_ips)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    options = {
        "name_prefix": req.name_prefix,
        "keep_original_host": req.keep_original_host,
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
        req.node_links,
        req.preferred_ips,
        req.name_prefix,
        req.keep_original_host,
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
        stored_id = store.save_subscription(
            id=sub_id,
            source_hash=source_hash,
            payload=json.dumps(payload, ensure_ascii=False),
            name_prefix=options["name_prefix"],
            keep_original_host=options["keep_original_host"],
            node_count=len(expanded["nodes"]),
            created_at=created_at,
        )
        # A concurrent identical request may have stored first — return the id
        # that actually persisted rather than our discarded freshly-minted one.
        if stored_id != sub_id:
            sub_id, dedup = stored_id, True
    return SubscriptionOut(
        sub_id=sub_id,
        dedup=dedup,
        counts=CountsOut(
            input_nodes=counts["inputNodes"],
            endpoints=counts["preferredEndpoints"],
            output_nodes=counts["outputNodes"],
        ),
        warnings=parsed_nodes["warnings"]
        + parsed_eps["warnings"]
        + expanded["warnings"],
        preview=core.summarize_nodes(expanded["nodes"]),
        urls=_build_urls(sub_id, request),
    )


@router.get("/history", response_model=list[HistoryItemOut])
def history(store: StoreDep) -> list[dict]:
    return store.list_subscriptions(50)


@router.get("/{sub_id}", response_model=SubscriptionOut)
def load_subscription(
    sub_id: str, request: Request, store: StoreDep
) -> SubscriptionOut:
    """Pull a stored subscription back into the result panel (top-right)."""
    record = store.get_subscription(sub_id)
    if not record:
        raise HTTPException(
            status_code=404, detail="That subscription no longer exists."
        )
    nodes = record["payload"].get("nodes", [])
    stored_counts = record["payload"].get("counts")
    counts = CountsOut(
        input_nodes=stored_counts.get("inputNodes") if stored_counts else None,
        endpoints=stored_counts.get("preferredEndpoints") if stored_counts else None,
        output_nodes=(
            stored_counts.get("outputNodes", len(nodes))
            if stored_counts
            else len(nodes)
        ),
    )
    return SubscriptionOut(
        sub_id=sub_id,
        dedup=False,
        loaded=True,
        counts=counts,
        warnings=[],
        preview=core.summarize_nodes(nodes),
        urls=_build_urls(sub_id, request),
    )


@router.delete("/{sub_id}")
def delete_subscription(sub_id: str, store: StoreDep) -> dict:
    store.delete_subscription(sub_id)
    return {"ok": True}


@router.get("/{sub_id}/urls")
def subscription_urls(sub_id: str, request: Request) -> dict[str, str]:
    return _build_urls(sub_id, request)


@router.get("/{sub_id}/render")
def render_subscription(
    sub_id: str, request: Request, store: StoreDep, target: str = "raw"
) -> Response:
    record = store.get_subscription(sub_id)
    if not record:
        raise HTTPException(
            status_code=404, detail="That subscription no longer exists."
        )
    nodes = record["payload"].get("nodes", [])
    # The page passed the public surge URL so #!MANAGED-CONFIG points back at
    # the live subscription; raw/clash ignore it (the page passed nothing).
    request_url = _build_urls(sub_id, request)["surge"] if target == "surge" else ""
    try:
        body, content_type, filename = core.render_subscription(
            target, nodes, request_url
        )
    except ValueError as exc:
        # The page surfaced these as disabled-button tooltips.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{sub_id}/qr.png")
def subscription_qr(sub_id: str, request: Request) -> Response:
    raw_url = _build_urls(sub_id, request)["raw (Shadowrocket / V2rayN)"]
    return Response(content=_qr_png(raw_url), media_type="image/png")


# ------------------------------------------------------------ public /sub/...
_CORS = {"Access-Control-Allow-Origin": "*"}


@public_router.get("/sub/{sub_id}")
def serve_subscription(sub_id: str, request: Request, store: StoreDep) -> Response:
    """Serve a rendered subscription — subgen.subserver semantics, natively."""
    # The token gate runs BEFORE the id lookup, exactly like the old handler.
    token = config.ACCESS_TOKEN
    if token and request.query_params.get("token", "") != token:
        return PlainTextResponse(
            "Forbidden: invalid token", status_code=403, headers=dict(_CORS)
        )
    record = store.get_subscription(sub_id)
    if not record:
        return PlainTextResponse("not found", status_code=404, headers=dict(_CORS))
    nodes = record["payload"].get("nodes", [])
    target = core.detect_target(
        request.headers.get("User-Agent", ""),
        request.query_params.get("target", ""),
    )
    # Full incoming URL (Surge #!MANAGED-CONFIG embeds it for re-fetching).
    request_url = str(request.url)
    try:
        body, content_type, filename = core.render_subscription(
            target, nodes, request_url
        )
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400, headers=dict(_CORS))
    headers = dict(_CORS)
    if request.query_params.get("download", "") == "1":
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(content=body, media_type=content_type, headers=headers)
