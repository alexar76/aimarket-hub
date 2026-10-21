"""Hub API — FastAPI application exposing federation endpoints.

Core routes: .well-known, manifest (v2), federated search, routing proxy,
federation announce, peers list, stats, plugin catalog.

Plugins extend the hub via entry points — they register their own routes
and hook into the invoke pipeline (pre/post checks). See plugin.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import random
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from aimarket_hub.metrics import (
    metrics_payload,
    record_invoke,
    set_federation_peer_health,
)

# Pydantic models imported from api_models
from aimarket_hub.api_models import (
    AnnounceRequest,
    AppealRequest,
    AuditClaimRequest,
    AuditSyncRequest,
    ChannelCloseRequest,
    ChannelOpenRequest,
    PermissionViolationRequest,
    LotteryEntitlementRequest,
    ReceiptVerifyRequest,
    InvokeRequest,
    IpoRequest,
    ReputationEventsRequest,
    StudioRunRequest,
)
from aimarket_hub.channels import (
    channel_escrow_binding,
    _is_production_mode,
    capture_hold,
    channel_balance,
    channel_stats,
    close_channel,
    debit_channel,
    hold_channel,
    open_channel,
    payment_readiness,
    release_hold,
)
from aimarket_hub import consumer_labels
from aimarket_hub import deposit_watch
from aimarket_hub import ecosystem
from aimarket_hub import openapi_merge
from aimarket_hub import security_posture
from aimarket_hub import terminal_ssr
from aimarket_hub import credits
from aimarket_hub import topup
from aimarket_hub import mandates
from aimarket_hub import subcontract
from aimarket_hub import invoke_funding
from aimarket_hub import chain_net
from aimarket_hub import realm
from aimarket_hub import theme
from aimarket_hub import settle
from aimarket_hub import deliveries
from aimarket_hub import x402
from aimarket_hub import contracts_declaration
from aimarket_hub import verified_settlement
from aimarket_hub.access_policy import capability_access_mode, capability_is_publicly_offerable
from aimarket_hub.invoke_outcome import (
    OUTCOME_INCOMPLETE,
    classify_invoke_outcome,
    missing_required_inputs,
)
from aimarket_hub.verified_settlement import VerifiedSettlementService

# Reserved consumer_hub for operator/admin smoke tests. Public sandbox visitors
# and paying channels must NOT use this — otherwise /stats/live collapses all
# real traffic into "operator_self" and hides external demand.
OPERATOR_SELF_CONSUMER = "operator_self"

#: The deposit-claim registry's name for an operator crediting an on-chain payment by hand.
OPERATOR_CREDIT_STACK = "aimarket-hub-operator-credit"

from aimarket_hub.config import HubConfig
from aimarket_hub.catalog import catalogue_display_name, route_freshness
from aimarket_hub.database import HubDatabase
from aimarket_hub.demo_seeder import seed_capabilities
from aimarket_hub.federation_transport import peer_is_aimarket_hub
from aimarket_hub.fulfillment import capability_is_fulfillable
from aimarket_hub.models import InvocationStat, Peer
from aimarket_hub.plugin import PluginRegistry
from aimarket_hub.safety_gate import default_safety_gate
from aimarket_hub.sandbox_trials import (
    consume_sandbox_trial,
    ensure_trial_ledger,
    release_sandbox_trial,
    free_tier_covers,
    model_budget_refusal,
    sandbox_demo_result,
    sandbox_enabled,
    sandbox_quota,
    sandbox_stub_invoke_enabled,
    trial_policy,
)
from aimarket_hub.semantic_search import interpret_intent
from aimarket_hub.signing import Signer, pqc_required, receipt_signature_version
from aimarket_hub.trust import TrustScorer

from aimarket_hub.hub_routes import wellknown as _wellknown_routes
from aimarket_hub.hub_routes import catalog as _catalog_routes
from aimarket_hub.hub_routes import supply as _supply_routes
from aimarket_hub.hub_routes import verification as _verification_routes
from aimarket_hub.hub_routes import mandates as _mandates_routes
from aimarket_hub.hub_routes import invoke as _invoke_routes
from aimarket_hub.hub_routes import federation as _federation_routes
from aimarket_hub.hub_routes import reputation as _reputation_routes
from aimarket_hub.hub_routes import accounts as _accounts_routes
from aimarket_hub.hub_routes import market_stats as _market_stats_routes
from aimarket_hub.hub_routes import capital as _capital_routes
from aimarket_hub.hub_routes import channels as _channels_routes
from aimarket_hub.hub_routes import plugins as _plugins_routes
from aimarket_hub.hub_routes import agents as _agents_routes
from aimarket_hub.hub_routes import pages as _pages_routes
from aimarket_hub.hub_routes import middleware as _middleware_routes
from aimarket_hub.hub_routes import payments as _payments_routes
from aimarket_hub.hub_routes import credentials as _credentials_routes
from aimarket_hub.hub_routes import rate_limits as _rate_limits_routes

logger = logging.getLogger(__name__)

# One worker for the whole process, created on first use. Inbound federation discovery is a
# courtesy: it must never compete with request handling, and it must never grow without
# bound. Deliberately NOT per-app — create_app() runs once in production but hundreds of
# times in a test suite, and a pool per app leaked a thread each time (measured: the suite
# went from 2.5 to 17 minutes before this was a singleton).
_INBOUND_POOL = None
_INBOUND_POOL_LOCK = threading.Lock()


def _inbound_pool():
    global _INBOUND_POOL
    if _INBOUND_POOL is None:
        with _INBOUND_POOL_LOCK:
            if _INBOUND_POOL is None:
                from concurrent.futures import ThreadPoolExecutor

                _INBOUND_POOL = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="aimarket-inbound"
                )
    return _INBOUND_POOL

_HUB_STARTED_AT = time.time()

_SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@")


# Upper bounds for peer-reported accounting numbers. Rejecting an out-of-range value
# outright would let a peer erase its own stats, so we clamp instead — but the clamp
# must exist: `int(1e30)` is a valid Python int and a fatal one for SQLite
# ("Python int too large to convert to SQLite INTEGER"), which turned a hostile
# latency_ms into an unhandled 500 on the federated invoke path.
_MAX_REPORTED_PRICE_USD = 1_000_000.0
_MAX_REPORTED_LATENCY_MS = 2_147_483_647  # int32, safely inside SQLite's 8-byte INTEGER


def _as_float(value: Any, *, max_value: float | None = None) -> float:
    """Coerce a peer-supplied number to a finite, non-negative float (0.0 otherwise).

    Federated accounting (routing fee, invocation stats) reads price/latency straight
    out of a remote hub's response body, which the hub does not control. A string,
    None, NaN or negative value there would either raise inside the fee arithmetic
    (surfacing as a 500) or record nonsense — so an unusable number bills nothing.
    ``max_value`` additionally clamps a merely absurd (but finite) number, because a
    finite value can still be fatal downstream — see _MAX_REPORTED_LATENCY_MS.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(num) or num < 0:
        return 0.0
    if max_value is not None and num > max_value:
        return float(max_value)
    return num


