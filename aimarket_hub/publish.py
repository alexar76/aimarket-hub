"""Developer publish — register a capability + invoke URL in the hub catalog."""
from __future__ import annotations

import os
import math
import re
from typing import Any

from .models import Capability

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_CAP_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*@[vV]\d+$")


def _invoke_url_allowed(url: str, *, allow_local: bool = True) -> bool:
    """May this hub list a capability served at ``url``?

    The operator's explicit local configuration is consulted only after the
    authenticated caller has been granted operator authority. Community publishers
    always pass the public-network guard, even on a hub with private providers.
    """
    from aimarket_hub.crawler import _url_is_safe

    if allow_local and os.environ.get("AIMARKET_ALLOW_LOCAL_PUBLISH", "").strip() == "1":
        low = url.lower()
        if low.startswith(("http://127.", "http://localhost", "http://[::1]")):
            return True
        from urllib.parse import urlparse

        from aimarket_hub.outbound_http import invoke_gateway_hosts

        host = (urlparse(url).hostname or "").lower()
        if host and host in invoke_gateway_hosts():
            return True
    return _url_is_safe(url)


def private_invoke_authorized(cap: Capability) -> bool:
    """Private provider access belongs to operator-managed local listings only.

    The exact-URL grant is written by the authenticated supply route, never read
    from a submitted manifest. A stored publisher name does not prove how a
    legacy row was authenticated. Unmarked rows use public transport even when
    a global gateway exception is configured.
    """
    if cap.source_hub != "local" or not cap.invoke_url:
        return False
    evidence = cap.catalogue_evidence
    return bool(isinstance(evidence, dict) and evidence.get("operator_invoke_url") == cap.invoke_url)


def validate_manifest(data: dict[str, Any], *, allow_local: bool = True) -> Capability:
    """Parse a manifest; HTTP callers must supply their authenticated local authority."""
    product_id = str(data.get("product_id", "")).strip()
    capability_id = str(data.get("capability_id", "")).strip()
    name = str(data.get("name", "")).strip() or capability_id.split("@")[0]
    version = str(data.get("version", "v1")).strip()
    if not version.startswith("v"):
        version = f"v{version}"

    if not _SAFE_ID.match(product_id):
        raise ValueError("product_id must be alphanumeric (dots/dashes ok), max 128 chars")
    if not _CAP_ID.match(capability_id):
        raise ValueError("capability_id must look like my.tool@v1")

    invoke_url = str(data.get("invoke_url", "")).strip()
    if not invoke_url.startswith(("http://", "https://")):
        raise ValueError("invoke_url must be http(s) and reachable by the hub")
    if not _invoke_url_allowed(invoke_url, allow_local=allow_local):
        raise ValueError(
            "invoke_url must be a public http(s) endpoint; "
            "private providers require operator authorization and local configuration"
        )

    raw_price = data.get("price_per_call_usd", 0.01)
    price = float(raw_price)
    if isinstance(raw_price, bool) or not math.isfinite(price) or price < 0 or price > 1000:
        raise ValueError("price_per_call_usd must be between 0 and 1000")

    input_schema = data.get("input_schema") or {"type": "object", "properties": {}}
    output_schema = data.get("output_schema") or {"type": "object", "properties": {}}
    if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
        raise ValueError("input_schema and output_schema must be JSON objects")

    return Capability(
        capability_id=capability_id,
        product_id=product_id,
        name=name,
        version=version,
        description=str(data.get("description", "")).strip(),
        input_schema=input_schema,
        output_schema=output_schema,
        price_per_call_usd=price,
        p50_latency_ms=int(data.get("p50_latency_ms", 200)),
        success_rate_30d=float(data.get("success_rate_30d", 0.99)),
        source_hub="local",
        source_hub_name=str(data.get("publisher", "community")),
        trust_score=float(data.get("trust_score", 0.5)),
        agent=str(data.get("agent", "")).strip(),
        invoke_url=invoke_url,
        publisher_id=str(data.get("publisher_id") or data.get("publisher") or "").strip(),
        payout_address=str(data.get("payout_address") or data.get("pay_to") or "").strip(),
        provider_pubkey=str(data.get("provider_pubkey", "")).strip(),
    )
