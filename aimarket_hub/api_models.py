"""Pydantic request/response models for the hub API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    intent: str = Field("", max_length=4000)
    budget: float | None = Field(None, ge=0, le=100_000)
    max_latency_ms: int | None = Field(None, ge=0)
    min_trust: float | None = Field(None, ge=0, le=1)
    hub: str = Field("any", max_length=256)
    limit: int = Field(20, ge=1, le=100)


class VerifyBlock(BaseModel):
    """Buyer opt-in for Pay-on-Verified settlement (escrow hold until a Metis verdict).

    The buyer supplies the task `intent` the verdict is judged against; money moves
    only after Metis scores the delivered output. `wait` holds the HTTP response
    until the verdict (bounded by `wait_timeout_s`); default is async — the invoke
    returns immediately with a pending envelope resolvable at
    GET /ai-market/v2/verification/{nonce}.
    """

    requested: bool = True
    intent: str = Field("", max_length=20_000)
    mode: str = Field("auto", pattern="^(auto|fast|thinking|council|agent)$")
    wait: bool = False
    wait_timeout_s: float = Field(300, ge=1, le=300)


class InvokeRequest(BaseModel):
    product_id: str = Field(..., min_length=2, max_length=80)
    capability_id: str = Field(..., min_length=2, max_length=80)
    source_hub: str = Field("local", max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    verify: VerifyBlock | None = None


class IpoRequest(BaseModel):
    """Float a product as an ACEX CapShares listing (Agent IPO)."""
    product_id: str = Field(..., min_length=2, max_length=80)
    name: str | None = Field(None, max_length=80)
    symbol: str | None = Field(None, max_length=12)
    max_supply: int | None = Field(None, ge=1, le=10_000_000_000)
    treasury: str | None = Field(None, max_length=128)
    audit_score_bps: int | None = Field(None, ge=0, le=10_000)
    revenue_share_bps: int | None = Field(None, ge=0, le=10_000)


class AuditSyncRequest(BaseModel):
    """Sync on-chain or external auditor coverage into the hub ledger."""
    auditor: str = Field(..., min_length=2, max_length=128)
    cover_usd: float = Field(..., gt=0, le=10_000_000)
    score_bps: int = Field(..., ge=7000, le=10_000)
    phase: str = Field("insuring", max_length=16)


class AuditClaimRequest(BaseModel):
    auditor: str = Field(..., min_length=2, max_length=128)


class AnnounceRequest(BaseModel):
    hub_url: str = Field(..., max_length=256)
    well_known_url: str = Field(..., max_length=256)
    capabilities_count: int = Field(0, ge=0)
    hub_name: str = Field("", max_length=128)
    signer_public_key: str = Field("", max_length=128)
    signature: dict[str, str] | None = None


class ReputationEventsRequest(BaseModel):
    events: list[dict[str, Any]] = Field(..., min_length=1, max_length=100)


class ChannelOpenRequest(BaseModel):
    deposit_usd: float = Field(..., gt=0, le=10_000)
    token: str | None = Field(None, max_length=16)
    chain: str | None = Field(None, max_length=32)
    wallet: str = Field("", max_length=128)
    tx_hash: str = Field("", max_length=128)


class ChannelCloseRequest(BaseModel):
    channel_id: str = Field(..., min_length=8, max_length=64)
    settle_tx_hash: str = Field("", max_length=128)
    wallet: str = Field("", max_length=128)  # for authorization
