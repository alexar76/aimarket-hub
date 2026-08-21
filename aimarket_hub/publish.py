"""Developer publish — register a capability + invoke URL in the hub catalog."""
from __future__ import annotations

import os
import re
from typing import Any

from .models import Capability

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_CAP_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*@[vV]\d+$")


def _invoke_url_allowed(url: str) -> bool:
    from aimarket_hub.crawler import _url_is_safe

    if os.environ.get("AIMARKET_ALLOW_LOCAL_PUBLISH", "").strip() == "1":
        low = url.lower()
        if low.startswith(("http://127.", "http://localhost", "http://[::1]")):
            return True
    return _url_is_safe(url)


def validate_manifest(data: dict[str, Any]) -> Capability:
    """Parse and validate a developer capability manifest."""
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
    if not _invoke_url_allowed(invoke_url):
        raise ValueError(
            "invoke_url must be a public https endpoint "
            "(or set AIMARKET_ALLOW_LOCAL_PUBLISH=1 for localhost dev)"
        )

    price = float(data.get("price_per_call_usd", 0.01))
    if price < 0 or price > 1000:
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
        provider_pubkey=str(data.get("provider_pubkey", "")).strip(),
    )
