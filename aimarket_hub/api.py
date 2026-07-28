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
import math
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
from aimarket_hub import verified_settlement
from aimarket_hub.verified_settlement import VerifiedSettlementService

# Hub-local invokes record this instead of hub_url so /stats/live does not
# inflate "external federation" traffic when the operator smoke-tests caps.
OPERATOR_SELF_CONSUMER = "operator_self"

from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.demo_seeder import seed_capabilities
from aimarket_hub.fulfillment import capability_is_fulfillable
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
            "Content-Type", "Authorization", "X-Payment-Channel", "X-Payment-Channel-Secret",
            "X-AIMarket-Affiliate",
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
            # Offerable, not stored: these three numbers are how a peer sizes us up, and
            # they must match what /v2/manifest is willing to list.
            "products_count": db.count_offerable("local"),
            "capabilities_count": db.count_offerable("local"),
            "federated_capabilities_count": db.count_offerable(),
            "supported_chains": config.payment_chains,
            "supported_tokens": config.payment_tokens,
            # Payment recipient hidden from public manifest — exposed only when stub mode
            # `payment_ready` (not `not stub`): stub off with AIFACTORY_PROD unset skips
            # on-chain verification entirely, so the old flag could advertise payments as
            # configured on a hub that credits any tx_hash. See HubConfig.payment_readiness.
            "payment_configured": config.payment_ready,
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
        # The manifest is the federation's and every MCP client's view of what is on
        # sale here, so it gets the same gate as /search: a row this hub cannot execute
        # is not a tool, and advertising it to peers propagates the dead listing.
        caps = [c for c in db.list_capabilities(limit=1000) if capability_is_fulfillable(c)]
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
            # Counted off the filtered list, not the table: a count that includes rows
            # absent from `tools` tells a peer we have stock we will not show it.
            "local_capabilities": sum(1 for c in caps if c.source_hub == "local"),
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
        category: str | None = None,
        include_demo: bool = False,
        limit: int = 20,
    ):
        limit = min(limit, 100)
        results = db.search_capabilities(intent, limit=limit * 2)
        results = supply_security.filter_for_discover(results)
        floor = min_trust if min_trust is not None else supply_security.policy.min_trust_discover
        category_key = (category or "").strip().lower()

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
            "category": category_key or None,
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
        publisher_id = str(body.get("publisher_id", "")).strip()
        if not publisher_id:
            # Named before the auth check on purpose: the credential is scoped TO the
            # publisher_id, so there is nothing to authorize without one.
            raise HTTPException(status_code=400, detail="publisher_id required")
        _require_stake_subject(authorization, publisher_id)
        amount = _money_arg(body.get("amount_usd", 0), "amount_usd")
        tx_hash = str(body.get("tx_hash", "")).strip()
        payer_signature = str(body.get("payer_signature", "") or "").strip()
        if (
            amount > 0
            and not tx_hash
            and _is_production_mode()
            and not supply_security.policy.relaxed
        ):
            raise HTTPException(
                status_code=400,
                detail="tx_hash required for stake deposits in production "
                       "(AIMARKET_SUPPLY_SECURITY_RELAXED=1 to bypass for dev)",
            )
        try:
            return {
                **supply_security.stake(
                    publisher_id, amount, tx_hash, payer_signature=payer_signature
                ),
                "protocol_version": "v2",
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/supply/register")
    async def supply_register(
        body: dict[str, Any],
        authorization: str = Header(default=""),
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
        publisher_id = manifest_publisher_id(body)
        if not publisher_id:
            raise HTTPException(
                status_code=400,
                detail="publisher_id is required (wallet address or stable publisher slug)",
            )
        _require_subject_credential(authorization, publisher_id)
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
    ):
        sandbox_mode = bool(x_sandbox_visitor and sandbox_enabled())
        # Master crypto switch (default OFF): when off, every invoke is served FREE —
        # no 402, no payment channel, no debit. Capabilities still execute normally.
        crypto_on = config.crypto_enabled
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
            # Crypto off → free tier: charge nothing and never reserve/debit a channel.
            price = 0.0 if (sandbox_mode or not crypto_on) else list_price

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

            if price > 0 and x_payment_channel:
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

            try:
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
                if vs_plan is not None and vs_plan.active and vs_plan.paid:
                    # Deferred settlement: the hold STAYS reserved. Ownership passes to
                    # the verified_settlements row at register() below, which is where
                    # `resolved` flips — until then `finally` still owns the release.
                    if not reserved:
                        # Unreachable today (plan.paid implies a priced invoke on a
                        # channel, which always reserves above). If the two predicates
                        # ever drift apart, refuse instead of registering a "paid"
                        # verified settlement with no money behind it.
                        logger.error(
                            "invoke: verified settlement planned for %s without a reservation",
                            nonce,
                        )
                        raise HTTPException(
                            status_code=502,
                            detail="settlement failed: no reservation backs the verified invoke",
                        )
                elif reserved:
                    capture = capture_hold(nonce)
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
            finally:
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
                        release = release_hold(nonce)
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

        headers: dict[str, str] = {
            "X-AIMarket-Routing-Hub": config.hub_url,
            "X-AIMarket-Routing-Fee": str(config.routing_fee_bps),
        }
        if x_payment_channel:
            headers["X-Payment-Channel"] = x_payment_channel

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

        # A peer controls these numbers, so coerce defensively: a string/None price
        # would otherwise blow up the fee arithmetic (500) or the stat insert, and a
        # merely huge finite latency (1e30) is fatal to the SQLite INTEGER column.
        price = _as_float(result_data.get("price_usd"), max_value=_MAX_REPORTED_PRICE_USD)
        stat = InvocationStat(
            capability_id=body.capability_id,
            product_id=body.product_id,
            source_hub=body.source_hub,
            price_usd=price,
            latency_ms=int(_as_float(
                result_data.get("latency_ms"), max_value=_MAX_REPORTED_LATENCY_MS,
            )),
            success=bool(result_data.get("success", False)),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            consumer_hub=OPERATOR_SELF_CONSUMER,
        )
        db.record_invocation(stat)

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
        fee_basis = min(price, listed_price)
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
        if federated_verify_env is not None:
            result_data["verification"] = federated_verify_env
        return result_data

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
    async def channel_close(body: ChannelCloseRequest, request: Request):
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

    mcp_router = APIRouter(prefix="/ai-market", tags=["mcp"])
    attach_mcp_routes(mcp_router, db=db, hub_url=config.hub_url)
    app.include_router(mcp_router)

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

    # The HTML handlers below are SYNC on purpose — they stat() and read() files from
    # disk, which must not happen on the event loop (it would stall concurrent
    # /invoke settlement). FastAPI dispatches sync handlers to its threadpool.
    @app.get("/", response_class=HTMLResponse)
    def landing_page():
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
    def plugins_demo():
        hub_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        demo_html = os.path.join(hub_root, "plugins-demo.html")
        if os.path.isfile(demo_html):
            with open(demo_html, encoding="utf-8") as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>Plugin demo not found</h1>", status_code=404)

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

    if widget_dir:
        app.mount("/widget", StaticFiles(directory=widget_dir, html=True), name="widget")

    return app
