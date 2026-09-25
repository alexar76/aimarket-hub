"""A2A 1.0 discovery facade for the marketplace.

The Hub is not a conversational model and must not pretend to be one.  Its A2A skill is
the thing it can actually deliver synchronously: search the live, policy-filtered market
catalogue and return structured offers.  Paid execution remains on the Hub/MCP invoke
rails, where payment, sandbox quotas and receipt verification already live.

The implementation intentionally returns direct ``Message`` responses.  No A2A Task is
created, so there is no second task store to drift away from PostgreSQL and the required
task management methods can answer honestly (empty list / not found).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

A2A_PROTOCOL_VERSION = "1.0"
MAX_A2A_BODY_BYTES = 128 * 1024
MAX_A2A_PARTS = 16
MAX_A2A_QUERY_CHARS = 2_000
MAX_A2A_RESULTS = 20

SearchCallable = Callable[..., Awaitable[dict[str, Any]]]


def build_agent_card(*, hub_name: str, hub_url: str, hub_version: str) -> dict[str, Any]:
    """Return an A2A 1.0 AgentCard containing only implemented capabilities."""
    base = hub_url.rstrip("/")
    return {
        "name": hub_name.strip() or "Independent AI Hub",
        "description": (
            "Search a live federated market of executable AI capabilities. Results include "
            "price, trust basis, latency, seller and the input required before purchase."
        ),
        "supportedInterfaces": [
            {
                "url": f"{base}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": A2A_PROTOCOL_VERSION,
            }
        ],
        "provider": {"organization": hub_name.strip() or "Independent AI", "url": base},
        "version": hub_version,
        "documentationUrl": f"{base}/developers",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": "marketplace-search",
                "name": "Search the AI capability market",
                "description": (
                    "Find executable offers by natural-language intent and optional budget, "
                    "latency, trust, hub and category filters."
                ),
                "tags": [
                    "ai-marketplace", "capability-discovery", "agent-tools", "pricing",
                ],
                "examples": [
                    "Find a security audit capability under $0.10",
                    "Найди проверяемый сервис памяти для AI-агента",
                ],
                "inputModes": ["text/plain", "application/json"],
                "outputModes": ["text/plain", "application/json"],
            }
        ],
    }


def _rpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
            "data": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": reason,
                    "domain": "a2a-protocol.org",
                }
            ],
        },
    }


def _json_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    reason: str,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        _rpc_error(request_id, code, message, reason=reason),
        status_code=status_code,
        media_type="application/json",
        headers={"A2A-Version": A2A_PROTOCOL_VERSION},
    )


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a JSON number")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and out < minimum:
        raise ValueError(f"{name} must be >= {minimum:g}")
    if maximum is not None and out > maximum:
        raise ValueError(f"{name} must be <= {maximum:g}")
    return out


def _search_args(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    message = params.get("message")
    if not isinstance(message, dict):
        raise ValueError("message is required")
    if message.get("role") != "ROLE_USER":
        raise ValueError("message.role must be ROLE_USER")
    message_id = message.get("messageId")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 200:
        raise ValueError("message.messageId must be a non-empty string up to 200 characters")
    if message.get("taskId") is not None:
        raise LookupError("task not found")

    parts = message.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_A2A_PARTS:
        raise ValueError(f"message.parts must contain 1..{MAX_A2A_PARTS} parts")

    texts: list[str] = []
    options: dict[str, Any] | None = None
    content_fields = {"text", "raw", "url", "data"}
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            raise ValueError(f"message.parts[{index}] must be an object")
        present = [name for name in content_fields if name in part]
        if len(present) != 1:
            raise ValueError(
                f"message.parts[{index}] must contain exactly one of text, raw, url, data"
            )
        kind = present[0]
        if kind == "text":
            if not isinstance(part["text"], str):
                raise ValueError(f"message.parts[{index}].text must be a string")
            texts.append(part["text"])
        elif kind == "data":
            if options is not None or not isinstance(part["data"], dict):
                raise ValueError("exactly one structured data part is supported")
            options = part["data"]
        else:
            raise TypeError("only text and structured JSON inputs are supported")

    options = options or {}
    allowed = {"intent", "limit", "budget", "maxLatencyMs", "minTrust", "hub", "category"}
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(f"unsupported search option: {unknown[0]}")
    intent_value = options.get("intent", "\n".join(texts).strip())
    if not isinstance(intent_value, str):
        raise ValueError("intent must be a string")
    intent = intent_value.strip()
    if len(intent) > MAX_A2A_QUERY_CHARS:
        raise ValueError(f"intent exceeds {MAX_A2A_QUERY_CHARS} characters")

    limit_value = options.get("limit", 10)
    if isinstance(limit_value, bool) or not isinstance(limit_value, int):
        raise ValueError("limit must be an integer")
    if not 1 <= limit_value <= MAX_A2A_RESULTS:
        raise ValueError(f"limit must be between 1 and {MAX_A2A_RESULTS}")

    budget = _number(options.get("budget"), "budget", minimum=0)
    max_latency_value = options.get("maxLatencyMs")
    if max_latency_value is not None and (
        isinstance(max_latency_value, bool) or not isinstance(max_latency_value, int)
    ):
        raise ValueError("maxLatencyMs must be an integer")
    max_latency = _number(max_latency_value, "maxLatencyMs", minimum=1)
    min_trust = _number(options.get("minTrust"), "minTrust", minimum=0, maximum=1)

    hub = options.get("hub", "any")
    category = options.get("category")
    if not isinstance(hub, str) or not hub.strip() or len(hub) > 300:
        raise ValueError("hub must be a non-empty string up to 300 characters")
    if category is not None and (
        not isinstance(category, str) or not category.strip() or len(category) > 80
    ):
        raise ValueError("category must be a non-empty string up to 80 characters")

    search_args = {
        "intent": intent,
        "budget": budget,
        "max_latency_ms": int(max_latency) if max_latency is not None else None,
        "min_trust": min_trust,
        "hub": hub.strip(),
        "category": category.strip() if isinstance(category, str) else None,
        "include_demo": False,
        "include_stale": False,
        "limit": limit_value,
    }
    return intent, search_args


def attach_a2a_routes(
    router: APIRouter,
    *,
    hub_name: str,
    hub_url: str,
    hub_version: str,
    search: SearchCallable,
) -> None:
    """Attach the public Agent Card and bounded A2A JSON-RPC endpoint."""
    card = build_agent_card(hub_name=hub_name, hub_url=hub_url, hub_version=hub_version)
    card_bytes = json.dumps(
        card, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    etag = f'"{hashlib.sha256(card_bytes).hexdigest()}"'
    cache_headers = {"Cache-Control": "public, max-age=300", "ETag": etag}

    @router.api_route("/.well-known/agent-card.json", methods=["GET", "HEAD"])
    async def agent_card(request: Request):
        if request.headers.get("if-none-match", "").strip() == etag:
            return Response(status_code=304, headers=cache_headers)
        return JSONResponse(card, headers=cache_headers)

    @router.post("/a2a")
    async def a2a_jsonrpc(
        request: Request,
        a2a_version: str = Header(default="", alias="A2A-Version"),
    ):
        content_length = request.headers.get("content-length", "").strip()
        if content_length.isdigit() and int(content_length) > MAX_A2A_BODY_BYTES:
            return _json_error(
                None, -32600, "Request payload too large",
                reason="REQUEST_TOO_LARGE", status_code=413,
            )
        raw = await request.body()
        if len(raw) > MAX_A2A_BODY_BYTES:
            return _json_error(
                None, -32600, "Request payload too large",
                reason="REQUEST_TOO_LARGE", status_code=413,
            )
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return _json_error(
                None, -32600, "Content-Type must be application/json",
                reason="INVALID_CONTENT_TYPE",
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_error(None, -32700, "Invalid JSON payload", reason="JSON_PARSE_ERROR")
        if not isinstance(payload, dict):
            return _json_error(
                None, -32600, "Request payload validation error",
                reason="INVALID_REQUEST",
            )

        request_id = payload.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            request_id = None
            return _json_error(
                request_id, -32600, "Request id must be a string or integer",
                reason="INVALID_REQUEST_ID",
            )
        if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return _json_error(
                request_id, -32600, "Request payload validation error",
                reason="INVALID_REQUEST",
            )
        if a2a_version.strip() != A2A_PROTOCOL_VERSION:
            return _json_error(
                request_id, -32009,
                f"A2A protocol version {a2a_version.strip() or '0.3'} is not supported",
                reason="VERSION_NOT_SUPPORTED",
            )

        method = payload["method"]
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return _json_error(
                request_id, -32602, "Invalid parameters", reason="INVALID_PARAMS",
            )

        if method == "SendMessage":
            configuration = params.get("configuration")
            if configuration is not None and not isinstance(configuration, dict):
                return _json_error(
                    request_id, -32602, "Invalid parameters", reason="INVALID_CONFIGURATION",
                )
            configuration = configuration or {}
            if configuration.get("taskPushNotificationConfig") is not None:
                return _json_error(
                    request_id, -32003, "Push notifications are not supported",
                    reason="PUSH_NOTIFICATION_NOT_SUPPORTED",
                )
            accepted = configuration.get("acceptedOutputModes")
            if accepted is not None and (
                not isinstance(accepted, list)
                or not any(mode in {"text/plain", "application/json"} for mode in accepted)
            ):
                return _json_error(
                    request_id, -32005, "Requested output content type is not supported",
                    reason="CONTENT_TYPE_NOT_SUPPORTED",
                )
            try:
                intent, kwargs = _search_args(params)
            except LookupError:
                return _json_error(
                    request_id, -32001, "Task not found", reason="TASK_NOT_FOUND",
                )
            except TypeError as exc:
                return _json_error(
                    request_id, -32005, str(exc), reason="CONTENT_TYPE_NOT_SUPPORTED",
                )
            except ValueError as exc:
                return _json_error(
                    request_id, -32602, str(exc), reason="INVALID_PARAMS",
                )
            try:
                result = await search(**kwargs)
            except Exception:
                return _json_error(
                    request_id, -32603, "Marketplace search failed", reason="INTERNAL_ERROR",
                )
            count = len(result.get("matches", [])) if isinstance(result, dict) else 0
            incoming_message = params["message"]
            context_id = incoming_message.get("contextId")
            if not isinstance(context_id, str) or not context_id or len(context_id) > 200:
                context_id = str(uuid.uuid4())
            response_message = {
                "messageId": str(uuid.uuid4()),
                "contextId": context_id,
                "role": "ROLE_AGENT",
                "parts": [
                    {
                        "text": f"Found {count} executable market offer{'s' if count != 1 else ''} for: {intent or 'browse'}",
                        "mediaType": "text/plain",
                    },
                    {"data": result, "mediaType": "application/json"},
                ],
                "metadata": {
                    "marketProtocol": "aimarket-v2",
                    "invokeEndpoint": f"{hub_url.rstrip('/')}/ai-market/v2/invoke",
                    "mcpEndpoint": f"{hub_url.rstrip('/')}/mcp",
                },
            }
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": {"message": response_message}},
                headers={"A2A-Version": A2A_PROTOCOL_VERSION},
            )

        if method == "ListTasks":
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": {"tasks": []}},
                headers={"A2A-Version": A2A_PROTOCOL_VERSION},
            )
        if method in {"GetTask", "CancelTask"}:
            return _json_error(
                request_id, -32001, "Task not found", reason="TASK_NOT_FOUND",
            )
        if method in {"SendStreamingMessage", "SubscribeToTask", "GetExtendedAgentCard"}:
            return _json_error(
                request_id, -32004, "Operation is not supported",
                reason="UNSUPPORTED_OPERATION",
            )
        if method in {
            "CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig",
            "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig",
        }:
            return _json_error(
                request_id, -32003, "Push notifications are not supported",
                reason="PUSH_NOTIFICATION_NOT_SUPPORTED",
            )
        return _json_error(request_id, -32601, "Method not found", reason="METHOD_NOT_FOUND")
