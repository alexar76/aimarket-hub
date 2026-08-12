"""Hub-native MCP JSON-RPC at ``/ai-market/mcp``.

Peers that read ``mcp_endpoint`` from ``/.well-known/ai-market.json`` need a real
handler — advertising a 404 is a protocol lie. This surface speaks Streamable-HTTP
MCP (JSON-RPC 2.0 POST, SSE ``data:`` framing) with two tools that map onto the
hub's own search + invoke paths. Paid invokes still require a payment channel
header; this gateway does not invent free credit.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "aimarket-hub-mcp"
SERVER_VERSION = "1.0.0"

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
            "(e.g. security) and budget filter. Returns matching capability ids and prices."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "category": {"type": "string"},
                "budget": {"type": "number"},
                "limit": {"type": "integer"},
            },
            "required": ["intent"],
        },
    },
    {
        "name": "market_invoke",
        "description": (
            "Invoke a capability on this hub. Free capabilities work without payment "
            "headers; paid ones require X-Payment-Channel (+ secret) and, for escrow "
            "channels, a payment_authorization object."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "capability_id": {"type": "string"},
                "input": {"type": "object"},
                "payment_channel": {"type": "string"},
                "payment_channel_secret": {"type": "string"},
                "payment_authorization": {"type": "object"},
            },
            "required": ["product_id", "capability_id"],
        },
    },
]


def attach_mcp_routes(app_router: APIRouter, *, db: Any, hub_url: str) -> None:
    """Register ``POST /mcp`` (mounted under ``/ai-market`` by the caller)."""

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
            matches.append({
                "capability_id": cap.capability_id,
                "product_id": cap.product_id,
                "name": cap.name,
                "price_per_call_usd": cap.price_per_call_usd,
                "description": (cap.description or "")[:240],
            })
            if len(matches) >= limit:
                break
        return json.dumps({"intent": intent, "matches": matches}, indent=2)

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
        if isinstance(arguments.get("payment_authorization"), dict):
            body["payment_authorization"] = arguments["payment_authorization"]
        headers = {"content-type": "application/json"}
        if arguments.get("payment_channel"):
            headers["X-Payment-Channel"] = str(arguments["payment_channel"])
        if arguments.get("payment_channel_secret"):
            headers["X-Payment-Channel-Secret"] = str(arguments["payment_channel_secret"])
        # Prefer loopback to this process when hub_url is public; fall back to configured URL.
        base = (hub_url or "").rstrip("/") or str(request.base_url).rstrip("/")
        url = f"{base}/ai-market/v2/invoke"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        try:
            payload = resp.json()
        except Exception:
            payload = {"status_code": resp.status_code, "body": resp.text[:500]}
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
    async def mcp_info() -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "service": SERVER_NAME,
            "version": SERVER_VERSION,
            "tools": [t["name"] for t in TOOLS],
            "transport": "streamable-http",
        })
