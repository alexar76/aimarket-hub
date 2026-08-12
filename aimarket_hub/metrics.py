"""Prometheus metrics for the AIMarket Hub.

Exposed at ``GET /metrics`` (Prometheus text format). Scrape from the factory
Prometheus job ``aimarket-hub`` — see ``prometheus.yml`` and
``docs/observability-prometheus.md``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

REGISTRY = CollectorRegistry(auto_describe=True)

hub_up = Gauge(
    "aimarket_hub_up",
    "1 if the hub process is serving requests",
    registry=REGISTRY,
)
hub_up.set(1)

invokes_total = Counter(
    "aimarket_hub_invokes_total",
    "Total /ai-market/v2/invoke attempts by capability and result class",
    ["capability", "result"],
    registry=REGISTRY,
)

invoke_duration_seconds = Histogram(
    "aimarket_hub_invoke_duration_seconds",
    "Wall time of /ai-market/v2/invoke handlers",
    ["capability"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)

payment_required_total = Counter(
    "aimarket_hub_payment_required_total",
    "Invokes rejected with HTTP 402 payment_required",
    ["capability"],
    registry=REGISTRY,
)


def _cap_label(capability_id: str | None) -> str:
    raw = (capability_id or "unknown").strip() or "unknown"
    # Keep cardinality bounded for Prometheus.
    return raw[:96]


def record_invoke(capability_id: str | None, result: str, duration_s: float | None = None) -> None:
    """Record one invoke outcome. ``result`` is a short class: ok, payment_required, error, …"""
    cap = _cap_label(capability_id)
    label = (result or "error").strip()[:32] or "error"
    invokes_total.labels(capability=cap, result=label).inc()
    if label == "payment_required":
        payment_required_total.labels(capability=cap).inc()
    if duration_s is not None and duration_s >= 0:
        invoke_duration_seconds.labels(capability=cap).observe(duration_s)


@contextmanager
def track_invoke(capability_id: str | None) -> Iterator[dict[str, str]]:
    """Context manager: sets ``slot['result']`` (default ``ok``) and records on exit."""
    slot: dict[str, str] = {"result": "ok", "capability": _cap_label(capability_id)}
    t0 = time.perf_counter()
    try:
        yield slot
    except Exception:
        if slot.get("result") == "ok":
            slot["result"] = "error"
        raise
    finally:
        record_invoke(
            slot.get("capability") or capability_id,
            slot.get("result") or "error",
            duration_s=time.perf_counter() - t0,
        )


def metrics_payload() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics HTTP response."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