def _money_arg(value: Any, field: str, *, allow_zero: bool = True) -> float:
    """Parse a caller-supplied USD amount or raise HTTP 400.

    Guards the stake/bond routes. ``float()`` alone is not enough: JSON bodies reach
    these handlers as free-form dicts, and both ``"inf"`` and a bare ``Infinity``
    literal survive json.loads → float(). A non-finite stake credit is written to the
    ledger BEFORE the response is serialized, so it persisted an infinite stake (and an
    infinite hub trust-anchor edge) for an arbitrary publisher_id and only then failed
    with a 500 — i.e. the error surfaced after the damage, not instead of it.
    """
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from exc
    if not math.isfinite(num):
        raise HTTPException(status_code=400, detail=f"{field} must be a finite number")
    if num < 0 or (num == 0 and not allow_zero):
        raise HTTPException(status_code=400, detail=f"{field} must be positive")
    return num


def _trim_rate_buckets(
    buckets: dict[str, list[float]], window: float, max_keys: int, keep: str,
) -> None:
    """Bound the in-memory invoke-rate table in place.

    Expired buckets go first — they carry no state at all. Evicting only those is not
    a bound though: enough distinct client addresses inside a single window keeps every
    bucket live and the dict grows without limit (memory DoS via source rotation). So
    if the table is still over the cap we drop the least-recently-active buckets, which
    are the ones closest to expiring anyway. ``keep`` is the bucket we just charged; it
    must survive, otherwise the trim would hand the caller a fresh window.
    """
    for key in [k for k, v in buckets.items() if not v or v[-1] <= window]:
        if key != keep:
            buckets.pop(key, None)
    excess = len(buckets) - max_keys
    if excess <= 0:
        return
    for key in sorted(buckets, key=lambda k: buckets[k][-1]):
        if excess <= 0:
            break
        if key == keep:
            continue
        buckets.pop(key, None)
        excess -= 1


def _env_positive_int(name: str, default: int) -> int:
    """Non-negative int env knob; garbage keeps the default (0 = limiter disabled)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r, using %d", name, raw, default)
        return default
    return value if value >= 0 else default


def _parse_subject_tokens(raw: str, env_name: str) -> dict[str, str]:
    """Parse a per-subject credential registry ("subj-a:secretA,subj-b:secretB").

    Used for BOTH namespaced registries — ``AIMARKET_PUBLISHER_TOKENS`` (publisher ids)
    and ``AIMARKET_AGENT_TOKENS`` (agent ids). One parser, so the two registries cannot
    drift on what a malformed entry means. Malformed entries are dropped with a warning
    naming the registry rather than silently becoming a subject named "" — an empty id
    would match every unnamed caller.
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        subject, sep, token = entry.partition(":")
        subject, token = subject.strip(), token.strip()
        if not sep or not subject or not token:
            logger.warning("Ignoring malformed %s entry (want <id>:<token>)", env_name)
            continue
        out[subject] = token
    return out


def _parse_trusted_proxies(raw: str) -> frozenset[str]:
    """Addresses whose ``X-Forwarded-For`` this hub is willing to believe.

    Empty by default: a hub with no declared proxy trusts NOTHING and keys its rate
    limits on the raw peer address. ``*`` is rejected on purpose — blanket-trusting the
    header lets any client name its own IP and hand itself a fresh rate-limit bucket per
    request, which is strictly worse than the one-shared-bucket problem this exists to
    fix.
    """
    out: set[str] = set()
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        if entry == "*":
            logger.error(
                "AIMARKET_TRUSTED_PROXIES=* is refused: blanket-trusting "
                "X-Forwarded-For lets every caller forge its own client address. "
                "List the proxy address(es) explicitly (e.g. 127.0.0.1)."
            )
            continue
        out.add(entry)
    return frozenset(out)


#: Where a scraper of /metrics may connect from directly: loopback, the RFC 1918 ranges a
#: container network or an operator's LAN uses, and IPv6 unique-local addresses.
_INTERNAL_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7",
))


def _client_address(request: Any, trusted_proxies: frozenset[str]) -> str:
    """The client address rate limits key on, honouring a declared reverse proxy.

    Behind ``deploy/nginx/*.conf`` the hub's peer is always the proxy (127.0.0.1 for a
    same-host nginx, the bridge gateway from a container), so keying on
    ``request.client.host`` alone put THE WHOLE INTERNET IN ONE BUCKET — the per-IP
    channel cap became a global cap and the LRU eviction the ledger leans on had nothing
    left to bound.

    ``X-Forwarded-For`` is consulted ONLY when the immediate peer is a declared proxy
    (``AIMARKET_TRUSTED_PROXIES``); an unlisted peer's header is ignored outright, so a
    direct caller cannot forge a client address.

    The list is walked RIGHT TO LEFT and the first entry that is not itself a trusted
    proxy wins. nginx uses ``$proxy_add_x_forwarded_for``, which APPENDS the real peer to
    whatever the client sent, so a client that sends ``X-Forwarded-For: 1.2.3.4`` produces
    ``1.2.3.4, <real client>``: reading left-to-right would take the forged value, while
    the rightmost untrusted entry is the address nginx itself observed.
    """
    peer = ""
    client = getattr(request, "client", None)
    if client is not None:
        peer = str(getattr(client, "host", "") or "").strip()
    if not peer or peer not in trusted_proxies:
        return peer
    headers = getattr(request, "headers", None)
    raw = ""
    if headers is not None:
        try:
            raw = headers.get("x-forwarded-for") or ""
        except Exception:
            raw = ""
    for hop in reversed([h.strip() for h in str(raw).split(",")]):
        if hop and hop not in trusted_proxies:
            return hop
    # Every hop is a trusted proxy (or the header is absent): the proxy itself is the
    # only address we can attribute this request to. Not exempt — one shared bucket.
    return peer


#: A peer's payment headers that travel back to the buyer unchanged: the x402 V2 offer, the
#: settlement response of either dialect, and the invoice nonce a seller-direct door names.
_PEER_PAYMENT_HEADERS = ("payment-required", "payment-response", "x-payment-response",
                         "x-payment-nonce")


def _peer_payment_headers(resp: Any) -> dict[str, str]:
    """The peer's payment headers to forward, plus the CORS exposure a browser buyer needs."""
    out = {k: v for k, v in resp.headers.items() if k.lower() in _PEER_PAYMENT_HEADERS}
    if out:
        out["Access-Control-Expose-Headers"] = ", ".join(sorted(out))
    return out


def _announced_relay(peer: str, xff: str, trusted_proxies: frozenset[str]) -> bool:
    """A caller that is not our proxy but forwards for somebody else (it sent X-Forwarded-For).

    Careful with what uvicorn already did: with AIMARKET_TRUSTED_PROXIES set, its
    ProxyHeadersMiddleware replaces the socket peer by the rightmost untrusted X-Forwarded-For
    hop BEFORE the app runs, so every request nginx relays arrives with an untrusted "peer" and
    the header still present. That peer then EQUALS the header's rightmost untrusted hop; a real
    relay's own address does not. (X-Real-IP is no signal at all: nginx sets it on every call.)
    """
    if not xff or not peer or peer in trusted_proxies:
        return False
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if hop not in trusted_proxies:
            return hop != peer
    return False


def _is_safe_path(segment: str) -> bool:
    """Reject path traversal in product/capability ids used in upstream URLs."""
    if not segment or len(segment) > 80:
        return False
    if ".." in segment or "/" in segment or "\\" in segment:
        return False
    return all(c in _SAFE_ID_CHARS for c in segment)


