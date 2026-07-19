"""Hub API — FastAPI application exposing federation endpoints.

Core routes: .well-known, manifest (v2), federated search, routing proxy,
federation announce, peers list, stats, plugin catalog.

Plugins extend the hub via entry points — they register their own routes
and hook into the invoke pipeline (pre/post checks). See plugin.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import secrets
import time
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Pydantic models imported from api_models
from aimarket_hub.api_models import (
    AnnounceRequest,
    AuditClaimRequest,
    AuditSyncRequest,
    ChannelCloseRequest,
    ChannelOpenRequest,
    InvokeRequest,
    IpoRequest,
    ReputationEventsRequest,
)
from aimarket_hub.channels import (
    _is_production_mode,
    channel_balance,
    channel_stats,
    close_channel,
    debit_channel,
    hold_channel,
    open_channel,
    release_hold,
)
from aimarket_hub import verified_settlement
from aimarket_hub.verified_settlement import VerifiedSettlementService

# Hub-local invokes record this instead of hub_url so /stats/live does not
# inflate "external federation" traffic when the operator smoke-tests caps.
OPERATOR_SELF_CONSUMER = "operator_self"

from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.demo_seeder import seed_capabilities
from aimarket_hub.models import InvocationStat, Peer
from aimarket_hub.plugin import PluginRegistry
from aimarket_hub.safety_gate import default_safety_gate
from aimarket_hub.sandbox_trials import (
    consume_sandbox_trial,
    sandbox_demo_result,
    sandbox_enabled,
    sandbox_quota,
    sandbox_stub_invoke_enabled,
)
from aimarket_hub.signing import Signer
from aimarket_hub.trust import TrustScorer

logger = logging.getLogger(__name__)

_HUB_STARTED_AT = time.time()

_SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@")


def _is_safe_path(segment: str) -> bool:
    """Reject path traversal in product/capability ids used in upstream URLs."""
    if not segment or len(segment) > 80:
        return False
    if ".." in segment or "/" in segment or "\\" in segment:
        return False
    return all(c in _SAFE_ID_CHARS for c in segment)


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
    trust_scorer = trust_scorer or TrustScorer(db)
    builtin_safety = default_safety_gate()  # Built-in fallback
    # Pay-on-Verified settlement worker (buyer-opt-in escrow holds; env-gated per invoke).
    verify_svc = VerifiedSettlementService(db=db, signer=signer, consumer_hub=OPERATOR_SELF_CONSUMER)

    # Seed marketplace with initial capability catalog on first start
    if os.environ.get("AIMARKET_SKIP_SEED", "").strip().lower() not in ("1", "true", "yes"):
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

    # Plugin discovery
    if plugins is None:
        plugins = PluginRegistry()
        plugins.discover(db)
        plugins.startup_all(db)

    if not config.crypto_enabled:
        logger.warning(
            "[crypto-disabled] AIFACTORY_CRYPTO_ENABLED=0 — payment channels offline; "
            "free/sandbox tier active; no 402, no on-chain verification/escrow; NFT minting "
            "blocked. Manifest/receipt signing and federation are unaffected. "
            "Set AIFACTORY_CRYPTO_ENABLED=1 to enable the on-chain economy."
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
        version="3.0.0",
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
            "Content-Type", "Authorization", "X-Payment-Channel", "X-AIMarket-Affiliate",
            "X-AIMarket-Sandbox-Visitor", "X-Market-Signature", "X-AIMarket-Routing-Hub",
            "X-AIMarket-Routing-Fee", "X-AIMarket-Crawler", "X-Provider-Signature",
        ],
    )

    # Admin token for federation/crawl-control endpoints.
    # If unset, those endpoints are disabled (fail-closed).
    _ADMIN_TOKEN = os.environ.get("AIMARKET_ADMIN_TOKEN", "").strip()
    if not _ADMIN_TOKEN:
        logger.warning(
            "AIMARKET_ADMIN_TOKEN not set — /federation/announce and "
            "/federation/crawl will reject all requests. Set this env var "
            "to enable peer registration and crawling."
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

    # Per-consumer /invoke rate limit (sliding window). Without it an anonymous
    # caller could hammer a provider — forcing failures to grief its stake and
    # burning its upstream capacity. Mirrors the channels ledger _check_rate.
    _invoke_rate: dict[str, list[float]] = {}
    _INVOKE_RATE_MAX = int(os.environ.get("AIMARKET_INVOKE_RATE_PER_MIN", "60"))
    _INVOKE_RATE_WINDOW_S = 60.0

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

    from aimarket_hub.supply_security import SupplySecurity

    supply_security = SupplySecurity(
        db, config, signer=signer, slash_registry=slash_registry,
    )
    app.state.supply_security = supply_security
    # Verify-first: repeat Metis verdict failures escalate to a calibrated slash.
    verify_svc.attach_supply_security(supply_security)
    app.state.verify_svc = verify_svc  # exposed so the escalation wiring is testable

    router = APIRouter(prefix="/ai-market/v2", tags=["hub-v2"])
    wellknown_router = APIRouter(tags=["wellknown"])

    # ── .well-known ────────────────────────────────────────────

    @wellknown_router.get("/.well-known/ai-market.json")
    async def well_known():
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

        wk = {
            "name": config.hub_name,
            "protocol_versions": ["v1", "v2"],
            "hub_version": "3.0.0",
            "mcp_endpoint": f"{config.hub_url}/ai-market/mcp",
            "manifest_url": f"{config.hub_url}/ai-market/v2/manifest",
            "products_count": db.count_capabilities("local"),
            "capabilities_count": db.count_capabilities("local"),
            "federated_capabilities_count": db.count_capabilities(),
            "supported_chains": config.payment_chains,
            "supported_tokens": config.payment_tokens,
            # Payment recipient hidden from public manifest — exposed only when stub mode
            "payment_configured": not config.payment_verify_stub,
            "payment_testnet": config.payment_testnet,
            "signer_public_key": signer.public_key_b64,
            "federation": {
                "crawl_interval_s": config.crawl_interval_s,
                "routing_fee_bps": config.routing_fee_bps,
                "min_trust_score": config.min_trust_score,
                "seed_list": config.seed_list,
            },
            "peers": peers,
            "plugins_loaded": plugins.count(),
        }

        # Merge plugin manifest extensions
        plugin_ext = plugins.get_manifest_extensions()
        if plugin_ext:
            wk["plugin_extensions"] = plugin_ext

        # Advertise the agent-native MCP servers so external AI agents can discover + add them
        # (the on-chain/HTTP capabilities are also reachable as MCP tools).
        wk["mcp_servers"] = [
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
        wk["signature"] = signer.sign_object(wk)
        return wk

    # ── Manifest ────────────────────────────────────────────────

    @router.get("/manifest")
    async def v2_manifest():
        caps = db.list_capabilities(limit=1000)
        tools = []
        for c in caps:
            tools.append({
                "name": c.tool_name(),
                "description": c.description,
                "input_schema": c.input_schema,
                "output_schema": c.output_schema,
                "price_per_call_usd": c.price_per_call_usd,
                "p50_latency_ms": c.p50_latency_ms,
                "success_rate_30d": c.success_rate_30d,
                "product_id": c.product_id,
                "capability_id": c.capability_id,
                "source_hub": c.source_hub,
                "source_hub_name": c.source_hub_name,
                "routed_price_usd": c.routed_price_usd,
                "routing_fee_bps": c.routing_fee_bps,
                "trust_score": c.trust_score,
                "invoke_url": c.invoke_url or None,
            })

        by_hub: dict[str, dict[str, Any]] = {}
        for p in db.list_peers():
            by_hub[p.url] = {
                "capabilities_count": p.capabilities_count,
                "trust_score": p.trust_score,
                "last_crawl": p.last_crawl,
            }
        by_hub["local"] = {
            "capabilities_count": db.count_capabilities("local"),
            "trust_score": 1.0,
            "last_crawl": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        manifest_body = {
            "protocol_version": "v2",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "base_url": config.hub_url,
            "total_capabilities": len(caps),
            "local_capabilities": db.count_capabilities("local"),
            "federated_capabilities": db.count_federated(),
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
        'what costs what' in one call without a full manifest/search."""
        caps = db.list_capabilities(limit=1000)
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
        limit: int = 20,
    ):
        limit = min(limit, 100)
        results = db.search_capabilities(intent, limit=limit * 2)
        results = supply_security.filter_for_discover(results)
        floor = min_trust if min_trust is not None else supply_security.policy.min_trust_discover

        filtered: list[dict[str, Any]] = []
        for cap in results:
            if hub != "any" and cap.source_hub != hub:
                continue
            if cap.trust_score < floor:
                continue
            if max_latency_ms is not None and cap.p50_latency_ms > max_latency_ms:
                continue
            routed = cap.routed_price_usd or cap.price_per_call_usd
            if budget is not None and routed > budget:
                continue
            filtered.append({
                "product_id": cap.product_id,
                "capability_id": cap.capability_id,
                "source_hub": cap.source_hub,
                "source_hub_name": cap.source_hub_name,
                "name": cap.name,
                "description": cap.description,
                "score": 0.8,
                "price_per_call_usd": cap.price_per_call_usd,
                "routed_price_usd": routed,
                "routing_fee_bps": cap.routing_fee_bps,
                "trust_score": cap.trust_score,
                "p50_latency_ms": cap.p50_latency_ms,
                "publisher_id": cap.publisher_id or None,
                "stake_usd": cap.stake_usd or None,
                "demo": cap.is_demo,
            })
            if len(filtered) >= limit:
                break

        return {
            "query": intent,
            "matches": filtered,
            "total_hubs_searched": db.peer_count() + 1,
            "protocol_version": "v2",
        }

    # ── Developer publish ─────────────────────────────────────

    @router.post("/supply/stake")
    async def supply_stake(
        body: dict[str, Any],
        authorization: str = Header(default=""),
    ):
        """Deposit stake (USD bookkeeping) to unlock community publish."""
        _require_publish(authorization)
        publisher_id = str(body.get("publisher_id", "")).strip()
        if not publisher_id:
            raise HTTPException(status_code=400, detail="publisher_id required")
        try:
            amount = float(body.get("amount_usd", 0))
            tx_hash = str(body.get("tx_hash", "")).strip()
            return {
                **supply_security.stake(publisher_id, amount, tx_hash),
                "protocol_version": "v2",
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/supply/register")
    async def supply_register(
        body: dict[str, Any],
        authorization: str = Header(default=""),
    ):
        """Register a community capability with a direct invoke URL."""
        from aimarket_hub.publish import validate_manifest

        _require_publish(authorization)
        try:
            cap = validate_manifest(body)
            publisher_id, pubkey = supply_security.validate_publish(body)
            cap.publisher_id = publisher_id
            cap.provider_pubkey = pubkey
            cap.stake_usd = db.supply_stake_get(publisher_id)
            trust = supply_security.after_publish(cap, publisher_id)
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
            "search_hint": f"{config.hub_url}/ai-market/v2/search?intent={cap.name}",
            "protocol_version": "v2",
        }

    # ── Self-bond (consumer cost/conduct bond → self-slash enforcement) ──
    @router.post("/self-bond/register")
    async def self_bond_register(body: dict[str, Any], authorization: str = Header(default="")):
        """Register a staked consumer cost/conduct bond (ceiling + client commitment)."""
        _require_publish(authorization)
        agent_id = str(body.get("agent_id", "")).strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id required")
        try:
            return {
                **supply_security.register_self_bond(
                    agent_id,
                    str(body.get("evm_address", "")),
                    float(body.get("ceiling_usd", 0)),
                    float(body.get("bond_usd", 0)),
                    str(body.get("commitment", "")),
                ),
                "protocol_version": "v2",
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/self-bond/slash")
    async def self_bond_slash(body: dict[str, Any], authorization: str = Header(default="")):
        """Slash an agent's self-bond on a declared-ceiling-vs-observed-spend breach.

        Authenticated (operator/publish token) and grounded in hub-recorded
        settlement: slashing on a caller-supplied ``observed_spend_usd`` alone would
        let anyone drain any agent's bonded collateral. The claimed overspend is
        therefore capped at the spend the hub ledger actually debited for the bonded
        wallet; if the hub holds no settlement record for it, we refuse rather than
        slash on an unverified number.
        """
        _require_publish(authorization)
        agent_id = str(body.get("agent_id", "")).strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id required")
        bond = db.self_bond_get(agent_id)
        if not bond:
            raise HTTPException(status_code=404, detail="no self-bond registered")
        claimed = max(0.0, float(body.get("observed_spend_usd", 0) or 0))
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
    ):
        sandbox_mode = bool(x_sandbox_visitor and sandbox_enabled())
        # Master crypto switch (default OFF): when off, every invoke is served FREE —
        # no 402, no payment channel, no debit. Capabilities still execute normally.
        crypto_on = config.crypto_enabled
        client_ip = request.client.host if request.client else ""

        # Per-consumer rate limit (griefing / provider-hammering protection). Key on
        # the payment channel when present, else the visitor/client so an anonymous
        # caller cannot flood a provider.
        rate_key = x_payment_channel or x_sandbox_visitor or client_ip or "anonymous"
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
                sandbox_meta = {"sandbox": True, **{k: trial[k] for k in ("remaining", "used", "max_trials") if k in trial}}

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

            invoke_url = (cap.invoke_url or "").strip()
            provider_invoke_failed = False

            # SEC-02: paid capabilities must be paid for. Without a payment channel
            # the debit below is silently skipped (remaining=None), i.e. a free
            # invoke. In production, require X-Payment-Channel before executing so
            # the upstream capability is never run for free.
            if (
                crypto_on
                and not sandbox_mode
                and cap.price_per_call_usd > 0
                and not x_payment_channel
            ):
                return JSONResponse(status_code=402, content={
                    "success": False,
                    "error": "payment_required",
                    "detail": "X-Payment-Channel required for paid capability invoke",
                    "needed": cap.price_per_call_usd,
                    "protocol_version": "v2",
                })

            # Balance pre-authorization: reject BEFORE running the (billable)
            # upstream capability if the channel can't cover the price. The
            # authoritative debit happens only after execution, so without this a
            # depleted channel would get paid provider work done for free.
            if (
                crypto_on
                and not sandbox_mode
                and cap.price_per_call_usd > 0
                and x_payment_channel
            ):
                _bal = channel_balance(x_payment_channel)
                if _bal is not None and _bal < cap.price_per_call_usd:
                    return JSONResponse(status_code=402, content={
                        "success": False,
                        "error": "payment_required",
                        "detail": "insufficient channel balance for capability price",
                        "needed": cap.price_per_call_usd,
                        "balance": _bal,
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

            if invoke_url:
                try:
                    sanitized = supply_security.sanitize_input(body.input)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                try:
                    supply_security.check_invoke_trust(cap)
                except ValueError as exc:
                    raise HTTPException(status_code=403, detail=str(exc)) from exc
                try:
                    from aimarket_hub.outbound_http import safe_post

                    ir = await safe_post(
                        invoke_url,
                        json={
                            "input": sanitized,
                            "product_id": body.product_id,
                            "capability_id": body.capability_id,
                        },
                        headers={
                            "X-Payment-Channel": x_payment_channel or "",
                            "X-AIMarket-Sandbox": "1" if sandbox_mode else "",
                        },
                        invoke=True,
                    )
                    if ir.status_code == 200:
                        payload = ir.json()
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

            # ── Payment: debit channel unless sandbox trial / crypto off ──────────
            # vs_plan was computed before execution (validation-first, see above).
            list_price = cap.price_per_call_usd
            # Crypto off → free tier: charge nothing and never debit a channel.
            price = 0.0 if (sandbox_mode or not crypto_on) else list_price
            nonce = f"rcpt_{secrets.token_hex(16)}"
            # Whether the price was reserved as a Pay-on-Verified escrow hold (vs a
            # plain debit). Tracked so an exception before the settlement row is
            # durably registered can release the orphaned hold (below).
            vs_held = False

            if sandbox_mode or not crypto_on:
                remaining = None
            elif x_payment_channel:
                if vs_plan is not None and vs_plan.active and vs_plan.paid:
                    # Deferred debit: reserve the price (auth leg). Capture/release
                    # happens in the background once the Metis verdict lands.
                    pay_result = hold_channel(
                        x_payment_channel, price, receipt_id=nonce,
                        secret=x_payment_channel_secret or "",
                    )
                    vs_held = not pay_result.get("error")
                else:
                    pay_result = debit_channel(
                        x_payment_channel, price, receipt_id=nonce,
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
            else:
                remaining = None

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

            # ── Pay-on-Verified: register the pending settlement RIGHT AFTER the ───
            # hold, before any other bookkeeping. register() commits the durable
            # verified_settlements row that makes an escrow hold recoverable (the
            # startup/sweep reconciler re-queues pending rows). Only pure receipt
            # assembly sits between hold and register; if register itself raises we
            # release the orphaned hold so a crash can't lock the buyer's funds.
            verification_env = None
            vs_rejection = None
            if vs_plan is not None and vs_plan.active:
                try:
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
                except Exception:
                    if vs_held:
                        with contextlib.suppress(Exception):
                            release_hold(nonce)
                    raise

            stat = InvocationStat(
                capability_id=body.capability_id,
                product_id=body.product_id,
                source_hub="local",
                price_usd=price,
                latency_ms=elapsed_ms,
                success=True,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                consumer_hub=OPERATOR_SELF_CONSUMER,
            )
            db.record_invocation(stat)

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

            # Attach provenance receipt if the plugin generated one
            provenance_receipt = result.pop("_provenance_receipt", None)

            # ── Pay-on-Verified: resolve the buyer's chosen sync/async mode ───────
            if vs_plan is not None:
                if vs_plan.active and vs_plan.wait:
                    # Sync opt-in: hold the response until the verdict, bounded.
                    # On timeout we return the pending envelope (NOT an error) —
                    # the buyer falls back to GET /verification/{nonce}.
                    resolved = await verify_svc.wait_for(nonce, timeout=vs_plan.wait_timeout_s)
                    if resolved:
                        verification_env = resolved.get("verification") or verification_env
                        receipt = resolved.get("receipt") or receipt
                        vs_rejection = resolved.get("rejection_receipt")
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
            return JSONResponse(status_code=200, content=response_body)

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

        # Transport selection: oracle / AIMarket-v2 peers expose a single
        # `mcp_endpoint` (from their /.well-known/ai-market.json) and expect the
        # {capability_id, input} envelope. Legacy factory-product peers use the
        # /capabilities/{product}/{cap}/invoke path with the bare input. Previously
        # every federated invoke assumed the legacy path, so oracle capabilities
        # were never actually invokable through the hub — resolve the real endpoint.
        oracle_like = any(
            c in ("oracle", "simulation", "math-viz", "randomness-beacon")
            for c in (peer.categories or [])
        )
        mcp_endpoint: str | None = None
        if oracle_like and peer.well_known_url:
            try:
                from aimarket_hub.outbound_http import safe_get

                wkr = await safe_get(peer.well_known_url, timeout=10)
                if wkr.status_code == 200:
                    ep = (wkr.json() or {}).get("mcp_endpoint")
                    mcp_endpoint = ep if isinstance(ep, str) and ep else None
            except Exception as exc:  # well-known unreachable → fall back to legacy path
                logger.warning("invoke: well-known lookup failed for %s: %s", peer.url, exc)

        try:
            from aimarket_hub.outbound_http import safe_post

            headers: dict[str, str] = {
                "X-AIMarket-Routing-Hub": config.hub_url,
                "X-AIMarket-Routing-Fee": str(config.routing_fee_bps),
            }
            if x_payment_channel:
                headers["X-Payment-Channel"] = x_payment_channel
            if mcp_endpoint:
                resp = await safe_post(
                    mcp_endpoint,
                    json={
                        "capability_id": body.capability_id,
                        "input": body.input,
                        "product_id": body.product_id,
                        "source_hub": config.hub_url,
                    },
                    headers=headers,
                )
            else:
                provider_url = f"{peer.url}/capabilities/{body.product_id}/{body.capability_id}/invoke"
                resp = await safe_post(provider_url, json=body.input, headers=headers)
                if resp.status_code == 402:
                    return JSONResponse(status_code=402, content=resp.json())
                if resp.status_code >= 400:
                    raise HTTPException(status_code=502, detail=f"Provider returned {resp.status_code}")

                result_data = resp.json()
                # Normalize the AIMarket-v2 oracle envelope to the fields the hub's
                # accounting/return path expects (it has `ok`/`receipt`, not top-level
                # `success`/`latency_ms`).
                if mcp_endpoint and isinstance(result_data, dict):
                    rcpt = result_data.get("receipt") or {}
                    result_data.setdefault("success", bool(result_data.get("ok", True)))
                    result_data.setdefault("latency_ms", rcpt.get("latency_ms", 0))
                stat = InvocationStat(
                    capability_id=body.capability_id,
                    product_id=body.product_id,
                    source_hub=body.source_hub,
                    price_usd=result_data.get("price_usd", 0),
                    latency_ms=result_data.get("latency_ms", 0),
                    success=result_data.get("success", False),
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    consumer_hub=OPERATOR_SELF_CONSUMER,
                )
                db.record_invocation(stat)

                # Collect routing fee from the payment channel
                price = result_data.get("price_usd", 0)
                routing_fee = round(price * config.routing_fee_bps / 10000, 6)
                if x_payment_channel and routing_fee > 0:
                    fee_result = debit_channel(x_payment_channel, routing_fee,
                                               receipt_id=f"route_{secrets.token_hex(8)}",
                                               secret=x_payment_channel_secret or "")
                    if fee_result.get("error"):
                        logger.warning("routing_fee: could not debit %s from channel %s: %s",
                                       routing_fee, x_payment_channel, fee_result.get("error"))
                    else:
                        logger.info("routing_fee: collected $%.6f (%d bps) from channel %s",
                                    routing_fee, config.routing_fee_bps, x_payment_channel)

                result_data["routed_via"] = config.hub_url
                result_data["routing_fee_bps"] = config.routing_fee_bps
                if federated_verify_env is not None and isinstance(result_data, dict):
                    result_data["verification"] = federated_verify_env
                return result_data

        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Provider unreachable: {exc}") from exc

    # ── Federation ──────────────────────────────────────────────

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

    @router.get("/federation/peers")
    async def list_peers():
        peers = db.list_peers()
        return {
            "peers": [
                {"url": p.url, "name": p.name, "capabilities_count": p.capabilities_count,
                 "last_crawl": p.last_crawl, "trust_score": p.trust_score, "depth": p.depth,
                 "categories": p.categories, "well_known_url": p.well_known_url}
                for p in peers
            ],
            "count": len(peers),
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
        if not db.set_peer_trusted(url, trusted):
            raise HTTPException(status_code=404, detail=f"peer not found: {url}")
        return {"url": url, "trusted": trusted, "status": "updated"}

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

    @router.post("/reputation/disputes")
    async def submit_dispute(
        body: dict,
        authorization: str = Header(default=""),
    ):
        """File a dispute: consumer-signed (portable PoM) or hub-admin (legacy operator path)."""
        from aimarket_hub.reputation_oracle import Dispute

        sig = body.get("signature")
        consumer_pubkey = str(body.get("consumer_pubkey") or "").strip()

        if sig and consumer_pubkey:
            required = ("dispute_id", "invocation_id", "provider_hub", "consumer_hub")
            missing = [k for k in required if not str(body.get(k) or "").strip()]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "missing_fields", "fields": missing},
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
            "version": "3.0.0",
            "uptime_seconds": int(time.time() - _HUB_STARTED_AT),
        }

    @router.get("/stats/live")
    async def live_stats(limit: int = 50):
        stats = db.recent_stats(limit=min(limit, 200))
        summary = db.stats_summary()
        ch = channel_stats()
        summary["open_channels"] = ch.get("open_channels", 0)
        summary["settled_volume_usd"] = ch.get("settled_volume_usd", 0)
        hub_url = (config.hub_url or "").rstrip("/")
        events = []
        operator_self_events = 0
        for row in stats:
            ev = dict(row)
            ch_val = str(ev.get("consumer_hub") or "")
            if ch_val in (OPERATOR_SELF_CONSUMER, "local") or (
                hub_url and ch_val.rstrip("/") == hub_url
            ):
                ev["traffic_class"] = "operator_self"
                operator_self_events += 1
            else:
                ev["traffic_class"] = "external"
            events.append(ev)
        summary["operator_self_events_in_page"] = operator_self_events
        summary["hub_url"] = hub_url
        return {"events": events, "summary": summary, "protocol_version": "v2"}

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

    @router.post("/channel/open")
    async def channel_open(body: ChannelOpenRequest):
        if not config.crypto_enabled:
            return JSONResponse(status_code=503, content={
                "success": False, "error": "crypto_disabled",
                "detail": "payment channels disabled by operator (AIFACTORY_CRYPTO_ENABLED=0) — capabilities are served free",
            })
        result = open_channel(
            deposit_usd=body.deposit_usd,
            token=body.token,
            chain=body.chain,
            wallet=body.wallet,
            tx_hash=body.tx_hash,
        )
        if result.get("error"):
            return JSONResponse(status_code=400, content=result)
        return result

    @router.post("/channel/close")
    async def channel_close(body: ChannelCloseRequest):
        if not config.crypto_enabled:
            return JSONResponse(status_code=503, content={
                "success": False, "error": "crypto_disabled",
                "detail": "payment channels disabled by operator (AIFACTORY_CRYPTO_ENABLED=0)",
            })
        result = close_channel(
            channel_id=body.channel_id,
            settle_tx_hash=body.settle_tx_hash,
            wallet=body.wallet or "",
        )
        if result.get("error"):
            return JSONResponse(status_code=400, content=result)
        return result

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
    async def plugin_registry():
        """Serve the static curated plugin registry."""
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

    app.include_router(wellknown_router)
    app.include_router(router)
    app.include_router(capital_alias)

    # ── Landing page & Integration Examples ──────────────────

    from aimarket_hub.landing import DOCS_HTML, INTEGRATION_EXAMPLES_HTML

    hub_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    terminal_home_path = os.path.join(hub_root, "terminal-home.html")

    def _load_terminal_home() -> str:
        if os.path.isfile(terminal_home_path):
            with open(terminal_home_path, encoding="utf-8") as f:
                return f.read()
        return DOCS_HTML

    @app.get("/", response_class=HTMLResponse)
    async def landing_page():
        return HTMLResponse(_load_terminal_home())

    @app.get("/developers", response_class=HTMLResponse)
    async def developers_page():
        return HTMLResponse(DOCS_HTML)

    @app.get("/examples", response_class=HTMLResponse)
    async def integration_examples():
        return HTMLResponse(INTEGRATION_EXAMPLES_HTML)

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
    async def plugins_demo():
        hub_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        demo_html = os.path.join(hub_root, "plugins-demo.html")
        if os.path.isfile(demo_html):
            with open(demo_html, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>Plugin demo not found</h1>", status_code=404)

    @app.get("/widget/demo", response_class=HTMLResponse)
    async def widget_demo():
        demo_html = os.path.join(widget_dir, "demo.html") if widget_dir else None
        if demo_html and os.path.isfile(demo_html):
            with open(demo_html, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>Widget demo not found</h1>", status_code=404)

    @app.get("/live", response_class=HTMLResponse)
    async def live_economy_stream():
        """Legacy URL — same immersive terminal as `/`."""
        return HTMLResponse(_load_terminal_home())

    if widget_dir:
        app.mount("/widget", StaticFiles(directory=widget_dir, html=True), name="widget")

    return app
