"""Data models for hub entities — capabilities, peers, stats."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Capability:
    """A single AI capability indexed by the hub."""

    capability_id: str
    product_id: str
    name: str
    version: str = "v1"
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    price_per_call_usd: float = 0.35
    p50_latency_ms: int = 3000
    success_rate_30d: float = 0.97
    source_hub: str = "local"
    source_hub_name: str = ""
    routed_price_usd: float | None = None
    routing_fee_bps: int = 0
    trust_score: float = 0.0
    agent: str = ""
    prompt_template: str = ""
    invoke_url: str = ""
    publisher_id: str = ""
    provider_pubkey: str = ""
    stake_usd: float = 0.0
    is_demo: bool = False

    def tool_name(self) -> str:
        return f"{self.product_id}.{self.name}@{self.version}"


@dataclass
class Peer:
    """A known peer hub."""

    url: str
    name: str
    capabilities_count: int = 0
    last_crawl: str = ""
    trust_score: float = 0.0
    well_known_url: str = ""
    manifest_url: str = ""
    public_key: str = ""
    depth: int = 0  # BFS depth from seed
    discoverer: str = ""  # Which hub told us about this one
    categories: list[str] = field(default_factory=list)  # self-declared well-known categories
    trusted: bool = False  # operator-approved (or seed-pinned): manifests indexed only if True
    status: str = "active"  # active | key_mismatch | …
    pin_reject_reason: str = ""  # e.g. peer rejected: key changed
    advertised_public_key: str = ""  # last seen key that failed the pin check
    # ── Post-quantum identity (migration 029) ───────────────────────────────────
    # Pinned on FIRST SIGHT, exactly like `public_key`. An unpinned PQ key authenticates the
    # document and not the peer, so this column is what makes requiring PQ mean anything.
    pq_public_key: str = ""
    advertised_pq_public_key: str = ""  # last PQ key that failed the pin check
    first_seen: str = ""  # UTC ISO-8601 of first contact; "" for peers predating migration 023
    # ── What the PEER says about itself (migration 028) ──────────────────────────
    # Read from its own /.well-known/ai-market.json on every successful crawl. None of it
    # is a fact this hub established, which is why it is served under a `declared` envelope
    # and why `declared_id` can never become an identity — see aimarket_hub/peer_identity.
    description: str = ""
    hub_version: str = ""
    declared_id: str = ""  # ecosystem.product / ecosystem.project — a CLAIM, not an id
    mcp_endpoint: str = ""  # display only; routing resolves it live per invoke


@dataclass
class InvocationStat:
    """A recorded invocation for stats/live feed."""

    capability_id: str
    product_id: str
    source_hub: str
    price_usd: float
    latency_ms: int
    success: bool
    timestamp: str
    consumer_hub: str = "local"


@dataclass
class ReputationEvent:
    """A signed reputation attestation."""

    event_type: str  # invocation_success, invocation_failure, bond_update
    provider_hub: str
    capability_id: str | None
    timestamp: str
    price_usd: float
    latency_ms: int
    consumer_hub: str
    signature: str
