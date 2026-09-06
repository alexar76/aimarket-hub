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
from urllib.parse import urlparse
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
    AuditClaimRequest,
    AuditSyncRequest,
    ChannelCloseRequest,
    ChannelOpenRequest,
    PermissionViolationRequest,
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
    release_hold,
)
from aimarket_hub import credits
from aimarket_hub import chain_net
from aimarket_hub import realm
from aimarket_hub import theme
from aimarket_hub import x402
from aimarket_hub import verified_settlement
from aimarket_hub.access_policy import capability_access_mode, capability_is_publicly_offerable
from aimarket_hub.verified_settlement import VerifiedSettlementService

# Reserved consumer_hub for operator/admin smoke tests. Public sandbox visitors
# and paying channels must NOT use this — otherwise /stats/live collapses all
# real traffic into "operator_self" and hides external demand.
OPERATOR_SELF_CONSUMER = "operator_self"

from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.demo_seeder import seed_capabilities
from aimarket_hub.federation_transport import peer_is_aimarket_hub
from aimarket_hub.fulfillment import capability_is_fulfillable
from aimarket_hub.models import InvocationStat, Peer
from aimarket_hub.plugin import PluginRegistry
from aimarket_hub.safety_gate import default_safety_gate
from aimarket_hub.sandbox_trials import (
    consume_sandbox_trial,
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
from aimarket_hub.signing import Signer
from aimarket_hub.trust import TrustScorer

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


# The rule itself lives in `federation_transport` because the admission assay needs the
# same answer: it kept its own copy, read `mcp_endpoint` verbatim, and could never
# sandbox-probe a hub peer. Re-exported under the old name so this module's callers and
# tests are unaffected.
_peer_is_aimarket_hub = peer_is_aimarket_hub


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


# ── Router factory ─────────────────────────────────────────────


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
    db = db or HubDatabase(config.db_path)
    signer = signer or Signer(config.signing_key_path)
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
    hub_x402 = x402.PaymentStore(db._conn)
    # Which realm is this? Logged, never published: the bubble must be indistinguishable
    # from inside, so the only reader told about it is the operator, who is outside.
    if realm.is_uni():
        logger.warning(
            "[realm=uni] SEALED BUBBLE — chain id %s, simulated economy. Every amount this "
            "hub reports is virtual and must never be quoted as revenue.",
            realm.uni_chain_id(),
        )
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
        crawl_task: asyncio.Task | None = None

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
                    return await crawler.crawl(clear_first=False)
                finally:
                    await crawler.close()

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
        # Pay-on-Verified: re-queue verifications left pending by a previous process
        # so a hub restart never strands an escrow hold.
        with contextlib.suppress(Exception):
            await verify_svc.reconcile()
        try:
            yield
        finally:
            if crawl_task is not None:
                crawl_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await crawl_task
            with contextlib.suppress(Exception):
                await verify_svc.shutdown()

    app = FastAPI(
        title=f"{config.hub_name} — AIMarket Hub v3",
        version=config.hub_version,
        description="Lean federation hub for AI capability discovery and routing. Plugins extend functionality.",
        lifespan=_lifespan,
    )
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
            "X-AIMarket-Affiliate",
            "X-AIMarket-Sandbox-Visitor", "X-Market-Signature", "X-AIMarket-Routing-Hub",
            "X-AIMarket-Routing-Fee", "X-AIMarket-Crawler", "X-Provider-Signature",
        ],
    )

    @app.middleware("http")
    async def _x402_middleware(request: Request, call_next):
        """Speak x402 on every 402 this hub emits.

        One insertion point instead of ten. The hub returns `402` from ten places inside
        `invoke`, each an inline dict; editing all ten would risk missing one, and a payment
        surface that is right nine times out of ten is not a payment surface. Here the price
        is read back out of the response the handler already built (`needed`), so a new 402
        added tomorrow is covered without anyone remembering to.

        Emits both dialects: the V2 `PAYMENT-REQUIRED` header (current protocol) and the V1
        `accepts` array merged into the existing body. Existing consumers read the same keys
        they always did.
        """
        response = await call_next(request)
        if getattr(response, "status_code", 0) != 402:
            return response

        from aimarket_hub import x402

        # Bail out BEFORE touching the stream. Everything below consumes body_iterator, and a
        # consumed iterator cannot be handed back — returning the drained response object was
        # a 402 with an empty body and a stale Content-Length, i.e. the hub's own payment
        # refusal destroyed by the code meant to enrich it.
        if not x402.enabled() or getattr(request.state, "x402_passthrough", False):
            return response

        raw_headers = list(getattr(response, "raw_headers", []) or [])
        body = b""
        if hasattr(response, "body_iterator"):
            body = b"".join([chunk async for chunk in response.body_iterator])
        else:
            body = getattr(response, "body", b"") or b""

        def _rebuild(new_body: bytes, extra: dict[str, str] | None = None) -> Response:
            """Reconstruct from the ORIGINAL raw headers so duplicates (Set-Cookie, Vary)
            survive; Content-Length is dropped because the body may have changed."""
            rebuilt = Response(content=new_body, status_code=402)
            rebuilt.raw_headers = [
                (k, v) for k, v in raw_headers if k.lower() != b"content-length"
            ]
            if not any(k.lower() == b"content-type" for k, _ in rebuilt.raw_headers):
                rebuilt.raw_headers.append((b"content-type", b"application/json"))
            rebuilt.raw_headers.append((b"content-length", str(len(new_body)).encode()))
            for key, value in (extra or {}).items():
                rebuilt.raw_headers.append((key.encode(), value.encode()))
            return rebuilt

        try:

            payload: dict[str, Any] = {}
            with contextlib.suppress(Exception):
                payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                payload = {}

            price = payload.get("needed")
            if price is None:
                price = payload.get("price_usd", 0)
            # Behind nginx `request.url` is the scheme uvicorn saw, which is http — so the
            # advertised resource was `http://modelmarket.dev/…` on a hub that only serves
            # https. A payer that checks what they are paying for against the URL they
            # called sees a mismatch, and an http URL in a payment offer is the kind of
            # detail a careful client refuses on. Prefer the hub's own canonical base.
            resource_url = str(request.url)
            if config.hub_url:
                base = config.hub_url.rstrip("/")
                path = request.url.path or ""
                query = f"?{request.url.query}" if request.url.query else ""
                resource_url = f"{base}{path}{query}"
            description = str(payload.get("detail") or payload.get("error") or "")[:200]

            v2 = x402.payment_required_v2(price or 0, resource_url, description)
            if v2 is None:
                return _rebuild(body)

            payload.update(x402.v1_body_fields(price or 0, resource_url, description))
            return _rebuild(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                {
                    "PAYMENT-REQUIRED": x402.encode_header(v2),
                    # Browsers cannot read a non-safelisted response header unless it is
                    # exposed; without this an in-browser x402 client sees a 402 with no
                    # payment terms at all.
                    "Access-Control-Expose-Headers": "PAYMENT-REQUIRED",
                },
            )
        except Exception as exc:  # pragma: no cover - a 402 must survive this failing
            logger.debug("x402 enrichment skipped: %s", exc)
            return _rebuild(body)

    @app.middleware("http")
    async def _invoke_metrics_middleware(request: Request, call_next):
        """Record Prometheus series for /ai-market/v2/invoke (local + federated)."""
        path = request.url.path.rstrip("/")
        if not path.endswith("/invoke"):
            return await call_next(request)
        t0 = time.perf_counter()
        response = await call_next(request)
        cap = getattr(request.state, "aimarket_capability_id", None) or "unknown"
        code = getattr(response, "status_code", 500)
        if code == 402:
            result = "payment_required"
        elif code == 429:
            result = "rate_limited"
        elif code == 403:
            result = "safety_blocked"
        elif 200 <= code < 300:
            result = "ok"
        elif code >= 500:
            result = "error"
        else:
            result = "client_error"
        record_invoke(cap, result, duration_s=time.perf_counter() - t0)
        return response

    def _refresh_federation_gauges() -> None:
        """Recompute federation peer health at scrape time.

        Read here rather than at the end of a crawl: a crawl that stops running is
        exactly one of the conditions worth alerting on, and gauges last written by
        the last successful cycle would report the federation as fine.
        """
        rejected = 0
        newest_per_peer: list[float] = []
        now = time.time()
        for peer in db.list_peers():
            if str(getattr(peer, "status", "") or "") == "key_mismatch":
                rejected += 1
            stamp = str(getattr(peer, "last_crawl", "") or "").strip()
            if not stamp:
                continue
            try:
                parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            newest_per_peer.append(now - parsed.timestamp())
        set_federation_peer_health(rejected, max(newest_per_peer) if newest_per_peer else None)

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        try:
            _refresh_federation_gauges()
        except Exception as exc:  # noqa: BLE001 — a scrape must never 500
            logger.warning("Could not refresh federation gauges: %s", exc)
        body, ctype = metrics_payload()
        return Response(content=body, media_type=ctype)

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

    def _pay_publisher_share(cap: Any, price_usd: float, receipt_id: str) -> None:
        """Credit the capability's publisher their share of a completed sale.

        Silent when there is nobody to pay: a capability the operator published themselves
        has no separate publisher account, and a publisher identified by a wallet address
        rather than a credit account cannot be paid on this rail. Never raises — the buyer
        has already been charged and the work already delivered, so a payout problem is an
        operator alarm, not a failed invoke.
        """
        if price_usd <= 0 or config.publisher_share_bps <= 0:
            return
        publisher_id = str(getattr(cap, "publisher_id", "") or "").strip()
        if not publisher_id or not credits.enabled():
            return
        if hub_credits.account(publisher_id) is None:
            return
        share = round(price_usd * config.publisher_share_bps / 10000, 6)
        if share <= 0:
            return
        try:
            result = hub_credits.pay_publisher(
                publisher_id, share, receipt_id=receipt_id, note="capability sale",
            )
        except Exception as exc:  # noqa: BLE001 - never fail a delivered call on a payout
            logger.error("payout: crediting %s failed: %s", publisher_id, exc)
            return
        if result.get("error"):
            logger.error("payout: crediting %s failed: %s", publisher_id, result["error"])
        else:
            logger.info(
                "payout: %s earned $%.6f (%d bps of $%.6f)",
                publisher_id, share, config.publisher_share_bps, price_usd,
            )

    def _accept_x402(payload: dict, price_usd: float, capability_id: str) -> dict:
        """Verify and record an x402 authorization, or say why it was refused.

        Three refusals, all of them cheap and all of them before the work: terms that do not
        match what we advertised, a nonce already spent here, and a receivable book already
        at its ceiling. The last one is the one operators forget: a verified signature is a
        promise, not a payment, so an unbounded book is an unbounded bet on strangers'
        balances.
        """
        verified = x402.verify_payment(payload, price_usd=price_usd)
        if not verified.get("ok"):
            return {"error": verified.get("error") or "payment could not be verified"}
        if hub_x402.seen(verified["nonce"]):
            return {"error": "this authorization has already been used"}
        ceiling = x402.max_unsettled_usd()
        if ceiling and hub_x402.unsettled_usd() + float(verified["amount_usd"]) > ceiling:
            logger.warning(
                "x402: refusing payment from %s — unsettled book at $%.4f would exceed the "
                "$%.2f ceiling (AIMARKET_X402_MAX_UNSETTLED_USD)",
                verified["payer"], hub_x402.unsettled_usd(), ceiling,
            )
            return {"error": (
                "this hub is not accepting more unsettled x402 payments right now — "
                "use a credit account"
            )}
        hub_x402.record(verified, capability_id=capability_id)
        logger.info(
            "x402: accepted $%.6f from %s for %s (unsettled)",
            verified["amount_usd"], verified["payer"], capability_id,
        )
        return verified

    def _payment_hint(credits_on: bool, crypto_on: bool) -> str:
        """What a 402 should actually tell the payer.

        The old text named one header and stopped ("X-Payment-Channel required"), which on a
        hub with the chain switched off described a rail the caller could not reach: opening
        a channel needs a funded escrow the operator may never have deployed. A 402 that does
        not name a reachable way to pay is a dead end, so this reports the rails THIS hub has
        actually got switched on.
        """
        if credits_on and crypto_on:
            return (
                "payment required — send X-API-Key for a credit account "
                f"(POST {config.hub_url}/ai-market/v2/accounts) or X-Payment-Channel for an "
                "on-chain channel"
            )
        if credits_on:
            return (
                "payment required — send X-API-Key. Get a key with "
                f"POST {config.hub_url}/ai-market/v2/accounts, then top it up with the operator"
            )
        return "X-Payment-Channel required for paid capability invoke"

    def _payment_ways(credits_on: bool, crypto_on: bool) -> dict[str, Any]:
        """Machine-readable twin of the hint, so an agent can act on a 402 without parsing prose."""
        ways: list[dict[str, Any]] = []
        if credits_on:
            ways.append({
                "rail": "credits",
                "header": "X-API-Key",
                "signup": f"{config.hub_url}/ai-market/v2/accounts",
                "open_signup": credits.signup_open(),
            })
        if crypto_on:
            ways.append({
                "rail": "channel",
                "header": "X-Payment-Channel",
                "open": f"{config.hub_url}/ai-market/v2/channel/open",
            })
        return {"payment_ways": ways} if ways else {}

    def _resolve_invoke_consumer_hub(
        *,
        x_sandbox_visitor: str | None = None,
        x_payment_channel: str | None = None,
        authorization: str = "",
    ) -> str:
        """Who to credit on InvocationStat.consumer_hub for /stats/live.

        - Admin Bearer smoke → ``operator_self`` (does not count as external)
        - Paying channel → ``channel:<id>`` (external)
        - Sandbox visitor → ``sandbox:<id>`` (external)
        - Else → ``anonymous`` (external)

        Keeps the live feed honest: a Cursor/agent/browser trial is demand, not
        the operator testing their own hub.
        """
        if _is_admin_bearer(authorization):
            return OPERATOR_SELF_CONSUMER

        def _label(prefix: str, raw: str) -> str:
            cleaned = "".join(c for c in raw.strip() if c in _SAFE_ID_CHARS)[:64]
            return f"{prefix}:{cleaned}" if cleaned else prefix

        if x_payment_channel and x_payment_channel.strip():
            return _label("channel", x_payment_channel)
        if x_sandbox_visitor and x_sandbox_visitor.strip():
            return _label("sandbox", x_sandbox_visitor)
        return "anonymous"

    _PUBLISH_TOKEN = os.environ.get("AIMARKET_PUBLISH_TOKEN", "").strip()

    def _require_publish(authorization: str) -> None:
        """Developer catalog publish — token required in production."""
        import hmac
        if _PUBLISH_TOKEN:
            if not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing Bearer token")
            token = authorization[7:]
            if not hmac.compare_digest(token, _PUBLISH_TOKEN):
                raise HTTPException(status_code=403, detail="Invalid publish token")
        elif _is_production_mode():
            raise HTTPException(
                status_code=503,
                detail="Publish disabled: set AIMARKET_PUBLISH_TOKEN",
            )

    # Per-SUBJECT credentials (finding #12 residual, + its own two residuals).
    #
    # /supply/stake, /supply/register and /self-bond/register all act on a CALLER-NAMED
    # SUBJECT, and the shared publish token — held by every publisher on the hub —
    # cannot tell one caller from another. Under it, any holder could credit stake to an
    # arbitrary publisher, register a slashable spend ceiling against a rival agent, or
    # publish capabilities AS another publisher (which stamps the victim's publisher_id
    # onto the capability row, charges the victim's publish-rate budget, reads the
    # victim's stake as the capability's collateral, and points a later slash for that
    # capability at the victim's money).
    #
    # These routes cannot simply become admin-only: stake → register IS the documented
    # self-service community publish flow, and an operator-gated stake step breaks every
    # community publisher. So the subject must authenticate AS ITSELF.
    #
    # TWO NAMESPACES, not one. The registry used to be publisher-only, and
    # /self-bond/register authenticated an AGENT id against it: once an operator
    # configured a single publisher entry, that publisher registry became authoritative
    # for agent identities too, so an agent that is not a publisher was locked out —
    # and an agent id that happened to collide with a publisher id was authorised by an
    # unrelated namespace. They are now separate registries:
    #     AIMARKET_PUBLISHER_TOKENS="pub-a:secretA,..."     (publisher ids)
    #     AIMARKET_AGENT_TOKENS="agent-a:secretX,..."       (agent ids)
    # Configuring one says nothing about the other.
    #
    # WHAT OPERATORS MUST CONFIGURE, exactly:
    #   * community PUBLISHERS (stake + register): issue each publisher its own secret
    #     and list it in AIMARKET_PUBLISHER_TOKENS. The publisher then sends
    #     `Authorization: Bearer <its secret>` and may only touch its own publisher_id —
    #     the whole self-service flow (POST /supply/stake then POST /supply/register with
    #     the same publisher_id) keeps working for a publisher acting as ITSELF.
    #   * bonded AGENTS (self-bond): list each agent in AIMARKET_AGENT_TOKENS. An agent
    #     that also stakes uses the same secret on /supply/stake (the stake ledger is one
    #     account space — see _require_stake_subject).
    #   * or drive any of them with AIMARKET_ADMIN_TOKEN, accepted for any subject
    #     (operator-provisioned publishers/agents).
    # In production, with none of that configured, these routes refuse (503) with that
    # instruction instead of accepting the shared token — the point of the finding.
    _PUBLISHER_TOKENS = _parse_subject_tokens(
        os.environ.get("AIMARKET_PUBLISHER_TOKENS", ""), "AIMARKET_PUBLISHER_TOKENS"
    )
    _AGENT_TOKENS = _parse_subject_tokens(
        os.environ.get("AIMARKET_AGENT_TOKENS", ""), "AIMARKET_AGENT_TOKENS"
    )
    _NS_PUBLISHER = ("publisher", "AIMARKET_PUBLISHER_TOKENS", _PUBLISHER_TOKENS)
    _NS_AGENT = ("agent", "AIMARKET_AGENT_TOKENS", _AGENT_TOKENS)
    if _is_production_mode():
        if not _PUBLISHER_TOKENS:
            logger.warning(
                "AIMARKET_PUBLISHER_TOKENS not set — /supply/stake and /supply/register "
                "accept only AIMARKET_ADMIN_TOKEN in production (the shared publish "
                "token cannot prove which publisher is calling)"
            )
        if not _AGENT_TOKENS:
            logger.warning(
                "AIMARKET_AGENT_TOKENS not set — /self-bond/register accepts only "
                "AIMARKET_ADMIN_TOKEN in production (agent ids are a separate namespace "
                "from AIMARKET_PUBLISHER_TOKENS)"
            )

    def _require_subject_credential(
        authorization: str,
        subject_id: str,
        namespaces: tuple[tuple[str, str, dict[str, str]], ...] = (_NS_PUBLISHER,),
    ) -> str:
        """Authorize a mutation acting AS ``subject_id``. Returns the granting role.

        ``namespaces`` are the (role, env_name, registry) credential namespaces that may
        speak for this subject — one for a publisher-only or agent-only route, both where
        the subject space is genuinely shared (the stake ledger).

        Order matters: the admin token wins first (operators manage anyone), then the
        subject's own credential. A configured registry is authoritative FOR ITS OWN
        NAMESPACE — a subject that is not in it cannot fall back to the shared token,
        otherwise adding one credential would leave every other id in that namespace
        wide open — and says nothing about the other namespace.
        """
        import hmac

        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        # A missing token is only the caller's problem when SOME token could have worked.
        # Deciding that needs the registries, so the 401 is raised further down, once we
        # know a credential exists to present — answering 401 on a hub where nothing is
        # configured invites a retry loop no token can ever satisfy, and hides the fact
        # that the operator, not the caller, has to act (the route's pre-existing
        # contract for that state is 503).
        if token and _ADMIN_TOKEN and hmac.compare_digest(token, _ADMIN_TOKEN):
            return "admin"
        registered = [
            (role, env_name, registry[subject_id])
            for role, env_name, registry in namespaces
            if subject_id in registry
        ]
        if len({secret for _, _, secret in registered}) > 1:
            # The same id claimed by two namespaces with DIFFERENT secrets: the stake
            # ledger has exactly one account per id, so the registries disagree about who
            # owns that account and nothing here can pick a winner. Fail closed.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{subject_id} is registered with different credentials in "
                    + " and ".join(env for _, env, _ in registered)
                    + " — the stake ledger holds one account per id; reconcile the "
                    "registries (or use AIMARKET_ADMIN_TOKEN)"
                ),
            )
        if registered:
            # A credential for this subject EXISTS, so "no token" is genuinely the
            # caller's omission and 401 is the honest, actionable answer.
            if not token:
                raise HTTPException(status_code=401, detail="Missing Bearer token")
            for role, _env_name, secret in registered:
                if hmac.compare_digest(token, secret):
                    return role
            raise HTTPException(
                status_code=403,
                detail=f"token does not authenticate {subject_id}",
            )
        configured = [env_name for _, env_name, registry in namespaces if registry]
        if configured:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"no credential is registered for {subject_id} — ask the operator to "
                    f"add it to {' or '.join(configured)}"
                ),
            )
        if _is_production_mode():
            raise HTTPException(
                status_code=503,
                detail=(
                    "disabled: the shared publish token cannot prove which caller is "
                    "acting as " + subject_id + ". Set "
                    + " / ".join(f"{env}='<id>:<secret>,...'" for _, env, _ in namespaces)
                    + " or call with AIMARKET_ADMIN_TOKEN."
                ),
            )
        # Dev/relaxed: keep the documented self-service flow working unchanged.
        _require_publish(authorization)
        logger.warning(
            "mutation acting as %s authorized by the SHARED publish token "
            "(non-production only) — set %s before going live",
            subject_id, "/".join(env for _, env, _ in namespaces),
        )
        return "shared-publish-token"

    def _credits_publisher(x_api_key: str | None) -> str:
        """A credit account speaking as a publisher.

        Publisher identity used to exist only as `AIMARKET_PUBLISHER_TOKENS` — a
        comma-separated env map parsed once at app construction — so onboarding one
        community publisher meant editing .env and restarting the hub. That is not a
        signup flow, it is a maintenance window per customer, and it is why the reference
        deployment has never had a third-party publisher.

        A credit account is already an authenticated, self-serve, per-caller identity, so
        it can be one here too. The publisher id IS the account id: it cannot collide with
        an operator-configured name (those are chosen by the operator, these are minted
        `acct_…`), and the credential is the account's own key rather than a shared token.
        """
        if not x_api_key or not credits.enabled():
            return ""
        ledger = hub_credits
        return ledger.resolve(x_api_key) if ledger is not None else ""

    def _require_stake_subject(authorization: str, subject_id: str) -> str:
        """Authorize a stake-ledger mutation for ``subject_id``.

        ``supply_stakes`` is ONE account space keyed by id: publishers stake there to
        unlock community publish and bonded agents stake there to back a self-bond, so a
        credential from either namespace may speak for a stake subject. A collision (the
        same id in both registries with different secrets) is refused rather than
        resolved — see _require_subject_credential.
        """
        return _require_subject_credential(
            authorization, subject_id, (_NS_PUBLISHER, _NS_AGENT)
        )

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

    # Per-consumer /invoke rate limit (sliding window). Without it an anonymous
    # caller could hammer a provider — forcing failures to grief its stake and
    # burning its upstream capacity. Mirrors the channels ledger _check_rate.
    #
    # LIMITATION — this window lives in PROCESS memory, not in the ledger DB:
    #   * it resets to empty on every restart/redeploy, so a caller can reset its
    #     own bucket by waiting out a deploy (bounded, acceptable);
    #   * it is NOT shared between processes. With N uvicorn workers behind one
    #     socket, N independent windows exist, so the effective ceiling would be
    #     N x AIMARKET_INVOKE_RATE_PER_MIN — i.e. a multi-worker deploy would be
    #     silently N times more permissive than the operator configured.
    # The second failure mode is closed WHENEVER THE WORKER COUNT IS DECLARED: the
    # configured budget is DIVIDED across it, so the aggregate ceiling matches
    # AIMARKET_INVOKE_RATE_PER_MIN no matter how many workers run. Declare the count
    # with AIMARKET_WORKERS (or the WEB_CONCURRENCY / UVICORN_WORKERS conventions used
    # by the container entrypoints).
    #   RESIDUAL, stated precisely because the fix does NOT cover it: an operator who
    #   scales to N workers WITHOUT declaring the count (or with a malformed value)
    #   still gets the full budget in each worker, i.e. N x the configured aggregate.
    #   That direction is permissive, not restrictive. Nothing in-process can observe
    #   its sibling workers, so the only real fix is a shared counter — which belongs
    #   in the ledger DB alongside channels._check_rate (cross-package note). What we
    #   do here is refuse to be silent about it: production logs a warning at startup.
    _invoke_rate: dict[str, list[float]] = {}
    _INVOKE_RATE_WINDOW_S = 60.0
    # Hard cap on tracked keys: the dict is keyed on client IP, so an attacker rotating
    # source addresses must not be able to grow it without bound (memory DoS). Expired
    # buckets go first; if every bucket is still live we drop the least-recently-active
    # ones, which are the closest to expiring anyway.
    _INVOKE_RATE_MAX_KEYS = 20_000

    def _declared_workers() -> int:
        for var in ("AIMARKET_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS"):
            raw = os.environ.get(var, "").strip()
            if not raw:
                continue
            try:
                return max(1, int(raw))
            except ValueError:
                logger.warning(
                    "%s=%r is not an integer — assuming 1 worker for the invoke "
                    "rate limiter (per-worker budget stays at the configured value)",
                    var, raw,
                )
                return 1
        return 1

    _INVOKE_RATE_CONFIGURED = int(os.environ.get("AIMARKET_INVOKE_RATE_PER_MIN", "60"))
    _INVOKE_RATE_WORKERS = _declared_workers()
    if _INVOKE_RATE_CONFIGURED <= 0:
        _INVOKE_RATE_MAX = _INVOKE_RATE_CONFIGURED  # <= 0 keeps the documented "disabled"
    else:
        # Integer division floors, so round up to keep at least one request per
        # worker per window (a 1-per-minute budget across 4 workers must not become 0).
        _INVOKE_RATE_MAX = max(1, -(-_INVOKE_RATE_CONFIGURED // _INVOKE_RATE_WORKERS))
    if _INVOKE_RATE_WORKERS > 1:
        logger.info(
            "invoke rate limit: %d/min configured across %d declared worker(s) "
            "→ %d/min per worker (in-memory window)",
            _INVOKE_RATE_CONFIGURED, _INVOKE_RATE_WORKERS, _INVOKE_RATE_MAX,
        )
    elif _INVOKE_RATE_MAX > 0 and _is_production_mode():
        # The undeclared-worker residual above is invisible from inside the process,
        # so make it loud instead of pretending it does not exist.
        logger.warning(
            "invoke rate limit: no worker count declared (AIMARKET_WORKERS / "
            "WEB_CONCURRENCY / UVICORN_WORKERS) — each worker enforces the full "
            "%d/min, so an N-worker deploy admits N x that in aggregate",
            _INVOKE_RATE_CONFIGURED,
        )

    def _check_invoke_rate(consumer: str) -> bool:
        """Return True if within the per-consumer invoke rate limit, else False."""
        if _INVOKE_RATE_MAX <= 0:
            return True
        now = time.time()
        window = now - _INVOKE_RATE_WINDOW_S
        bucket = [t for t in _invoke_rate.get(consumer, []) if t > window]
        if len(bucket) >= _INVOKE_RATE_MAX:
            _invoke_rate[consumer] = bucket
            return False
        bucket.append(now)
        _invoke_rate[consumer] = bucket
        if len(_invoke_rate) > _INVOKE_RATE_MAX_KEYS:
            _trim_rate_buckets(
                _invoke_rate, window, _INVOKE_RATE_MAX_KEYS, keep=consumer,
            )
        return True

    _signup_rate: dict[str, list[float]] = {}

    def _signup_subnet(client_ip: str) -> str:
        """The /24 of an IPv4 address, the /48 of an IPv6 one, or "" if unparsable."""
        try:
            parsed = ipaddress.ip_address(str(client_ip or "").strip())
        except ValueError:
            return ""
        network = ipaddress.ip_network(f"{parsed}/{24 if parsed.version == 4 else 48}", strict=False)
        return f"net:{network}"

    def _signup_allowed(client_ip: str) -> bool:
        """Cap self-serve account creation per address.

        An open signup that mints rows for free is a write amplifier, and the free grant
        makes each row worth a few cents of invoke — so the door is open but narrow. The
        operator can widen or shut it with AIMARKET_CREDITS_SIGNUPS_PER_HOUR / _OPEN_SIGNUP.
        """
        try:
            limit = int(os.getenv("AIMARKET_CREDITS_SIGNUPS_PER_HOUR", "5"))
        except (TypeError, ValueError):
            limit = 5
        if limit <= 0:
            return False
        now = time.time()
        window = now - 3600.0
        # One address is a weak identity: a residential-proxy pool hands out a
        # fresh one per request. The /24 (or /48) it came from is harder to
        # multiply, so the subnet gets a bucket too, wider than the single host.
        keys = [client_ip, _signup_subnet(client_ip)]
        limits = [limit, max(limit, limit * 4)]
        fresh: dict[str, list[float]] = {}
        for key, ceiling in zip(keys, limits):
            if not key:
                continue
            bucket = [t for t in _signup_rate.get(key, []) if t > window]
            if len(bucket) >= ceiling:
                _signup_rate[key] = bucket
                return False
            fresh[key] = bucket
        for key, bucket in fresh.items():
            bucket.append(now)
            _signup_rate[key] = bucket
        if len(_signup_rate) > 2048:
            _trim_rate_buckets(_signup_rate, window, 2048, keep=client_ip)
        return True

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

    # ── .well-known ────────────────────────────────────────────

    def _note_inbound_federation(crawler_hub: str, user_agent: str) -> None:
        """Record and quarantine as PENDING a public hub that crawled us.

        Federation was one-way blind: a peer could index this hub, route buyers to
        it and appear in its own catalogue, while this hub had no idea the peer
        existed. The crawler has always sent ``X-AIMarket-Crawler: <its hub url>``;
        nothing ever read it. Now we do.

        Runs off the request path (see the caller) because the SSRF-safe URL check
        resolves DNS, which must never be on the hot path of a document every peer
        in the network fetches on a timer. Fails silently by design: discovery is a
        courtesy, and no failure of it may affect serving ``.well-known``.
        """
        try:
            from aimarket_hub.crawler import _url_is_safe

            url = (crawler_hub or "").strip()
            if not url or len(url) > 300 or any(c in url for c in "\r\n\t"):
                return
            if not url.startswith(("http://", "https://")):
                return
            url = url.rstrip("/")

            # Recording is syntactic-only and capped. Noting that a hub reads us costs
            # nothing and is useful even when its DNS is broken or its host is down;
            # gating the note on a name resolving would have hidden exactly the peers
            # an operator most wants to hear about.
            db.record_inbound_federation(url, user_agent)

            if db.get_peer(url) is not None:
                return
            # Admission is different: a pending peer is a URL the crawler will later
            # fetch, so it passes the same DNS-resolving SSRF check as a seed. Done
            # here, once per never-before-seen hub, and never on the response path.
            if not _url_is_safe(url):
                logger.debug("Inbound crawler %s not admitted — URL failed safety check", url[:60])
                return
            outcome = db.announce_peer(
                Peer(
                    url=url,
                    name=url,
                    well_known_url=f"{url}/.well-known/ai-market.json",
                    depth=1,
                    discoverer="inbound-crawl",
                ),
                max_pending=_pending_peer_cap(),
            )
            if outcome == "added":
                logger.info(
                    "Open federation: %s crawled us and was recorded as PENDING. "
                    "It is visible to the operator and indexed by nothing until approved.",
                    url,
                )
        except Exception as exc:  # pragma: no cover - never break .well-known
            logger.debug("inbound federation note failed: %s", exc)

    # Every protocol schema declares an absolute `$id` under /schemas/. Until now nothing
    # served that path, so a validator that dereferenced `$id` — which is what `$id` is for —
    # got a 404, and `$ref` between schemas could not resolve at all. Serving them here makes
    # the identifier true. The `$id` values stay canonical (modelmarket.dev) on every mirror,
    # which is correct JSON Schema semantics: identity is a name, not a location.
    _SCHEMA_FILES = frozenset({
        "well-known.json", "manifest.json", "receipt.json",
        "federation-announce.json", "provenance-receipt.json",
    })

    @wellknown_router.get("/discovery/resources")
    async def bazaar_discovery(type: str = "http", limit: int = 20, offset: int = 0):
        """A Bazaar-compatible discovery index of what this hub sells.

        The x402 Bazaar is not a registry anyone registers with — it is a per-facilitator
        index, and its specification says in as many words that any facilitator may run its
        own. Serving this envelope means every official x402 SDK can enumerate this hub's
        catalogue with no code changes: point a facilitator client at this host and call
        `listResources`.

        That matters more than one endpoint usually does. Each existing Bazaar catalogues
        only what its own facilitator settles, and they do not federate — Coinbase's index
        and PayAI's share no entries at all. An index that can be crawled by others is the
        one thing that turns a set of disjoint catalogues back into a market, which is what
        this protocol is for.

        Public and unauthenticated by design. SDK clients attach auth headers on their way
        in; a public index ignores them rather than rejecting the request.
        """
        limit = max(1, min(int(limit or 20), 100))
        offset = max(0, int(offset or 0))
        if (type or "http").lower() != "http":
            # Only HTTP-invocable capabilities are advertised. Claiming an "mcp" type here
            # would promise a tool surface each capability does not individually expose.
            return {"x402Version": 2, "items": [],
                    "pagination": {"limit": limit, "offset": offset, "total": 0}}

        from aimarket_hub import x402

        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        priced = [c for c in db.list_capabilities(limit=1000)
                  if float(getattr(c, "price_per_call_usd", 0) or 0) > 0]
        window = priced[offset:offset + limit]
        items = [i for i in (x402.bazaar_item(c, config.hub_url, stamp) for c in window) if i]
        return {
            "x402Version": 2,
            "items": items,
            "pagination": {"limit": limit, "offset": offset, "total": len(priced)},
        }

    @wellknown_router.get("/schemas/{filename}")
    async def protocol_schema(filename: str):
        # Allow-list, not sanitisation: this handler reads from disk by a name taken from the
        # URL, and a deny-list of traversal tricks is a game you eventually lose.
        if filename not in _SCHEMA_FILES:
            raise HTTPException(status_code=404, detail="unknown schema")
        from aimarket_hub.validator import _SCHEMA_DIR

        path = _SCHEMA_DIR / filename
        if not path.is_file():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Schemas are not bundled with this deployment. Set AIMARKET_SCHEMA_DIR "
                    "to a checkout of aimarket-protocol/schemas."
                ),
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(status_code=500, detail="schema unreadable")
        return JSONResponse(
            payload,
            media_type="application/schema+json",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @wellknown_router.get("/schemas")
    async def protocol_schema_index():
        return {
            "schemas": sorted(_SCHEMA_FILES),
            "base": f"{config.hub_url.rstrip('/')}/schemas/",
            "canonical_note": (
                "Each document's own $id is its canonical identifier and does not change "
                "when mirrored by another hub."
            ),
        }

    @wellknown_router.api_route(
        "/.well-known/ai-market.json", methods=["GET", "HEAD"]
    )
    async def well_known(
        x_aimarket_crawler: str = Header(default=""),
        user_agent: str = Header(default=""),
    ):
        if x_aimarket_crawler:
            # Off the response path: a peer's discovery must not cost us latency.
            #
            # Deliberately NOT asyncio's default executor. The work below calls
            # getaddrinfo, which blocks, and the default pool is the same one asyncio uses
            # for its own DNS — an unauthenticated header could therefore starve the event
            # loop's name resolution. A single dedicated worker bounds the damage to this
            # feature, and a full queue drops the note rather than queueing unboundedly.
            with contextlib.suppress(Exception):
                pool = _inbound_pool()
                if pool._work_queue.qsize() < 32:  # noqa: SLF001 - bounded backlog
                    pool.submit(
                        _note_inbound_federation, x_aimarket_crawler, user_agent
                    )
        # HEAD must succeed (Allow: GET alone → 405). Same payload as GET;
        # Starlette/FastAPI omit the body for HEAD automatically.
        peers = []
        for p in db.list_peers():
            peers.append({
                "url": p.url,
                "name": p.name,
                "capabilities_count": p.capabilities_count,
                "last_crawl": p.last_crawl,
                "trust_score": p.trust_score,
                "categories": p.categories,
            })

        # The observation layer is deliberately separate from `peers`.  It makes the
        # network eventually aware of every public Hub seen by a member, while preserving
        # the hard line between "an address exists" and "this Hub may sell/rout here".
        # The whole well-known document is signed below, so a crawler can attribute the
        # observation to this Hub; it must still quarantine the observed address.
        observed_hubs = []
        for p in _publishable_pending_peers(db)[: config.federation_gossip_max_observed]:
            observed_hubs.append({
                "url": p.url,
                "name": p.name or p.url,
                "first_seen": p.first_seen,
                "observed_by": config.hub_url,
                "depth": max(1, int(p.depth or 1)),
                "status": "observed",
            })

        erc8004 = None
        with contextlib.suppress(Exception):
            from aimarket_hub import x402 as _x402

            erc8004 = _x402.erc8004_declaration(config.hub_url)

        public_caps = [
            c for c in db.list_capabilities(limit=1000)
            if capability_is_fulfillable(c) and capability_is_publicly_offerable(c)
        ]
        public_local = [c for c in public_caps if (c.source_hub or "local") == "local"]
        # A Hub may have its own provider ecosystem.  Keep that topology separate from
        # federation `peers`: a locally published KOVA/AEGIS service is our child node, not
        # an independently trusted Hub.  The complete well-known document is signed below,
        # so another Monitor can draw this declaration without granting it routing trust.
        ecosystem_labels: dict[str, str] = {}
        for raw_label in os.environ.get("AIMARKET_ECOSYSTEM_LABELS", "").split(","):
            publisher, separator, display = raw_label.partition(":")
            if separator and publisher.strip() and display.strip():
                ecosystem_labels[publisher.strip()] = display.strip()[:80]
        ecosystem_by_publisher: dict[str, dict[str, Any]] = {}
        for c in public_local:
            publisher_id = str(c.publisher_id or "").strip()
            if not publisher_id:
                continue
            item = ecosystem_by_publisher.setdefault(publisher_id, {
                "id": publisher_id,
                "name": ecosystem_labels.get(publisher_id) or (
                    str(c.source_hub_name or publisher_id).strip() or publisher_id
                ),
                "role": "provider",
                "url": "",
                "capabilities_count": 0,
                "categories": set(),
            })
            item["capabilities_count"] += 1
            category = str(getattr(c, "category", "") or "").strip()
            if category:
                item["categories"].add(category)
            invoke_url = str(c.invoke_url or "").strip()
            if invoke_url and not item["url"]:
                # Provider manifests use /capabilities/<product>/<capability>/invoke.  The
                # prefix is the human/API address of the node and contains no secret.
                item["url"] = invoke_url.split("/capabilities/", 1)[0].rstrip("/")
        ecosystem_nodes = []
        for item in ecosystem_by_publisher.values():
            item["categories"] = sorted(item["categories"])
            ecosystem_nodes.append(item)
        ecosystem_nodes.sort(key=lambda item: item["id"])
        wk = {
            "name": config.hub_name,
            "protocol_versions": ["v1", "v2"],
            "hub_version": config.hub_version,
            "mcp_endpoint": f"{config.hub_url}/ai-market/mcp",
            "manifest_url": f"{config.hub_url}/ai-market/v2/manifest",
            # Offerable, not stored: these three numbers are how a peer sizes us up, and
            # they must match what /v2/manifest is willing to list.
            "products_count": len({c.product_id for c in public_local}),
            "capabilities_count": len(public_local),
            "federated_capabilities_count": len(public_caps),
            "supported_chains": config.payment_chains,
            "supported_tokens": config.payment_tokens,
            # Payment recipient hidden from public manifest — exposed only when stub mode
            # `payment_ready` (not `not stub`): stub off with AIFACTORY_PROD unset skips
            # on-chain verification entirely, so the old flag could advertise payments as
            # configured on a hub that credits any tx_hash. See HubConfig.payment_readiness.
            "payment_configured": config.payment_ready,
            # How to pay THIS hub, in the manifest, because a price nobody can discover a
            # rail for is not an offer. `credits` needs no wallet, no chain and no contract
            # — it is the rail an operator can actually switch on — so an agent that reads
            # this knows whether it can transact here before it tries.
            "payment_rails": {
                "credits": {
                    "enabled": credits.enabled(),
                    "open_signup": credits.enabled() and credits.signup_open(),
                    "signup_url": f"{config.hub_url}/ai-market/v2/accounts",
                    "header": "X-API-Key",
                    "free_grant_usd": credits.signup_grant_usd() if credits.enabled() else 0.0,
                    "min_unit_usd": 0.00001,
                },
                "channel": {
                    "enabled": bool(config.crypto_enabled),
                    "header": "X-Payment-Channel",
                    "chains": config.payment_chains,
                },
            },
            # Published next to the price list on purpose: an agent that sees only
            # "payments configured" and a price assumes it needs a funded wallet before
            # it can find out whether a capability is any good.
            "free_trial": trial_policy(),
            "payment_testnet": config.payment_testnet,
            "signer_public_key": signer.public_key_b64,
            "federation": {
                "crawl_interval_s": config.crawl_interval_s,
                "routing_fee_bps": config.routing_fee_bps,
                "min_trust_score": config.min_trust_score,
                "seed_list": config.seed_list,
            },
            "peers": peers,
            "ecosystem": {
                "version": 1,
                "relationship": "owned-provider",
                "nodes": ecosystem_nodes,
            },
            "observed_hubs": observed_hubs,
            "plugins_loaded": plugins.count(),
            # Loaded ≠ reachable: a plugin whose register_routes body is `pass` still counts
            # as loaded, and eight of the fifteen shipped ones are exactly that.
            "plugins_with_routes": plugins.routed_count(),
        }

        # Merge plugin manifest extensions
        plugin_ext = plugins.get_manifest_extensions()
        if plugin_ext:
            wk["plugin_extensions"] = plugin_ext

        # Advertise the agent-native MCP servers so external AI agents can discover + add them
        # (the on-chain/HTTP capabilities are also reachable as MCP tools).
        # The hosted one comes first because it is the only entry that costs a reader
        # nothing to try: the others must be installed before they can say anything.
        wk["mcp_servers"] = [
            {
                "name": "aimarket-hub",
                "transport": "streamable-http",
                "url": f"{config.hub_url}/mcp",
                "description": (
                    "This hub's marketplace as MCP tools: search the catalogue and invoke a "
                    "capability. A few trial invokes per caller with no wallet, key or install; "
                    "after that the hub answers 402."
                ),
                "tools": ["market_search", "market_invoke"],
            },
            {
                "name": "aimarket-oracle-gateway",
                "transport": "stdio",
                "glama": "https://glama.ai/mcp/servers/alexar76/aimarket-oracle-gateway",
                "description": "Verifiable oracle services (Platon VRF, Chronos VDF, LUMEN reputation) as MCP tools — pay-per-call, every result verifiable.",
                "tools": [
                    "get_random", "get_randomness_beacon", "ask_oracle", "verify_random",
                    "compute_vdf", "verify_vdf", "get_reputation_scores", "get_agent_trust",
                    "verify_reputation", "list_oracle_capabilities",
                ],
            },
            {
                "name": "aimarket-mcp-packager",
                "transport": "stdio",
                "glama": "https://glama.ai/mcp/servers/alexar76/aimarket-plugins",
                "description": "Package any AIMarket capability as a self-hosted MCP server.",
                "tools": ["package_capability", "generate_dockerfile", "generate_claude_desktop_config"],
            },
        ]
        wk["prices_url"] = f"{config.hub_url}/ai-market/v2/prices"
        wk["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Sign the ENTIRE document (sign_object) so every field — mcp_servers, prices_url,
        # federation, peers, generated_at — is tamper-evident and an external validator can verify
        # the hub from .well-known alone. (manifest_canonical only covered structural fields, so it
        # left these unprotected.)
        if erc8004:
            # BEFORE sign_object, deliberately. A self-declared identity that sits outside
            # the document signature is rewritable in transit by any relay — the same class
            # of hole as an unsigned tools[] (spec §7.3.2). Declared here, never verified.
            wk["erc8004"] = erc8004

        wk["signature"] = signer.sign_object(wk)
        return wk

    # ── Manifest ────────────────────────────────────────────────

    @router.get("/manifest")
    async def v2_manifest():
        # The manifest is the federation's and every MCP client's view of what is on
        # sale here, so it gets the same gate as /search: a row this hub cannot execute
        # is not a tool, and advertising it to peers propagates the dead listing.
        caps = [
            c for c in db.list_capabilities(limit=1000)
            if capability_is_fulfillable(c) and capability_is_publicly_offerable(c)
        ]
        # A published success rate with no invocations behind it is the crawler's neutral
        # baseline, not a measurement — and the two are indistinguishable from the number
        # alone. Carry the observation count so a buyer can tell, and serve the MEASURED
        # rate the moment there is one (the crawler's comment has always said the hub
        # computes this itself; nothing did, so 0.5 was frozen into every signed manifest).
        observed = db.observations_30d()
        observed_caps = observed["by_capability"]
        observed_hubs = observed["by_hub"]
        tools = []
        for c in caps:
            attempts, successes = observed_caps.get(c.capability_id, (0, 0))
            tools.append({
                "name": c.tool_name(),
                "description": c.description,
                "input_schema": c.input_schema,
                "output_schema": c.output_schema,
                "price_per_call_usd": c.price_per_call_usd,
                "access_mode": capability_access_mode(c),
                "offerable": capability_is_publicly_offerable(c),
                "p50_latency_ms": c.p50_latency_ms,
                "success_rate_30d": (
                    round(successes / attempts, 4) if attempts else c.success_rate_30d
                ),
                # "measured" → the rate above is successes/attempts over the 30d window.
                # "unobserved" → nothing was invoked; the rate is a neutral placeholder and
                # must not be rendered as a score. Consumers key off THIS, not the number.
                "reputation_basis": "measured" if attempts else "unobserved",
                "observations_30d": attempts,
                "product_id": c.product_id,
                "capability_id": c.capability_id,
                "source_hub": c.source_hub,
                "source_hub_name": c.source_hub_name,
                # The protocol schema requires a number. Local publisher rows have no
                # federation markup and historically stored NULL here, which made the
                # entire signed manifest invalid to every strict peer crawler.
                "routed_price_usd": (
                    c.routed_price_usd
                    if c.routed_price_usd is not None
                    else c.price_per_call_usd
                ),
                "routing_fee_bps": c.routing_fee_bps,
                "trust_score": c.trust_score,
                # Where a caller POSTs this capability. A locally published capability
                # carries its provider's own URL. A FEDERATED one carried `null`: the
                # crawler deliberately does not store a peer-supplied invoke_url (a peer
                # could point it anywhere), so the entry advertised a price, schemas and
                # a source hub with no address to send anything to. The address is this
                # hub — routed invokes go here with `source_hub` naming the peer, which
                # the entry above already carries. Derived from our own configured
                # hub_url, never from the peer.
                "invoke_url": c.invoke_url or (
                    f"{config.hub_url.rstrip('/')}/ai-market/v2/invoke"
                    if (c.source_hub or "local") != "local"
                    else None
                ),
            })

        by_hub: dict[str, dict[str, Any]] = {}
        for p in db.list_peers():
            attempts, _successes = observed_hubs.get(p.url, (0, 0))
            by_hub[p.url] = {
                "capabilities_count": p.capabilities_count,
                "trust_score": p.trust_score,
                # Peer trust is only earned through observed trade. Without a single
                # invocation the score is whatever the crawler seeded, so say so rather
                # than let a peer's placeholder read as a rating.
                "trust_basis": "measured" if attempts else "unobserved",
                "observations_30d": attempts,
                "last_crawl": p.last_crawl,
            }
        local_attempts, _local_successes = observed_hubs.get("local", (0, 0))
        by_hub["local"] = {
            "capabilities_count": db.count_capabilities("local"),
            "trust_score": 1.0,
            # This hub trusts itself by construction, not by measurement.
            "trust_basis": "self",
            "observations_30d": local_attempts,
            "last_crawl": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        manifest_body = {
            "protocol_version": "v2",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "base_url": config.hub_url,
            "total_capabilities": len(caps),
            # Counted off the filtered list, not the table: a count that includes rows
            # absent from `tools` tells a peer we have stock we will not show it.
            "local_capabilities": sum(1 for c in caps if c.source_hub == "local"),
            "federated_capabilities": sum(1 for c in caps if c.source_hub != "local"),
            "hubs_indexed": db.peer_count(),
            "tools": tools,
            "by_hub": by_hub,
        }
        manifest_body["signature"] = signer.sign_manifest(manifest_body)
        return manifest_body

    # ── Prices (bulk) ───────────────────────────────────────────

    @router.get("/prices")
    async def v2_prices():
        """Compact, signed price list for every capability — so an agent can learn
        'what costs what' in one call without a full manifest/search.

        Fulfillable rows only, the same rule the manifest and /search apply. This list
        was the one storefront that did not: on 2026-08-25 it advertised 97 rows against
        the manifest's 85, and the twelve extras were exactly the unfulfillable local
        demos — headed by `audit.perf@v1` at $1.50, the most expensive item on the shelf.
        An agent that shops by price sorts descending, so the price list was steering
        buyers at precisely the capabilities the hub cannot run.
        """
        caps = [
            c for c in db.list_capabilities(limit=1000)
            if capability_is_fulfillable(c) and capability_is_publicly_offerable(c)
        ]
        prices = [
            {
                "capability_id": c.capability_id,
                "product_id": c.product_id,
                "price_usd": c.price_per_call_usd,
                "routed_price_usd": c.routed_price_usd,
                "routing_fee_bps": c.routing_fee_bps,
                "source_hub": c.source_hub,
            }
            for c in caps
        ]
        body = {
            "protocol_version": "v2",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "base_url": config.hub_url,
            "currency": "USD",
            "routing_fee_bps": config.routing_fee_bps,
            "count": len(prices),
            "prices": prices,
        }
        body["signature"] = signer.sign_object(body)  # sign all price rows, not just structural fields
        return body

    # ── Search ──────────────────────────────────────────────────

    @router.get("/search")
    async def search(
        intent: str = "",
        budget: float | None = None,
        max_latency_ms: int | None = None,
        min_trust: float | None = None,
        hub: str = "any",
        category: str | None = None,
        include_demo: bool = False,
        limit: int = 20,
    ):
        search_started = time.perf_counter()
        limit = min(limit, 100)
        detailed = db.search_capabilities_detailed(intent, limit=limit * 2)
        # filter_for_discover preserves input order. A capability id may legitimately be
        # offered by multiple hubs, so explanations are keyed by the whole routed offer.
        detail_by_offer = {
            (m.capability.capability_id, m.capability.product_id, m.capability.source_hub): m
            for m in detailed
        }
        results = supply_security.filter_for_discover([m.capability for m in detailed])
        floor = min_trust if min_trust is not None else supply_security.policy.min_trust_discover
        category_key = (category or "").strip().lower()

        # Same honesty rule as /manifest: an agent picking between offers has to be able
        # to tell an earned trust score from the crawler's placeholder.
        observed_search = db.observations_30d()["by_capability"]
        filtered: list[dict[str, Any]] = []
        for cap in results:
            if hub != "any" and cap.source_hub != hub:
                continue
            if not include_demo and getattr(cap, "is_demo", False):
                continue
            # Never offer what this hub cannot run. The is_demo check above is not
            # enough on its own: the twelve seeded showcase rows predate migration 013,
            # so their flag is 0 and they were being sold at $0.15–$1.50 while the
            # invoke path answered 404. include_demo does not override this — a buyer
            # asking to see demos still must not be quoted a price for a dead row.
            if not capability_is_fulfillable(cap):
                continue
            if not capability_is_publicly_offerable(cap):
                continue
            if category_key:
                blob = (
                    f"{cap.capability_id} {cap.product_id} {cap.name} {cap.description}"
                ).lower()
                # "security" must not match Platon merely because its description
                # mentions a "security model" — require a tighter token.
                if category_key == "security":
                    if not any(
                        t in blob
                        for t in (
                            "security.", "skopos", "posture", "cve",
                            "secret-scan", "sec-feed", "security-rules",
                        )
                    ):
                        continue
                elif category_key not in blob:
                    continue
            if cap.trust_score < floor:
                continue
            if max_latency_ms is not None and cap.p50_latency_ms > max_latency_ms:
                continue
            routed = cap.routed_price_usd or cap.price_per_call_usd
            if budget is not None and routed > budget:
                continue
            match = detail_by_offer.get((cap.capability_id, cap.product_id, cap.source_hub))
            # What the buyer must send. Without this a client can only guess the body,
            # and a guessed body is a refusal that still costs a trial — see
            # _input_requirements for the production case this comes from.
            input_required, input_hint = _input_requirements(cap.input_schema)
            filtered.append({
                "product_id": cap.product_id,
                "capability_id": cap.capability_id,
                "source_hub": cap.source_hub,
                "source_hub_name": cap.source_hub_name,
                "name": cap.name,
                "description": cap.description,
                "score": match.score if match else 0.0,
                "match_type": match.match_type if match else "browse",
                "matched_concepts": list(match.matched_concepts) if match else [],
                "matched_terms": list(match.matched_terms) if match else [],
                "score_breakdown": {
                    "lexical": match.lexical_score if match else 0.0,
                    "semantic": match.semantic_score if match else 0.0,
                    "quality": match.quality_score if match else 0.0,
                },
                "price_per_call_usd": cap.price_per_call_usd,
                "access_mode": capability_access_mode(cap),
                "routed_price_usd": routed,
                "routing_fee_bps": cap.routing_fee_bps,
                "trust_score": cap.trust_score,
                "reputation_basis": (
                    "measured" if observed_search.get(cap.capability_id, (0, 0))[0] else "unobserved"
                ),
                "observations_30d": observed_search.get(cap.capability_id, (0, 0))[0],
                "p50_latency_ms": cap.p50_latency_ms,
                "publisher_id": cap.publisher_id or None,
                "stake_usd": cap.stake_usd or None,
                "demo": cap.is_demo,
                "offerable": capability_is_publicly_offerable(cap),
                "input_required": input_required,
                "input_hint": input_hint,
            })
            if len(filtered) >= limit:
                break

        interpretation = interpret_intent(intent)
        return {
            "query": intent,
            "category": category_key or None,
            "matches": filtered,
            "search": {
                "mode": "hybrid-semantic-v1",
                "languages": ["en", "ru", "es", "fr", "zh"],
                "interpreted_concepts": list(interpretation.concepts),
                "latency_ms": round((time.perf_counter() - search_started) * 1000.0, 2),
                "query_stays_local": True,
            },
            "total_hubs_searched": db.peer_count() + 1,
            "protocol_version": "v2",
        }

    # ── Developer publish ─────────────────────────────────────

    @router.post("/supply/stake")
    async def supply_stake(
        body: dict[str, Any],
        authorization: str = Header(default=""),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        """Deposit stake (USD bookkeeping) to unlock community publish.

        Auth is PER-SUBJECT (_require_stake_subject): the caller must present the stake
        subject's own credential — from either credential namespace, because the stake
        ledger is one account space shared by publishers and bonded agents — or the
        operator token. Under the shared publish token this endpoint could not tell one
        publisher from another, so any token holder could credit stake to an arbitrary
        ``publisher_id`` — inflating a colluding publisher's trust score and stake gate.
        See the gate's docstring for exactly what operators must configure.

        Stake credit raises the target's trust score and unlocks the stake gate, so the
        number must be backed by a real deposit rather than by the caller's word: in
        production we require a ``tx_hash`` for EVERY positive amount here
        (SupplySecurity.stake then verifies it on-chain regardless of size, closing the
        old sub-minimum drip-feed bypass) AND a ``payer_signature`` proving control of
        the wallet that paid it — every deposit lands in the same settlement wallet, so
        recipient+amount alone let a publisher claim a stranger's inbound transfer as its
        own collateral. The refusal carries the exact challenge text to sign.

        The amount is also range-checked before it reaches the ledger. ``float()`` alone
        accepted ``"inf"`` / a bare ``Infinity`` literal, and ``stake()``'s only size
        guard is ``amount_usd <= 0`` — which infinity passes. The credit was written and
        the hub trust-anchor edge updated BEFORE the 500 that non-finite JSON produced,
        so a dev/relaxed hub ended up with a publisher holding infinite stake.
        """
        credit_account = _credits_publisher(x_api_key)
        publisher_id = str(body.get("publisher_id", "")).strip() or credit_account
        if not publisher_id:
            # Named before the auth check on purpose: the credential is scoped TO the
            # publisher_id, so there is nothing to authorize without one.
            raise HTTPException(status_code=400, detail="publisher_id required")
        if credit_account:
            # The key IS the credential, and it speaks for exactly one publisher.
            if publisher_id != credit_account:
                raise HTTPException(
                    status_code=403,
                    detail="an API key may only stake for its own account id",
                )
        else:
            _require_stake_subject(authorization, publisher_id)
        amount = _money_arg(body.get("amount_usd", 0), "amount_usd")
        tx_hash = str(body.get("tx_hash", "")).strip()
        payer_signature = str(body.get("payer_signature", "") or "").strip()
        if (
            amount > 0
            and not tx_hash
            and not credit_account
            and _is_production_mode()
            and not supply_security.policy.relaxed
        ):
            raise HTTPException(
                status_code=400,
                detail="tx_hash required for stake deposits in production "
                       "(AIMARKET_SUPPLY_SECURITY_RELAXED=1 to bypass for dev)",
            )
        settled_ref = ""
        if credit_account and amount > 0:
            # Take the collateral BEFORE recording it. The debit is the whole reason this
            # counts as stake rather than as the caller's word: the money leaves the
            # publisher's spendable balance and the operator holds it, which is exactly
            # what a later slash needs to be able to burn.
            ledger = hub_credits
            debit = ledger.debit(
                credit_account, amount,
                receipt_id=f"stake_{secrets.token_hex(8)}", note="supply stake",
                as_collateral=True,
            ) if ledger is not None else {"error": "credits unavailable"}
            if debit.get("error"):
                raise HTTPException(
                    status_code=402,
                    detail=f"stake not posted: {debit['error']}",
                )
            settled_ref = f"credits:{credit_account}:{secrets.token_hex(6)}"
        try:
            return {
                **supply_security.stake(
                    publisher_id, amount, tx_hash, payer_signature=payer_signature,
                    settled_ref=settled_ref,
                ),
                "protocol_version": "v2",
            }
        except ValueError as exc:
            if settled_ref:
                # The debit already happened; a refused stake must not keep the money.
                ledger = hub_credits
                if ledger is not None:
                    ledger.return_collateral(credit_account, amount, note="stake refused")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/supply/register")
    async def supply_register(
        body: dict[str, Any],
        authorization: str = Header(default=""),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        """Register a community capability with a direct invoke URL.

        PER-SUBJECT gated (_require_subject_credential, publisher namespace). This route
        moves no money directly, which is why it kept the shared publish token for one
        pass too long — but the manifest NAMES the publisher it registers as, and every
        holder of the shared token could name someone else. Registering as a rival:
          * stamps the victim's ``publisher_id`` on the capability row and reads the
            victim's stake balance as that capability's collateral, so a later
            failure-driven slash for it burns the VICTIM'S money;
          * spends the victim's publish-rate budget (5/hour), locking them out of their
            own publish flow;
          * attaches an attacker-controlled ``invoke_url`` to the victim's identity and
            trust score.
        So the caller must authenticate as the publisher it claims to be. A publisher
        acting as ITSELF is unaffected: it presents its own credential from
        AIMARKET_PUBLISHER_TOKENS (the same one /supply/stake takes) and the documented
        stake → register flow works end to end. Operators who provision publishers
        centrally can keep using AIMARKET_ADMIN_TOKEN for any publisher_id.
        """
        from aimarket_hub.publish import validate_manifest
        from aimarket_hub.supply_security import manifest_publisher_id

        # Resolved with the SAME helper validate_publish uses, and named before the auth
        # check: the credential is scoped to this publisher_id, so authorizing against a
        # different reading of the manifest than the ledger writes would be no gate at all.
        credit_account = _credits_publisher(x_api_key)
        publisher_id = manifest_publisher_id(body) or credit_account
        if not publisher_id:
            raise HTTPException(
                status_code=400,
                detail="publisher_id is required (wallet address or stable publisher slug)",
            )
        if credit_account:
            # Self-serve publishing: the key authenticates exactly one publisher, so there
            # is no shared token that could name somebody else's id.
            if publisher_id != credit_account:
                raise HTTPException(
                    status_code=403,
                    detail="an API key may only publish as its own account id",
                )
            body = {**body, "publisher_id": publisher_id}
        else:
            _require_subject_credential(authorization, publisher_id)
        try:
            cap = validate_manifest(body)
            publisher_id, pubkey = supply_security.validate_publish(body)
            cap.publisher_id = publisher_id
            cap.provider_pubkey = pubkey
            cap.stake_usd = db.supply_stake_get(publisher_id)
            # Admission is a publish-time gate, never a per-invoke tax.  The external
            # auditor returns its deterministic verdict immediately; its optional Metis
            # second opinion remains asynchronous and is refreshed from the telemetry
            # path without holding this request open.
            admission = await supply_chain_admission.evaluate(body)
            if admission.get("blocked"):
                decision = admission.get("decision") or "unavailable"
                raise HTTPException(
                    status_code=403 if decision in {"review", "reject"} else 503,
                    detail={
                        "code": "supply_chain_admission_denied",
                        "decision": decision,
                        "audit_id": admission.get("audit_id"),
                        "score": admission.get("score"),
                        "risk_tier": admission.get("risk_tier"),
                        "findings": admission.get("findings") or [],
                    },
                )
            trust = supply_security.after_publish(cap, publisher_id)
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "published": True,
            "product_id": cap.product_id,
            "capability_id": cap.capability_id,
            "invoke_url": cap.invoke_url,
            "publisher_id": publisher_id,
            "trust_score": trust,
            "stake_usd": cap.stake_usd,
            "price_per_call_usd": cap.price_per_call_usd,
            "supply_chain_admission": admission,
            "search_hint": f"{config.hub_url}/ai-market/v2/search?intent={cap.name}",
            "protocol_version": "v2",
        }

    # ── Self-bond (consumer cost/conduct bond → self-slash enforcement) ──
    @router.post("/self-bond/register")
    async def self_bond_register(body: dict[str, Any], authorization: str = Header(default="")):
        """Register a staked consumer cost/conduct bond (ceiling + client commitment).

        PER-SUBJECT gated in the AGENT namespace (AIMARKET_AGENT_TOKENS): the agent's own
        credential, or the operator token. It used to authenticate agent ids against the
        PUBLISHER registry, so one configured publisher entry made that registry
        authoritative over agent identities — an agent that was not a publisher was locked
        out, and an agent id colliding with a publisher id was authorised by an unrelated
        namespace. Registering a bond encumbers the named agent's already-staked
        collateral and, worse, SETS THE CEILING that /self-bond/slash later measures
        breaches against — under the shared publish token any holder could register a
        hair-thin ceiling on a rival agent and have the operator slash it for ordinary
        spending. Setting another party's slashing threshold is stake-state mutation, so
        it gets the same gate as /supply/stake.

        Both amounts are finiteness-checked here rather than in the callee: the callee's
        guards are ``bond_usd <= 0`` / ``ceiling_usd < 0``, and infinity passes both. An
        infinite ceiling can never be overspent, so it would durably neuter the bond it
        pretends to register.
        """
        agent_id = str(body.get("agent_id", "")).strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id required")
        _require_subject_credential(authorization, agent_id, (_NS_AGENT,))
        ceiling_usd = _money_arg(body.get("ceiling_usd", 0), "ceiling_usd")
        bond_usd = _money_arg(body.get("bond_usd", 0), "bond_usd", allow_zero=False)
        try:
            return {
                **supply_security.register_self_bond(
                    agent_id,
                    str(body.get("evm_address", "")),
                    ceiling_usd,
                    bond_usd,
                    str(body.get("commitment", "")),
                ),
                "protocol_version": "v2",
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/self-bond/slash")
    async def self_bond_slash(body: dict[str, Any], authorization: str = Header(default="")):
        """Slash an agent's self-bond on a declared-ceiling-vs-observed-spend breach.

        ADMIN-only, and grounded in hub-recorded settlement. This endpoint destroys
        ANOTHER party's staked collateral, so the shared publish token — held by every
        publisher on the hub — was the wrong gate: any publisher could burn a rival's
        bond (and, via the federated slash log, its reputation across the mesh) up to
        the spend the ledger happened to have recorded. Moving stake belongs with the
        operator token, like every other slash/settlement route in this file.

        The claimed overspend is additionally capped at the spend the hub ledger
        actually debited for the bonded wallet; if the hub holds no settlement record
        for it, we refuse rather than slash on an unverified number.
        """
        _require_admin(authorization)
        agent_id = str(body.get("agent_id", "")).strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id required")
        bond = db.self_bond_get(agent_id)
        if not bond:
            raise HTTPException(status_code=404, detail="no self-bond registered")
        # Finiteness-checked like the other money arguments: a garbage string here used
        # to raise out of float() as an unhandled 500 instead of a 400.
        claimed = _money_arg(body.get("observed_spend_usd", 0) or 0, "observed_spend_usd")
        from aimarket_hub.channels import wallet_recorded_spend_usd
        recorded = wallet_recorded_spend_usd(str(bond.get("evm_address", "")))
        if recorded is None:
            raise HTTPException(
                status_code=422,
                detail="cannot slash: no hub-recorded settlement for the bonded wallet "
                       "to validate observed_spend_usd against",
            )
        # Cap the claim at what the hub itself observed — a forged/inflated
        # observed_spend cannot slash beyond recorded settlement.
        observed = min(claimed, recorded)
        try:
            return {
                **supply_security.slash_self_bond(
                    agent_id,
                    observed,
                    str(body.get("evidence", "")),
                ),
                "observed_spend_claimed_usd": claimed,
                "observed_spend_recorded_usd": recorded,
                "protocol_version": "v2",
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/self-bond/{agent_id}")
    async def self_bond_get(agent_id: str):
        bond = db.self_bond_get(agent_id)
        if not bond:
            raise HTTPException(status_code=404, detail="no self-bond registered")
        return {**bond, "protocol_version": "v2"}

    # ── Invoke (plugin-gated) ───────────────────────────────────

    @router.get("/sandbox/quota")
    async def sandbox_quota_endpoint(visitor_id: str = ""):
        """Remaining free widget trials for a visitor id (embed localStorage)."""
        return {**sandbox_quota(visitor_id), "protocol_version": "v2"}

    @router.get("/verification/{nonce}")
    async def verification_lookup(nonce: str):
        """Pay-on-Verified verdict lookup: the async settlement's polling surface.

        Returns the (possibly still pending) verification envelope, the receipt
        with the envelope folded in, and — for refunded verdicts — the signed
        verification_rejection receipt.
        """
        rec = verify_svc.lookup(nonce)
        if rec is None:
            return JSONResponse(status_code=404, content={
                "success": False,
                "error": "verification_not_found",
                "protocol_version": "v2",
            })
        return {"success": True, **rec, "protocol_version": "v2"}

    @router.post("/invoke")
    async def invoke(
        body: InvokeRequest,
        request: Request,
        x_payment_channel: str | None = Header(default=None, alias="X-Payment-Channel"),
        x_payment_channel_secret: str | None = Header(default=None, alias="X-Payment-Channel-Secret"),
        x_aimarket_route_ok: str | None = Header(default=None, alias="X-AIMarket-Route-Ok"),
        x_sandbox_visitor: str | None = Header(default=None, alias="X-AIMarket-Sandbox-Visitor"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        x_payment: str | None = Header(default=None, alias="X-PAYMENT"),
        payment: str | None = Header(default=None, alias="PAYMENT"),
        authorization: str = Header(default=""),
    ):
        sandbox_mode = bool(x_sandbox_visitor and sandbox_enabled())
        # Master crypto switch (default OFF): when off, every invoke is served FREE —
        # no 402, no payment channel, no debit. Capabilities still execute normally.
        crypto_on = config.crypto_enabled
        # Credits: the second rail, and the only one a hub can charge on without a chain.
        # `price_rail_active` is what decides whether a listed price is real money — before
        # this existed it was `crypto_on` alone, which is why a default deployment served
        # every priced capability for free.
        credits_on = credits.enabled()
        credits_ledger = hub_credits if credits_on else None
        credit_account = ""
        if credits_ledger is not None and x_api_key:
            credit_account = credits_ledger.resolve(x_api_key)
            if not credit_account:
                # Fail closed on an unknown key rather than falling through to the free
                # path: a typo in a key must not silently become an unbilled invoke.
                return JSONResponse(status_code=401, content={
                    "success": False,
                    "error": "invalid_api_key",
                    "detail": "X-API-Key is not a known credit account on this hub",
                    "protocol_version": "v2",
                })
        # x402: a payer who answered our 402 with a signed EIP-3009 authorization. Verified
        # against the terms we advertised, single-use by nonce, and settled out of band —
        # see the x402 module docstring for why that gap is stated rather than hidden.
        x402_payment = None
        x402_raw = (x_payment or payment or "").strip()
        if x402_raw and x402.accept_enabled():
            decoded = x402.decode_payment(x402_raw)
            if decoded is None:
                return JSONResponse(status_code=400, content={
                    "success": False,
                    "error": "payment_malformed",
                    "detail": "the payment header is neither base64 JSON nor JSON",
                    "protocol_version": "v2",
                })
            x402_payment = decoded
        price_rail_active = crypto_on or credits_on or bool(x402_payment)
        request.state.aimarket_capability_id = body.capability_id
        consumer_hub_label = _resolve_invoke_consumer_hub(
            x_sandbox_visitor=x_sandbox_visitor,
            x_payment_channel=x_payment_channel,
            authorization=authorization,
        )
        # Proxy-aware (see _client_address): behind nginx the raw peer is the proxy, so
        # keying the limiter on it would put every caller in one bucket.
        client_ip = _client_address(request, _TRUSTED_PROXIES)

        # Per-consumer rate limit (griefing / provider-hammering protection). Key on
        # the network/session identity (client_ip), NOT the unauthenticated
        # X-Payment-Channel header: a caller could rotate that header per request to
        # get a fresh empty bucket and nullify the limiter. Sandbox flows fall back
        # to the sandbox-visitor id when there is no client address.
        rate_key = client_ip or x_sandbox_visitor or "anonymous"
        if not _check_invoke_rate(rate_key):
            return JSONResponse(status_code=429, content={
                "success": False,
                "error": "rate_limited",
                "detail": "invoke rate limit exceeded — slow down",
                "protocol_version": "v2",
            })

        if body.source_hub == "local":
            # ── Safety check: plugins first, then built-in ───────
            block = plugins.run_pre_checks(body.input, {
                "product_id": body.product_id,
                "capability_id": body.capability_id,
                "channel_id": x_payment_channel,
            })
            if not block:
                # Built-in safety gate as fallback
                verdict = builtin_safety.pre_invoke_check(body.input)
                if not verdict.passed:
                    block = {
                        "blocked": True,
                        "category": verdict.category,
                        "reason": verdict.reason,
                        "refund": True,
                        "plugin": "builtin-safety",
                    }

            if block:
                rejection_receipt = {
                    "type": "safety_rejection",
                    "product_id": body.product_id,
                    "capability_id": body.capability_id,
                    "channel_id": x_payment_channel,
                    "category": block.get("category"),
                    "reason": block.get("reason"),
                    "plugin": block.get("plugin"),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "refunded": True,
                    "nonce": f"blocked_{int(time.time())}_{body.product_id[:8]}",
                }
                with contextlib.suppress(Exception):
                    rejection_receipt["signature"] = signer.sign_receipt(rejection_receipt)
                return JSONResponse(status_code=403, content={
                    "success": False,
                    "error": "safety_blocked",
                    "category": block.get("category"),
                    "reason": block.get("reason"),
                    "blocked_by": block.get("plugin"),
                    "rejection_receipt": rejection_receipt,
                    "refund": {"refunded": True, "reason": "plugin_blocked"},
                    "protocol_version": "v2",
                })

            # ── Sandbox trial (widget try-before-buy) ───────────
            sandbox_meta: dict[str, Any] = {}
            if sandbox_mode:
                if x_payment_channel:
                    return JSONResponse(status_code=400, content={
                        "success": False,
                        "error": "sandbox_conflict",
                        "detail": "Do not send X-Payment-Channel with X-AIMarket-Sandbox-Visitor",
                        "protocol_version": "v2",
                    })
            # The trial is NOT taken here. Eight refusals sit between this point and the
            # first attempt to run anything — invalid ids, unknown capability, "federated,
            # not local", payment_required, payment_failed — and a debit taken here survived
            # every one of them. Measured on production: five invokes with an omitted
            # source_hub, each answered with the exact hint needed to fix the call, emptied a
            # caller's 5/5 allowance without ever running a capability. Charging for
            # validation is the one thing a free tier must not do, so the debit moved to
            # immediately before execution and the ledger gained a release for the exits
            # after it (see the `finally` at the end of this branch).

            # ── Execute ─────────────────────────────────────────
            t0 = time.time()
            factory_url = os.environ.get("AIFACTORY_PUBLIC_URL", "").strip()

            if not _is_safe_path(body.product_id) or not _is_safe_path(body.capability_id):
                raise HTTPException(status_code=400, detail="Invalid product or capability ID")

            cap = db.get_capability(body.product_id, body.capability_id)
            if cap is None:
                cap = db.find_by_capability_id(body.capability_id)
            if cap is None:
                raise HTTPException(status_code=404, detail=f"Unknown capability: {body.capability_id}")

            # find_by_capability_id is deliberately unfiltered by source_hub, so a
            # capability that exists here only as a crawled peer listing (crawler stores
            # those under source_hub=<peer url>; local publishes are always "local")
            # resolves on this branch even though source_hub="local" asked for LOCAL
            # execution. Two things went wrong when it did: the caller's omitted
            # source_hub surfaced as `502 Factory returned 404`, blaming the factory for
            # a routing mistake, and a peer listing that happens to carry an invoke_url
            # was proxied directly — skipping the federated tail's peer approval,
            # reservation and routing fee. Name the hub to route through instead.
            if (cap.source_hub or "local") != "local":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{body.capability_id} is federated, not local — retry with "
                        f'source_hub="{cap.source_hub}", the value '
                        f"/ai-market/v2/search returns for this capability."
                    ),
                )

            invoke_url = (cap.invoke_url or "").strip()
            provider_invoke_failed = False

            # Refuse before demanding payment when there is provably nothing to attempt.
            # A caller holding a stale id still reaches this branch — the storefront
            # filters only decide what is advertised — and the paywall below fires before
            # anything asks whether execution is possible, so the refusal a buyer meets
            # first is "pay me", not "this does not run here".
            #
            # Deliberately narrow, and it mirrors the four execution branches below:
            # invoke_url, a static JSON pack, the configured factory, and the explicit
            # offline stub. A row with no invoke_url and no pack can still be served
            # through the last two, which `fulfillment.py` explains it cannot judge from
            # the row alone, so this fires only when every attempt is impossible. Where
            # the factory does answer the attempt goes ahead and the money is protected
            # instead by the hold being released on failure — see
            # TestFailedExecutionIsNotBilled, which pins that.
            if (
                not capability_is_fulfillable(cap)
                and not factory_url
                and not sandbox_stub_invoke_enabled()
            ):
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"{body.capability_id} is listed but not executable on this hub: "
                        "no invoke_url, no static pack and no execution backend. It is "
                        "excluded from the manifest, /search and /prices — the listing "
                        "you have is stale."
                    ),
                )

            # SEC-02: paid capabilities must be paid for. Without a payment channel
            # the debit below is silently skipped (remaining=None), i.e. a free
            # invoke. In production, require X-Payment-Channel before executing so
            # the upstream capability is never run for free.
            # A signed authorization is checked HERE, before the provider runs, for the
            # same reason the credit hold is taken here: work done for a payment that turns
            # out to be invalid is work nobody pays for.
            # The refuse/allow decision lives in unpaid_invoke.py so MOMUS's
            # unpaid_invoke_refused probe has a file Factory remediation can patch.
            from aimarket_hub.unpaid_invoke import must_refuse_unpaid_paid_capability

            x402_accepted = None
            if x402_payment is not None and cap.price_per_call_usd > 0 and not sandbox_mode:
                x402_accepted = _accept_x402(
                    x402_payment, cap.price_per_call_usd, body.capability_id,
                )
                if x402_accepted.get("error"):
                    return JSONResponse(status_code=402, content={
                        "success": False,
                        "error": "payment_invalid",
                        "detail": x402_accepted["error"],
                        "needed": cap.price_per_call_usd,
                        **_payment_ways(credits_on, crypto_on),
                        "protocol_version": "v2",
                    })

            if must_refuse_unpaid_paid_capability(
                price_usd=cap.price_per_call_usd,
                sandbox_mode=sandbox_mode,
                price_rail_active=price_rail_active,
                payment_channel=x_payment_channel,
                credit_account=credit_account,
                x402_accepted=x402_accepted,
            ):
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "payment_required",
                    "detail": _payment_hint(credits_on, crypto_on),
                    "needed": cap.price_per_call_usd,
                    **_payment_ways(credits_on, crypto_on),
                    "protocol_version": "v2",
                })

            # ── Pay-on-Verified: plan the opt-in BEFORE executing the provider ────
            # Validating here (not after execution) means a malformed verify block
            # (e.g. requested with an empty intent) is rejected without first running
            # — and paying for — the billable capability (free-work griefing).
            vs_plan = None
            if body.verify is not None:
                vs_plan = verified_settlement.plan(
                    body.verify,
                    list_price=cap.price_per_call_usd,
                    crypto_on=crypto_on,
                    sandbox_mode=sandbox_mode,
                    channel_id=x_payment_channel,
                )
                if vs_plan.error:
                    return JSONResponse(status_code=400, content={
                        "success": False,
                        "error": "verify_invalid",
                        "detail": vs_plan.error,
                        "protocol_version": "v2",
                    })
                if not vs_plan.active and vs_plan.skipped_envelope is None:
                    vs_plan = None  # requested: false — behave exactly like no block

            # ── Reserve the price BEFORE executing (auth leg of auth/capture) ─────
            # INVARIANT: the provider never performs billable work the ledger has not
            # already reserved. A pre-flight balance *read* could not hold that line —
            # N concurrent invokes on one channel all observed the same sufficient
            # balance, so the losers received their 402 only AFTER the provider had
            # done (and had to be paid for) the work. hold_channel() moves the cents
            # out of `balance` atomically under the ledger lock, so exactly as many
            # invokes proceed as the channel can actually pay for.
            #
            # The reservation is resolved on every exit path:
            #   success (plain invoke)  -> capture_hold  (recorded debit)
            #   success (Pay-on-Verified) -> stays held; the verdict captures/releases
            #   anything else           -> release_hold in the `finally` below
            list_price = cap.price_per_call_usd
            # No rail switched on → free tier: charge nothing and never reserve/debit.
            # With EITHER rail on, a listed price is money.
            price = 0.0 if (sandbox_mode or not price_rail_active) else list_price

            # ── Escrow-backed channels: the buyer's on-chain authorization ────────
            # A channel funded through AIMarketEscrow can only be collected on chain with
            # a DebitAuthorization signed by its depositor, so a paid invoke without one is
            # work this hub could never be paid for. Validated BEFORE the provider runs, so
            # a bad authorization costs a 402 rather than an unpaid delivery. Entirely
            # inert for transfer-funded channels, which is all of them until an operator
            # enables the bridge.
            escrow_binding = ""
            accepted_authorization = None
            if crypto_on and not sandbox_mode and x_payment_channel:
                with contextlib.suppress(Exception):
                    escrow_binding = channel_escrow_binding(x_payment_channel)

            if escrow_binding and price > 0:
                auth_error = None
                if body.payment_authorization is None:
                    auth_error = (
                        "this channel is backed by an on-chain escrow: a signed "
                        "payment_authorization is required for a paid invoke"
                    )
                else:
                    try:
                        from aimarket_hub.escrow_bridge import authorization as bridge_auth
                        from aimarket_hub.escrow_bridge import store as bridge_store
                    except Exception as exc:
                        logger.error("escrow-backed invoke but the bridge is unavailable: %s", exc)
                        auth_error = "escrow settlement is not available on this hub"
                if auth_error is None:
                    # The buyer chooses the receiptId (they must sign it before the invoke
                    # exists), and the hub then uses that SAME id as the ledger receipt —
                    # so the on-chain replay key and the off-chain one are literally the
                    # same string and cannot drift apart.
                    try:
                        accepted_authorization = bridge_auth.verify_and_store(
                            payload=body.payment_authorization,
                            ledger_channel_id=x_payment_channel,
                            escrow_channel_id=escrow_binding,
                            expected_amount_usd=price,
                            expected_receipt_id=str(
                                (body.payment_authorization or {}).get("receiptId")
                                or (body.payment_authorization or {}).get("receipt_id")
                                or ""
                            ),
                            authorizations=bridge_store.AuthorizationStore(),
                        )
                    except Exception as exc:
                        auth_error = str(exc)
                if auth_error is not None:
                    return JSONResponse(status_code=402, content={
                        "success": False,
                        "error": "payment_authorization_required",
                        "detail": auth_error,
                        "needed": price,
                        "protocol_version": "v2",
                    })

            nonce = (
                accepted_authorization.row.receipt_id
                if accepted_authorization is not None
                else f"rcpt_{secrets.token_hex(16)}"
            )
            reserved = False   # a ledger hold on `nonce` is outstanding
            resolved = False   # the hold reached its terminal owner (captured / verify)
            remaining: float | None = None
            # Which ledger holds the reservation. Capture and release must go back to the
            # same one — a hold taken on credits and captured on the channel ledger would
            # leave the buyer's money reserved forever and record a debit against nobody.
            hold_rail = ""

            if price > 0 and credit_account and credits_ledger is not None:
                pay_result = credits_ledger.hold(credit_account, price, nonce)
                if pay_result.get("error"):
                    return JSONResponse(status_code=402, content={
                        "success": False,
                        "error": "payment_required",
                        "detail": pay_result.get("error"),
                        "needed": price,
                        "balance": credits_ledger.balance(credit_account),
                        **_payment_ways(credits_on, crypto_on),
                        "protocol_version": "v2",
                    })
                reserved = True
                hold_rail = "credits"
                remaining = pay_result.get("remaining_balance", 0)
            elif price > 0 and x_payment_channel:
                pay_result = hold_channel(
                    x_payment_channel, price, receipt_id=nonce,
                    secret=x_payment_channel_secret or "",
                )
                if pay_result.get("error"):
                    return JSONResponse(status_code=402, content={
                        "success": False,
                        "error": "payment_required",
                        "detail": pay_result.get("error"),
                        "needed": price,
                        "balance": channel_balance(x_payment_channel),
                        "protocol_version": "v2",
                    })
                reserved = True
                hold_rail = "channel"
                remaining = pay_result.get("remaining_balance", 0)
            elif crypto_on and not sandbox_mode and x_payment_channel:
                # Free capability on a real channel: nothing to reserve, but keep the
                # zero-amount debit so the channel secret is still authenticated and
                # the response can report the buyer's balance (legacy behaviour).
                pay_result = debit_channel(
                    x_payment_channel, 0.0, receipt_id=nonce,
                    secret=x_payment_channel_secret or "",
                )
                if pay_result.get("error"):
                    return JSONResponse(status_code=402, content={
                        "success": False,
                        "error": "payment_failed",
                        "detail": pay_result.get("error"),
                        "needed": price,
                        "protocol_version": "v2",
                    })
                remaining = pay_result.get("remaining_balance", 0)

            # Validation is over; something is actually going to be attempted. Take the trial
            # now, and treat it as a reservation released by the `finally` below on any exit
            # that does not deliver — the same shape the payment hold above already uses.
            trial_visitor = ""
            trial_delivered = False
            if sandbox_mode:
                if not free_tier_covers(body.capability_id):
                    # Refused BEFORE the trial is touched: a capability the free tier does not
                    # cover must not cost the caller an allowance to discover that. Every call to
                    # these composes an answer with a paid model, so serving one free hands a
                    # stranger our model bill — and the visitor id is self-chosen, so the
                    # per-caller cap would not even bound it.
                    return JSONResponse(
                        status_code=402, content={
                            "success": False,
                            **model_budget_refusal(body.capability_id),
                            "protocol_version": "v2",
                        }
                    )
                trial = consume_sandbox_trial(x_sandbox_visitor or "", client_ip=client_ip)
                if trial.get("error"):
                    code = 429 if trial["error"] in ("trial_quota_exhausted", "rate_limit_exceeded") else 400
                    return JSONResponse(status_code=code, content={
                        "success": False,
                        "error": trial["error"],
                        "detail": trial.get("detail", ""),
                        "sandbox": trial,
                        "protocol_version": "v2",
                    })
                trial_visitor = x_sandbox_visitor or ""
                sandbox_meta = {"sandbox": True, **{k: trial[k] for k in ("remaining", "used", "max_trials") if k in trial}}

            try:
                if invoke_url:
                    try:
                        sanitized = supply_security.sanitize_input(body.input)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    try:
                        supply_security.check_invoke_trust(cap)
                    except TrustOracleUnavailable as exc:
                        # LUMEN down / unreachable (including the oracle answering 403)
                        # is a hub-dependency failure, not "this caller is forbidden".
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    except ValueError as exc:
                        raise HTTPException(status_code=403, detail=str(exc)) from exc
                    try:
                        from urllib.parse import urlparse

                        from aimarket_hub.outbound_http import (
                            invoke_gateway_hosts,
                            resolve_invoke_url,
                            safe_post,
                        )

                        provider_headers = {
                            "X-Payment-Channel": x_payment_channel or "",
                            "X-AIMarket-Sandbox": "1" if sandbox_mode else "",
                        }
                        # Private Hub→provider token: only for operator-named local
                        # gateway hosts (e.g. kova-api). Use the post-rewrite host so
                        # localhost→gateway remaps still get the token. Never forward
                        # to federated / public URLs.
                        capability_token = os.environ.get("AIMARKET_CAPABILITY_TOKEN", "").strip()
                        target_host = (urlparse(resolve_invoke_url(invoke_url)).hostname or "").lower()
                        if capability_token and target_host in set(invoke_gateway_hosts()):
                            provider_headers["X-AIMarket-Internal-Token"] = capability_token

                        ir = await safe_post(
                            invoke_url,
                            json={
                                "input": sanitized,
                                "product_id": body.product_id,
                                "capability_id": body.capability_id,
                            },
                            headers=provider_headers,
                            invoke=True,
                        )
                        if ir.status_code == 200:
                            try:
                                payload = ir.json()
                            except ValueError as exc:
                                # Same rule as the federated tail: a body the provider
                                # could not serialize is a PROVIDER fault (502), not a
                                # 500 from this hub and not the caller's bad request.
                                provider_invoke_failed = True
                                raise HTTPException(
                                    status_code=502,
                                    detail=f"Provider at {invoke_url} returned a non-JSON body",
                                ) from exc
                            if not isinstance(payload, dict):
                                provider_invoke_failed = True
                                raise HTTPException(
                                    status_code=502,
                                    detail=f"Provider at {invoke_url} returned a non-object JSON body",
                                )
                            result = payload.get("result", payload.get("output", payload))
                            sig = ir.headers.get("X-Provider-Signature", "")
                            try:
                                supply_security.verify_provider_response(
                                    cap, result, sig,
                                    product_id=body.product_id,
                                    input_payload=sanitized,
                                )
                            except ValueError as exc:
                                provider_invoke_failed = True
                                if cap.publisher_id:
                                    supply_security.record_invoke(
                                        publisher_id=cap.publisher_id,
                                        consumer_id=x_payment_channel or "",
                                        success=False,
                                        product_id=body.product_id,
                                        capability_id=body.capability_id,
                                    )
                                raise HTTPException(status_code=502, detail=str(exc)) from exc
                        else:
                            provider_invoke_failed = True
                            # Only a provider-side 5xx counts as a genuine provider fault
                            # worth recording (and slashing after the consecutive-failure
                            # threshold in record_invoke). A 4xx reflects bad consumer
                            # input, so it must never penalize the provider's stake.
                            if cap.publisher_id and ir.status_code >= 500:
                                supply_security.record_invoke(
                                    publisher_id=cap.publisher_id,
                                    consumer_id=x_payment_channel or "",
                                    success=False,
                                    product_id=body.product_id,
                                    capability_id=body.capability_id,
                                )
                            raise HTTPException(
                                status_code=502,
                                detail=f"Provider returned {ir.status_code}: {ir.text[:200]}",
                            )
                    except httpx.RequestError as exc:
                        # Transient provider unreachability / timeout is NOT proof of
                        # misbehavior — do not record it as a failure or slash the
                        # provider's stake on it. The invoke still fails for the consumer.
                        provider_invoke_failed = True
                        raise HTTPException(
                            status_code=502,
                            detail=f"Provider unreachable at {invoke_url}: {exc}",
                        ) from exc
                elif not factory_url and sandbox_stub_invoke_enabled():
                    # Explicit opt-in offline/CI stub (AIMARKET_SANDBOX_STUB_INVOKE=1).
                    # Default-off so production never silently returns fake results;
                    # covers both sandbox visitors and local demo invokes without a backend.
                    result = sandbox_demo_result(body.capability_id, body.product_id, body.input)
                elif (cap.prompt_template or "").strip().startswith("{"):
                    # Hub-local static packs (e.g. security-rules.sec-feed) — JSON stored
                    # in prompt_template, no external invoke_url required.
                    try:
                        result = json.loads(cap.prompt_template)
                    except json.JSONDecodeError as exc:
                        raise HTTPException(
                            status_code=500,
                            detail=f"capability prompt_template is not valid JSON: {exc}",
                        ) from exc
                elif not factory_url:
                    raise HTTPException(
                        status_code=503,
                        detail="No execution backend configured. Set AIFACTORY_PUBLIC_URL "
                               "to the factory API address (e.g. http://127.0.0.1:8081)."
                    )
                else:
                    try:
                        async with httpx.AsyncClient(timeout=30) as fc:
                            fr = await fc.post(
                                f"{factory_url}/capabilities/{body.product_id}/{body.capability_id}/invoke",
                                json={"input": body.input},
                                headers={
                                    "X-Payment-Channel": x_payment_channel or "",
                                    "X-AIMarket-Sandbox": "1" if sandbox_mode else "",
                                },
                            )
                            if fr.status_code == 200:
                                result = fr.json()
                            elif sandbox_stub_invoke_enabled():
                                logger.info(
                                    "Factory %s for %s/%s — stub invoke (AIMARKET_SANDBOX_STUB_INVOKE)",
                                    fr.status_code, body.product_id, body.capability_id,
                                )
                                result = sandbox_demo_result(
                                    body.capability_id, body.product_id, body.input,
                                )
                            else:
                                raise HTTPException(
                                    status_code=502,
                                    detail=f"Factory returned {fr.status_code}: {fr.text[:200]}"
                                )
                    except httpx.RequestError as exc:
                        if sandbox_mode and sandbox_stub_invoke_enabled():
                            result = sandbox_demo_result(body.capability_id, body.product_id, body.input)
                        else:
                            raise HTTPException(
                                status_code=502,
                                detail=f"Factory unreachable at {factory_url}: {exc}"
                            ) from exc
                elapsed_ms = int((time.time() - t0) * 1000)

                # ── Provider said it did not deliver ──────────────────────────────
                # The AIMarket-v2 envelope carries an explicit `ok: false` when the
                # work failed honestly (a GAIA relay whose upstream is offline, an
                # empty CAP feed). That is not a 200: billing it would debit the
                # buyer and hand them a signed receipt reading `success: true` over
                # an error body. Fail closed here — the reservation is still
                # unresolved, so `finally` hands the money back untouched.
                # Only an EXPLICIT false counts; most results carry no `ok` at all.
                if isinstance(result, dict) and (
                    result.get("ok") is False or result.get("success") is False
                ):
                    # An honest "I could not read" is correct provider behaviour, not
                    # misbehaviour: skip the success recording, do not slash the stake.
                    provider_invoke_failed = True
                    provider_error = str(
                        result.get("error") or result.get("detail") or "provider reported failure"
                    )[:200]
                    logger.info(
                        "invoke: provider %s/%s returned ok=false (%s) — no debit",
                        body.product_id, body.capability_id, provider_error,
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"Provider did not deliver: {provider_error}",
                    )

                # ── Post-check: plugins first, then built-in ────────
                post_block = plugins.run_post_checks(result, {
                    "product_id": body.product_id,
                    "capability_id": body.capability_id,
                })
                if not post_block:
                    verdict = builtin_safety.post_response_check(result)
                    if not verdict.passed:
                        post_block = {
                            "blocked": True,
                            "category": verdict.category,
                            "reason": verdict.reason,
                            "refund": True,
                            "plugin": "builtin-safety",
                        }
                if post_block:
                    return JSONResponse(status_code=403, content={
                        "success": False,
                        "error": "plugin_blocked_response",
                        "category": post_block.get("category"),
                        "reason": post_block.get("reason"),
                        "blocked_by": post_block.get("plugin"),
                        "refund": {"refunded": True},
                        "protocol_version": "v2",
                    })

                # ── Payment: settle the reservation taken before execution ────────────
                # The money was already moved out of the buyer's balance (auth leg).
                # Delivery succeeded and the post-checks passed, so the reservation now
                # either becomes a recorded debit (plain invoke) or stays held for the
                # Metis verdict (Pay-on-Verified).
                # `hold_rail == "channel"` is part of the condition, not an afterthought.
                # `plan()` decides `paid` from the PRESENCE of an X-Payment-Channel header,
                # while the hold above is taken on whichever rail won — and when a caller
                # sends BOTH X-API-Key and X-Payment-Channel, credits wins. The deferred
                # path then hands ownership to a verified_settlements row that records only
                # a channel_id, and `_finalize` resolves it with the CHANNEL ledger's
                # capture_hold/release_hold, which have no such reservation. The credits hold
                # was therefore neither captured nor released: the buyer's money stayed
                # frozen and the envelope's promised refund never happened. The two
                # predicates the old comment said were equivalent had already drifted.
                # A non-channel hold is simply not deferrable, so it settles now.
                if vs_plan is not None and vs_plan.active and vs_plan.paid \
                        and (not reserved or hold_rail == "channel"):
                    # Deferred settlement: the hold STAYS reserved. Ownership passes to
                    # the verified_settlements row at register() below, which is where
                    # `resolved` flips — until then `finally` still owns the release.
                    if not reserved:
                        # A "paid" verified settlement with no money behind it: refuse
                        # rather than register it.
                        logger.error(
                            "invoke: verified settlement planned for %s without a reservation",
                            nonce,
                        )
                        raise HTTPException(
                            status_code=502,
                            detail="settlement failed: no reservation backs the verified invoke",
                        )
                elif reserved:
                    capture = (
                        credits_ledger.capture_hold(nonce)
                        if hold_rail == "credits" and credits_ledger is not None
                        else capture_hold(nonce)
                    )
                    if capture.get("error"):
                        # The ledger refused to convert OUR OWN reservation into a debit
                        # (it existed a moment ago). Something is wrong with the ledger
                        # state, so fail closed: withhold the output and let `finally`
                        # hand the reservation back, rather than serving billable work
                        # that the ledger never recorded as paid.
                        logger.error(
                            "invoke: capture of reservation %s failed: %s",
                            nonce, capture.get("error"),
                        )
                        raise HTTPException(
                            status_code=502,
                            detail="settlement failed: reservation could not be captured",
                        )
                    # The reservation is now a recorded debit — nothing left to release.
                    resolved = True
                    # …and the seller gets their share of it. Only on the credits rail:
                    # that is the one ledger where value can move between two parties, and
                    # only for a publisher who holds an account here to be paid into.
                    _pay_publisher_share(cap, price, nonce)

                receipt_payload = {
                    "nonce": nonce,
                    "product_id": body.product_id,
                    "capability_id": body.capability_id,
                    "price_usd": price,
                    "list_price_usd": list_price,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    # Fields the signature canonical covers — included so the receipt
                    # is self-contained and a consumer can recompute + verify it.
                    "success": True,
                    "latency_ms": elapsed_ms,
                }
                if sandbox_mode:
                    receipt_payload["sandbox"] = True
                # Self-contained, verifiable receipt: signed fields + signature block.
                receipt = {**receipt_payload, "signature": signer.sign_receipt(receipt_payload)}

                # ── Pay-on-Verified: register the pending settlement BEFORE any other ──
                # bookkeeping. register() commits the durable verified_settlements row
                # that makes an escrow hold recoverable (the startup reconciler re-queues
                # pending rows). If it raises, the hold has no owner yet — the `finally`
                # below releases it so a crash can't lock the buyer's funds.
                verification_env = None
                vs_rejection = None
                if vs_plan is not None and vs_plan.active:
                    verification_env = verify_svc.register(
                        nonce=nonce,
                        product_id=body.product_id,
                        capability_id=body.capability_id,
                        channel_id=x_payment_channel or "",
                        provider_id=cap.publisher_id or "",
                        price_usd=price,
                        intent=vs_plan.intent,
                        output=result,
                        mode=vs_plan.mode,
                        receipt=receipt,
                        advisory=not vs_plan.paid,
                    )
                    # The durable settlement row now owns the hold (and the startup
                    # reconciler will finish it after a crash) — stop releasing it here.
                    resolved = True

                stat = InvocationStat(
                    capability_id=body.capability_id,
                    product_id=body.product_id,
                    source_hub="local",
                    price_usd=price,
                    latency_ms=elapsed_ms,
                    success=True,
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    consumer_hub=consumer_hub_label,
                )
                # Bookkeeping must never be able to destroy delivered work: the answer is
                # already produced here, and `record_invocation` only writes a metrics row.
                # See the federated path for the incident this comes from.
                try:
                    db.record_invocation(stat)
                except Exception as exc:
                    logger.error("invocation stat NOT recorded (call succeeded): %s", exc)

                if invoke_url and cap.publisher_id and not provider_invoke_failed:
                    supply_security.record_invoke(
                        publisher_id=cap.publisher_id,
                        consumer_id=x_payment_channel or "",
                        success=True,
                        product_id=body.product_id,
                        capability_id=body.capability_id,
                    )

                # ── ACEX revenue routing (factory → hub → ACEX leg) ──────
                # Feed the listing's CapShares pool only with revenue ACTUALLY earned:
                # gate on the charged `price` (0 when sandbox OR crypto off), not the
                # list price. Otherwise a crypto-off deployment — which debits nothing
                # and settles nothing on-chain — would still accrue phantom list-price
                # revenue to shareholders/auditors.
                # A verified hold defers revenue accrual to capture time: only money the
                # verdict actually captured may feed CapShares pools / audit rewards
                # (verified_settlement._accrue_acex mirrors these two blocks).
                vs_deferred = vs_plan is not None and vs_plan.active and vs_plan.paid

                acex_revenue = None
                if not sandbox_mode and price > 0 and not vs_deferred:
                    try:
                        from aimarket_hub import acex_ipo
                        accrued = acex_ipo.accrue_revenue(body.product_id, price)
                        if accrued.get("ok"):
                            acex_revenue = {
                                "to_pool_usd": accrued["to_pool_usd"],
                                "revenue_share_bps": accrued["revenue_share_bps"],
                            }
                    except Exception as exc:
                        logger.warning("ACEX accrue failed for %s: %s", body.product_id, exc)

                acex_audit_rewards = None
                if not sandbox_mode and price > 0 and not vs_deferred:
                    try:
                        from aimarket_hub import acex_audit
                        audit_acc = acex_audit.accrue_audit_rewards(body.product_id, price)
                        if audit_acc.get("ok"):
                            acex_audit_rewards = {
                                "to_auditors_usd": audit_acc["to_auditors_usd"],
                                "audit_fee_bps": audit_acc["audit_fee_bps"],
                                "bridge": audit_acc.get("bridge"),
                            }
                    except Exception as exc:
                        logger.warning("ACEX audit accrue failed for %s: %s", body.product_id, exc)

                # ── Pay-on-Verified: resolve the buyer's chosen sync/async mode ───────
                if vs_plan is not None:
                    if vs_plan.active and vs_plan.wait:
                        # Sync opt-in: hold the response until the verdict, bounded.
                        # On timeout we return the pending envelope (NOT an error) —
                        # the buyer falls back to GET /verification/{nonce}.
                        vs_resolved = await verify_svc.wait_for(nonce, timeout=vs_plan.wait_timeout_s)
                        if vs_resolved:
                            verification_env = vs_resolved.get("verification") or verification_env
                            receipt = vs_resolved.get("receipt") or receipt
                            vs_rejection = vs_resolved.get("rejection_receipt")
                            # The verdict may have released the hold back to the balance;
                            # recompute so remaining_balance never under-reports (a stale
                            # post-hold figure would hide the refund from the buyer).
                            if remaining is not None and (verification_env or {}).get("status") == "refunded":
                                _bal = channel_balance(x_payment_channel)
                                if _bal is not None:
                                    remaining = _bal
                    elif not vs_plan.active:
                        # Skipped (below floor / disabled): legacy settlement, envelope says why.
                        verification_env = vs_plan.skipped_envelope
                        receipt["verification"] = verification_env

                # ── Final artifacts: after safety and settlement are stable ─────────
                # Receipt issuance used to happen inside run_post_checks(), before the
                # built-in gate and before capture.  Besides leaving receipts behind for
                # blocked outputs, that call only carried product/capability ids, so AWR
                # committed to an empty input, local provider, zero latency and zero price.
                # The final hook sees the exact accepted output and the economics the hub
                # can actually attest.  A deferred Pay-on-Verified hold is deliberately
                # not described as captured; its later verdict owns that statement.
                verification_status = str((verification_env or {}).get("status") or "")
                if vs_deferred:
                    # A synchronous Pay-on-Verified request can already be settled or
                    # refunded by the time this final hook runs.  Commit to the money
                    # outcome we actually know, never to the original asking price as
                    # though it had necessarily reached the provider.
                    artifact_price = price if verification_status == "settled" else 0.0
                    artifact_settlement = (
                        "captured" if verification_status == "settled"
                        else "refunded" if verification_status == "refunded"
                        else "pending"
                    )
                else:
                    artifact_price = price
                    artifact_settlement = (
                        "captured" if reserved and price > 0 else "free"
                    )

                receipt_artifacts = plugins.run_receipt_hooks(result, {
                    "product_id": body.product_id,
                    "capability_id": body.capability_id,
                    "input": body.input,
                    "provider_hub": config.hub_url,
                    "latency_ms": elapsed_ms,
                    "price_usd": artifact_price,
                    "list_price_usd": list_price,
                    "status": "succeeded",
                    "nonce": nonce,
                    "settlement_status": artifact_settlement,
                    "gateway_receipt": receipt,
                })
                provenance_receipt = (
                    receipt_artifacts.get("provenance")
                    # Compatibility for an older external provenance plugin that still
                    # mutates the result in post_check. New issuers must use the final
                    # hook; keeping the read fallback avoids silently dropping receipts
                    # during a rolling hub/plugin upgrade.
                    or result.pop("_provenance_receipt", None)
                )

                response_body = {
                    "success": True,
                    "result": result,
                    "receipt": receipt,
                    "price_usd": price,
                    "list_price_usd": list_price,
                    "latency_ms": elapsed_ms,
                    "safety_checked": True,
                    "plugins_checked": plugins.count(),
                    "protocol_version": "v2",
                }
                if sandbox_meta:
                    response_body.update(sandbox_meta)
                if provenance_receipt:
                    response_body["provenance_receipt"] = provenance_receipt
                if acex_revenue is not None:
                    response_body["acex_revenue"] = acex_revenue
                if acex_audit_rewards is not None:
                    response_body["acex_audit_rewards"] = acex_audit_rewards
                if remaining is not None:
                    response_body["remaining_balance"] = remaining
                if verification_env is not None:
                    # HTTP 200 even on a refunded verdict: the SERVICE succeeded and the
                    # buyer keeps the output — the money outcome lives in the envelope
                    # (quality escrow, not censorship; contrast the safety-gate 403,
                    # which withholds output).
                    response_body["verification"] = verification_env
                if vs_rejection is not None:
                    response_body["rejection_receipt"] = vs_rejection
                # The caller keeps the output, so the trial was earned. Note this sits
                # alongside the verification envelope above: a refunded quality verdict still
                # delivered the result, and the free tier follows the money here rather than
                # inventing a second definition of "delivered".
                trial_delivered = True
                return JSONResponse(status_code=200, content=response_body)
            finally:
                if trial_visitor and not trial_delivered:
                    # Same rules as the reservation release below: never raise out of
                    # `finally`, never fail silently. The safety-gate 403 promises a refund
                    # in its response body — this is what makes that promise true for a free
                    # caller as well as a paying one.
                    try:
                        trial_release = release_sandbox_trial(trial_visitor)
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "invoke: releasing the sandbox trial for %s raised: %s",
                            trial_visitor, exc,
                        )
                    else:
                        if trial_release.get("error"):
                            logger.error(
                                "invoke: failed to release the sandbox trial for %s: %s",
                                trial_visitor, trial_release["error"],
                            )
                # Single release point for the pre-execution reservation. Runs on EVERY
                # exit between reserve and settle — the provider 502s, the safety
                # post-block 403 (whose response promises a refund, so this is what
                # makes that promise true), a failed capture, a failed verify
                # registration, or any unexpected exception. release_hold is a no-op
                # once the hold has been captured or already released, so a late
                # exception on an already-settled invoke cannot double-refund.
                if reserved and not resolved:
                    # Must not raise out of `finally` (it would mask the real error),
                    # but must never be silent either: an unreleased reservation is the
                    # buyer's money in limbo, and it blocks channel close.
                    try:
                        release = (
                            credits_ledger.release_hold(nonce)
                            if hold_rail == "credits" and credits_ledger is not None
                            else release_hold(nonce)
                        )
                    except Exception as exc:
                        logger.error(
                            "invoke: release of reservation %s raised: %s", nonce, exc,
                        )
                    else:
                        if release.get("error"):
                            logger.error(
                                "invoke: failed to release reservation %s: %s",
                                nonce, release.get("error"),
                            )

        # Federated invoke — route to provider hub.
        # Pay-on-Verified is a LOCAL-settlement feature (the escrow hold and the
        # Metis verdict both live on this hub). A verify block on a federated invoke
        # cannot be honoured — the routing fee is debited immediately and the remote
        # hub owns execution — so surface an explicit skipped envelope rather than
        # silently charging the buyer as if verification applied.
        federated_verify_env = None
        if body.verify is not None and getattr(body.verify, "requested", False):
            federated_verify_env = verified_settlement.skipped_envelope_for(
                body.verify, reason="federated_unsupported",
            )
        peer = db.get_peer(body.source_hub)
        if not peer:
            raise HTTPException(status_code=404, detail="Peer hub not found")
        # Existing is not the same as admitted. A peer ROW needs no credential at all to
        # appear: POST /ai-market/v2/federation/announce without an admin bearer, or merely a
        # GET of our own well-known carrying an `X-AIMarket-Crawler` header — both land
        # `status="pending"`. Quarantine means nothing this peer claims is indexed, listed or
        # served, and this branch nonetheless made it a live outbound transport for an
        # anonymous caller, with that caller's `input` in the body.
        #
        # The gate is STATUS, deliberately not `trusted`. `trusted` reads like the right flag
        # ("operator-approved … manifests indexed only if True") and `admit_peer` and the
        # admin trust route both set it — but the admin ANNOUNCE door does not, so requiring
        # it here would refuse the operator's own documented "add a peer directly" flow.
        # `status == "active"` is what actually separates an admitted peer from a quarantined
        # one. See SECURITY notes: that `trusted` is left False by one of the two admin doors
        # is its own inconsistency, and not one to change from inside the invoke path.
        peer_status = str(getattr(peer, "status", "active") or "active")
        if peer_status != "active":
            raise HTTPException(
                status_code=403,
                detail=f"peer is not admitted for routing (status={peer_status})",
            )

        # ── Trial, then payment, for a capability this hub BROKERS ────────────────
        # Until 2026-07-30 neither ran here: the payment gate lives in the `local` branch
        # above, and this branch only forwarded X-Payment-Channel and passed a peer's own 402
        # through. That was coherent for a third-party peer — it bills, this hub takes
        # `routing_fee_bps` — and wrong for the peers that actually exist. `oracle_core`
        # enforces nothing (no 402, no price check anywhere in it), so 42 of the 47 catalogued
        # capabilities were free to call while advertising a price, and the 1% routing fee was
        # a broker's cut of a sale that never happened.
        #
        # All the payment machinery — channels, holds, the ledger, escrow — is in this hub.
        # Building it into seventeen oracles to make them bill their own operator would be
        # absurd, so for a peer that does not charge, THIS HUB is the seller of record.
        #
        # The trial comes first and is the whole reason this is safe to turn on: a bot that
        # has just installed the bridge still gets its free calls, and only then meets a 402
        # that says what to do. Without it, switching the gate on would close the on-ramp the
        # bridge exists to open.
        # Scoped to the ROUTED peer, exactly like the `listed` lookup below — whose comment
        # already says it "can never resolve to a pricier capability published by someone
        # else". `find_by_capability_id` ignores product_id AND source_hub and returns
        # `ORDER BY trust_score DESC LIMIT 1`, and capability_ids collide here by design (its
        # own docstring says it exists because federated oracle caps share ids). So the price
        # QUOTED, HELD and CAPTURED could come from a different peer's row: a $7.50 capability
        # was billed at another peer's $0.01, and where that other row was priced 0 the
        # `fed_price > 0` gate never fired and the paid call was served for nothing.
        fed_cap = db.get_capability(
            body.product_id, body.capability_id, source_hub=peer.url,
        )
        if fed_cap is None:
            fed_cap = next(
                (c for c in db.list_capabilities(source_hub=peer.url)
                 if c.capability_id == body.capability_id),
                None,
            )
        fed_list_price = float(getattr(fed_cap, "price_per_call_usd", 0.0) or 0.0) if fed_cap else 0.0
        # Only for a peer this hub is DECLARED seller for (AIMARKET_SELLS_FOR). For anyone else
        # the broker shape is untouched: the peer bills, this hub forwards the channel and takes
        # its routing fee, and a 200 from the peer says nothing about whether it charged.
        fed_sells = config.sells_on_behalf_of(body.source_hub)
        # A key at the peer means this hub can actually settle with it, so it may resell:
        # charge the buyer the catalogued price, pay the peer out of its own account there,
        # keep the routing fee as the spread. Without one, a peer that charges cannot be
        # bought through this hub at all — its 402 lands on a buyer with no account there.
        fed_peer_key = config.peer_api_key(body.source_hub)
        fed_resells = bool(fed_peer_key)
        fed_price = 0.0 if (
            sandbox_mode or not price_rail_active or not (fed_sells or fed_resells)
        ) else fed_list_price
        if (fed_sells or fed_resells) and price_rail_active and not sandbox_mode \
                and fed_cap is None:
            # This hub is the one billing, and it has no catalogued price from this peer for
            # this capability. Guessing from another row is what produced the mispricing
            # above; charging 0 would serve a paid capability free. Say so instead.
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no catalogued price for {body.capability_id} at {peer.url} — "
                    "the peer's catalogue has not been crawled for this capability"
                ),
            )

        listed = None
        listed_price = 0.0
        expected_fee = 0.0
        if not fed_sells and not sandbox_mode and config.routing_fee_bps > 0:
            listed = db.get_capability(
                body.product_id, body.capability_id, source_hub=peer.url,
            )
            if listed is None:
                listed = next(
                    (c for c in db.list_capabilities(source_hub=peer.url)
                     if c.capability_id == body.capability_id),
                    None,
                )
            listed_price = _as_float(
                getattr(listed, "price_per_call_usd", 0.0),
                max_value=_MAX_REPORTED_PRICE_USD,
            ) if listed is not None else 0.0
            expected_fee = round(listed_price * config.routing_fee_bps / 10000, 6)

        # What the buyer will be charged in total, and therefore what an escrow-backed
        # channel has to authorize. The authorization used to cover `fed_price` alone while
        # the fee was taken separately — so a brokered or resold capability could not be
        # bought on the only funding rail production has: the buyer signed the price, the
        # fee had nothing behind it, and the invoke was refused `routing_fee_unpayable`
        # after the money was already deposited. Quoting one number and charging another is
        # the bug; the quote is the total now, in every 402 on this path.
        #
        # Quantized to whole cents because BOTH ends already are: the ledger bills
        # `max(1, ceil(usd*100))` cents and `escrow_verify.usd_to_base_units` converts the
        # signed amount the same way. Quoting $0.0101 asks the buyer to sign a number their
        # own signature can never match — the verifier demands the $0.02 it ceils to, the
        # client re-signs the quote, and the purchase deadlocks with the deposit already on
        # chain. One number, and it is the one everything downstream rounds to.
        fed_authorized_total = _ceil_to_cents(fed_price + expected_fee)

        # ── An escrow-backed channel needs a signed authorization HERE too ───────────────
        # The local branch above has required one since the bridge landed; this branch did
        # not, and every capability in the catalogue is federated — so every paid sale took
        # the buyer's cents off-chain with a hub-generated receipt id and left nothing that
        # could ever be collected on chain. `usedAmount` stayed 0 in the escrow, the depositor
        # could reclaim a consumed deposit, and the operator's revenue existed only as
        # `used_cents` in a SQLite file. Found on 2026-08-24 by buying one real invoke and
        # looking for the authorization it should have produced: the ledger showed a captured
        # hold, the bridge store was empty.
        #
        # The receipt id is the buyer's, not ours: the contract's replay key (`usedReceipts`)
        # and the ledger's receipt then are literally the same string and cannot drift.
        fed_escrow_binding = ""
        fed_authorization = None
        if crypto_on and not sandbox_mode and x_payment_channel:
            with contextlib.suppress(Exception):
                fed_escrow_binding = channel_escrow_binding(x_payment_channel)

        # The TOTAL, not the price: for a brokered peer the price is zero and the fee is
        # the whole charge, and gating on the price skipped the authorization block while
        # the fee block below still demanded one — a 402 telling the buyer to sign
        # something this code would never read.
        if fed_escrow_binding and fed_authorized_total > 0:
            fed_auth_error = None
            if body.payment_authorization is None:
                fed_auth_error = (
                    "this channel is backed by an on-chain escrow: a signed "
                    "payment_authorization is required for a paid invoke"
                )
            else:
                try:
                    from aimarket_hub.escrow_bridge import authorization as bridge_auth
                    from aimarket_hub.escrow_bridge import store as bridge_store
                except Exception as exc:
                    logger.error(
                        "escrow-backed federated invoke but the bridge is unavailable: %s", exc)
                    fed_auth_error = "escrow settlement is not available on this hub"
            if fed_auth_error is None:
                try:
                    fed_authorization = bridge_auth.verify_and_store(
                        payload=body.payment_authorization,
                        ledger_channel_id=x_payment_channel,
                        escrow_channel_id=fed_escrow_binding,
                        expected_amount_usd=fed_authorized_total,
                        expected_receipt_id=str(
                            (body.payment_authorization or {}).get("receiptId")
                            or (body.payment_authorization or {}).get("receipt_id")
                            or ""
                        ),
                        authorizations=bridge_store.AuthorizationStore(),
                    )
                except Exception as exc:
                    fed_auth_error = str(exc)
            if fed_auth_error is not None:
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "payment_authorization_required",
                    "detail": fed_auth_error,
                    # The TOTAL: sign this, not the list price, or the fee has nothing
                    # behind it and the next attempt is refused for the difference.
                    "needed": fed_authorized_total,
                    "price_usd": fed_price,
                    "routing_fee_usd": expected_fee,
                    "protocol_version": "v2",
                })

        fed_nonce = (
            fed_authorization.row.receipt_id
            if fed_authorization is not None
            else f"rcpt_{secrets.token_hex(16)}"
        )
        fed_reserved = False
        fed_settled = False
        # Which ledger the federated reservation lives in — same reason as `hold_rail`
        # on the local branch: capture and release must return to the ledger that held.
        fed_hold_rail = ""
        # A routing-fee reservation, taken BEFORE the peer is asked to do anything. See
        # the fee block below for why it is no longer collected after the fact.
        fed_fee_nonce = ""
        fed_fee_rail = ""
        fed_fee_reserved_usd = 0.0
        # A free-tier trial is a reservation too, released on every path that does not
        # deliver — the mirror of fed_reserved/fed_settled for the paid path.
        fed_trial_visitor = ""
        fed_trial_delivered = False
        fed_sandbox_meta: dict[str, Any] = {}

        if sandbox_mode and fed_sells:
            if x_payment_channel:
                return JSONResponse(status_code=400, content={
                    "success": False,
                    "error": "sandbox_conflict",
                    "detail": "Do not send X-Payment-Channel with X-AIMarket-Sandbox-Visitor",
                    "protocol_version": "v2",
                })
            if not free_tier_covers(body.capability_id):
                # Refused BEFORE the trial is touched: a capability the free tier does not
                # cover must not cost the caller an allowance to discover that. Every call to
                # these composes an answer with a paid model, so serving one free hands a
                # stranger our model bill — and the visitor id is self-chosen, so the
                # per-caller cap would not even bound it.
                return JSONResponse(
                    status_code=402, content={
                        "success": False,
                        **model_budget_refusal(body.capability_id),
                        "protocol_version": "v2",
                    }
                )
            trial = consume_sandbox_trial(x_sandbox_visitor or "", client_ip=client_ip)
            if trial.get("error"):
                code = 429 if trial["error"] in ("trial_quota_exhausted", "rate_limit_exceeded") else 400
                return JSONResponse(status_code=code, content={
                    "success": False,
                    "error": trial["error"],
                    "detail": trial.get("detail", ""),
                    "sandbox": trial,
                    "protocol_version": "v2",
                })
            # Held, not spent — see the `finally` below. The paid branch a few lines down
            # takes a hold so a peer that 402s or times out costs the buyer nothing; a free
            # caller was instead debited at the door and kept the debit through all nine
            # downstream exits. Five clear "retry with source_hub=..." hints in a row emptied
            # a real caller's allowance on production before this was paired.
            fed_trial_visitor = x_sandbox_visitor or ""
            fed_sandbox_meta = {"sandbox": True, **{
                k: trial[k] for k in ("remaining", "used", "max_trials") if k in trial
            }}
        elif price_rail_active and fed_price > 0 and not x_payment_channel and not credit_account:
            return JSONResponse(status_code=402, content={
                "success": False,
                "error": "payment_required",
                "detail": _payment_hint(credits_on, crypto_on),
                "needed": fed_authorized_total,
                "price_usd": fed_price,
                "routing_fee_usd": expected_fee,
                **_payment_ways(credits_on, crypto_on),
                "protocol_version": "v2",
            })
        elif fed_price > 0 and credit_account and credits_ledger is not None:
            fed_hold = credits_ledger.hold(credit_account, fed_price, fed_nonce)
            if fed_hold.get("error"):
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "payment_required",
                    "detail": fed_hold.get("error"),
                    "needed": fed_price,
                    "balance": credits_ledger.balance(credit_account),
                    **_payment_ways(credits_on, crypto_on),
                    "protocol_version": "v2",
                })
            fed_reserved = True
            fed_hold_rail = "credits"
        elif (fed_price > 0 or fed_authorization is not None) and x_payment_channel:
            # A HOLD, not a debit, and before the peer is asked to do anything — the same
            # order the local branch uses. Captured once the peer has actually served the
            # call, released by the `finally` below on every other path, so a peer that 402s,
            # times out or returns garbage never costs the buyer anything.
            #
            # Behind an authorization it is ONE hold for the whole authorized total, under
            # the receipt id the buyer signed. The bridge's invariant is that the cents the
            # ledger debits under that receipt equal the units the signature covers
            # (escrow_bridge/mirror.py, guard 2); a second hold under a `fee_…` receipt the
            # store has never seen leaves the signed row permanently over-collecting, so it
            # is BLOCKED forever — the buyer served and debited off chain, nothing
            # collectable on chain, which is worse than the refusal this replaced.
            fed_hold = hold_channel(
                x_payment_channel,
                fed_authorized_total if fed_authorization is not None else fed_price,
                receipt_id=fed_nonce,
                secret=x_payment_channel_secret or "",
            )
            if fed_hold.get("error"):
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "payment_required",
                    "detail": fed_hold.get("error"),
                    "needed": fed_price,
                    "balance": channel_balance(x_payment_channel),
                    "protocol_version": "v2",
                })
            fed_reserved = True
            fed_hold_rail = "channel"

        # ── Routing fee: RESERVED before the peer is asked to do anything ────────────
        # It used to be collected after the fact, and only `if x_payment_channel and
        # routing_fee > 0` — so a buyer who simply left the header out paid nothing, and a
        # failed debit was a `logger.warning` with the result served anyway: the one
        # fail-OPEN money path in a file where every other one fails closed. A brokerage
        # that is opt-in for the payer is not a brokerage, and a node whose only revenue
        # line works that way has no P&L.
        #
        # The basis is the price THIS hub published for the routed capability — the same
        # consent anchor the post-hoc code used, just read early enough to act on it. A
        # peer that turns out to charge less is refunded when the fee is settled below, and
        # a peer this hub is seller of record for owes no fee at all (it has already billed
        # the full list price, so a fee on top would bill the same call twice).
        if expected_fee > 0 and price_rail_active and fed_authorization is not None:
            # Already inside the single authorized hold above. Reserving it again here would
            # both double-charge the buyer and split the ledger record across two receipts.
            # The fee is recognised out of the captured total; the settlement tail's fee
            # block is a no-op because `fed_fee_nonce` stays empty.
            pass
        elif expected_fee > 0 and price_rail_active:
            fee_nonce = f"fee_{secrets.token_hex(8)}"
            if credit_account and credits_ledger is not None:
                fee_hold = credits_ledger.hold(credit_account, expected_fee, fee_nonce)
                fee_rail = "credits"
            elif x_payment_channel and (not fed_escrow_binding or fed_authorization is not None):
                # An escrow-backed channel is allowed here now, but ONLY behind an
                # authorization the buyer signed for price + fee (see fed_authorized_total).
                # Without one the fee would be an accrual with nothing on chain to collect
                # it, which is what the refusal below exists to prevent.
                fee_hold = hold_channel(
                    x_payment_channel, expected_fee, receipt_id=fee_nonce,
                    secret=x_payment_channel_secret or "",
                )
                fee_rail = "channel"
            else:
                # No payer, or an escrow-backed channel with no authorization covering the
                # fee. The escrow case is refused rather than accrued: collecting on chain
                # needs a DebitAuthorization the buyer signed for THIS amount, and an
                # uncollectable accrual is a number in SQLite pretending to be revenue.
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "routing_fee_unpayable" if fed_escrow_binding else "payment_required",
                    "detail": (
                        "this hub charges a routing fee to broker another hub's capability; "
                        + (
                            "this escrow-backed channel's authorization does not cover it — "
                            "sign a payment_authorization for the total in `needed`"
                            if fed_escrow_binding
                            else _payment_hint(credits_on, crypto_on)
                        )
                    ),
                    "needed": fed_authorized_total if fed_escrow_binding else expected_fee,
                    "price_usd": fed_price,
                    "routing_fee_usd": expected_fee,
                    "routing_fee_bps": config.routing_fee_bps,
                    **_payment_ways(credits_on, crypto_on),
                    "protocol_version": "v2",
                })
            if fee_hold.get("error"):
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "payment_required",
                    "detail": f"routing fee could not be reserved: {fee_hold.get('error')}",
                    "needed": expected_fee,
                    **_payment_ways(credits_on, crypto_on),
                    "protocol_version": "v2",
                })
            fed_fee_nonce = fee_nonce
            fed_fee_rail = fee_rail
            fed_fee_reserved_usd = expected_fee

        # Everything from here to the return is wrapped so the hold is ALWAYS handed back.
        # There are nine exits between this point and the response — a peer 402, an unreachable
        # provider, a non-JSON body, an SSRF refusal, a failed capture — and an unreleased
        # reservation is the buyer's money in limbo that also blocks channel close. The local
        # branch above has the same guard for the same reason; adding an exit here without one
        # would be silent, so the shape is structural rather than a release at each return.
        try:
            fed_started = time.time()

            # Transport selection: a peer that advertises `mcp_endpoint` in its
            # /.well-known/ai-market.json expects the {capability_id, input} envelope
            # there. A peer that advertises none gets the legacy
            # /capabilities/{product}/{cap}/invoke path with the bare input.
            # Resolved from what the peer publishes, for EVERY peer — see
            # _peer_invoke_endpoint for why a category allow-list was the wrong test.
            mcp_endpoint = await _peer_invoke_endpoint(peer)

            headers: dict[str, str] = {
                "X-AIMarket-Routing-Hub": config.hub_url,
                "X-AIMarket-Routing-Fee": str(config.routing_fee_bps),
            }
            # Upstream peers (ATLAS, oracles, …) do not share this hub's payment ledger.
            # Forwarding the buyer's X-Payment-Channel there is meaningless and leaves the
            # peer on a shared IP free-tier that exhausts quickly. Prefer a stable hub
            # federation visitor, or an optional hub-owned peer channel when configured.
            peer_ch = (os.environ.get("AIMARKET_PEER_PAYMENT_CHANNEL") or "").strip()
            peer_sec = (os.environ.get("AIMARKET_PEER_PAYMENT_CHANNEL_SECRET") or "").strip()
            if fed_peer_key:
                # The only header here that can actually settle: this hub's own credit
                # account on the peer. The buyer paid us; we pay them.
                headers["X-API-Key"] = fed_peer_key
            elif peer_ch:
                headers["X-Payment-Channel"] = peer_ch
                if peer_sec:
                    headers["X-Payment-Channel-Secret"] = peer_sec
            else:
                import hashlib as _hashlib

                fed_visitor = (os.environ.get("AIMARKET_FEDERATION_VISITOR") or "").strip() or (
                    "hub-fed-" + _hashlib.sha256((config.hub_url or "hub").encode()).hexdigest()[:20]
                )
                headers["X-AIMarket-Sandbox-Visitor"] = fed_visitor

            # ── Transport (the ONLY part that differs between peer kinds) ──────────
            # Everything after this — 402 passthrough, error mapping, envelope
            # normalization, stats, routing fee, return — is shared. It used to live
            # inside the legacy `else`, so an oracle peer fell out of the handler with no
            # return at all: HTTP 200 `null`, no invocation recorded, no routing fee
            # collected, and the `if mcp_endpoint` normalization below was dead code
            # (mcp_endpoint is falsy by construction inside that branch).
            try:
                from aimarket_hub.outbound_http import safe_post

                if mcp_endpoint:
                    # `source_hub` tells the far side where the call came from — EXCEPT on a
                    # peer hub's own /invoke, where that field selects the peer's routing
                    # branch. Sending our URL there asks it to route the call back to us,
                    # which is a loop, not a purchase; "local" asks it to serve its own
                    # capability, which is what was bought.
                    to_peer_hub = mcp_endpoint.rstrip("/").endswith("/ai-market/v2/invoke")
                    resp = await safe_post(
                        mcp_endpoint,
                        json={
                            "capability_id": body.capability_id,
                            "input": body.input,
                            "product_id": body.product_id,
                            "source_hub": "local" if to_peer_hub else config.hub_url,
                        },
                        headers=headers,
                    )
                else:
                    # product_id / capability_id are interpolated into the upstream path,
                    # so they get the same traversal guard the local branch applies.
                    if not _is_safe_path(body.product_id) or not _is_safe_path(body.capability_id):
                        raise HTTPException(status_code=400, detail="Invalid product or capability ID")
                    provider_url = f"{peer.url}/capabilities/{body.product_id}/{body.capability_id}/invoke"
                    resp = await safe_post(provider_url, json=body.input, headers=headers)
            except ValueError as exc:
                # ONLY safe_post's own refusal reaches here: an SSRF-unsafe / blocked-network
                # target, i.e. a request this hub declines to make. That is a client-visible
                # 400. A malformed provider RESPONSE is a different fault and maps to 502
                # below — the two must not share this handler (they did before, so a peer
                # returning non-JSON was reported to the caller as their own bad request).
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except httpx.RequestError as exc:
                raise HTTPException(status_code=502, detail=f"Provider unreachable: {exc}") from exc

            # ── Shared settlement tail ─────────────────────────────────────────────
            if resp.status_code == 402:
                # Payment passthrough, identical for both transports: the peer priced the
                # call and wants payment, no work was performed, so we charge no routing
                # fee and record no invocation. A non-JSON 402 body still surfaces as a 402
                # (the status is the signal) rather than becoming a confusing 502.
                try:
                    content = resp.json()
                except ValueError:
                    content = {
                        "success": False,
                        "error": "payment_required",
                        "detail": f"peer {peer.url} returned 402 with a non-JSON body",
                        "protocol_version": "v2",
                    }
                if fed_resells:
                    # We were supposed to be the one paying. A 402 here is OUR account at the
                    # peer being empty or refused, not the buyer's problem — and passing the
                    # peer's terms back would tell the buyer to go fund an account somewhere
                    # they never chose to deal with. The reservation is released by the
                    # `finally`, so this costs them nothing.
                    logger.error(
                        "resale: peer %s refused this hub's own credit key with 402 — "
                        "top up the hub's account there (AIMARKET_PEER_API_KEYS)", peer.url,
                    )
                    request.state.x402_passthrough = True
                    return JSONResponse(status_code=502, content={
                        "success": False,
                        "error": "upstream_unpaid",
                        "detail": (
                            f"this hub resells {peer.url} and its account there could not pay "
                            "for the call — nothing was charged to you"
                        ),
                        "protocol_version": "v2",
                    })
                # This 402 is the PEER's, verbatim. Enriching it would replace their payment
                # terms with ours — telling the payer to send our amount to our address for
                # someone else's capability. Mark it so the x402 middleware leaves it alone.
                request.state.x402_passthrough = True
                return JSONResponse(status_code=402, content=content)
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Provider returned {resp.status_code}",
                )

            try:
                result_data = resp.json()
            except ValueError as exc:
                # Provider fault, not caller fault → 502 (see the ValueError note above).
                raise HTTPException(
                    status_code=502,
                    detail=f"Provider returned a non-JSON body ({resp.status_code})",
                ) from exc
            if not isinstance(result_data, dict):
                # The accounting below is keyed on an object envelope. Refuse rather than
                # crash on a bare list/scalar body.
                raise HTTPException(
                    status_code=502,
                    detail="Provider returned a non-object JSON body",
                )

            # Normalize the AIMarket-v2 oracle envelope to the fields the hub's
            # accounting/return path expects (it has `ok`/`receipt`, not top-level
            # `success`/`latency_ms`/`price_usd`). This now actually runs for the
            # mcp_endpoint transport — which is the only place it was ever meant to.
            if mcp_endpoint:
                rcpt = result_data.get("receipt") or {}
                if not isinstance(rcpt, dict):
                    rcpt = {}
                result_data.setdefault("success", bool(result_data.get("ok", True)))
                result_data.setdefault("latency_ms", rcpt.get("latency_ms", 0))
                result_data.setdefault("price_usd", rcpt.get("price_usd", 0))

            # What the BUYER is told they paid must be what this hub actually charged them.
            # `price_usd` above is the peer's self-reported number, and a peer that does not
            # bill reports none — so a sale settled at the list price came back to the caller
            # as `"price_usd": 0` while their balance went down. Measured on a real call:
            # $0.004 debited, zero reported. Same defect the invocation stat already carries a
            # fix for, on the path the buyer actually reads.
            #
            # Only where this hub is the seller of record. For a third-party peer that bills
            # the buyer itself, the peer's own number is the true one and must not be
            # overwritten with ours.
            if fed_sells or fed_resells:
                result_data["price_usd"] = fed_price

            # Apply the same accepted-output boundary as local invokes.  A federated
            # response that a safety plugin or the built-in gate rejects must neither
            # be billed nor receive a successful final work receipt.
            post_block = plugins.run_post_checks(result_data, {
                "product_id": body.product_id,
                "capability_id": body.capability_id,
                "provider_hub": body.source_hub,
            })
            if not post_block:
                verdict = builtin_safety.post_response_check(result_data)
                if not verdict.passed:
                    post_block = {
                        "blocked": True,
                        "category": verdict.category,
                        "reason": verdict.reason,
                        "refund": True,
                        "plugin": "builtin-safety",
                    }
            if post_block:
                return JSONResponse(status_code=403, content={
                    "success": False,
                    "error": "plugin_blocked_response",
                    "category": post_block.get("category"),
                    "reason": post_block.get("reason"),
                    "blocked_by": post_block.get("plugin"),
                    "refund": {"refunded": True},
                    "protocol_version": "v2",
                })

            # A peer controls these numbers, so coerce defensively: a string/None price
            # would otherwise blow up the fee arithmetic (500) or the stat insert, and a
            # merely huge finite latency (1e30) is fatal to the SQLite INTEGER column.
            price = _as_float(result_data.get("price_usd"), max_value=_MAX_REPORTED_PRICE_USD)
            # ── The stat records what THIS hub settled, never the peer's word ──────────
            # Same rule the AWR receipt below already follows, and the same number the local
            # branch records. Where this hub is the seller of record (AIMARKET_SELLS_FOR),
            # `fed_price` IS the charge — 0 for a sandbox/crypto-off call, the list price for
            # a paid one — and it is captured a few lines below BEFORE this row is written.
            # Peers that answer with a bare product envelope carry no price at all
            # (ATLAS returns `{"ok": …}`, BASANOS `{"success": true, "result": …}`), so a
            # $0.02 sale of theirs reached /stats/live, the live feed and every revenue
            # aggregate over `invocation_stats` as $0.00 — while the buyer's channel was
            # correctly debited. `price` (the peer's self-reported number) stays as it was:
            # it is only the fee-basis cap below, which is exactly where NOT trusting the
            # peer is the point.
            # ── Did the peer refuse? ──────────────────────────────────────────────
            # `success: false` — the `ok: false` the envelope above is normalized from —
            # is an honest refusal: ATLAS answers `point_id required` that way when the
            # buyer's input is incomplete, GAIA when an upstream relay is offline. The
            # local branch has never billed one (it fails closed and hands the hold back).
            # This branch captured the hold unconditionally a few lines below and marked
            # the sandbox trial as earned on the way out, so the SAME refusal cost a
            # paying buyer the full list price and a free caller one of their three
            # trials. The peer's envelope still goes back untouched with its
            # `refuse_reason` — that is what tells the caller what to fix — and only the
            # settlement changes.
            #
            # EXPLICIT false only, exactly as the local branch reads it: a legacy
            # factory-product peer returns a bare result with no `success`/`ok` field at
            # all, and treating a missing flag as a refusal would stop paying for work
            # those peers really did.
            refused = result_data.get("success") is False or result_data.get("ok") is False
            charged_price = 0.0 if refused else (
                _as_float(fed_price, max_value=_MAX_REPORTED_PRICE_USD) if fed_sells else price
            )
            # Latency has the same shape — those peers report none, and the hub has been
            # timing the round trip since `fed_started`. A number the peer omitted is
            # unknown, not 0 ms.
            peer_latency_ms = int(_as_float(
                result_data.get("latency_ms"), max_value=_MAX_REPORTED_LATENCY_MS,
            ))
            stat = InvocationStat(
                capability_id=body.capability_id,
                product_id=body.product_id,
                source_hub=body.source_hub,
                price_usd=charged_price,
                latency_ms=peer_latency_ms or int((time.time() - fed_started) * 1000),
                success=bool(result_data.get("success", False)),
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                consumer_hub=consumer_hub_label,
            )
            # The peer did the work, so the hold becomes a debit. Fail CLOSED if the ledger
            # refuses: withholding the output is better than serving billable work the ledger never
            # recorded as paid, and the reservation is handed back below.
            # Not on a refusal: `fed_settled` stays false, so the `finally` releases the
            # hold and the buyer pays nothing.
            if fed_reserved and not refused:
                fed_capture = (
                    credits_ledger.capture_hold(fed_nonce)
                    if fed_hold_rail == "credits" and credits_ledger is not None
                    else capture_hold(fed_nonce)
                )
                if fed_capture.get("error"):
                    logger.error(
                        "federated invoke: capture of reservation %s failed: %s",
                        fed_nonce, fed_capture.get("error"),
                    )
                    raise HTTPException(
                        status_code=502,
                        detail="settlement failed: reservation could not be captured",
                    )
                fed_settled = True

            # Bookkeeping must never be able to destroy delivered work. At this point the
            # peer has answered, the money side is settled, and the caller is owed the
            # result — but `record_invocation` writes a row, and on 2026-09-05 that write
            # raised `sqlite3.OperationalError: database is locked` on
            # independentai.network/hub and turned EIGHT OUT OF EIGHT successful federated
            # invokes into a bare 500. The caller lost an answer that had already been
            # fetched from the peer, over a metrics row.
            #
            # A lost stat is a hole in a chart. A lost answer is the product failing.
            try:
                db.record_invocation(stat)
            except Exception as exc:
                logger.error(
                    "invocation stat NOT recorded for %s (the call itself succeeded): %s",
                    getattr(stat, "capability_id", "?"), exc,
                )

            # ── Routing fee: the basis is the PUBLISHED price, never the peer's word ──
            # The fee is a percentage of `price_usd`, and `price_usd` arrives in the peer's
            # own response body — so billing on it let the remote side choose how much of
            # THIS hub's consumer's channel to spend. Verified against the pre-cap code: a
            # peer answering `price_usd: 49000` took $490 out of a $500 channel on a single
            # invoke, for a capability the buyer never saw a price for.
            # The consent anchor is what this hub itself published for the routed
            # capability (the crawler stores the peer's catalog under source_hub=peer.url,
            # which is exactly what /catalog served the buyer). Cap the basis there; with no
            # catalogued price there is no agreed amount, so we charge nothing rather than
            # trusting the peer — fail closed on the money, not on the routing.
            # `listed` / `listed_price` were resolved before the peer was called, because
            # the fee is reserved up front now. Re-resolve only if that pass skipped it
            # (seller-of-record, sandbox, or a zero fee schedule), so a hub that charges
            # nothing still logs the same diagnostics it always did.
            if listed is None and listed_price == 0.0:
                listed = db.get_capability(
                    body.product_id, body.capability_id, source_hub=peer.url,
                )
                if listed is None:
                    # Same fallback the local branch makes for a caller that omits or
                    # mis-specifies product_id — but scoped to the ROUTED peer's own listings,
                    # so it can never resolve to a pricier capability published by someone else.
                    listed = next(
                        (c for c in db.list_capabilities(source_hub=peer.url)
                         if c.capability_id == body.capability_id),
                        None,
                    )
                listed_price = _as_float(
                    getattr(listed, "price_per_call_usd", 0.0),
                    max_value=_MAX_REPORTED_PRICE_USD,
                ) if listed is not None else 0.0
            # No delivery, no brokerage: a refusal earns this hub nothing either, so the
            # basis is zero regardless of what the peer put in its body.
            fee_basis = 0.0 if refused else min(price, listed_price)
            if listed is None and price > 0:
                logger.warning(
                    "routing_fee: peer %s reported $%.6f for %s/%s but this hub has no "
                    "catalogued price for it — charging no routing fee",
                    peer.url, price, body.product_id, body.capability_id,
                )
            elif price > listed_price:
                logger.warning(
                    "routing_fee: peer %s reported $%.6f for %s/%s over the catalogued "
                    "$%.6f — fee basis capped at the published price",
                    peer.url, price, body.product_id, body.capability_id, listed_price,
                )

            routing_fee = round(fee_basis * config.routing_fee_bps / 10000, 6)
            if fed_settled and fed_sells:
                # This hub already billed the full list price for a peer that does not charge, so
                # it is the seller here, not a broker between two parties. Taking a routing fee on
                # top would bill the same call twice.
                #
                # NOT true of a resale (`fed_resells`): there the hub pays the peer its price out
                # of the hub's own account and bills the buyer the same catalogued number, so the
                # fee is the entire margin. Zeroing it there would mean brokering at cost.
                routing_fee = 0.0
            # Settle the reservation taken before routing. Three outcomes, and none of
            # them is "serve anyway and log": captured when the peer delivered at the price
            # we quoted, released when it refused or turned out to be free, and
            # released-then-debited for the smaller amount when the peer charged less than
            # its own catalogue said.
            if fed_fee_nonce:
                _fee_ledger_capture = (
                    credits_ledger.capture_hold
                    if fed_fee_rail == "credits" and credits_ledger is not None
                    else capture_hold
                )
                _fee_ledger_release = (
                    credits_ledger.release_hold
                    if fed_fee_rail == "credits" and credits_ledger is not None
                    else release_hold
                )
                if routing_fee <= 0:
                    _fee_ledger_release(fed_fee_nonce)
                elif abs(routing_fee - fed_fee_reserved_usd) < 1e-9:
                    fee_capture = _fee_ledger_capture(fed_fee_nonce)
                    if fee_capture.get("error"):
                        logger.error(
                            "routing_fee: capture of reservation %s failed: %s",
                            fed_fee_nonce, fee_capture.get("error"),
                        )
                        raise HTTPException(
                            status_code=502,
                            detail="settlement failed: routing fee could not be captured",
                        )
                    logger.info("routing_fee: collected $%.6f (%d bps) via %s",
                                routing_fee, config.routing_fee_bps, fed_fee_rail)
                else:
                    _fee_ledger_release(fed_fee_nonce)
                    if fed_fee_rail == "credits" and credits_ledger is not None:
                        fee_result = credits_ledger.debit(
                            credit_account, routing_fee,
                            receipt_id=f"route_{secrets.token_hex(8)}", note="routing fee",
                        )
                    else:
                        fee_result = debit_channel(
                            x_payment_channel, routing_fee,
                            receipt_id=f"route_{secrets.token_hex(8)}",
                            secret=x_payment_channel_secret or "",
                        )
                    if fee_result.get("error"):
                        logger.error("routing_fee: could not debit %s via %s: %s",
                                     routing_fee, fed_fee_rail, fee_result.get("error"))
                    else:
                        logger.info("routing_fee: collected $%.6f (peer charged under its "
                                    "catalogued price) via %s", routing_fee, fed_fee_rail)
                fed_fee_nonce = ""

            result_data["routed_via"] = config.hub_url
            result_data["routing_fee_bps"] = config.routing_fee_bps
            if fed_sandbox_meta:
                result_data["sandbox"] = fed_sandbox_meta
            if federated_verify_env is not None:
                result_data["verification"] = federated_verify_env

            # The routing hub now issues the same portable AWR class for peer work as
            # for local work.  It signs the peer envelope (including its own gateway
            # receipt/attestation) while naming the actual provider hub.  Charged price
            # is the amount THIS hub settled, never the peer's self-reported list price.
            if bool(result_data.get("success", False)):
                fed_elapsed_ms = int((time.time() - fed_started) * 1000)
                receipt_artifacts = plugins.run_receipt_hooks(result_data, {
                    "product_id": body.product_id,
                    "capability_id": body.capability_id,
                    "input": body.input,
                    "provider_hub": body.source_hub,
                    "latency_ms": fed_elapsed_ms,
                    "price_usd": fed_price,
                    "list_price_usd": fed_list_price,
                    "status": "succeeded",
                    "nonce": fed_nonce,
                    "settlement_status": "captured" if fed_settled else "free",
                    "gateway_receipt": result_data.get("receipt"),
                })
                provenance_receipt = receipt_artifacts.get("provenance")
                if provenance_receipt:
                    result_data["provenance_receipt"] = provenance_receipt
            # The trial is earned by a DELIVERED result, not by a response. Set on the way
            # out rather than at the point of consumption, because everything between them
            # can still fail — and left false for a refusal, so the `finally` hands the
            # allowance back the way ATLAS's own gate hands back its own.
            fed_trial_delivered = not refused
            return result_data
        finally:
            if fed_trial_visitor and not fed_trial_delivered:
                # Same discipline, and same rule about `finally`: never raise out of it, never
                # be silent about a failure to hand the reservation back.
                try:
                    released = release_sandbox_trial(fed_trial_visitor)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "federated invoke: releasing the sandbox trial for %s raised: %s",
                        fed_trial_visitor, exc,
                    )
                else:
                    if released.get("error"):
                        logger.error(
                            "federated invoke: failed to release the sandbox trial for %s: %s",
                            fed_trial_visitor, released["error"],
                        )
            if fed_reserved and not fed_settled:
                # Must not raise out of `finally` — that would mask the real error — but must
                # never be silent either.
                try:
                    fed_release = (
                        credits_ledger.release_hold(fed_nonce)
                        if fed_hold_rail == "credits" and credits_ledger is not None
                        else release_hold(fed_nonce)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "federated invoke: release of reservation %s raised: %s",
                        fed_nonce, exc,
                    )
                else:
                    if fed_release.get("error"):
                        logger.error(
                            "federated invoke: failed to release reservation %s: %s",
                            fed_nonce, fed_release.get("error"),
                        )
            # The routing-fee reservation has the same rule as the price reservation: it is
            # taken before the peer is called, so every exit between the reservation and the
            # settlement above must hand it back. `fed_fee_nonce` is cleared once settled, so
            # this only fires on the paths that never got there.
            if fed_fee_nonce:
                try:
                    fee_release = (
                        credits_ledger.release_hold(fed_fee_nonce)
                        if fed_fee_rail == "credits" and credits_ledger is not None
                        else release_hold(fed_fee_nonce)
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "federated invoke: release of routing-fee reservation %s raised: %s",
                        fed_fee_nonce, exc,
                    )
                else:
                    if fee_release.get("error"):
                        logger.error(
                            "federated invoke: failed to release routing-fee reservation %s: %s",
                            fed_fee_nonce, fee_release.get("error"),
                        )

    # ── Federation ──────────────────────────────────────────────

    def _pending_peer_cap() -> int:
        """How many quarantined peers an unauthenticated caller may create.

        Two caps are documented and only one was ever read. Both announce doors passed
        ``federation_gossip_max_observed`` (2000) — pinned by ``test_pending_queue_is_capped``
        — while ``AIMARKET_FEDERATION_OPEN_MAX_PENDING`` (documented as "hard cap on pending
        peers: an open door is also a write amplifier", default 50) was referenced nowhere in
        the package. An operator who set it got silence.

        So: honour it when it is EXPLICITLY set, and otherwise keep the cap this hub already
        enforces. Lowering a live federation's effective ceiling from 2000 to 50 unasked is
        the operator's call, not this function's — but a knob that does nothing is a trap
        either way.
        """
        raw = os.environ.get("AIMARKET_FEDERATION_OPEN_MAX_PENDING", "").strip()
        if raw:
            return config.federation_open_max_pending
        return config.federation_gossip_max_observed

    def _validate_hub_url(url: str) -> None:
        """Reject URLs pointing to internal/private hosts (SSRF prevention).

        Uses the same DNS-resolving safety check as the crawler — protects
        against DNS rebinding (evil.com → 192.168.x.x) and IPv4-mapped IPv6.
        """
        from aimarket_hub.crawler import _url_is_safe

        if not url:
            raise HTTPException(status_code=400, detail="hub_url is required")
        if any(c in url for c in "\r\n\t"):
            raise HTTPException(status_code=400, detail="Invalid characters in URL")
        if not _url_is_safe(url):
            raise HTTPException(
                status_code=400,
                detail=f"URL resolves to restricted network or invalid scheme: {url[:40]}...",
            )

    @router.post("/federation/announce")
    async def federation_announce(
        body: AnnounceRequest,
        authorization: str = Header(default=""),
    ):
        """Register a peer.

        Two doors, and they lead to different places. With an admin token the
        operator adds a peer directly, as before. Without one, anyone may announce
        a public address — and lands in quarantine: ``status=pending``,
        ``trusted=False``. A sandbox assay then runs in the background; a pass
        auto-admits. Open federation changes who may knock, never who is trusted
        on the knock itself.
        """
        # Public announcements are observations, not admission. Like a blockchain
        # address seen on the network, a syntactically safe public Hub is always made
        # visible in quarantine; credentials are still required to trust it.
        open_mode = not _is_admin_bearer(authorization)
        if open_mode:
            _validate_hub_url(body.hub_url)
            if body.well_known_url:
                _validate_hub_url(body.well_known_url)
            url = body.hub_url.rstrip("/")
            outcome = db.announce_peer(
                Peer(
                    url=url,
                    name=(body.hub_name or url)[:200],
                    capabilities_count=0,  # a stranger's self-reported count is not evidence
                    well_known_url=body.well_known_url or f"{url}/.well-known/ai-market.json",
                    # NO key from an unauthenticated caller. announce_peer refuses to overwrite a
                    # peer it already knows — but for a hub this instance has never met, storing
                    # the announcer's key would let a stranger ESTABLISH the pin for someone
                    # else's URL. The real hub's genuine key would then look like a takeover and
                    # the crawl would abort forever. A pin is set by an operator or by a crawl
                    # this hub performed itself, never by a claim arriving over HTTP.
                    public_key="",
                    depth=1,
                    discoverer="announce:open",
                ),
                max_pending=_pending_peer_cap(),
            )
            if outcome == "rejected_cap":
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Pending peer queue is full "
                        f"({_pending_peer_cap()}). The operator must "
                        f"approve or reject waiting peers before new ones are accepted."
                    ),
                )
            if outcome == "added" and config.federation_assay:
                assay_url = url

                async def _assay_knock() -> None:
                    try:
                        from aimarket_hub.federation_assay import run_assay
                        await run_assay(assay_url, config=config, db=db, signer=signer)
                    except Exception as exc:
                        logger.warning("post-announce assay of %s failed: %s", assay_url, exc)

                asyncio.create_task(_assay_knock())
            return {
                "acknowledged": True,
                "peer_added": outcome == "added",
                "status": "known" if outcome == "known" else "pending",
                "trusted": False,
                "assay_scheduled": bool(outcome == "added" and config.federation_assay),
                "note": _knock_promise(),
            }
        _require_admin(authorization)
        _validate_hub_url(body.hub_url)
        if body.well_known_url:
            _validate_hub_url(body.well_known_url)
        peer = Peer(
            url=body.hub_url,
            name=body.hub_name or body.hub_url,
            capabilities_count=body.capabilities_count,
            last_crawl=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            well_known_url=body.well_known_url,
            public_key=body.signer_public_key,
            depth=1,
            discoverer="announce",
        )
        db.upsert_peer(peer)
        return {"acknowledged": True, "peer_added": True}

    def _peer_declared(p) -> dict[str, Any]:
        """What the PEER says about itself — kept in its own envelope on purpose.

        Every field here was published by the peer and copied verbatim. Flattening it into
        the row would let a consumer read `id` as an answer when it is a claim, which is
        the mistake this envelope exists to make impossible to write by accident.
        """
        return {
            "description": p.description,
            "hub_version": p.hub_version,
            "id": p.declared_id,
            # Display only. Routing re-resolves the endpoint per invoke
            # (federation_transport), because a stored one is stale the moment a peer moves.
            "mcp_endpoint": p.mcp_endpoint,
        }

    def _knock_promise() -> str:
        """What THIS hub will actually do with a knock, in its current configuration.

        The two knock-facing notes used to be fixed strings promising that "a sandbox assay
        runs automatically" and "a pass indexes this hub without an operator click". Neither
        clause is true everywhere it was printed. Measured 2026-09-05:
        `hunt.modelmarket.dev` runs with AIMARKET_FEDERATION_ASSAY=0 and still told two
        knockers an assay was scheduled - both had waited with no verdict at all; and
        `hub.modelmarket.dev` told CHARON a pass would admit it, while CHARON sat at
        verdict=pass, quarantined, for a day, because that hub has no judge token.

        A federation runs on what hubs tell each other. A note that describes a different
        hub's configuration is not a nicety to fix later - it is the wrong answer to
        "what happens next", given to the one party who cannot check.
        """
        assay_on = bool(getattr(config, "federation_assay", True))
        auto = bool(getattr(config, "federation_auto_admit", True)
                    and getattr(config, "federation_judge_key", ""))
        if not assay_on:
            return ("Recorded in quarantine. Automatic assay is switched off on this hub, so "
                    "no verdict will be reached on its own: an operator must review and "
                    "Approve.")
        if auto:
            return ("Recorded in quarantine. A sandbox assay runs automatically; a pass "
                    "indexes this hub without an operator click. Fail or review stay pending "
                    "for the operator desk.")
        return ("Recorded in quarantine. A sandbox assay runs automatically, but this hub has "
                "no judge token, so a pass is a scorecard rather than an admission: an "
                "operator must Approve. Fail or review stay pending too.")

    def _publishable_pending_peers(db_: Any) -> list[Any]:
        """Pending peers this hub is willing to show the public.

        An ALIAS of a hub already active here is withheld. Without that, the observation
        layer is a loop: `http://108.165.32.182:9083` is the competing lab's own address by
        raw IP, we observed it, we published it, their hub read it back from us and published
        it again — so a plaintext address neither hub advertises for itself kept circulating
        through the federation and kept being adopted as a peer. Nothing self-declared it;
        gossip sustained it alone.

        ONE rule, three surfaces: `/.well-known` observed_hubs, the public pending list on
        `/federation/peers`, and the landing block those feed. Filtering only the first left
        the raw IP rendered on the public landing page with a red "assay failed" badge — the
        same second-entry-point mistake this guard exists to stop.

        Only crawled peers carry a key, so this withholds exactly the aliases we can prove
        and leaves every genuine unknown visible. An operator sees the full list.

        The FIRST alias to check is our own. A hub is not a peer of itself, so it is absent
        from its own peer table and `active_peer_with_public_key` can never match it - which
        is how `http://108.165.32.182:9083` came to sit on the competing lab's own landing
        page as an unapproved hub with a red "assay failed" badge, signed with the very key
        that page is served under. Measured 2026-09-05: the pending row's advertised key, the
        raw-IP endpoint's key and this hub's own `signer_public_key` were the same string.
        Its own key is the one identity a hub can never be wrong about, so it is checked
        first and without a database lookup.
        """
        own_key = ""
        try:
            own_key = (signer.public_key_b64 or "").strip()
        except Exception:  # pragma: no cover - a hub with no signer has no self to confuse
            own_key = ""
        out = []
        for peer in db_.list_pending_peers():
            key = (getattr(peer, "public_key", "") or "").strip()
            advertised = (getattr(peer, "advertised_public_key", "") or "").strip()
            if own_key and own_key in (key, advertised):
                continue
            if key and db_.active_peer_with_public_key(key, exclude_url=peer.url):
                continue
            out.append(peer)
        return out

    @router.get("/federation/peers")
    async def list_peers(authorization: str = Header(default="")):
        peers = db.list_peers()
        # The operator sees aliases; the public does not.
        pending_rows = (
            db.list_pending_peers()
            if _is_admin_bearer(authorization)
            else _publishable_pending_peers(db)
        )
        from aimarket_hub.peer_identity import identity_for

        return {
            "peers": [
                {
                    "url": p.url,
                    "name": p.name,
                    "capabilities_count": p.capabilities_count,
                    "last_crawl": p.last_crawl,
                    "trust_score": p.trust_score,
                    "depth": p.depth,
                    "categories": p.categories,
                    "well_known_url": p.well_known_url,
                    "trusted": p.trusted,
                    "public_key": p.public_key,
                    "status": p.status,
                    "pin_reject_reason": p.pin_reject_reason,
                    "advertised_public_key": p.advertised_public_key,
                    # Which of OUR nodes this is, from the operator's pin — "" for every
                    # peer they never pinned, which is every stranger. A consumer that
                    # folds a peer onto a local node reads THIS, never `declared.id`.
                    **identity_for(p, config),
                    "declared": _peer_declared(p),
                }
                for p in peers
            ],
            "count": len(peers),
            # Peers that knocked but were never approved. Kept in their own key so
            # every existing consumer of "peers" keeps its exact meaning — approved
            # hubs — and nothing starts treating a stranger as a member of the
            # federation by accident.
            "pending": [
                {
                    "url": p.url,
                    "name": p.name,
                    "first_seen": p.first_seen,
                    "last_crawl": p.last_crawl,
                    "discoverer": p.discoverer,
                    "well_known_url": p.well_known_url,
                    "categories": p.categories,
                    "trusted": False,
                    "status": "pending",
                    "preview_capabilities": db.count_preview_capabilities(p.url),
                    "assay_verdict": (db.get_peer_assay(p.url) or {}).get("verdict"),
                    **identity_for(p, config),
                    "declared": _peer_declared(p),
                }
                for p in pending_rows
            ],
            "pending_count": len(pending_rows),
            "open_federation": bool(config.federation_open),
            "observation_gossip": True,
        }

    @router.get("/federation/preview")
    async def federation_preview(url: str = "", limit: int = 200):
        """What a PENDING peer says it offers — quarantined, never live.

        These rows come from ``peer_preview_capabilities``, a table that search,
        routing, invoke and the published manifest do not read. Showing them lets an
        operator judge a stranger by its catalogue instead of by its URL, without
        that catalogue becoming reachable. Every row is returned with
        ``quarantined: true``; a client that renders them next to real capabilities
        without saying so is misrepresenting this hub.
        """
        limit = max(1, min(int(limit or 200), 500))
        return {
            "capabilities": db.list_preview_capabilities(url.strip().rstrip("/"), limit=limit),
            "count": db.count_preview_capabilities(url.strip().rstrip("/")),
            "quarantined": True,
            "note": (
                "Preview only. These capabilities are not indexed, not searchable "
                "and not invocable until a sandbox assay passes or an operator "
                "approves their peer."
            ),
        }

    def _assay_payload(dossier: dict | None, url: str = "") -> dict:
        if not dossier:
            return {
                "url": url,
                "verdict": None,
                "trusted": False,
                "indexed": False,
                "auto_promoted": False,
                "quarantined": True,
                "checks": [],
                "sandbox": {},
                "note": (
                    "No assay yet. " + _knock_promise()
                    + " POST /ai-market/v2/federation/assay (admin) runs one now."
                ),
            }
        dossier = dict(dossier)
        target = str(dossier.get("url") or url or "").strip().rstrip("/")
        peer = db.get_peer(target) if target else None
        trusted = bool(peer and peer.trusted)
        sandbox = dossier.get("sandbox") if isinstance(dossier.get("sandbox"), dict) else {}
        dossier["trusted"] = trusted
        dossier["indexed"] = trusted
        dossier["quarantined"] = not trusted
        dossier["auto_promoted"] = bool(sandbox.get("auto_promoted") or dossier.get("auto_promoted"))
        return dossier

    @router.get("/federation/assay")
    async def federation_assay_get(url: str = ""):
        """Last post-quarantine assay dossier — public, like preview.

        A ``pass`` with auto-admit on (default) sets ``trusted`` and the next
        crawl indexes. Names and descriptions are never scored.
        """
        target = url.strip().rstrip("/")
        return _assay_payload(db.get_peer_assay(target) if target else None, target)

    @router.post("/federation/assay")
    async def federation_assay_run(body: dict, authorization: str = Header(default="")):
        """Run the sandbox assay on a pending (or any public) hub URL.

        Admin-only: it makes outbound fetches. A pass auto-admits when
        ``AIMARKET_FEDERATION_AUTO_ADMIT`` is on (default).
        """
        _require_admin(authorization)
        if not config.federation_assay:
            raise HTTPException(status_code=503, detail="federation assay is disabled")
        target = (body.get("url") or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="url is required")
        from aimarket_hub.federation_assay import run_assay
        dossier = await run_assay(target, config=config, db=db, signer=signer)
        return _assay_payload(dossier, dossier.get("url") or target)

    @router.get("/federation/inbound")
    async def federation_inbound(authorization: str = Header(default=""), limit: int = 100):
        """Hubs that have crawled THIS hub, by their own self-declared URL.

        Admin-only: it is a list of who reads us, which is the operator's business
        and nobody else's. No client IPs are stored — only the hub URL a crawler
        volunteers in ``X-AIMarket-Crawler`` and its User-Agent.
        """
        _require_admin(authorization)
        return {
            "inbound": db.list_inbound_federation(limit=max(1, min(int(limit or 100), 500))),
        }

    @router.post("/federation/crawl")
    async def trigger_crawl(authorization: str = Header(default="")):
        _require_admin(authorization)
        from aimarket_hub.crawler import Crawler
        crawler = Crawler(
            config=config, db=db, signer=signer, trust_scorer=trust_scorer,
            slash_registry=slash_registry,
        )
        try:
            stats = await crawler.crawl(clear_first=False)
        finally:
            await crawler.close()
        return {"status": "complete", "stats": stats}

    @router.post("/federation/peers/approve")
    async def approve_peer(body: dict, authorization: str = Header(default="")):
        """Operator approval (anti-TOFU): mark a discovered peer trusted so its manifests get
        indexed on the next crawl. Admin-only. Body: {url, trusted?: bool (default true)}."""
        _require_admin(authorization)
        url = (body.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        trusted = bool(body.get("trusted", True))
        if trusted and config.federation_assay_require:
            assay = db.get_peer_assay(url)
            if not assay or assay.get("verdict") != "pass":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "assay_required: last assay must verdict pass before approve "
                        "(AIMARKET_FEDERATION_ASSAY_REQUIRE=1). "
                        "POST /ai-market/v2/federation/assay first. An LLM verdict is not accepted."
                    ),
                )
        if trusted:
            # The auto-admit path checks this in `admit_peer`; this door did not, and it is
            # the actual click — the competing-lab hub sat ACTIVE at
            # http://108.165.32.182:9083 and PENDING as https://hub.modelmarket.dev, one
            # Approve away from being crawled and re-exported twice under one identity.
            existing = db.get_peer(url)
            key = (getattr(existing, "public_key", "") or "").strip() if existing else ""
            twin = db.active_peer_with_public_key(key, exclude_url=url) if key else None
            if twin:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"duplicate_identity: this signing key is already active at {twin}. "
                        "One hub, one peer row — admitting both would index and re-export the "
                        f"same catalogue twice. Remove {twin} first if this address should "
                        "replace it (DELETE /ai-market/v2/federation/peers?url=...)."
                    ),
                )
        if not db.set_peer_trusted(url, trusted):
            raise HTTPException(status_code=404, detail=f"peer not found: {url}")
        promoted = False
        cleared = 0
        if trusted:
            # Leaving the peer at status="pending" would keep it out of the peer
            # list it now belongs in, and leaving preview rows behind would show a
            # stale quarantined copy beside the real catalogue the next crawl indexes.
            promoted = db.promote_pending_peer(url)
            cleared = db.clear_preview_capabilities(url)
        return {
            "url": url,
            "trusted": trusted,
            "status": "updated",
            "promoted_from_pending": promoted,
            "preview_rows_cleared": cleared,
            "note": (
                "Manifests are indexed on the next crawl — POST /federation/crawl to "
                "do it now." if trusted else "Peer left untrusted; nothing is indexed."
            ),
        }

    @router.delete("/federation/peers")
    async def delete_peer(url: str, authorization: str = Header(default="")):
        """Reject a peer outright: remove the row and its preview catalogue.

        The pending queue needs an exit as well as an entrance. Without this an operator
        could approve waiting peers but never dismiss them, so an unauthenticated party
        could fill the queue to its cap and hold it shut against everyone else.
        """
        _require_admin(authorization)
        url = (url or "").strip().rstrip("/")
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        if not db.delete_peer(url):
            raise HTTPException(status_code=404, detail=f"peer not found: {url}")
        return {"url": url, "deleted": True}

    @router.post("/federation/peers/repin")
    async def repin_peer(body: dict, authorization: str = Header(default="")):
        """Operator re-pin after a legitimate peer key rotation — classical, PQ, or both.

        Fail-closed crawler rejects ``public key changed`` forever unless an admin
        updates the pin here. Seed pins (``AIMARKET_SEED_PUBKEYS``) only apply on
        first contact — they do not rotate an existing DB pin.

        Body: ``{url, public_key?, pq_public_key?, trusted?: true,
        previous_public_key?: str, previous_pq_public_key?: str, crawl?: true}``.
        At least one of ``public_key`` / ``pq_public_key`` is required — a peer can rotate its
        ML-DSA key alone, and requiring the classical one for that would invite an operator to
        paste the current value back in, which is a chance to paste the wrong one. The
        ``previous_*`` fields are optimistic concurrency (must match the current pin when set —
        keep the old value for rollback).
        """
        _require_admin(authorization)
        url = (body.get("url") or "").strip()
        public_key = (body.get("public_key") or "").strip()
        pq_public_key = (body.get("pq_public_key") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        if not public_key and not pq_public_key:
            raise HTTPException(
                status_code=400,
                detail="public_key or pq_public_key is required",
            )
        trusted_raw = body.get("trusted", True)
        trusted = None if trusted_raw is None else bool(trusted_raw)
        previous = body.get("previous_public_key")
        if previous is not None:
            previous = str(previous).strip()
        previous_pq = body.get("previous_pq_public_key")
        if previous_pq is not None:
            previous_pq = str(previous_pq).strip()
        try:
            result = db.repin_peer_public_key(
                url,
                public_key or None,
                trusted=trusted,
                previous_public_key=previous,
                pq_public_key=pq_public_key or None,
                previous_pq_public_key=previous_pq,
            )
        except ValueError as exc:
            detail = str(exc)
            code = 404 if detail.startswith("peer not found") else 400
            raise HTTPException(status_code=code, detail=detail) from exc

        crawl_stats = None
        if bool(body.get("crawl", True)):
            from aimarket_hub.crawler import Crawler
            crawler = Crawler(
                config=config, db=db, signer=signer, trust_scorer=trust_scorer,
                slash_registry=slash_registry,
            )
            try:
                crawl_stats = await crawler.crawl(clear_first=False)
            finally:
                await crawler.close()
        return {"status": "repinned", "peer": result, "crawl": crawl_stats}

    # ── Reputation ──────────────────────────────────────────────

    # Federated slash log (F2 transport). Declared BEFORE the /reputation/{hub_url:path}
    # catch-all so the exact paths match first.
    @router.get("/reputation/slashes")
    async def get_slashes():
        """This hub's signed slash attestations — peers pull and verify these."""
        return {"hub_url": config.hub_url, "slashes": slash_registry.export()}

    @router.get("/reputation/slashes/by-provider/{provider:path}")
    async def get_slash_signal(provider: str):
        """Union view of slashes against a provider across local + ingested peer logs."""
        sig = slash_registry.slash_signal(provider)
        sig["federated_penalty"] = slash_registry.federated_penalty(provider)
        # Local per-event audit trail + the calibration in force (cool-down / caps),
        # so a slashed provider can see exactly what fired and what the ceiling is.
        sig["local_events"] = db.supply_slash_events_recent(provider, limit=20)
        pol = supply_security.policy
        sig["policy"] = {
            "slash_failure_threshold": pol.slash_failure_threshold,
            "slash_failure_window_s": pol.slash_failure_window_s,
            "verified_fail_threshold": pol.verified_fail_threshold,
            "verified_fail_window_s": pol.verified_fail_window_s,
            "slash_cooldown_s": pol.slash_cooldown_s,
            "slash_daily_cap_usd": pol.slash_daily_cap_usd,
        }
        return sig

    @router.get("/reputation/{hub_url:path}")
    async def get_reputation(hub_url: str):
        score = trust_scorer.compute_score(hub_url)
        details = trust_scorer.score_details(hub_url)
        events = db.reputation_events_for(hub_url, limit=20)
        return {
            "hub_url": hub_url,
            "trust_score": score,
            "details": details,
            "recent_events": [
                {"type": e.event_type, "timestamp": e.timestamp,
                 "price_usd": e.price_usd, "latency_ms": e.latency_ms}
                for e in events
            ],
        }

    @router.post("/reputation/events")
    async def submit_reputation_events(body: ReputationEventsRequest):
        from aimarket_hub.models import ReputationEvent
        rejected = 0
        for ev in body.events:
            provider = ev.get("provider_hub", "")
            sig = ev.get("signature")
            consumer = ev.get("consumer_hub", "")

            # Require non-empty provider and consumer hubs
            if not provider or not consumer:
                rejected += 1
                continue

            # Require signature — prevents anonymous reputation poisoning
            if not sig or not isinstance(sig, dict) or not sig.get("value"):
                rejected += 1
                continue

            # Verify signature against known peer public key
            peer = db.get_peer(consumer)
            if not peer or not peer.public_key:
                rejected += 1
                continue

            # Build canonical form matching the event's signed fields
            canonical = (
                f"type:{ev.get('type','')}"
                f"|provider_hub:{provider}"
                f"|timestamp:{ev.get('timestamp','')}"
                f"|price_usd:{ev.get('price_usd',0)}"
                f"|latency_ms:{ev.get('latency_ms',0)}"
            )
            if not signer.verify(peer.public_key, sig["value"], canonical):
                rejected += 1
                continue

            db.record_reputation_event(ReputationEvent(
                event_type=ev.get("type", "unknown"),
                provider_hub=provider,
                capability_id=ev.get("capability_id"),
                timestamp=ev.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                price_usd=ev.get("price_usd", 0),
                latency_ms=ev.get("latency_ms", 0),
                consumer_hub=consumer,
                signature=json.dumps(sig),
            ))
        return {"received": len(body.events) - rejected, "rejected": rejected}

    _dispute_rate: dict[str, list[float]] = {}
    _DISPUTE_RATE_MAX = 6
    _DISPUTE_RATE_WINDOW_S = 60.0
    #: Serialized ``evidence`` bytes accepted per dispute. The field is NOT covered by
    #: Dispute.canonical(), so it costs the submitter no extra signature to pad it to the
    #: edge's body limit — the one lever that made the unbounded ledger a cheap memory DoS.
    _DISPUTE_EVIDENCE_MAX_BYTES = 16_384

    def _check_dispute_rate(client_ip: str) -> bool:
        key = (client_ip or "").strip() or "\x00no-address"
        now = time.time()
        window = now - _DISPUTE_RATE_WINDOW_S
        bucket = [t for t in _dispute_rate.get(key, []) if t > window]
        if len(bucket) >= _DISPUTE_RATE_MAX:
            _dispute_rate[key] = bucket
            return False
        bucket.append(now)
        _dispute_rate[key] = bucket
        if len(_dispute_rate) > 4096:
            _trim_rate_buckets(_dispute_rate, window, 4096, keep=key)
        return True

    @router.post("/reputation/disputes")
    async def submit_dispute(
        body: dict,
        request: Request,
        authorization: str = Header(default=""),
    ):
        """File a dispute: consumer-signed (portable PoM) or hub-admin (legacy operator path).

        The consumer-signed branch is UNAUTHENTICATED by design (that is what makes the
        proof portable), so it carries its own limits: a per-address rate cap, a bound on
        the unsigned ``evidence`` blob, and a duplicate-id refusal in the oracle. Without
        them the process-lifetime dispute list was remote-controlled memory.
        """
        from aimarket_hub.reputation_oracle import Dispute

        sig = body.get("signature")
        consumer_pubkey = str(body.get("consumer_pubkey") or "").strip()

        if sig and consumer_pubkey:
            if not _check_dispute_rate(_client_address(request, _TRUSTED_PROXIES)):
                return JSONResponse(status_code=429, content={
                    "success": False, "error": "rate_limited",
                    "detail": (
                        f"consumer-signed disputes limited to {_DISPUTE_RATE_MAX} "
                        "per minute per client address"
                    ),
                    "protocol_version": "v2",
                })
            required = ("dispute_id", "invocation_id", "provider_hub", "consumer_hub")
            missing = [k for k in required if not str(body.get(k) or "").strip()]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "missing_fields", "fields": missing},
                )
            evidence = body.get("evidence") or {}
            try:
                evidence_bytes = len(json.dumps(evidence, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="evidence must be JSON-serializable"
                ) from None
            if evidence_bytes > _DISPUTE_EVIDENCE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"evidence is {evidence_bytes} bytes; the limit is "
                        f"{_DISPUTE_EVIDENCE_MAX_BYTES} (it is not covered by the dispute "
                        "signature, so it cannot be trusted at any size)"
                    ),
                )
            dispute = Dispute(
                dispute_id=str(body["dispute_id"]),
                invocation_id=str(body["invocation_id"]),
                provider_hub=str(body["provider_hub"]),
                consumer_hub=str(body["consumer_hub"]),
                reason=str(body.get("reason") or ""),
                evidence=body.get("evidence") or {},
                requested_slash_pct=float(body.get("requested_slash_pct", 0)),
                timestamp=str(body.get("timestamp") or ""),
                signature=str(sig),
                consumer_pubkey=consumer_pubkey,
            )
            try:
                recorded = reputation_oracle.submit_signed_dispute(dispute)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "dispute_id": recorded.dispute_id,
                "status": "recorded",
                "mode": "consumer_signed",
            }

        _require_admin(authorization)
        for field in ("invocation_id", "provider_hub", "consumer_hub", "reason"):
            if not str(body.get(field) or "").strip():
                raise HTTPException(status_code=400, detail=f"{field} is required")
        dispute = reputation_oracle.file_dispute(
            invocation_id=str(body["invocation_id"]),
            provider_hub=str(body["provider_hub"]),
            consumer_hub=str(body["consumer_hub"]),
            reason=str(body["reason"]),
            requested_slash_pct=float(body.get("requested_slash_pct", 0.1)),
            evidence=body.get("evidence"),
        )
        return {
            "dispute_id": dispute.dispute_id,
            "status": "recorded",
            "mode": "hub_operator",
        }

    @router.post("/reputation/disputes/{dispute_id}/resolve")
    async def resolve_dispute(
        dispute_id: str,
        body: dict,
        authorization: str = Header(default=""),
    ):
        """Resolve a dispute (slash bond). Requires m-of-n signatures when quorum is configured."""
        _require_admin(authorization)
        slash_pct = float(body.get("slash_pct", 0))
        ruling_note = str(body.get("ruling_note", "") or "")
        signatures = body.get("signatures") or []
        result = reputation_oracle.resolve_dispute(
            dispute_id,
            slash_pct=slash_pct,
            ruling_note=ruling_note,
            signatures=signatures,
        )
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result)
        return result

    @router.get("/reputation/oracle/quorum")
    async def oracle_quorum_status():
        """Public quorum config (keys only, no secrets)."""
        q = reputation_oracle.quorum
        if q is None:
            return {"mode": "single_operator", "authorities": 0, "threshold": 1}
        return {
            "mode": "m_of_n",
            "authorities": len(q.authorities),
            "threshold": q.threshold,
        }

    @router.get("/health")
    async def hub_health():
        return {
            "status": "ok",
            "service": "aimarket-hub",
            "version": config.hub_version,
            "uptime_seconds": int(time.time() - _HUB_STARTED_AT),
        }

    def _public_consumer_label(raw: str) -> str:
        """Pseudonymize a consumer label before it goes out on a PUBLIC feed.

        ``_resolve_invoke_consumer_hub`` labels a paid invoke ``channel:<channel_id>`` and a
        trial ``sandbox:<id>``. Both are live credentials-adjacent identifiers: the channel
        id is what /channel/close and /channel/status take, and publishing it with a
        timestamp handed anyone the ids of currently-paying buyers. The prefix is what the
        live feed is actually for (paid vs trial vs anonymous), so keep that and replace the
        id with a stable digest — same grouping key across events, not a usable id.
        """
        for prefix in ("channel:", "sandbox:"):
            if raw.startswith(prefix):
                ident = raw[len(prefix):]
                if not ident:
                    return raw
                return prefix + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
        return raw

    def _operator_self_labels(hub_url: str) -> tuple[str, ...]:
        """Consumer labels that mean "us", not a buyer.

        One list, used by both the page-scoped classification and the lifetime totals —
        when the two were written out separately they could disagree about what counts
        as our own traffic, which is the whole point of the metric.
        """
        labels = [OPERATOR_SELF_CONSUMER, "local"]
        if hub_url:
            labels.append(hub_url)
        return tuple(labels)

    def _is_operator_self(consumer_hub: str, self_labels: tuple[str, ...]) -> bool:
        return str(consumer_hub or "").strip().rstrip("/") in self_labels

    # ── Credit accounts: the self-serve half of the credits rail ────────────────
    # The blocker this replaces: the only buyer identity the hub had was
    # AIMARKET_PUBLISHER_TOKENS / AIMARKET_AGENT_TOKENS, parsed out of os.environ once at
    # app construction. One new customer meant editing .env and restarting the hub, which
    # is not an onboarding path, it is a maintenance window per signup.

    @router.post("/accounts")
    async def create_credit_account(
        request: Request,
        body: dict | None = None,
        authorization: str = Header(default=""),
    ):
        ledger = hub_credits
        if not credits.enabled() or ledger is None:
            raise HTTPException(
                status_code=503,
                detail="credit accounts are off on this hub (AIMARKET_CREDITS_ENABLED=0)",
            )
        is_admin = _is_admin_bearer(authorization)
        if not credits.signup_open() and not is_admin:
            raise HTTPException(
                status_code=403,
                detail="this hub issues credit accounts by hand — ask the operator for a key",
            )
        client_ip = _client_address(request, _TRUSTED_PROXIES) or "anonymous"
        if not is_admin and not _signup_allowed(client_ip):
            raise HTTPException(
                status_code=429,
                detail="too many accounts from this address — try again later",
            )
        label = str((body or {}).get("label") or "")[:120]
        # A self-minted key gets the operator's advertised free grant; an operator-minted
        # one can be opened with any starting balance, because at that point the operator
        # has already decided what it is worth.
        grant = credits.signup_grant_usd()
        grant_note = ""
        if is_admin:
            with contextlib.suppress(TypeError, ValueError):
                grant = max(0.0, float((body or {}).get("grant_usd", grant)))
        elif "grant_usd" in (body or {}):
            # Refuse rather than ignore. Silently overriding a privileged field is
            # how a caller comes to believe it is the operator: a service holding
            # the wrong token asked for grant_usd=0, got the signup grant, and
            # only discovered it was not admin at the first call that moves money
            # — which is the one call that must not be the first to find out.
            raise HTTPException(
                status_code=403,
                detail="grant_usd is operator-only; this request is not admin-authenticated",
            )
        elif grant > 0:
            # A farmed grant is bounded by a budget, not by telling people from
            # bots — which is not a thing this hub can do. When the budget is
            # spent the account is still opened, just at zero, so a real buyer
            # can still top up and work.
            allowed, reason = ledger.grant_within_budget(grant)
            if not allowed:
                grant, grant_note = 0.0, reason
        account = ledger.create_account(
            label=label,
            grant_usd=grant,
            grant_note=credits.OPERATOR_GRANT_NOTE if is_admin else credits.SIGNUP_GRANT_NOTE,
        )
        return {
            "success": True,
            **account,
            **({"grant_note": grant_note} if grant_note else {}),
            # Said once, here, because it is the only time the key exists in plaintext.
            "note": "store api_key now — only its hash is kept, it cannot be shown again",
            "usage": "send it as the X-API-Key header on /ai-market/v2/invoke",
            "protocol_version": "v2",
        }

    @router.get("/account")
    async def read_credit_account(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        ledger = hub_credits
        if not credits.enabled() or ledger is None:
            raise HTTPException(status_code=503, detail="credit accounts are off on this hub")
        account_id = ledger.resolve(x_api_key or "")
        if not account_id:
            raise HTTPException(status_code=401, detail="unknown or disabled X-API-Key")
        return {"success": True, **(ledger.account(account_id) or {}), "protocol_version": "v2"}

    @router.get("/account/ledger")
    async def read_credit_ledger(
        limit: int = 50,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        """Every movement on the caller's own account.

        A prepaid balance is the operator holding the buyer's money; the buyer gets to see
        what happened to it without asking.
        """
        ledger = hub_credits
        if not credits.enabled() or ledger is None:
            raise HTTPException(status_code=503, detail="credit accounts are off on this hub")
        account_id = ledger.resolve(x_api_key or "")
        if not account_id:
            raise HTTPException(status_code=401, detail="unknown or disabled X-API-Key")
        return {
            "success": True,
            "account_id": account_id,
            "entries": ledger.recent(account_id, limit=limit),
            "protocol_version": "v2",
        }

    @router.post("/accounts/{account_id}/credit")
    async def credit_account(
        account_id: str, body: dict, authorization: str = Header(default=""),
    ):
        """Top up an account — OPERATOR ONLY.

        Deliberately the only way money enters the rail. Whatever the operator uses to get
        paid (a checkout, an invoice, a stablecoin transfer they watched land) calls this
        once it has the money; the hub does not pretend to have collected anything itself.
        """
        _require_admin(authorization)
        ledger = hub_credits
        if not credits.enabled() or ledger is None:
            raise HTTPException(status_code=503, detail="credit accounts are off on this hub")
        try:
            amount = float(body.get("amount_usd"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="amount_usd must be a number")
        result = ledger.grant(
            account_id,
            amount,
            note=str(body.get("note") or "")[:200],
            reference=str(body.get("reference") or "")[:200],
        )
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return {"success": True, **result, "protocol_version": "v2"}

    @router.post("/accounts/{account_id}/status")
    async def set_credit_account_status(
        account_id: str, body: dict, authorization: str = Header(default=""),
    ):
        _require_admin(authorization)
        ledger = hub_credits
        if not credits.enabled() or ledger is None:
            raise HTTPException(status_code=503, detail="credit accounts are off on this hub")
        result = ledger.set_status(account_id, str(body.get("status") or ""))
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return {"success": True, **result, "protocol_version": "v2"}

    @router.get("/stats/live")
    async def live_stats(limit: int = 50):
        supply_chain_admission.schedule_refresh()
        stats = db.recent_stats(limit=min(limit, 200))
        summary = db.stats_summary()
        public_caps = [
            c for c in db.list_capabilities(limit=1000)
            if capability_is_fulfillable(c) and capability_is_publicly_offerable(c)
        ]
        summary["capabilities_count"] = len(public_caps)
        summary["offerable_capabilities_count"] = len(public_caps)
        summary["federated_capabilities_count"] = sum(
            1 for c in public_caps if (c.source_hub or "local") != "local"
        )
        ch = channel_stats()
        summary["open_channels"] = ch.get("open_channels", 0)
        # Volume that has FINISHED flowing through channels = settled + expired.
        # Reading channels.stats()["settled_volume_usd"] here counted only rows in
        # state 'settled', so every dollar spent through a channel that timed out
        # instead of being closed vanished from the live metric — which was the
        # original defect, re-introduced one layer up when the ledger was fixed.
        # The name stays for consumers; the breakdown below makes it unambiguous.
        summary["settled_volume_usd"] = ch.get("closed_volume_usd", 0)
        summary["settled_only_volume_usd"] = ch.get("settled_volume_usd", 0)
        summary["expired_volume_usd"] = ch.get("expired_volume_usd", 0)
        # Money the operator owes depositors but has not paid out (ACCT-001). No
        # depositor is named here — the detail view is admin-only — but the total is
        # public on purpose: it is the solvency number.
        summary["outstanding_obligations_usd"] = ch.get("outstanding_obligations_usd", 0)
        summary["outstanding_obligations"] = ch.get("outstanding_obligations", 0)
        hub_url = (config.hub_url or "").rstrip("/")
        self_labels = _operator_self_labels(hub_url)
        events = []
        operator_self_events = 0
        external_events = 0
        for row in stats:
            ev = dict(row)
            ch_val = str(ev.get("consumer_hub") or "")
            if _is_operator_self(ch_val, self_labels):
                ev["traffic_class"] = "operator_self"
                operator_self_events += 1
            else:
                ev["traffic_class"] = "external"
                external_events += 1
            ev["consumer_hub"] = _public_consumer_label(ch_val)
            events.append(ev)
        summary["operator_self_events_in_page"] = operator_self_events
        summary["external_events_in_page"] = external_events
        # Same split over the whole table, so the public card can print a breakdown
        # that actually adds up to `total_invocations` instead of one page of it.
        lifetime_split = db.consumer_traffic_totals(self_labels)
        summary["external_invocations"] = lifetime_split["external"]
        summary["operator_self_invocations"] = lifetime_split["operator_self"]
        # The credits rail's own P&L, published for the same reason the channel ledger's
        # obligations are: `credits_earned_usd` is what this node has actually taken, and
        # `outstanding_credit_usd` is prepaid money it is holding for somebody else.
        if credits.enabled():
            _cl = hub_credits
            if _cl is not None:
                summary["credits"] = _cl.stats()
        if x402.accept_enabled():
            # Signed but not collected. Kept out of `credits` on purpose: one is cash the
            # operator holds, the other is a promise they have not yet submitted on chain.
            summary["x402"] = hub_x402.stats()
        summary["hub_url"] = hub_url
        summary["supply_chain_admission"] = supply_chain_admission.public_summary()
        return {
            "events": events,
            "summary": summary,
            "supply_chain_audits": db.supply_audits_recent(limit=min(limit, 50)),
            "protocol_version": "v2",
        }

    _violation_rate: dict[str, list[float]] = {}
    _VIOLATION_RATE_MAX = 12
    _VIOLATION_RATE_WINDOW_S = 60.0

    def _check_violation_rate(client_ip: str) -> bool:
        key = (client_ip or "").strip() or "\x00no-address"
        now = time.time()
        window = now - _VIOLATION_RATE_WINDOW_S
        bucket = [t for t in _violation_rate.get(key, []) if t > window]
        if len(bucket) >= _VIOLATION_RATE_MAX:
            _violation_rate[key] = bucket
            return False
        bucket.append(now)
        _violation_rate[key] = bucket
        if len(_violation_rate) > 4096:
            _trim_rate_buckets(_violation_rate, window, 4096, keep=key)
        return True

    @router.post("/supply/permission-violation")
    async def supply_permission_violation(
        body: PermissionViolationRequest,
        request: Request,
        authorization: str = Header(default=""),
    ):
        """Report, with a signature, that a capability broke its own declaration.

        This is the runtime half of publish-time admission: a declared permission
        is only a claim until somebody who watched the capability run can
        contradict it in a way a third party can re-check. The signature is
        verified against the declaration on record, so this endpoint cannot be
        used to invent a declaration.

        The signature proves the reporter committed to the declaration digest on
        record — not that it ever ran the capability, and ``reporter_pubkey`` is
        free to mint. So the slash threshold counts only reporters whose identity
        this hub AUTHENTICATED: present ``Authorization: Bearer <token>`` for the
        ``consumer_id`` you claim (AIMARKET_AGENT_TOKENS / AIMARKET_PUBLISHER_TOKENS,
        or the admin token). An unauthenticated report is still recorded and still
        returned in ``distinct_reporters``, but it cannot take a publisher's stake.
        """
        if not _check_violation_rate(_client_address(request, _TRUSTED_PROXIES)):
            return JSONResponse(status_code=429, content={
                "success": False, "error": "rate_limited",
                "detail": (
                    f"permission-violation reports limited to {_VIOLATION_RATE_MAX} "
                    "per minute per client address"
                ),
                "protocol_version": "v2",
            })
        # A Bearer token, when offered, must actually authorize the claimed consumer_id
        # (raises 401/403/409 otherwise — see _require_subject_credential). No token is
        # not an error: the report is accepted as an unbound observation.
        reporter_bound = False
        if authorization.startswith("Bearer ") and body.consumer_id.strip():
            _require_subject_credential(
                authorization, body.consumer_id.strip(), (_NS_PUBLISHER, _NS_AGENT)
            )
            reporter_bound = True
        outcome = supply_chain_admission.record_permission_violation(
            product_id=body.product_id,
            capability_id=body.capability_id,
            permission=body.permission,
            reporter_pubkey=body.reporter_pubkey,
            signature=body.signature,
            consumer_id=body.consumer_id,
            consumer_bound=reporter_bound,
        )
        if not outcome.get("accepted"):
            return JSONResponse(status_code=400, content={
                "success": False,
                "error": outcome.get("reason", "rejected"),
                "protocol_version": "v2",
            })
        ladder = supply_security.record_permission_violation(
            publisher_id=outcome["publisher_id"],
            product_id=body.product_id,
            capability_id=body.capability_id,
            permission=outcome["permission"],
            # The AUTHENTICATED count, not the key count — see the docstring above.
            distinct_reporters=outcome["bound_reporters"],
            threshold=outcome["threshold"],
            consumer_id=body.consumer_id if reporter_bound else "",
            reporter_bound=reporter_bound,
        )
        return {
            "success": True,
            "recorded": not outcome["duplicate"],
            "permission": outcome["permission"],
            "distinct_reporters": outcome["distinct_reporters"],
            "bound_reporters": outcome["bound_reporters"],
            "reporter_authenticated": outcome["reporter_bound"],
            "threshold": outcome["threshold"],
            "contradicted": outcome["contradicted"],
            "slashed": ladder.get("slashed", False),
            "protocol_version": "v2",
        }

    @router.get("/supply/audits")
    async def supply_audits(limit: int = 20):
        """Public, dossier-free admission receipts for Monitor and operators."""
        supply_chain_admission.schedule_refresh()
        return {
            "summary": supply_chain_admission.public_summary(),
            "audits": db.supply_audits_recent(limit=min(max(limit, 1), 50)),
            "protocol_version": "v2",
        }

    # ── ACEX Capital (Pulse Terminal) ───────────────────────────

    from aimarket_hub.capital_pricing import hub_capital_pricing

    @router.get("/capital/pricing")
    async def capital_pricing(
        chain: str = "any",
        listing_id: str | None = None,
        limit: int = 50,
    ):
        """Capability revenue indices for Pulse Terminal (ACEX Phase 2)."""
        return hub_capital_pricing(db, chain=chain, listing_id=listing_id, limit=min(limit, 200))

    capital_alias = APIRouter(prefix="/api/v2/capital", tags=["acex-capital"])

    @capital_alias.get("/pricing")
    async def capital_pricing_alias(
        chain: str = "any",
        listing_id: str | None = None,
        limit: int = 50,
    ):
        return hub_capital_pricing(db, chain=chain, listing_id=listing_id, limit=min(limit, 200))

    # ── ACEX Agent IPO (factory → hub → ACEX leg) ────────────────
    # Float a product as CapShares, inspect the cap table, and distribute the
    # invoke-revenue pool to holders. Mounted on both the hub prefix
    # (/ai-market/v2/capital/...) and the Pulse Terminal alias (/api/v2/capital/...).
    from aimarket_hub import acex_ipo

    def _register_capital_ipo(r: APIRouter, base: str) -> None:
        @r.post(base + "/ipo")
        async def capital_ipo(body: IpoRequest, authorization: str = Header(default="")):
            _require_admin(authorization)  # minting CapShares is privileged
            res = acex_ipo.float_product(
                body.product_id, name=body.name, symbol=body.symbol,
                max_supply=body.max_supply, treasury=body.treasury,
                audit_score_bps=body.audit_score_bps, revenue_share_bps=body.revenue_share_bps,
            )
            code = 400 if res.get("error") else 200
            return JSONResponse(status_code=code, content={**res, "protocol_version": "v2"})

        @r.get(base + "/listings")
        async def capital_listings(limit: int = 50):
            return {"listings": acex_ipo.list_listings(limit=min(limit, 500)),
                    "protocol_version": "v2"}

        @r.get(base + "/listings/{listing_id}")
        async def capital_listing(listing_id: str):
            res = acex_ipo.listing_state(listing_id)
            code = 404 if res.get("error") else 200
            return JSONResponse(status_code=code, content={**res, "protocol_version": "v2"})

        @r.post(base + "/listings/{listing_id}/distribute")
        async def capital_distribute(listing_id: str, authorization: str = Header(default="")):
            _require_admin(authorization)
            res = acex_ipo.distribute(listing_id)
            code = 404 if res.get("error") == "unknown_listing" else (400 if res.get("error") else 200)
            return JSONResponse(status_code=code, content={**res, "protocol_version": "v2"})

        @r.get(base + "/holdings")
        async def capital_holdings(holder: str):
            return {"holder": holder, "positions": acex_ipo.holder_positions(holder),
                    "protocol_version": "v2"}

    def _register_capital_audit(r: APIRouter, base: str) -> None:
        from aimarket_hub import acex_audit

        @r.get(base + "/audit")
        async def capital_audit_list(limit: int = 50):
            return {
                "listings": acex_audit.list_audit_states(limit=min(limit, 500)),
                "protocol_version": "v2",
            }

        @r.get(base + "/audit/{listing_id}")
        async def capital_audit_detail(listing_id: str):
            res = acex_audit.listing_audit_state(listing_id)
            code = 404 if res.get("error") else 200
            return JSONResponse(status_code=code, content={**res, "protocol_version": "v2"})

        @r.post(base + "/audit/{listing_id}/sync")
        async def capital_audit_sync(
            listing_id: str,
            body: AuditSyncRequest,
            authorization: str = Header(default=""),
        ):
            _require_admin(authorization)
            res = acex_audit.sync_coverage(
                listing_id,
                body.auditor,
                cover_usd=body.cover_usd,
                score_bps=body.score_bps,
                phase=body.phase,
            )
            code = 404 if res.get("error") == "unknown_listing" else (400 if res.get("error") else 200)
            return JSONResponse(status_code=code, content={**res, "protocol_version": "v2"})

        @r.post(base + "/audit/{listing_id}/claim")
        async def capital_audit_claim(
            listing_id: str,
            body: AuditClaimRequest,
            authorization: str = Header(default=""),
        ):
            # SEC-05: claiming auditor rewards moves funds — gate behind admin auth
            # like the rest of the capital/audit privileged surface (mint/sync).
            _require_admin(authorization)
            res = acex_audit.claim_audit_reward(listing_id, body.auditor)
            code = 400 if res.get("error") else 200
            return JSONResponse(status_code=code, content={**res, "protocol_version": "v2"})

    _register_capital_ipo(router, "/capital")   # /ai-market/v2/capital/...
    _register_capital_ipo(capital_alias, "")     # /api/v2/capital/...
    _register_capital_audit(router, "/capital")
    _register_capital_audit(capital_alias, "")

    # ── Payment Channels ────────────────────────────────────────

    # Per-IP limit on channel open/close, paired with the ledger's per-wallet windows.
    # The ledger keys on the caller-supplied wallet, so it can only ration an identity
    # the attacker chooses; its bucket table now evicts LRU rather than refusing when
    # full (which had turned a flood into a total outage), and THIS is the cap that
    # actually bounds the flood, because the client address is not attacker-chosen.
    _channel_rate: dict[str, list[float]] = {}
    _CHANNEL_RATE_WINDOW_S = 60.0
    _CHANNEL_RATE_MAX_KEYS = 20_000
    _CHANNEL_RATE_CONFIGURED = _env_positive_int("AIMARKET_CHANNEL_RATE_PER_MIN", 30)
    if _CHANNEL_RATE_CONFIGURED <= 0:
        _CHANNEL_RATE_MAX = _CHANNEL_RATE_CONFIGURED  # 0 keeps the documented "disabled"
    else:
        # Same in-process-window residual as the invoke limiter above, closed the same
        # way: this budget is an AGGREGATE, so divide it across the declared worker
        # count (_INVOKE_RATE_WORKERS is that count, read once for the process).
        # Without the division an N-worker deploy silently admitted N x the configured
        # cap — and this cap is what the ledger's LRU eviction relies on to bound a
        # flood, so an unbounded version of it re-opens the DoS it was added to close.
        # Round up so a tiny budget never floors to zero requests per worker.
        _CHANNEL_RATE_MAX = max(1, -(-_CHANNEL_RATE_CONFIGURED // _INVOKE_RATE_WORKERS))
        if _INVOKE_RATE_WORKERS > 1:
            logger.info(
                "channel rate limit: %d/min configured across %d declared worker(s) "
                "→ %d/min per worker (in-memory window)",
                _CHANNEL_RATE_CONFIGURED, _INVOKE_RATE_WORKERS, _CHANNEL_RATE_MAX,
            )

    def _check_channel_ip_rate(client_ip: str) -> bool:
        """Return True if this client address is within the channel-endpoint budget.

        An address-less caller (ASGI transports without a peer, e.g. some test
        clients) shares one bucket rather than being exempt — "no address" must not
        read as "no limit".
        """
        if _CHANNEL_RATE_MAX <= 0:
            return True
        key = (client_ip or "").strip() or "\x00no-address"
        now = time.time()
        window = now - _CHANNEL_RATE_WINDOW_S
        bucket = [t for t in _channel_rate.get(key, []) if t > window]
        if len(bucket) >= _CHANNEL_RATE_MAX:
            _channel_rate[key] = bucket
            return False
        bucket.append(now)
        _channel_rate[key] = bucket
        if len(_channel_rate) > _CHANNEL_RATE_MAX_KEYS:
            _trim_rate_buckets(_channel_rate, window, _CHANNEL_RATE_MAX_KEYS, keep=key)
        return True

    def _channel_rate_limited() -> JSONResponse:
        return JSONResponse(status_code=429, content={
            "success": False,
            "error": "rate_limited",
            "detail": (
                f"channel endpoint rate limit exceeded — max {_CHANNEL_RATE_MAX} "
                "requests per minute per client address"
            ),
            "protocol_version": "v2",
        })

    @router.post("/channel/open")
    async def channel_open(body: ChannelOpenRequest, request: Request):
        if not config.crypto_enabled:
            return JSONResponse(status_code=503, content={
                "success": False, "error": "crypto_disabled",
                "detail": "payment channels disabled by operator (AIFACTORY_CRYPTO_ENABLED=0) — capabilities are served free",
            })
        if not _check_channel_ip_rate(_client_address(request, _TRUSTED_PROXIES)):
            return _channel_rate_limited()
        # payer_signature is what makes a production open possible AT ALL: the ledger
        # refuses an on-chain-verified deposit without proof the caller controls the
        # paying wallet, so a transport that dropped this field turned every real
        # channel open into "missing or invalid payer proof".
        result = open_channel(
            deposit_usd=body.deposit_usd,
            token=body.token,
            chain=body.chain,
            wallet=body.wallet,
            tx_hash=body.tx_hash,
            payer_signature=body.payer_signature,
            escrow_channel_id=body.escrow_channel_id,
        )
        if result.get("error"):
            return JSONResponse(status_code=400, content=result)
        return result

    @router.post("/channel/close")
    async def channel_close(
        body: ChannelCloseRequest,
        request: Request,
        x_payment_channel_secret: str | None = Header(default=None, alias="X-Payment-Channel-Secret"),
    ):
        if not config.crypto_enabled:
            return JSONResponse(status_code=503, content={
                "success": False, "error": "crypto_disabled",
                "detail": "payment channels disabled by operator (AIFACTORY_CRYPTO_ENABLED=0)",
            })
        if not _check_channel_ip_rate(_client_address(request, _TRUSTED_PROXIES)):
            return _channel_rate_limited()
        result = close_channel(
            channel_id=body.channel_id,
            settle_tx_hash=body.settle_tx_hash,
            wallet=body.wallet or "",
            secret=x_payment_channel_secret or "",
        )
        if result.get("error"):
            return JSONResponse(status_code=400, content=result)
        return result

    # ── Payout obligations (operator liability ledger, ACCT-001) ─
    #
    # Closing or expiring a channel records the unspent remainder as a DEBT rather
    # than transferring it (nothing in the hub can send value). Until these routes
    # existed the debt was recorded and then invisible: an operator had no way to see
    # who was owed what, and no way to write a payout off once they had made it.
    # Admin-gated because a row names a depositor's wallet, their balance and their
    # deposit tx — and because marking one paid destroys a customer's claim.

    def _bearer(authorization: str) -> str:
        return authorization[7:] if authorization.startswith("Bearer ") else ""

    # ── Escrow settlement bridge (opt-in, read-only surface) ─────────────────
    # Admin-gated for the same reasons the obligations ledger is: a row names a
    # depositor's wallet and the amount the hub intends to collect from them. There is
    # deliberately NO route that broadcasts — submission stays an operator CLI action
    # (`python -m aimarket_hub.escrow_bridge.cli submit --yes`), so nothing reachable
    # over HTTP can move funds.

    @router.get("/escrow/status")
    async def escrow_bridge_status(
        limit: int = 200,
        authorization: str = Header(default=""),
    ):
        """Bridge configuration, the pending authorization queue, and what it would submit."""
        _require_admin(authorization)
        try:
            from aimarket_hub.escrow_bridge import config as bridge_config
            from aimarket_hub.escrow_bridge import signer as bridge_signer
            from aimarket_hub.escrow_bridge import store as bridge_store
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"escrow bridge unavailable: {type(exc).__name__}"
            ) from exc

        snapshot: dict[str, Any] = {
            "config": bridge_config.describe(),
            "signer": bridge_signer.build_signer().name,
            "protocol_version": "v2",
        }
        try:
            # create=False: inspecting a hub that never enabled the bridge must not
            # materialise its schema as a side effect of asking.
            authorizations = bridge_store.AuthorizationStore(create=False)
        except bridge_store.StoreError:
            snapshot["store"] = "absent — no authorization has been recorded yet"
            snapshot["queue"] = []
            return snapshot
        snapshot["store"] = authorizations.stats()
        snapshot["queue"] = [
            row.as_dict() for row in authorizations.unresolved(limit=min(max(limit, 1), 1_000))
        ]
        return snapshot

    @router.get("/escrow/plan")
    async def escrow_bridge_plan(
        limit: int = 100,
        authorization: str = Header(default=""),
    ):
        """Simulate every pending authorization against the chain. Sends NOTHING.

        Forced into plan mode regardless of how submission is configured, so this route
        cannot broadcast even on a hub that is fully armed to.
        """
        _require_admin(authorization)
        try:
            from aimarket_hub.escrow_bridge import mirror as bridge_mirror
            from aimarket_hub.escrow_bridge import signer as bridge_signer
            from aimarket_hub.escrow_bridge import store as bridge_store
            from aimarket_hub.escrow_bridge.errors import BridgeError
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"escrow bridge unavailable: {type(exc).__name__}"
            ) from exc
        try:
            engine = bridge_mirror.Mirror(
                authorizations=bridge_store.AuthorizationStore(),
                signer=bridge_signer.PlanOnlySigner(),
            )
            report = engine.run(limit=min(max(limit, 1), 500))
        except BridgeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {**report.as_dict(), "protocol_version": "v2"}

    @router.get("/channel/obligations")
    async def channel_obligations_list(
        status: str = "owed",
        limit: int = 500,
        authorization: str = Header(default=""),
    ):
        _require_admin(authorization)
        from aimarket_hub.channels import channel_obligations, channel_obligations_total

        token = _bearer(authorization)
        allowed_status = {"owed", "paid", ""}
        if status not in allowed_status:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {sorted(allowed_status)}",
            )
        return {
            "obligations": channel_obligations(
                status=status, limit=min(max(limit, 1), 5_000), operator_token=token,
            ),
            "totals": {
                state: channel_obligations_total(status=state, operator_token=token)
                for state in ("owed", "paid")
            },
            "note": (
                "Recorded debts to depositors. The hub never moves funds: pay them "
                "out-of-band, then POST /channel/obligations/{channel_id}/paid."
            ),
            "protocol_version": "v2",
        }

    @router.post("/channel/obligations/{channel_id}/paid")
    async def channel_obligation_mark_paid(
        channel_id: str,
        body: dict[str, Any],
        authorization: str = Header(default=""),
    ):
        """Attest that an obligation was settled out-of-band. Never sends value."""
        _require_admin(authorization)
        from aimarket_hub.channels import mark_obligation_paid

        payout_tx_hash = str(body.get("payout_tx_hash", "") or "").strip()
        result = mark_obligation_paid(
            channel_id, payout_tx_hash, operator_token=_bearer(authorization),
        )
        if result.get("error"):
            return JSONResponse(status_code=400, content={**result, "protocol_version": "v2"})
        return {**result, "protocol_version": "v2"}

    # ── Plugin Catalog (the marketplace) ───────────────────────

    @router.get("/plugins")
    async def list_plugins():
        """List all loaded plugins."""
        return {
            "plugins": plugins.list_plugins(),
            "count": plugins.count(),
            "discovery": "Plugins are auto-discovered via setuptools entry_points (group: aimarket.plugins)",
            "registry_url": f"{config.hub_url}/ai-market/v2/plugins/registry",
        }

    @router.get("/plugins/registry")
    def plugin_registry():
        """Serve the static curated plugin registry.

        Deliberately a SYNC handler: it does blocking stat()+read()+json.load() on a
        file up to 5MB. As `async def` that ran on the event loop and stalled every
        concurrent /invoke for the duration of the read; FastAPI runs a sync handler in
        its threadpool instead, which is exactly the semantics this needs.
        """
        import os as _os
        registry_path = _os.path.join(_os.path.dirname(__file__), "..", "plugins.json")
        if _os.path.isfile(registry_path):
            if _os.path.getsize(registry_path) > 5_000_000:
                logger.warning("plugins.json exceeds 5MB — rejecting")
                return {"registry_version": "1.0", "plugins": [], "error": "registry file too large"}
            with open(registry_path) as f:
                return json.load(f)
        return {"registry_version": "1.0", "plugins": [], "note": "No plugins.json found. Add plugins via PR to the curated registry."}

    @router.get("/plugins/{plugin_name}")
    async def plugin_detail(plugin_name: str):
        """Get details about a specific loaded plugin."""
        plugin = plugins.get_plugin(plugin_name)
        if not plugin:
            raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not loaded")
        return plugin.plugin_info()

    # ── Register routes ─────────────────────────────────────────

    # Register plugin routes under /ai-market/v2/p/{name}/...
    for p in plugins.plugins:
        if "register_routes" not in p._list_hooks():
            continue
        plugin_router = APIRouter(prefix=f"/p/{p.name}", tags=[f"plugin-{p.name}"])
        try:
            p.register_routes(plugin_router)
            router.include_router(plugin_router)
        except Exception as exc:
            logger.error("Plugin %s route registration failed: %s", p.name, exc)

    # MCP at /ai-market/mcp (advertised in well-known) — not under /v2.
    from aimarket_hub.mcp_gateway import attach_mcp_routes

    def _mcp_caller(request: Any) -> str:
        return _client_address(request, _TRUSTED_PROXIES)

    mcp_router = APIRouter(prefix="/ai-market", tags=["mcp"])
    attach_mcp_routes(mcp_router, db=db, hub_url=config.hub_url, client_address=_mcp_caller)
    app.include_router(mcp_router)

    # The same gateway at the apex /mcp, because that is the URL a human pastes into an
    # MCP client and the one an MCP-registry listing can carry. /ai-market/mcp stays for
    # peers that read `mcp_endpoint` out of the well-known manifest.
    apex_mcp_router = APIRouter(tags=["mcp"])
    attach_mcp_routes(apex_mcp_router, db=db, hub_url=config.hub_url, client_address=_mcp_caller)
    app.include_router(apex_mcp_router)

    app.include_router(wellknown_router)
    app.include_router(router)
    app.include_router(capital_alias)

    # ── Landing page & Integration Examples ──────────────────

    from aimarket_hub import landing
    from aimarket_hub.theme import SITE_CSS, apply_shell, ga_head_html

    # Rendered per app, AFTER theme.configure — a module constant built at import froze one
    # operator's address into every code sample on the page.
    DOCS_HTML = landing.docs_html()
    INTEGRATION_EXAMPLES_HTML = landing.integration_examples_html()

    hub_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    terminal_home_path = os.path.join(hub_root, "terminal-home.html")
    cap_i18n_path = os.path.join(hub_root, "cap-descriptions-i18n.json")
    hub_ui_i18n_path = os.path.join(hub_root, "hub-ui-i18n.json")
    studio_ui_i18n_path = os.path.join(hub_root, "studio-ui-i18n.json")

    def _load_terminal_home() -> str:
        """The home page, wearing this operator's name.

        The file is the design of record and carries the reference deployment's title, og
        tags and nav brand as literals; served unmodified they told every visitor to a
        stranger's hub whose page they were on.
        """
        if not os.path.isfile(terminal_home_path):
            return DOCS_HTML
        with open(terminal_home_path, encoding="utf-8") as f:
            html = f.read()
        name = config.hub_name.strip() or "AIMarket Hub"
        if name == "modelmarket.dev":
            return html
        html = html.replace(
            "<title>modelmarket.dev — AI Economy Protocol</title>",
            f"<title>{name} — AI Economy Protocol</title>",
        )
        html = html.replace(
            'content="modelmarket.dev — AI Economy Live"', f'content="{name} — AI Economy Live"',
        )
        html = html.replace(
            'content="https://modelmarket.dev/"', f'content="{config.hub_url.rstrip("/")}/"',
        )
        html = html.replace(
            '<span class="core"></span> modelmarket.dev</a>',
            f'<span class="core"></span> {name}</a>',
        )
        # The hero wordmark is split across spans — `<span class="grad">model</span>market…`
        # — so it survived every brand rewrite here and every third-party hub showed OUR name
        # in letters twice the size of its own. Seen on independentai.network/hub/.
        html = html.replace(
            '<h1 id="hero-brand"><span class="grad">model</span>market<span class="grad">.dev</span></h1>',
            f'<h1 id="hero-brand"><span class="grad">{name}</span></h1>',
        )
        # And the client-side translator sets document.title from the dictionary, whose
        # page_title carries the reference brand. Without this the page re-brands itself back
        # to modelmarket.dev a moment after loading, undoing the <title> rewrite above.
        html = html.replace(
            'window.__HUB_BRAND = "modelmarket.dev";',
            f'window.__HUB_BRAND = {json.dumps(name)};',
        )
        if not config.ecosystem_links:
            html = re.sub(
                r'\s*<a href="https://use\.modelmarket\.dev[^"]*"[^>]*>.*?</a>', "", html,
                flags=re.DOTALL,
            )
        ga = ga_head_html()
        if ga and "googletagmanager.com/gtag/js" not in html:
            html = html.replace("</head>", ga + "\n</head>", 1)
        return html

    # The HTML handlers below are SYNC on purpose — they stat() and read() files from
    # disk, which must not happen on the event loop (it would stall concurrent
    # /invoke settlement). FastAPI dispatches sync handlers to its threadpool.
    @app.get("/", response_class=HTMLResponse)
    def landing_page():
        return HTMLResponse(_load_terminal_home())

    @app.get("/cap-descriptions-i18n.json")
    def cap_descriptions_i18n():
        """Short capability glosses in EN/RU/ES/FR/ZH for the terminal catalogue UI."""
        if not os.path.isfile(cap_i18n_path):
            raise HTTPException(status_code=404, detail="cap-descriptions-i18n.json missing")
        return FileResponse(
            cap_i18n_path,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/hub-ui-i18n.json")
    def hub_ui_i18n():
        """Portal chrome strings (nav, hero, stats, feed) in EN/RU/ES/FR/ZH."""
        if not os.path.isfile(hub_ui_i18n_path):
            raise HTTPException(status_code=404, detail="hub-ui-i18n.json missing")
        return FileResponse(
            hub_ui_i18n_path,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/studio-ui-i18n.json")
    def studio_ui_i18n():
        """HEPHAESTUS studio chrome strings in EN/RU/ES/FR/ZH."""
        if not os.path.isfile(studio_ui_i18n_path):
            raise HTTPException(status_code=404, detail="studio-ui-i18n.json missing")
        return FileResponse(
            studio_ui_i18n_path,
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/assets/site.css")
    def site_css():
        """The one stylesheet behind /developers, /examples and the demo pages.

        Served from `theme.py` rather than a file so the page chrome cannot drift between
        the templates that are Python strings and the ones that are static HTML.
        """
        return Response(
            content=SITE_CSS,
            media_type="text/css",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/developers", response_class=HTMLResponse)
    async def developers_page():
        return HTMLResponse(DOCS_HTML)

    @app.get("/examples", response_class=HTMLResponse)
    async def integration_examples():
        return HTMLResponse(INTEGRATION_EXAMPLES_HTML)

    # ── HEPHAESTUS studio ───────────────────────────────────────

    # The studio composes a graph out of THIS hub's catalogue, so it is served from the
    # same origin as the manifest it reads — the hub's CORS is fail-closed, and a builder
    # that cannot read the catalogue is not a builder.
    @app.post("/studio/run")
    async def studio_run(
        body: StudioRunRequest,
        x_sandbox_visitor: str | None = Header(default=None, alias="X-AIMarket-Sandbox-Visitor"),
    ):
        """Submit a studio blueprint to the pipeline executor, same-origin.

        The executor is a different service (the factory), and the browser cannot reach it
        cross-origin, so the hub forwards. This is deliberately NOT a general proxy:

          * the target comes from ``AIMARKET_PIPELINE_EXECUTOR_URL`` only, never from the
            request — a forwarder that takes its destination from the caller is an SSRF
            gadget, whatever it is called;
          * unset means 503, not a default guess;
          * the node list is shape- and size-validated here as well as by the executor, so
            this route cannot be used to post arbitrary bodies anywhere;
          * no caller credentials are forwarded. The studio's run path is the free/sandbox
            one; paid runs go through the executor directly with their own channel.
        """
        executor = (os.environ.get("AIMARKET_PIPELINE_EXECUTOR_URL") or "").strip().rstrip("/")
        if not executor:
            return JSONResponse(status_code=503, content={
                "error": "executor_not_configured",
                "detail": (
                    "This hub has no pipeline executor configured. Set "
                    "AIMARKET_PIPELINE_EXECUTOR_URL to the factory base URL, or POST the "
                    "blueprint to /ai-market/pipelines on the executor yourself."
                ),
                "protocol_version": "v2",
            })

        payload: dict[str, Any] = {"nodes": [n.model_dump(exclude_none=True) for n in body.nodes]}
        if body.channel_id:
            payload["channel_id"] = body.channel_id

        # `post_configured`, not `safe_post`: the destination is the hub's own configuration,
        # so the caller-SSRF refusal is a false positive here — and on the first deploy it
        # blocked the only address that was actually reachable while allowing one that did
        # not resolve. See outbound_http.post_configured for the whole argument.
        from aimarket_hub.outbound_http import post_configured

        try:
            resp = await post_configured(
                f"{executor}/ai-market/pipelines",
                json=payload,
                timeout=120.0,
                # The visitor's own trial identity, and nothing else. The hub meters its
                # free allowance per visitor, so dropping this merges every caller into
                # one bucket; forwarding anything MORE would hand a stranger's credential
                # to another service.
                headers=(
                    {"X-AIMarket-Sandbox-Visitor": x_sandbox_visitor}
                    if x_sandbox_visitor else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 — the executor is a separate service
            return JSONResponse(status_code=502, content={
                "error": "executor_unreachable",
                "detail": str(exc)[:300],
                "protocol_version": "v2",
            })

        try:
            out = resp.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=502, content={
                "error": "executor_returned_non_json",
                "status_code": resp.status_code,
                "protocol_version": "v2",
            })
        # Where the signed bill of materials can be fetched — as a path on THIS origin, not
        # the executor's address. The executor is typically an internal name
        # (`http://aicom-app-1:8080`), and publishing it both leaks infrastructure and hands
        # the browser a link it cannot follow.
        if isinstance(out, dict) and out.get("trace_id"):
            out["trace_url"] = f"/studio/trace/{out['trace_id']}"
        return JSONResponse(status_code=resp.status_code, content=out)

    @app.get("/studio/trace/{trace_id}")
    async def studio_trace(trace_id: str):
        """Fetch one signed bill of materials through this origin.

        The page must be able to show the verifiable original rather than only its own
        summary of it, and the executor is not reachable from a browser.
        """
        executor = (os.environ.get("AIMARKET_PIPELINE_EXECUTOR_URL") or "").strip().rstrip("/")
        if not executor:
            return JSONResponse(status_code=503, content={
                "error": "executor_not_configured", "protocol_version": "v2",
            })
        if not re.fullmatch(r"tr_[0-9a-f]{6,32}", trace_id or ""):
            # The id goes into a URL on another service; only the executor's own format
            # may travel, so this cannot be steered into an arbitrary path.
            return JSONResponse(status_code=400, content={
                "error": "invalid_trace_id", "protocol_version": "v2",
            })

        from aimarket_hub.outbound_http import get_configured

        try:
            resp = await get_configured(f"{executor}/ai-market/pipelines/{trace_id}", timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=502, content={
                "error": "executor_unreachable", "detail": str(exc)[:200],
                "protocol_version": "v2",
            })
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=502, content={
                "error": "executor_returned_non_json", "protocol_version": "v2",
            })

    # Resolve the built studio — repo layout, sibling layout, and the Docker one.
    studio_dir = None
    for candidate in (
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "hephaestus", "studio", "dist")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hephaestus", "studio", "dist")),
        "/app/hephaestus/studio/dist",
    ):
        if os.path.isdir(candidate):
            studio_dir = candidate
            break

    if studio_dir:
        app.mount("/studio", StaticFiles(directory=studio_dir, html=True), name="studio")
    else:
        @app.get("/studio", response_class=HTMLResponse)
        async def studio_not_built():
            # Say which build is missing instead of 404ing — the deployment that hits this
            # is one where the image shipped without the studio bundle.
            return HTMLResponse(
                "<h1>HEPHAESTUS studio is not built</h1>"
                "<p>Run <code>npm install &amp;&amp; npm run build</code> in "
                "<code>hephaestus/studio</code>, or deploy an image that includes "
                "<code>hephaestus/studio/dist</code>.</p>",
                status_code=503,
            )

    # ── Widget & Live Stream static serving ─────────────────────

    # Resolve widget dir — handles both repo layout (../../aimarket-widget)
    # and Docker layout (../aimarket-widget).
    widget_dir = None
    for candidate in (
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "aimarket-widget")),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "aimarket-widget")),
        "/app/aimarket-widget",
    ):
        if os.path.isdir(candidate):
            widget_dir = candidate
            break

    @app.get("/plugins/demo", response_class=HTMLResponse)
    def plugins_demo():
        hub_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        demo_html = os.path.join(hub_root, "plugins-demo.html")
        if os.path.isfile(demo_html):
            with open(demo_html, encoding="utf-8") as f:
                return HTMLResponse(
                    apply_shell(
                        f.read(),
                        "Plugin Demo — modelmarket.dev",
                        "Live playground for every plugin loaded on this hub — real routes, "
                        "real responses.",
                        active="plugins",
                    )
                )
        return HTMLResponse("<h1>Plugin demo not found</h1>", status_code=404)

    operator_html_path = os.path.join(hub_root, "operator.html")

    def _operator_page() -> HTMLResponse:
        if not os.path.isfile(operator_html_path):
            return HTMLResponse("<h1>Operator desk not found</h1>", status_code=404)
        with open(operator_html_path, encoding="utf-8") as f:
            html = f.read()
        name = (config.hub_name or "").strip() or "AIMarket Hub"
        return HTMLResponse(
            apply_shell(
                html,
                f"Operator desk — {name}",
                "Approve exceptions or dismiss pending federation hubs. "
                "Sandbox assay auto-admits a pass. Admin token required.",
                active="operator",
            ),
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    @app.get("/operator", response_class=HTMLResponse)
    def operator_desk():
        return _operator_page()

    @app.get("/operator/", response_class=HTMLResponse)
    def operator_desk_slash():
        return _operator_page()

    @app.get("/widget/demo", response_class=HTMLResponse)
    def widget_demo():
        demo_html = os.path.join(widget_dir, "demo.html") if widget_dir else None
        if demo_html and os.path.isfile(demo_html):
            with open(demo_html, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>Widget demo not found</h1>", status_code=404)

    @app.get("/live", response_class=HTMLResponse)
    def live_economy_stream():
        """Legacy URL — same immersive terminal as `/`."""
        return HTMLResponse(_load_terminal_home())

    @app.get("/widget/widget.js")
    def widget_js():
        """Serve the embed script with THIS hub's host in its allowlist.

        The widget refuses a `data-hub-url` outside same-origin, localhost or a built-in
        suffix list — a real control, since a hostile embed could otherwise point a
        visitor's invocations and affiliate header at an attacker. The list was the literal
        `["modelmarket.dev"]` in every copy, which means a fork shipped an allowlist naming
        a domain its operator does not control and not their own: any third-party page could
        embed their script pointed at the reference hub, and no third-party page could point
        it at theirs. Substituting the serving hub's own host keeps the control and puts the
        operator in charge of it.
        """
        path = os.path.join(widget_dir, "widget.js") if widget_dir else None
        if not (path and os.path.isfile(path)):
            raise HTTPException(status_code=404, detail="widget not bundled with this hub")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        host = (urlparse(config.hub_url).hostname or "").strip().lower()
        if host and not host.startswith("localhost") and host != "127.0.0.1":
            src = src.replace(
                'var HUB_ALLOW_SUFFIXES = ["modelmarket.dev"];',
                f'var HUB_ALLOW_SUFFIXES = ["{host}"];',
            )
        return Response(content=src, media_type="application/javascript")

    if widget_dir:
        app.mount("/widget", StaticFiles(directory=widget_dir, html=True), name="widget")

    return app
