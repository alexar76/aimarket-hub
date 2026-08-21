"""Hub-native MCP JSON-RPC at ``/mcp`` (and ``/ai-market/mcp``).

Peers that read ``mcp_endpoint`` from ``/.well-known/ai-market.json`` need a real
handler — advertising a 404 is a protocol lie. This surface speaks Streamable-HTTP
MCP (JSON-RPC 2.0 POST, SSE ``data:`` framing) with two tools that map onto the
hub's own search + invoke paths.

It is also the endpoint a stranger pastes into an MCP client, which is a different
job from serving a peer, and two things follow from that:

* **The trial tier has to reach them.** This gateway does not invent credit — it
  presents each caller to the hub's own trial ledger under an identity derived from
  their address, and the hub decides what that identity is still allowed. Without it
  the first thing a newcomer met was the payment wall, so the endpoint was reachable
  and useless.
* **The identity must be per caller.** One process serves everybody here; a single
  shared identity would spend the whole allowance on whoever arrived first.

Paid invokes are unchanged: a caller who supplies a payment channel is a customer,
not a visitor, and is never put on the trial tier.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from typing import Any, Callable

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "aimarket-hub-mcp"
SERVER_VERSION = "1.0.0"

# Salt for the visitor digest, so the trial ledger stores a token rather than a client
# address. Ephemeral: a restart re-rolls it and allowances start over, which is the
# forgiving direction to fail.
_VISITOR_SALT = secrets.token_hex(16)


def visitor_for(client_address: str) -> str:
    """An opaque, stable trial identity for one caller.

    Keyed on the address rather than the MCP session because rotating a session id is
    free. The hub requires 8-64 chars of ``[A-Za-z0-9_-]``.
    """
    basis = (client_address or "").strip() or "anonymous"
    return f"mcpx-{hmac.new(_VISITOR_SALT.encode(), basis.encode(), hashlib.sha256).hexdigest()[:24]}"

router = APIRouter(tags=["mcp"])


def _sse(payload: dict[str, Any], *, session_id: str | None = None) -> Response:
    body = f"event: message\ndata: {json.dumps(payload)}\n\n"
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return Response(content=body, media_type="text/event-stream", headers=headers)


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


TOOLS = [
    {
        "name": "market_search",
        "description": (
            "Search this hub's capability catalogue by intent. Optional category "
            "(e.g. security) and budget filter. Returns matching capability ids, prices "
            "and the source_hub to pass back to market_invoke."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "What you want done, in plain language."},
                "category": {"type": "string", "description": "Optional category filter, e.g. 'security'."},
                "budget": {"type": "number", "description": "Optional cap on price per call, in USD."},
                "limit": {"type": "integer", "description": "Maximum results (default 10, max 50)."},
            },
            "required": ["intent"],
        },
    },
    {
        "name": "market_invoke",
        "description": (
            "Invoke a capability found via market_search. A few trial invokes are granted "
            "per caller with no wallet, key or channel, and each returns the hub's signed "
            "receipt; when the allowance is spent the hub answers 402 and this reports that "
            "rather than inventing a result. Paid access uses payment_channel (+ secret) "
            "and, for escrow channels, a payment_authorization object."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "The product_id from market_search."},
                "capability_id": {"type": "string", "description": "The exact capability_id from market_search."},
                "source_hub": {
                    "type": "string",
                    "description": (
                        "The source_hub from market_search, when it shows one. Required for "
                        "federated capabilities — most of the catalogue; omitting it makes the "
                        "hub look for the capability locally and answer 404."
                    ),
                },
                "input": {"type": "object", "description": "Input object for the capability; {} when it takes none."},
                "payment_channel": {"type": "string"},
                "payment_channel_secret": {"type": "string"},
                "payment_authorization": {"type": "object"},
            },
            "required": ["product_id", "capability_id"],
        },
    },
]


def attach_mcp_routes(
    app_router: APIRouter,
    *,
    db: Any,
    hub_url: str,
    client_address: Callable[[Any], str] | None = None,
) -> None:
    """Register ``POST /mcp`` on the given router.

    ``client_address`` resolves a request to the caller's address the same way the rest
    of the hub does (proxy-aware, forged headers ignored). It is injected rather than
    imported because ``api`` imports this module, and it is optional so a test can mount
    these routes without standing up the whole app.
    """

    def _caller(request: Request) -> str:
        if client_address is not None:
            try:
                return client_address(request) or ""
            except Exception:  # a limiter helper must never take the endpoint down
                logger.warning("client address resolution failed", exc_info=True)
        client = getattr(request, "client", None)
        return str(getattr(client, "host", "") or "")

    async def _search(arguments: dict[str, Any]) -> str:
        intent = str(arguments.get("intent") or "").strip()
        category = str(arguments.get("category") or "").strip().lower()
        budget = arguments.get("budget")
        limit = min(int(arguments.get("limit") or 10), 50)
        caps = db.search_capabilities(intent, limit=limit * 3)
        matches = []
        for cap in caps:
            blob = f"{cap.capability_id} {cap.product_id} {cap.name} {cap.description}".lower()
            if category and category not in blob and not (
                category == "security" and ("security" in blob or "skopos" in blob or "posture" in blob)
            ):
                continue
            if budget is not None:
                try:
                    if float(cap.price_per_call_usd) > float(budget):
                        continue
                except (TypeError, ValueError):
                    continue
            if getattr(cap, "is_demo", False):
                continue
            # source_hub travels with the match because market_invoke needs it verbatim
            # for anything this hub does not execute itself. Federated capabilities are
            # most of the catalogue, and an invoke without it falls through to the local
            # path and answers 404 — a search result you cannot act on.
            source_hub = str(getattr(cap, "source_hub", "") or "")
            matches.append({
                "capability_id": cap.capability_id,
                "product_id": cap.product_id,
                "name": cap.name,
                "price_per_call_usd": cap.price_per_call_usd,
                "description": (cap.description or "")[:240],
                "source_hub": source_hub,
            })
            if len(matches) >= limit:
                break
        return json.dumps({"intent": intent, "matches": matches}, indent=2)

    def _is_priced(product_id: str, capability_id: str, source_hub: str) -> bool:
        """Whether this capability costs anything. Unknown counts as priced.

        Erring towards "priced" spends a trial on a capability that may be free; erring the
        other way would send an unpaid invoke at something that is not, which is the failure
        that matters.
        """
        for origin in ({source_hub, "local"} if source_hub else {"local"}):
            try:
                cap = db.get_capability(product_id, capability_id, origin)
            except Exception:
                return True
            if cap is not None:
                try:
                    return float(cap.price_per_call_usd or 0) > 0
                except (TypeError, ValueError):
                    return True
        return True

    def _decode(resp: Any) -> Any:
        try:
            return resp.json()
        except Exception:
            return {"status_code": resp.status_code, "body": resp.text[:500]}

    async def _invoke(arguments: dict[str, Any], request: Request) -> str:
        # Delegate to the hub's own HTTP invoke so payment/security paths stay single-sourced.
        import httpx

        product_id = str(arguments.get("product_id") or "").strip()
        capability_id = str(arguments.get("capability_id") or "").strip()
        if not product_id or not capability_id:
            raise ValueError("product_id and capability_id are required")
        body: dict[str, Any] = {
            "product_id": product_id,
            "capability_id": capability_id,
            "input": arguments.get("input") if isinstance(arguments.get("input"), dict) else {},
        }
        source_hub = str(arguments.get("source_hub") or "").strip()
        if source_hub and source_hub != "local":
            body["source_hub"] = source_hub
        if isinstance(arguments.get("payment_authorization"), dict):
            body["payment_authorization"] = arguments["payment_authorization"]
        headers = {"content-type": "application/json"}
        paying = bool(arguments.get("payment_channel"))
        if paying:
            headers["X-Payment-Channel"] = str(arguments["payment_channel"])
        if arguments.get("payment_channel_secret"):
            headers["X-Payment-Channel-Secret"] = str(arguments["payment_channel_secret"])
        caller = _caller(request)

        # Only a PRICED capability needs the trial. Spending an allowance on a free one
        # would cap it at three calls for no reason, and — because the hub consumes the
        # trial before it ever looks at the price — would also hide the 402 behind a 429.
        trial_headers = dict(headers)
        if not paying and _is_priced(product_id, capability_id, source_hub):
            # Present the caller to the hub's own trial ledger. The hub decides whether the
            # trial is open and how much of it is left; this only says WHO is asking, and
            # says it per caller so one visitor cannot spend everybody's allowance.
            trial_headers["X-AIMarket-Sandbox-Visitor"] = visitor_for(caller)
        if caller:
            # Name the real caller so the hub's per-IP limiter bounds each visitor rather
            # than the gateway as a whole. This only survives because the invoke below goes
            # to loopback: routed through the public URL, nginx would append its own hop and
            # _client_address would read that instead, putting everyone in one bucket.
            trial_headers["X-Forwarded-For"] = caller

        # Loopback, not hub_url: hub_url is the PUBLIC address, so posting there sends the
        # request out of the container and back in through nginx — a pointless round trip
        # that also destroys the forwarded caller above.
        # The port comes from the socket this request arrived on rather than a constant,
        # because a hub that is not on 9083 (a second instance, `aimarket serve --port`, a
        # dev run) would otherwise post into whatever else holds that port — observed
        # answering "Unknown capability" from an unrelated hub process rather than failing.
        server = request.scope.get("server") or ()
        port = server[1] if len(server) >= 2 and server[1] else 9083
        base = os.environ.get("AIMARKET_INTERNAL_BASE", f"http://127.0.0.1:{port}").rstrip("/")
        url = f"{base}/ai-market/v2/invoke"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=body, headers=trial_headers)
            payload = _decode(resp)
            # A spent allowance is 429 `trial_quota_exhausted`, which carries no price and
            # which an agent reads as "retry later". What the caller actually needs is the
            # payment gate's answer, so ask for it: the same invoke without a trial identity
            # is an ordinary unpaid invoke, and the hub answers 402 with the price.
            if (resp.status_code == 429
                    and isinstance(payload, dict)
                    and payload.get("error") == "trial_quota_exhausted"):
                retry = await client.post(url, json=body, headers=headers)
                if retry.status_code == 402:
                    payload = _decode(retry)
                    if isinstance(payload, dict):
                        payload["trial_exhausted"] = True
        return json.dumps(payload, indent=2, default=str)[:50_000]

    @app_router.post("/mcp")
    async def mcp_rpc(request: Request) -> Response:
        try:
            msg = await request.json()
        except Exception:
            return _sse(_err(None, -32700, "Parse error"))

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if req_id is None and isinstance(method, str) and method.startswith("notifications/"):
            return Response(status_code=202)

        if method == "initialize":
            sid = secrets.token_hex(16)
            return _sse(
                _ok(
                    req_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                ),
                session_id=sid,
            )

        if method == "ping":
            return _sse(_ok(req_id, {}))

        if method == "tools/list":
            tools = [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in TOOLS
            ]
            return _sse(_ok(req_id, {"tools": tools}))

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                if name == "market_search":
                    text = await _search(arguments if isinstance(arguments, dict) else {})
                elif name == "market_invoke":
                    text = await _invoke(arguments if isinstance(arguments, dict) else {}, request)
                else:
                    return _sse(_err(req_id, -32602, f"Unknown tool: {name}"))
                return _sse(
                    _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
                )
            except Exception as exc:
                logger.exception("mcp tools/call %s failed", name)
                return _sse(
                    _ok(
                        req_id,
                        {
                            "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                            "isError": True,
                        },
                    )
                )

        if method == "resources/list":
            return _sse(_ok(req_id, {"resources": []}))

        return _sse(_err(req_id, -32601, f"Method not found: {method}"))

    @app_router.get("/mcp")
    async def mcp_info(request: Request) -> JSONResponse:
        """Info, or the spec's refusal — depending on what the caller asked for.

        A Streamable-HTTP client GETs this endpoint to open the server-initiated stream and
        expects either an SSE body or 405. Answering 200 JSON to that request gives it a
        stream that yields nothing and ends, which reads as a dropped connection. Humans and
        canaries curl the same URL with no Accept header, and for them the info document is
        the useful answer, so the two are told apart by what they say they accept.
        """
        from aimarket_hub.sandbox_trials import sandbox_enabled

        if "text/event-stream" in (request.headers.get("accept") or ""):
            return JSONResponse(
                {"error": "This endpoint answers MCP over JSON-RPC POST; it offers no "
                          "server-initiated stream."},
                status_code=405,
                headers={"Allow": "POST, DELETE"},
            )
        return JSONResponse({
            "status": "ok",
            "service": SERVER_NAME,
            "version": SERVER_VERSION,
            "tools": [t["name"] for t in TOOLS],
            "transport": "streamable-http",
            # Read from the ledger's own switch rather than stated as a constant: a redeploy
            # that drops AIMARKET_SANDBOX_ENABLED turns every newcomer's first invoke into a
            # payment wall, and a hard-coded "per-caller" here would still call that healthy.
            "trial": "per-caller" if sandbox_enabled() else "disabled",
        })

    @app_router.delete("/mcp")
    async def mcp_delete() -> Response:
        # Sessions carry no server-side state, so a client's termination is a no-op that
        # still has to succeed — clients treat an error here as a broken connection.
        return Response(status_code=204)