# ── What a caller must supply to invoke ────────────────────────
#
# The catalogue carries every provider's `input_schema`, but /search never surfaced
# any of it, so a client had no way to know a capability has required fields. The
# hub's own console then sent one shape — `{"text": <query>}` — to everything, which
# is fine for a free-text oracle and invalid for every capability that needs real
# arguments: on 2026-08-25 production showed `atlas.point.read@v1` refused 8/8 with
# `point_id required`, plus the same refusal on situation.brief / watchbox.check /
# fire.weather / gnss.degradation / ais.public.read. A refusal still spends a free
# trial and (on a paid channel) still settles, so guessing the body is not free.
#
# Only the REQUIRED properties are echoed, and only the fields a form needs: the
# whole schema on every one of up to 100 matches would bloat a browse response for
# data the caller can still fetch from the provider's manifest.
_INPUT_HINT_KEYS = ("type", "description", "enum", "minimum", "maximum", "default")


def _input_requirements(schema: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Required input names + a compact per-field hint, from a capability schema.

    Defensive about the shape: `input_schema` is a peer-controlled blob that the
    crawler stores verbatim, so anything that is not the JSON-Schema object we expect
    yields "no known requirements" rather than an exception on a browse request.
    """
    if not isinstance(schema, dict):
        return [], {}
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    # `required` must be a LIST. A peer that writes it as a bare string would
    # otherwise be iterated character by character — "point_id" became eight
    # single-letter required fields (caught by the peer-blob test below).
    declared = schema.get("required")
    declared = declared if isinstance(declared, (list, tuple)) else []
    required: list[str] = []
    hints: dict[str, dict[str, Any]] = {}
    for name in declared:
        if not isinstance(name, str) or name in hints:
            continue
        # A property listed as required but never described is still required — the
        # caller has to be told about it even with no hint to give.
        spec = props.get(name)
        spec = spec if isinstance(spec, dict) else {}
        required.append(name)
        hints[name] = {
            k: spec[k] for k in _INPUT_HINT_KEYS
            if k in spec and isinstance(spec[k], (str, int, float, bool, list))
        }
    return required, hints


def _observe_answer(db: HubDatabase, source_hub: Any, body: Any, response: Any) -> None:
    """Digests of what was asked and what came back (answer_dependence.py). Bookkeeping: never fails a call."""
    try:
        from aimarket_hub import answer_dependence

        answer_dependence.observe(
            db, (str(source_hub or "local"), body.product_id, body.capability_id), body.input, response,
        )
    except Exception as exc:
        logger.warning("answer observation NOT recorded for %s: %s", getattr(body, "capability_id", "?"), exc)


def _catalogue_route(db: HubDatabase, config: HubConfig, capability: Any):
    """Route state shared by every buyer-facing catalogue surface."""
    source_hub = str(getattr(capability, "source_hub", "") or "local")
    peer = None if source_hub == "local" else db.get_peer(source_hub)
    return route_freshness(
        capability,
        peer,
        max_age_s=config.catalog_max_stale_s,
    )


def _catalogue_snapshot(db: HubDatabase, config: HubConfig, *, limit: int = 1000):
    """Fulfillable, public offers whose route is currently sellable."""
    return [
        capability
        for capability in db.list_capabilities(limit=limit)
        if capability_is_fulfillable(capability)
        and capability_is_publicly_offerable(capability)
        and _catalogue_route(db, config, capability).sellable
    ]



def _ceil_to_cents(usd: float) -> float:
    """Round a price UP to a whole cent — the granularity both money paths already use.

    `channels._dollars_to_cents_bill` bills `max(1, ceil(usd*100))` cents and
    `escrow_bridge.escrow_verify.usd_to_base_units` converts a signed amount the same way,
    so any quote finer than a cent is a number the buyer cannot sign and the hub would not
    honour: the verifier demands the cent it ceils to, the client re-signs the quote it was
    given, and the purchase deadlocks with the deposit already on chain.
    """
    if usd <= 0:
        return 0.0
    return math.ceil(round(usd * 100, 6)) / 100.0


# ── Federated transport resolution ─────────────────────────────
#
# Which URL a routed invoke is POSTed to. A peer that publishes `mcp_endpoint` in
# its `/.well-known/ai-market.json` is telling us where to send invokes; a peer
# that does not gets the legacy `/capabilities/{product}/{cap}/invoke` path.
#
# This used to be decided by matching the peer's self-declared categories against a
# hardcoded list — ("oracle", "simulation", "math-viz", "randomness-beacon") — and
# only those four peers were asked for their endpoint. Every other peer got the
# legacy path regardless of what it advertised. GAIA, the physical-world sensor
# gateway, declares `["iot", "sensors", "physical-data", "verification"]` and
# advertises `mcp_endpoint: https://iot.modelmarket.dev/ai-market/v2/invoke`; the
# hub POSTed the legacy path instead, that path answers 405, and every routed
# invoke of a live, priced, signed sensor reading came back as
# `502 Provider returned 405`. A category string is a label a peer chose for
# discovery; the endpoint it publishes is an instruction. Read the instruction.
_PEER_ENDPOINT_TTL_S = 300.0
_peer_endpoint_cache: dict[str, tuple[float, str | None]] = {}
# Read from the same well-known fetch: does the peer declare that buyers pay its listings
# directly (payment_rails.seller_direct.enabled)? Decides the routing fee — see invoke.
_peer_seller_direct: dict[str, bool] = {}


# The rule itself lives in `federation_transport` because the admission assay needs the
# same answer: it kept its own copy, read `mcp_endpoint` verbatim, and could never
# sandbox-probe a hub peer. Re-exported under the old name so this module's callers and
# tests are unaffected.
_peer_is_aimarket_hub = peer_is_aimarket_hub



# The salt is per process unless AIMARKET_BUYER_SALT pins it: a peer needs a buyer id that is
# stable for as long as its rate window, not forever, and an id that cannot be linked across hubs
# or back to an address.
_BUYER_SALT = (os.environ.get("AIMARKET_BUYER_SALT") or "").encode() or secrets.token_bytes(32)


def _opaque_buyer(rate_key: str | None) -> str:
    """An opaque, stable id for one buyer as seen by THIS hub, sent to peers on routed invokes.

    It is an HMAC of the key this hub rate-limits the buyer by (their address, or their sandbox
    visitor id) — never the address, channel or account itself, which are this hub's business.
    A peer can give each buyer their own quota with it; it cannot tell who they are.
    """
    import hmac as _hmac

    digest = _hmac.new(_BUYER_SALT, (rate_key or "anonymous").encode(), hashlib.sha256).hexdigest()
    return "b-" + digest[:20]


def _provider_channel_ref(channel_id: str | None) -> str:
    """What a provider this hub executes is told about the buyer's payment channel.

    The raw channel id used to be forwarded as X-Payment-Channel. It is the ledger's handle
    for hold/debit/close. Before the fail-closed secret check, a secretless channel's id
    alone authorized those calls — every provider a buyer called learned enough to spend
    that buyer's balance on its own listings. Secretless channels are now frozen. No
    provider needs the id itself: the oracles read the header's PRESENCE (a trusted proxy
    forwarding a paying call, oracle_core/tiers.py), the attested apps hash it into an
    actor id, and the factory's channels live in a different ledger that never held it.

    So the provider gets an HMAC reference: present exactly when the buyer sent a channel,
    the same for every call on that channel, and not a channel id any ledger will find.
    """
    raw = (channel_id or "").strip()
    if not raw:
        return ""
    import hmac as _hmac

    digest = _hmac.new(
        _BUYER_SALT, b"provider-channel-ref\x00" + raw.encode(), hashlib.sha256,
    ).hexdigest()
    return "chref-" + digest[:24]

async def _peer_invoke_endpoint(peer: Any, *, now: float | None = None) -> str | None:
    """The peer's advertised invoke endpoint, or None to use the legacy path.

    Cached per peer URL: resolving it costs a well-known fetch, and doing that on
    every routed invoke would add a round trip to each call. A negative result is
    cached too, so a peer with no `mcp_endpoint` is not re-probed per invoke.
    """
    key = getattr(peer, "url", "") or ""
    stamp = time.time() if now is None else now
    hit = _peer_endpoint_cache.get(key)
    if hit is not None and stamp - hit[0] < _PEER_ENDPOINT_TTL_S:
        return hit[1]

    endpoint: str | None = None
    well_known = getattr(peer, "well_known_url", "") or ""
    if well_known:
        try:
            from aimarket_hub.outbound_http import safe_get

            resp = await safe_get(well_known, timeout=10)
            if resp.status_code == 200:
                doc = resp.json() or {}
                rails = doc.get("payment_rails") if isinstance(doc, dict) else None
                direct = rails.get("seller_direct") if isinstance(rails, dict) else None
                _peer_seller_direct[key] = bool(isinstance(direct, dict) and direct.get("enabled") is True)
                # An AIMarket hub is NOT reachable at its own `mcp_endpoint` with this
                # envelope: that endpoint speaks MCP JSON-RPC over SSE, so posting
                # `{capability_id, input}` there returns `Method not found` inside a
                # text/event-stream body and the routed call dies as "provider returned a
                # non-JSON body". Hub-to-hub routing was therefore impossible — which is
                # why every peer in the live federation is an oracle or a satellite and
                # never another hub. A peer that declares itself a hub gets its v2 invoke
                # route, which speaks exactly the envelope we are about to send.
                if _peer_is_aimarket_hub(doc):
                    base = str(getattr(peer, "url", "") or "").rstrip("/")
                    endpoint = f"{base}/ai-market/v2/invoke" if base else None
                else:
                    advertised = doc.get("mcp_endpoint")
                    if isinstance(advertised, str) and advertised.strip():
                        candidate = advertised.strip()
                        # A peer names WHERE ON ITSELF to send, never where to send. The
                        # crawler, reading this same field, keeps it "only when it is safe
                        # and same-origin: a peer that could store an arbitrary URL here
                        # would have a stored SSRF pointer" — and this is the reader that
                        # actually POSTs, with the caller's `input` in the body and the
                        # response handed back. Same rule here, or the careful one upstream
                        # was protecting the display copy and nothing else.
                        from aimarket_hub.federation_assay import same_origin

                        # SAME-ORIGIN is the whole rule, and deliberately not also
                        # `_url_is_safe`. The peer's own URL was already address-checked when
                        # it was admitted, and `safe_post` pins the resolved IP at send time,
                        # so a same-origin endpoint is the destination we would have used
                        # anyway — no reachability is gained. Adding a DNS-dependent check
                        # here instead made TRANSPORT SELECTION depend on whether the peer
                        # resolved at that instant: an unresolvable-but-legitimate host got
                        # silently rerouted to a different path. The address question belongs
                        # where the connection is made, not where the route is chosen.
                        peer_url = str(getattr(peer, "url", "") or "")
                        if same_origin(peer_url, candidate):
                            endpoint = candidate
                        else:
                            logger.warning(
                                "invoke: peer %s advertised an mcp_endpoint we will not "
                                "follow (%r); using its own invoke route instead",
                                peer_url, candidate[:200],
                            )
                            base = peer_url.rstrip("/")
                            endpoint = f"{base}/ai-market/v2/invoke" if base else None
        except Exception as exc:  # unreachable well-known → legacy path, not an error
            logger.warning("invoke: well-known lookup failed for %s: %s", key, exc)
            # Do NOT cache a transport decision made from a failed lookup: the peer
            # may be momentarily down while still being a v2 peer, and caching the
            # fallback would pin it to the wrong transport for the whole TTL.
            return None

    _peer_endpoint_cache[key] = (stamp, endpoint)
    return endpoint


async def _peer_sells_direct(peer: Any) -> bool:
    """True when the peer's own well-known says its buyers pay its listings directly.

    Resolved by the same cached well-known read as the invoke endpoint, so it costs no
    extra fetch per call. An unreadable well-known is False: the old fee rule applies.
    """
    await _peer_invoke_endpoint(peer)
    return _peer_seller_direct.get(getattr(peer, "url", "") or "", False)


# ── Router factory ─────────────────────────────────────────────


def _authorization_too_short(authorization: Any) -> bool:
    """True when an escrow DebitAuthorization dies before a verified call could be collected."""
    try:
        deadline = int((authorization or {}).get("deadline") or 0)
    except (TypeError, ValueError, AttributeError):
        return False  # malformed: the bridge's own verification refuses it with a precise reason
    return deadline < time.time() + verified_settlement.min_authorization_deadline_s()


def create_app(
    config: HubConfig | None = None,
    db: HubDatabase | None = None,
    signer: Signer | None = None,
    trust_scorer: TrustScorer | None = None,
    plugins: PluginRegistry | None = None,
) -> FastAPI:
    """Create the hub FastAPI app.

    Args:
        config: Hub configuration
        db: Hub database
        signer: Ed25519 signer
        trust_scorer: Trust score computer
        plugins: Plugin registry (auto-discovers from entry_points if None)
    """
    config = config or HubConfig()
    if db is None:
        # Production must never silently fall back to a local SQLite file when an env file
        # is missing or malformed. That creates a fresh, apparently healthy empty market
        # beside the real PostgreSQL ledger — the worst failure mode for balances and trials.
        # Injected databases remain available to isolated tests; the standalone production
        # entry point is fail-closed.
        database_url = (config.database_url or "").strip()
        if _is_production_mode() and not database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError(
                "production Hub requires DATABASE_URL=postgresql://...; "
                "SQLite fallback is disabled when AIFACTORY_PROD=1"
            )
        db = HubDatabase(config.db_path, database_url=database_url)
    # The free tier is part of this hub's contract with strangers, so its ledger is built
    # here rather than on the first call that needs it. Lazily, a hub that cannot keep trial
    # counters starts clean, reports healthy, and 500s at the visitor.
    if sandbox_enabled():
        ensure_trial_ledger()
    if signer is None:
        from aimarket_hub.signing import hub_signs_hybrid, pqc_signing_setting

        hybrid = hub_signs_hybrid(config.signing_key_path)
        signer = Signer(config.signing_key_path, pqc=hybrid)
        if hybrid and pqc_signing_setting() is None:
            logger.info("Signing hybrid (Ed25519 + ML-DSA-65) by default, as every AIMarket hub "
                        "does; AIMARKET_PQC=0 signs Ed25519 only.")
        if pqc_signing_setting() is False and os.path.exists(f"{config.signing_key_path}_mldsa"):
            logger.warning(
                "AIMARKET_PQC=0 on a hub that holds an ML-DSA-65 key: peers that pinned it will "
                "refuse this hub's Ed25519-only documents (ratchet). Unset it to keep signing hybrid.")
    # Bind the credits rail to THIS app's database. A new app may be a new database
    # (every test builds one), and a rail still pointed at the previous file would debit
    # accounts that no longer describe this hub.
    # Bound to THIS app, not to a module global. A process can hold more than one hub —
    # every test builds several, and the cross-hub settlement suite runs two real hubs that
    # pay each other — and a shared global meant the second app's ledger silently answered
    # the first app's requests: a valid key on hub B was looked up in hub A's database and
    # came back 401. `configure` still sets the module handle for single-app entry points
    # (the CLI, quickstart), but nothing inside a request path reads it.
    hub_credits = credits.configure(db._conn)
    # Mandates and subcontracting (aimarket-protocol/mandates.md) live beside the credits
    # ledger they constrain, on the same backend, bound to this app for the same reason.
    hub_mandates = mandates.MandateStore(db._conn, config.hub_url)
    hub_jobs = subcontract.JobStore(db._conn, signer, config.hub_url)
    hub_x402 = x402.PaymentStore(db._conn)
    hub_settle = settle.PaymentStore(db._conn)
    hub_invoices = settle.InvoiceStore(db._conn)
    hub_topups = topup.TopupStore(db._conn)
    hub_deposits = deposit_watch.DepositStore(db._conn)
    settle.refuse_unbound_setting()
    # Which realm is this? Logged, never published: the bubble must be indistinguishable
    # from inside, so the only reader told about it is the operator, who is outside.
    if realm.is_uni():
        logger.warning(
            "[realm=uni] SEALED BUBBLE — chain id %s, simulated economy. Every amount this "
            "hub reports is virtual and must never be quoted as revenue.",
            realm.uni_chain_id(),
        )
        # Peers admitted through a door the seal did not watch (see realm.peer_inside).
        try:
            realm.evict_outside_peers(db)
        except Exception as exc:  # noqa: BLE001 - logged loudly; the doors still hold
            logger.error("realm: could not evict outside peers: %s", exc)
    if config.crypto_enabled:
        # Fail closed at boot rather than at the first payment offer: a hub that has already
        # advertised a mainnet asset inside a bubble cannot take it back.
        try:
            spec = chain_net.active_network()
            realm.assert_sealed(
                chain_id=spec.chain_id, rpc_urls=spec.rpc_urls, addresses=spec.addresses,
            )
        except realm.RealmBreach:
            raise
        except Exception as exc:  # noqa: BLE001 - a resolver problem is not a breach
            logger.warning("realm: could not verify the chain configuration: %s", exc)
    # Whose hub is this? Every page the hub serves used to be branded, linked and
    # escrow-stamped for one specific deployment, so rebranding meant editing source.
    theme.configure(
        name=config.hub_name,
        hub_url=config.hub_url,
        source_url=config.source_url,
        escrow_url=(
            f"https://basescan.org/address/{config.escrow_evm_address.strip()}"
            if config.escrow_evm_address.strip() else ""
        ),
        ecosystem_links=config.ecosystem_links,
    )

    # A new app is a new hub: different config, possibly a different peer set. Carrying
    # a cached transport decision in from a previous instance in the same process is
    # wrong, and it is observable — running the federated-invoke suite in one process
    # made a peer resolved by an earlier test keep its endpoint for a later test that
    # had configured its well-known to fail, so the legacy-fallback assertion saw the
    # cached endpoint instead. Found by building the image and running the suite where
    # it can actually execute; both local interpreters skip these tests at fixture setup.
    _peer_endpoint_cache.clear()
    _peer_seller_direct.clear()
    trust_scorer = trust_scorer or TrustScorer(db)
    builtin_safety = default_safety_gate()  # Built-in fallback
    # Pay-on-Verified settlement worker (buyer-opt-in escrow holds; env-gated per invoke).
    verify_svc = VerifiedSettlementService(db=db, signer=signer, consumer_hub=OPERATOR_SELF_CONSUMER)

    # Seed marketplace with initial capability catalog on first start.
    #
    # Never in production. The seeded showcase exists so a fresh hub is not empty, and
    # every one of its twelve rows is unexecutable — no invoke_url, no static pack — so on
    # a real deployment they are stock that cannot be delivered. `seed_capabilities`
    # re-creates them whenever the local count reaches zero, which makes deleting them a
    # temporary act: they came back on the next restart. Cleaning the catalogue and then
    # having the next deploy undo it is worse than never seeding, so production opts out
    # here rather than relying on AIMARKET_SKIP_SEED being remembered in a deploy capture.
    _prod = os.environ.get("AIFACTORY_PROD", "").strip().lower() in ("1", "true", "yes")
    _skip = os.environ.get("AIMARKET_SKIP_SEED", "").strip().lower() in ("1", "true", "yes")
    if not _skip and not _prod:
        seeded = seed_capabilities(db)
        if seeded:
            import logging
            logging.getLogger(__name__).info("Seeded %d marketplace capabilities", seeded)

    # Import real products from AI-Factory if bridge is available
    try:
        from aimarket_hub.factory_bridge import import_factory_products
        real = import_factory_products(db)
        if real:
            import logging
            logging.getLogger(__name__).info("Imported %d real factory products", real)
    except Exception:
        pass

    # Build multilingual document profiles before the first visitor types. The profile
    # cache is only worthwhile for a real catalogue; tiny unit-test/demo databases stay
    # instant. Ranking itself is local and read-only.
    if db.count_capabilities() > 20:
        with contextlib.suppress(Exception):
            db.search_capabilities_detailed("weather wildfire navigation proof", limit=1)

    # Plugin discovery
    if plugins is None:
        plugins = PluginRegistry()
        plugins.discover(db)
        plugins.startup_all(db)

    if not config.crypto_enabled:
        access = (
            "prepaid credits can still enforce 402 via X-API-Key"
            if credits.enabled()
            else "free/sandbox tier active; no payment rail can enforce 402"
        )
        logger.warning(
            "[crypto-disabled] AIFACTORY_CRYPTO_ENABLED=0 — payment channels offline; "
            "%s; no on-chain verification/escrow; NFT minting "
            "blocked. Manifest/receipt signing and federation are unaffected. "
            "Set AIFACTORY_CRYPTO_ENABLED=1 to enable the on-chain economy.",
            access,
        )

    @contextlib.asynccontextmanager
    async def _lifespan(fastapi_app: FastAPI):
        """Run a periodic federation crawl so peers (e.g. Platon) are discovered
        and indexed automatically — honouring crawl_interval_s, which until now
        was advertised but never consumed. Opt out with AIMARKET_AUTO_CRAWL=0."""
        from aimarket_hub.mcp_gateway import install_access_log_redaction

        install_access_log_redaction()   # uvicorn has configured its loggers by now
        crawl_task: asyncio.Task | None = None
        collect_task: asyncio.Task | None = None
        sweep_task: asyncio.Task | None = None
        a2a_sweep_task: asyncio.Task | None = None
        openapi_task: asyncio.Task | None = None
        deposit_task: asyncio.Task | None = None

        def _run_crawl_blocking() -> dict:
            from aimarket_hub.crawler import Crawler
            from aimarket_hub.slash_sync import SlashRegistry
            # Own DB connection + trust scorer for this worker thread so the crawl's
            # synchronous SQLite work (per-peer trust scoring, per-tool upsert+commit)
            # never blocks the server event loop and no sqlite3 connection is shared
            # across threads. WAL lets the app's connection keep serving meanwhile.
            crawl_db = HubDatabase(config.db_path, database_url=config.database_url)
            # The slash registry is DB-backed, so it MUST also use the crawl-thread
            # connection — handing the app's registry here would make this worker thread
            # write the app's sqlite connection concurrently with request handlers. The
            # app registry is refreshed from disk after the crawl returns (see below).
            crawl_slash_registry = SlashRegistry(config.hub_url, db=crawl_db)
            crawler = Crawler(
                config=config, db=crawl_db, signer=signer,
                trust_scorer=TrustScorer(crawl_db),
                slash_registry=crawl_slash_registry,
            )

            async def _do() -> dict:
                try:
                    stats = await crawler.crawl(clear_first=False)
                finally:
                    await crawler.close()
                # A few free federated capabilities are asked whether their answer depends on the
                # input (answer_dependence.py), on this thread's own connection. Never fails the cycle.
                try:
                    from aimarket_hub import answer_dependence
                    from aimarket_hub.outbound_http import safe_post

                    probed = await answer_dependence.probe_free(
                        crawl_db, config, resolve_endpoint=_peer_invoke_endpoint, post=safe_post,
                    )
                    if isinstance(stats, dict):
                        stats["answer_probe"] = probed
                except Exception as exc:
                    logger.warning("answer probe cycle failed: %s", exc)
                return stats

            try:
                return asyncio.run(_do())
            finally:
                with contextlib.suppress(Exception):
                    crawl_db.close()

        async def _periodic_crawl() -> None:
            # Stagger so the server is serving before the first crawl runs.
            await asyncio.sleep(max(0.0, config.crawl_initial_delay_s))
            interval = max(60, int(config.crawl_interval_s))
            while True:
                try:
                    # Offload the whole crawl (async HTTP + sync DB) to a worker
                    # thread so concurrent /invoke, /search, /.well-known never stall.
                    stats = await asyncio.to_thread(_run_crawl_blocking)
                    # The crawl persisted newly-ingested peer attestations via its own
                    # connection; refresh the app registry from disk (event-loop thread)
                    # so federated_penalty reflects them without a restart — no
                    # cross-thread write to the app connection.
                    slash_registry.reload()
                    logger.info("auto-crawl complete: %s", stats)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a bad cycle must never kill the loop
                    logger.warning("auto-crawl cycle failed: %s", exc)
                # Jitter avoids a thundering herd across federated hubs.
                jitter = random.uniform(0, min(60.0, interval * 0.1))
                await asyncio.sleep(interval + jitter)

        if config.auto_crawl and config.seed_list:
            crawl_task = asyncio.create_task(_periodic_crawl())
            logger.info(
                "Auto-crawl enabled: %d seed(s), interval %ds",
                len(config.seed_list), max(60, int(config.crawl_interval_s)),
            )
        elif config.auto_crawl:
            logger.info("Auto-crawl enabled but seed list is empty — nothing to crawl.")
        # KI-11: an authorized escrow debit is captured on every paid invoke and, until
        # now, collected only when a person ran the CLI. Off unless an interval is set,
        # and bounded by the bridge's own policy and spend caps rather than by this loop.
        with contextlib.suppress(Exception):
            from aimarket_hub.escrow_bridge import collector as escrow_collector

            if escrow_collector.interval_s() > 0:
                collect_task = asyncio.create_task(escrow_collector.run_forever())
            else:
                logger.debug(
                    "escrow collection: %s", escrow_collector.blocked_reason()
                )
        # Subcontracting allowances a root call failed to settle (a crash mid-call, a
        # release that kept failing) are buyer money frozen in a hold. Sweeping only when
        # the next allowance is asked for would leave them frozen on a hub nobody asks
        # again; so the sweep also runs at start and on a timer.
        def _sweep_allowances_once() -> None:
            try:
                recovered = invoke_funding.sweep_stale_credit_holds(
                    credits_ledger=hub_credits, store=hub_mandates, limit=50,
                )
                if recovered:
                    logger.warning("credits: released %d abandoned invoke hold(s)", recovered)
                n = invoke_funding.sweep_stale_allowances(
                    credits_ledger=hub_credits, store=hub_mandates, jobs=hub_jobs, limit=50,
                )
                if n:
                    logger.warning("subcontract: settled %d stale allowance(s)", n)
            except Exception as exc:  # noqa: BLE001 - a bad cycle must never kill the loop
                logger.warning("subcontract: allowance sweep failed: %s", exc)

        async def _periodic_allowance_sweep(interval: float) -> None:
            while True:
                await asyncio.sleep(interval)
                _sweep_allowances_once()

        try:
            sweep_interval = float(os.getenv("AIMARKET_SUBCONTRACT_SWEEP_S", "120"))
        except ValueError:
            sweep_interval = 120.0
        if sweep_interval > 0 and hub_credits is not None and credits.enabled():
            # Once now — a restart after a crash is exactly when stranded allowances exist —
            # then on the timer.
            _sweep_allowances_once()
            sweep_task = asyncio.create_task(_periodic_allowance_sweep(max(10.0, sweep_interval)))
        # A2A tasks whose process died mid-call (a2a_tasks leases): recovered within a minute of
        # their lease lapsing, from the ledger — not left WORKING until somebody calls /a2a.
        a2a_sweep = getattr(fastapi_app.state, "a2a_sweep", None)
        if a2a_sweep is not None:
            async def _periodic_a2a_sweep() -> None:
                while True:
                    await asyncio.sleep(60.0)
                    try:
                        a2a_sweep()
                    except Exception as exc:  # noqa: BLE001 - a bad cycle must never kill the loop
                        logger.warning("a2a: periodic task sweep failed: %s", exc)

            a2a_sweep_task = asyncio.create_task(_periodic_a2a_sweep())
        # USDC landing in the top-up wallet becomes credit without anybody presenting it
        # (deposit_watch.py) — only on a wallet that receives top-ups and nothing else.
        if deposit_watch.enabled() and hub_credits is not None:
            async def _periodic_deposit_watch() -> None:
                from starlette.concurrency import run_in_threadpool

                while True:
                    # Wait first: a hub restarting in a loop must not hit the node on every start,
                    # and nothing is lost — the cursor resumes where the last scan stopped.
                    await asyncio.sleep(deposit_watch.interval_s())
                    try:
                        summary = await run_in_threadpool(_deposit_watcher().scan_once)
                        if any(k in summary for k in ("credited", "unattributed")):
                            logger.warning("deposit watch: %s", summary)
                    except Exception as exc:  # noqa: BLE001 - a bad cycle must never kill the loop
                        logger.warning("deposit watch: scan failed, retried next cycle: %s", exc)

            deposit_task = asyncio.create_task(_periodic_deposit_watch())
        else:
            logger.info("deposit watch: %s", deposit_watch.unavailable_reason())
        # Same-origin paid routes served beside the hub (x402 sidecar) join /openapi.json,
        # where x402 indexers look for them. Fetched off the request path, on a timer.
        merge_urls = openapi_merge.merge_urls(config.hub_url)
        if merge_urls:
            async def _periodic_openapi_merge() -> None:
                while True:
                    await openapi_merge.refresh(merge_urls)
                    await asyncio.sleep(openapi_merge.REFRESH_S)

            openapi_task = asyncio.create_task(_periodic_openapi_merge())
        # Pay-on-Verified: re-queue verifications left pending by a previous process
        # so a hub restart never strands an escrow hold.
        with contextlib.suppress(Exception):
            await verify_svc.reconcile()
        try:
            yield
        finally:
            for task in (crawl_task, collect_task, sweep_task, openapi_task, deposit_task, a2a_sweep_task):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await verify_svc.shutdown()
            with contextlib.suppress(Exception):
                plugins.shutdown_all()

    app = FastAPI(
        title=f"{config.hub_name} — AIMarket Hub v3",
        version=config.hub_version,
        description="Lean federation hub for AI capability discovery and routing. Plugins extend functionality.",
        lifespan=_lifespan,
    )
    _own_openapi = app.openapi
    app.openapi = lambda: openapi_merge.merged(_own_openapi())  # type: ignore[method-assign]
    # CORS: default to empty allowlist (require AIMARKET_CORS_ORIGINS to enable cross-origin).
    # Previous default of ["*"] enabled drive-by CSRF on state-changing endpoints.
    _cors_env = os.environ.get("AIMARKET_CORS_ORIGINS", "").strip()
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type", "Authorization", "X-Payment-Channel", "X-Payment-Channel-Secret",
            "X-AIMarket-Affiliate", "X-API-Key", "X-AIMarket-Mandate",
            "X-AIMarket-Mandate-Proof", "X-AIMarket-A2A-Proof",
            "X-Payment", "PAYMENT-SIGNATURE", "X-Payment-Nonce", "X-Payment-Secret",
            "X-AIMarket-Sandbox-Visitor", "X-Market-Signature", "X-AIMarket-Routing-Hub",
            "X-AIMarket-Routing-Fee", "X-AIMarket-Buyer", "X-AIMarket-Crawler", "X-Provider-Signature",
            "A2A-Version", "A2A-Extensions",
        ],
    )

    # Middleware and /metrics.
    _middleware_routes.register(
        app=app,
        config=config,
        db=db,
        hub_invoices=hub_invoices,
    )

    # Admin token for federation/crawl-control endpoints.
    # If unset, those endpoints are disabled (fail-closed).
    _ADMIN_TOKEN = os.environ.get("AIMARKET_ADMIN_TOKEN", "").strip()
    if not _ADMIN_TOKEN:
        logger.warning(
            "AIMARKET_ADMIN_TOKEN not set — public Hub observations still land "
            "in quarantine, but federation approval and crawl-control requests "
            "will be rejected. Set this env var to enable operator actions."
        )

    def _require_admin(authorization: str) -> None:
        """Reject if Bearer token doesn't match AIMARKET_ADMIN_TOKEN. Fail-closed."""
        import hmac
        if not _ADMIN_TOKEN:
            raise HTTPException(
                status_code=503,
                detail="Admin endpoints disabled: AIMARKET_ADMIN_TOKEN not configured",
            )
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization[7:]
        if not hmac.compare_digest(token, _ADMIN_TOKEN):
            raise HTTPException(status_code=403, detail="Invalid admin token")

    def _is_admin_bearer(authorization: str) -> bool:
        """True when Authorization is a valid admin Bearer (no raise)."""
        import hmac
        if not _ADMIN_TOKEN or not authorization.startswith("Bearer "):
            return False
        return hmac.compare_digest(authorization[7:], _ADMIN_TOKEN)

    # Payment helpers for invoke.
    payments_scope = _payments_routes.register(
        _is_admin_bearer=_is_admin_bearer,
        config=config,
        hub_credits=hub_credits,
        hub_invoices=hub_invoices,
        hub_settle=hub_settle,
    )
    _bind_hub_fee_payee = payments_scope._bind_hub_fee_payee
    _bind_listing_payee = payments_scope._bind_listing_payee
    _credit_hearth_owner = payments_scope._credit_hearth_owner
    _pay_publisher_share = payments_scope._pay_publisher_share
    _payment_hint = payments_scope._payment_hint
    _payment_ways = payments_scope._payment_ways
    _pays_a_third_party = payments_scope._pays_a_third_party
    _release_market_payment = payments_scope._release_market_payment
    _resolve_invoke_consumer_hub = payments_scope._resolve_invoke_consumer_hub
    _settle_market_payment = payments_scope._settle_market_payment
    _unbind_payee = payments_scope._unbind_payee

    # Credential checks.
    credentials_scope = _credentials_routes.register(
        _ADMIN_TOKEN=_ADMIN_TOKEN,
        config=config,
        hub_credits=hub_credits,
    )
    _AGENT_TOKENS = credentials_scope._AGENT_TOKENS
    _NS_AGENT = credentials_scope._NS_AGENT
    _NS_PUBLISHER = credentials_scope._NS_PUBLISHER
    _credits_publisher = credentials_scope._credits_publisher
    _ecosystem_policy = credentials_scope._ecosystem_policy
    _require_stake_subject = credentials_scope._require_stake_subject
    _require_subject_credential = credentials_scope._require_subject_credential

    # Reverse-proxy trust for the per-IP rate limits below. See _client_address: without
    # a declared proxy the hub keys on the raw peer, which behind deploy/nginx/*.conf is
    # the proxy itself — one bucket for the whole internet.
    _TRUSTED_PROXIES = _parse_trusted_proxies(
        os.environ.get("AIMARKET_TRUSTED_PROXIES", "")
    )
    if not _TRUSTED_PROXIES and _is_production_mode():
        logger.warning(
            "AIMARKET_TRUSTED_PROXIES not set — per-IP rate limits key on the immediate "
            "peer. Behind a reverse proxy that is the PROXY's address, so every caller "
            "shares one bucket; set it to the proxy address(es) (e.g. 127.0.0.1)."
        )

    def _mcp_caller(request: Any) -> str:
        return _client_address(request, _TRUSTED_PROXIES)

    # Rate limits.
    rate_limits_scope = _rate_limits_routes.register(
    )
    _INVOKE_RATE_WORKERS = rate_limits_scope._INVOKE_RATE_WORKERS
    _check_invoke_rate = rate_limits_scope._check_invoke_rate
    _signup_allowed = rate_limits_scope._signup_allowed

    # Attach plugin registry to app state
    app.state.plugins = plugins

    # Federated slash registry (F2 transport): this hub's signed slash log + ingested peers.
    # DB-backed so the authored log (and its monotonic seq) survives restarts.
    from aimarket_hub.slash_sync import SlashRegistry

    slash_registry = SlashRegistry(config.hub_url, db=db)
    app.state.slash_registry = slash_registry

    from aimarket_hub.oracle_quorum import RulingQuorum
    from aimarket_hub.reputation_oracle import ReputationOracle

    reputation_oracle = ReputationOracle(
        signer=signer,
        hub_url=config.hub_url,
        slash_registry=slash_registry,
        quorum=RulingQuorum.from_env(),
    )
    app.state.reputation_oracle = reputation_oracle

    from aimarket_hub.supply_security import SupplySecurity, TrustOracleUnavailable

    supply_security = SupplySecurity(
        db, config, signer=signer, slash_registry=slash_registry,
    )
    app.state.supply_security = supply_security
    from aimarket_hub.supply_chain_admission import SupplyChainAdmission

    supply_chain_admission = SupplyChainAdmission(db)
    app.state.supply_chain_admission = supply_chain_admission
    # Verify-first: repeat Metis verdict failures escalate to a calibrated slash.
    verify_svc.attach_supply_security(supply_security)
    app.state.verify_svc = verify_svc  # exposed so the escalation wiring is testable

    router = APIRouter(prefix="/ai-market/v2", tags=["hub-v2"])
    wellknown_router = APIRouter(tags=["wellknown"])
    # Names a route reads at request time but create_app binds further down (the
    # federation helpers, the pipeline provider). Route groups are registered in route
    # order, so these reach them through here instead of as register() arguments.
    late = SimpleNamespace()

    # Discovery: /.well-known/*, bazaar resources, protocol schemas.
    _wellknown_routes.register(
        _AGENT_TOKENS=_AGENT_TOKENS,
        config=config,
        db=db,
        hub_deposits=hub_deposits,
        late=late,
        plugins=plugins,
        signer=signer,
        wellknown_router=wellknown_router,
    )

    # Catalogue: signed manifest, prices, search.
    catalog_scope = _catalog_routes.register(
        _pays_a_third_party=_pays_a_third_party,
        config=config,
        db=db,
        router=router,
        signer=signer,
        supply_security=supply_security,
    )
    search = catalog_scope.search

    # Supply: stake policy, staking, registration, self-bonds.
    _supply_routes.register(
        _NS_AGENT=_NS_AGENT,
        _credits_publisher=_credits_publisher,
        _require_admin=_require_admin,
        _require_stake_subject=_require_stake_subject,
        _require_subject_credential=_require_subject_credential,
        config=config,
        db=db,
        hub_credits=hub_credits,
        hub_mandates=hub_mandates,
        router=router,
        supply_chain_admission=supply_chain_admission,
        supply_security=supply_security,
    )

    # Trial quota, verification and appeals, receipts, lottery entitlement.
    _verification_routes.register(
        _ADMIN_TOKEN=_ADMIN_TOKEN,
        _AGENT_TOKENS=_AGENT_TOKENS,
        _NS_AGENT=_NS_AGENT,
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _check_invoke_rate=_check_invoke_rate,
        _require_subject_credential=_require_subject_credential,
        config=config,
        router=router,
        signer=signer,
        verify_svc=verify_svc,
    )

    # Mandates and subcontract job trees.
    mandates_scope = _mandates_routes.register(
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _check_invoke_rate=_check_invoke_rate,
        _is_admin_bearer=_is_admin_bearer,
        hub_credits=hub_credits,
        hub_jobs=hub_jobs,
        hub_mandates=hub_mandates,
        router=router,
    )
    _api_key_account = mandates_scope._api_key_account

    # POST /invoke.
    _invoke_routes.register(
        TrustOracleUnavailable=TrustOracleUnavailable,
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _bind_hub_fee_payee=_bind_hub_fee_payee,
        _bind_listing_payee=_bind_listing_payee,
        _check_invoke_rate=_check_invoke_rate,
        _credit_hearth_owner=_credit_hearth_owner,
        _ecosystem_policy=_ecosystem_policy,
        _mcp_caller=_mcp_caller,
        _pay_publisher_share=_pay_publisher_share,
        _payment_hint=_payment_hint,
        _payment_ways=_payment_ways,
        _pays_a_third_party=_pays_a_third_party,
        _release_market_payment=_release_market_payment,
        _resolve_invoke_consumer_hub=_resolve_invoke_consumer_hub,
        _settle_market_payment=_settle_market_payment,
        _unbind_payee=_unbind_payee,
        builtin_safety=builtin_safety,
        config=config,
        db=db,
        hub_credits=hub_credits,
        hub_jobs=hub_jobs,
        hub_mandates=hub_mandates,
        late=late,
        plugins=plugins,
        router=router,
        signer=signer,
        supply_security=supply_security,
        verify_svc=verify_svc,
    )

    # Federation: knock, peers, assay, crawl, admission.
    federation_scope = _federation_routes.register(
        _is_admin_bearer=_is_admin_bearer,
        _require_admin=_require_admin,
        config=config,
        db=db,
        late=late,
        router=router,
        signer=signer,
        slash_registry=slash_registry,
        trust_scorer=trust_scorer,
    )
    _publishable_pending_peers = federation_scope._publishable_pending_peers

    # Reputation: slashes, events, deliveries, disputes.
    _reputation_routes.register(
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _require_admin=_require_admin,
        config=config,
        db=db,
        reputation_oracle=reputation_oracle,
        router=router,
        signer=signer,
        slash_registry=slash_registry,
        supply_security=supply_security,
        trust_scorer=trust_scorer,
    )

    # Health, credit accounts, top-ups, payer wallets, deposits.
    accounts_scope = _accounts_routes.register(
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _is_admin_bearer=_is_admin_bearer,
        _require_admin=_require_admin,
        _signup_allowed=_signup_allowed,
        config=config,
        db=db,
        hub_credits=hub_credits,
        hub_deposits=hub_deposits,
        hub_settle=hub_settle,
        hub_topups=hub_topups,
        router=router,
        signer=signer,
    )
    _deposit_watcher = accounts_scope._deposit_watcher

    # Live stats, permission violations, supply audits.
    _market_stats_routes.register(
        _NS_AGENT=_NS_AGENT,
        _NS_PUBLISHER=_NS_PUBLISHER,
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _ecosystem_policy=_ecosystem_policy,
        _require_subject_credential=_require_subject_credential,
        config=config,
        db=db,
        hub_credits=hub_credits,
        hub_x402=hub_x402,
        router=router,
        supply_chain_admission=supply_chain_admission,
        supply_security=supply_security,
    )

    # ACEX capital: pricing, IPO, audits.
    capital_scope = _capital_routes.register(
        _require_admin=_require_admin,
        db=db,
        router=router,
    )
    capital_alias = capital_scope.capital_alias

    # Payment channels and the escrow bridge.
    _channels_routes.register(
        _INVOKE_RATE_WORKERS=_INVOKE_RATE_WORKERS,
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _require_admin=_require_admin,
        config=config,
        router=router,
    )

    # Plugins.
    _plugins_routes.register(
        config=config,
        plugins=plugins,
        router=router,
    )

    # Agent doors: MCP and A2A.
    _agents_routes.register(
        _api_key_account=_api_key_account,
        _check_invoke_rate=_check_invoke_rate,
        _mcp_caller=_mcp_caller,
        app=app,
        config=config,
        db=db,
        hub_invoices=hub_invoices,
        hub_mandates=hub_mandates,
        search=search,
        wellknown_router=wellknown_router,
    )
    app.include_router(wellknown_router)
    app.include_router(router)
    app.include_router(capital_alias)

    # Pages, studio, seller operations, pipeline provider.
    _pages_routes.register(
        _TRUSTED_PROXIES=_TRUSTED_PROXIES,
        _check_invoke_rate=_check_invoke_rate,
        _ecosystem_policy=_ecosystem_policy,
        _mcp_caller=_mcp_caller,
        _publishable_pending_peers=_publishable_pending_peers,
        app=app,
        config=config,
        db=db,
        hub_jobs=hub_jobs,
        late=late,
        signer=signer,
    )

    return app
