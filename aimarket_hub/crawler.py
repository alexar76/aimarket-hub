"""Federation crawler — discovers peer hubs via .well-known/ai-market.json.

Implements BFS crawl: seed list → discover peers → crawl their manifests.
SSRF-hardened, response-size-limited, validates prices, pins public keys.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from ipaddress import ip_address, ip_network
from typing import Any
from urllib.parse import urlparse

import httpx

from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
from aimarket_hub.signing import Signer
from aimarket_hub.trust import TrustScorer
from aimarket_hub.validator import validate_manifest, validate_well_known

logger = logging.getLogger(__name__)

USER_AGENT = "AIMarketHub/2.0.0"

# Max response body size for well-known and manifest fetches
MAX_RESPONSE_BYTES = 2_000_000  # 2 MB

# Blocked IP ranges (RFC1918, link-local, loopback, metadata services)
_BLOCKED_NETS = [
    ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"), ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"), ip_network("0.0.0.0/8"),
    ip_network("100.64.0.0/10"), ip_network("224.0.0.0/4"),
    ip_network("fc00::/7"), ip_network("fe80::/10"),
    ip_network("::1/128"),
]

# Price/latency/success bounds for imported capabilities
MIN_PRICE_USD = 0.0
MAX_PRICE_USD = 1000.0
MIN_LATENCY_MS = 0
MAX_LATENCY_MS = 300_000   # 5 minutes
MIN_SUCCESS_RATE = 0.0
MAX_SUCCESS_RATE = 1.0
MAX_CAPABILITIES_PER_MANIFEST = 1000


def _is_private_url(url: str) -> bool:
    """Check if URL resolves to a private/blocked network.

    Resolves DNS names via getaddrinfo and checks ALL resolved IPs against the
    blocklist. NOTE: this is a point-in-time check only — a caller that then
    connects by hostname re-resolves at connect time, so on its own this does NOT
    stop DNS rebinding. For rebinding-resistant fetches use
    outbound_http.safe_get / safe_post, which pin the validated IP (http + https).
    """
    import socket

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return True

        # Try parsing as literal IP first
        try:
            addr = ip_address(hostname)
            for net in _BLOCKED_NETS:
                if addr in net:
                    return True
            # Also reject IPv4-mapped IPv6 to private IPv4
            if hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped is not None:
                for net in _BLOCKED_NETS:
                    if addr.ipv4_mapped in net:
                        return True
            return False
        except ValueError:
            pass  # not an IP literal, fall through to DNS resolution

        # DNS name — resolve and check ALL returned IPs
        try:
            addrinfos = socket.getaddrinfo(hostname, None)
        except (socket.gaierror, UnicodeError):
            # Can't resolve — treat as unsafe (don't fetch unresolvable hosts)
            return True
        for _family, _, _, _, sockaddr in addrinfos:
            ip_str = sockaddr[0]
            try:
                addr = ip_address(ip_str)
            except ValueError:
                continue
            for net in _BLOCKED_NETS:
                if addr in net:
                    return True
            if hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped is not None:
                for net in _BLOCKED_NETS:
                    if addr.ipv4_mapped in net:
                        return True
        return False
    except Exception:
        # Defense in depth: if anything goes wrong, treat as unsafe
        return True


def _url_is_safe(url: str) -> bool:
    """Reject dangerous URL schemes and internal hosts."""
    if not url:
        return False
    if not url.startswith(("https://", "http://")):
        return False
    if any(c in url for c in "\r\n\t"):
        return False
    return not _is_private_url(url)


def _coerce_categories(value: Any) -> list[str]:
    """Sanitize a peer's self-declared categories — bounded count, strings only."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:32]:
        if isinstance(item, str):
            s = item.strip()[:64]
            if s:
                out.append(s)
    return out


def _coerce_count(value: Any) -> int:
    """Clamp a peer-advertised count to a sane non-negative range."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 1_000_000_000))


class Crawler:
    """BFS federation crawler — SSRF-hardened, key-pinning, bounded."""

    def __init__(
        self,
        config: HubConfig | None = None,
        db: HubDatabase | None = None,
        signer: Signer | None = None,
        trust_scorer: TrustScorer | None = None,
        slash_registry: Any = None,
    ):
        self.config = config or HubConfig()
        self.db = db or HubDatabase(self.config.db_path)
        self.signer = signer or Signer(self.config.signing_key_path)
        self.trust_scorer = trust_scorer or TrustScorer(self.db)
        self.slash_registry = slash_registry  # optional slash_sync.SlashRegistry for federated pull
        self._http = httpx.AsyncClient(
            timeout=self.config.request_timeout_s,
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
            ),
        )

    async def _safe_get(self, url: str) -> httpx.Response:
        """Fetch a URL with size limits and SSRF protection.

        Disables redirects (each redirect target must pass _url_is_safe).
        Validates Content-Type is JSON-like.
        """
        if not _url_is_safe(url):
            raise ValueError(f"Unsafe URL rejected: {url[:60]}")

        # follow_redirects=False — each hop would need its own _url_is_safe check.
        # If the server returns 3xx, the caller can re-fetch with the new URL
        # explicitly (after re-running _url_is_safe).
        resp = await self._http.get(
            url,
            headers={"User-Agent": USER_AGENT, "X-AIMarket-Crawler": self.config.hub_url},
            follow_redirects=False,
        )
        # Check Content-Length before reading body
        content_length = resp.headers.get("content-length")
        if content_length and int(content_length) > MAX_RESPONSE_BYTES:
            raise ValueError(f"Response too large: {content_length} bytes")
        resp.raise_for_status()
        # Validate Content-Type — only accept JSON
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "json" not in ctype:
            raise ValueError(f"Unexpected Content-Type: {ctype[:60]}")
        # Read with size limit
        body = b""
        async for chunk in resp.aiter_bytes(chunk_size=65536):
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("Response body exceeds 2 MB limit")
        resp._content = body
        return resp

    async def crawl(self, *, clear_first: bool = True) -> dict[str, Any]:
        """Run a full crawl cycle. Returns summary dict.

        When clear_first=True, old federated capabilities are cleared before
        re-crawling. SQLite WAL mode + single DELETE makes this atomic —
        if the crawl fails, the next successful crawl will repopulate.
        """
        # Don't clear before crawl — clear + repopulate atomically after success.
        # This prevents the empty-catalog DoS window (EXP-34).
        self.db.count_federated()

        logger.info("Crawler starting with %d seeds", len(self.config.seed_list))
        visited: set[str] = set()
        queue: deque[tuple[str, int, str]] = deque()

        # Validate seed URLs
        safe_seeds = [s for s in self.config.seed_list if _url_is_safe(s)]
        for seed_url in safe_seeds:
            queue.append((seed_url, 0, "seed"))

        stats = {"discovered": 0, "indexed": 0, "errors": 0, "peers_found": 0}

        while queue:
            url, depth, discoverer = queue.popleft()
            if url in visited:
                continue
            # SSRF guard: reject internal URLs discovered mid-crawl
            if not _url_is_safe(url):
                logger.warning("Skipping unsafe peer URL: %s", url[:60])
                continue
            visited.add(url)

            if depth > self.config.max_crawl_depth:
                continue

            result = await self._crawl_one(url, depth, discoverer)
            if result is None:
                stats["errors"] += 1
                continue

            stats["discovered"] += 1
            stats["indexed"] += result["capabilities_count"]
            stats["peers_found"] += len(result.get("new_peer_urls", []))

            for peer_url in result.get("new_peer_urls", []):
                if peer_url not in visited:
                    queue.append((peer_url, depth + 1, url))

        slashes_pulled = 0
        for peer in self.db.list_peers():
            score = self.trust_scorer.compute_score(peer.url)
            peer.trust_score = score
            self.db.upsert_peer(peer)
            # Federated reputation (F2): pull the peer's signed slash log, bound to its key.
            if self.slash_registry is not None and getattr(peer, "public_key", ""):
                slashes_pulled += await self._pull_peer_slashes(
                    peer.url, peer.public_key, trusted=bool(getattr(peer, "trusted", False)),
                )

        if slashes_pulled:
            stats["slashes_pulled"] = slashes_pulled
        logger.info(
            "Crawl complete: %d discovered, %d indexed, %d errors, %d slashes",
            stats["discovered"], stats["indexed"], stats["errors"], slashes_pulled,
        )
        return stats

    async def _pull_peer_slashes(self, peer_url: str, peer_pubkey: str, *, trusted: bool = False) -> int:
        """Fetch and ingest a peer's signed slash log, binding issuer identity to the peer's
        published key (a peer may only vouch for slashes it signed). Fault-tolerant: a failing
        peer is skipped, never aborting the crawl.

        F6 anti-poisoning: the weak tier (no consumer PoM) is only accepted from
        **operator-TRUSTED** peers. From a first-contact / unapproved peer we accept
        only STRONG (consumer-signed) attestations — those are independently verifiable
        and cannot be forged without a real wronged consumer, so an untrusted peer can
        never manufacture a federated penalty. Without this gate, two throwaway Sybil
        hubs could weak-slash a competitor (the very attack ``require_pom`` prevents).
        """
        url = peer_url.rstrip("/") + "/ai-market/v2/reputation/slashes"
        try:
            if not _url_is_safe(url):
                return 0
            resp = await self._safe_get(url)
            if resp.status_code != 200:
                return 0
            envelopes = resp.json().get("slashes", [])
            # Weak tier is opt-in (AIMARKET_SLASH_ACCEPT_WEAK, default on) AND only from
            # trusted peers; a slash still needs ≥2 distinct weak issuers to move the
            # penalty, so approved peers cannot single-handedly slash either.
            weak_enabled = os.environ.get("AIMARKET_SLASH_ACCEPT_WEAK", "1").strip() != "0"
            accept_weak = weak_enabled and trusted
            return self.slash_registry.ingest_remote(
                envelopes, verifier=self.signer,
                expected_issuer_pubkey=peer_pubkey, accept_weak=accept_weak,
            )
        except Exception as exc:
            logger.debug("slash pull from %s failed: %s", peer_url[:60], exc)
            return 0

    async def _crawl_one(
        self, well_known_url: str, depth: int, discoverer: str,
    ) -> dict[str, Any] | None:
        """Crawl a single hub via its .well-known endpoint."""
        base_url = well_known_url.rsplit("/.well-known/", 1)[0]

        # 1. Fetch .well-known (SSRF-hardened)
        try:
            resp = await self._safe_get(well_known_url)
            if resp.status_code == 304:
                return {"capabilities_count": 0, "new_peer_urls": []}
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", well_known_url, exc)
            return None

        try:
            wk = resp.json()
        except json.JSONDecodeError:
            logger.warning("Invalid JSON at %s", well_known_url)
            return None

        # 2. Validate well-known structure
        errors = validate_well_known(wk)
        if errors:
            logger.warning("Invalid well-known at %s: %s", well_known_url, errors)
            return None

        # 3. Record or update peer (pubkey pinning)
        hub_name = wk.get("name", base_url)
        categories = _coerce_categories(wk.get("categories"))
        existing_peer = self.db.get_peer(base_url)
        advertised_key = wk.get("signer_public_key", "")
        prior_key = existing_peer.public_key if existing_peer else ""

        # Operator-vouched trusted seed: if we pinned this seed's key out of band
        # (federation_seeds.json / AIMARKET_SEED_PUBKEYS) and the peer advertises
        # exactly that key, trust + index it on FIRST contact instead of waiting
        # for manual approval. A key mismatch falls back to the safe path below.
        pinned_seed_key = (
            self.config.seed_pubkeys.get(base_url)
            or self.config.seed_pubkeys.get(well_known_url)
        )
        trusted_via_seed = bool(
            pinned_seed_key and advertised_key and advertised_key == pinned_seed_key
        )
        is_first_contact = not (prior_key or trusted_via_seed)

        if prior_key:
            # Subsequent crawl: pubkey MUST match what we pinned previously.
            if advertised_key and advertised_key != prior_key:
                logger.warning(
                    "Peer %s public key changed! Rejecting (possible takeover). "
                    "Old: %s... New: %s...",
                    base_url, prior_key[:16], advertised_key[:16],
                )
                return None
            pinned_key = prior_key
        elif trusted_via_seed:
            pinned_key = pinned_seed_key
            logger.info(
                "Trusted seed %s — advertised key matches operator pin; "
                "indexing on first contact.", base_url,
            )
        else:
            # First contact, untrusted: record peer but DO NOT trust the pubkey
            # until an operator approves it (or pins it via AIMARKET_SEED_PUBKEYS).
            pinned_key = advertised_key
            if pinned_seed_key and advertised_key and advertised_key != pinned_seed_key:
                logger.warning(
                    "Seed %s advertised key %s... does NOT match operator pin %s... "
                    "— treating as untrusted first contact.",
                    base_url, advertised_key[:16], pinned_seed_key[:16],
                )
            else:
                logger.info(
                    "First contact with peer %s — recording but not trusting "
                    "manifest until manual approval. Advertised pubkey: %s...",
                    base_url, advertised_key[:16] if advertised_key else "(none)",
                )

        # A peer's manifests are indexed ONLY when trusted: either operator-pinned via seed, or
        # explicitly approved before (preserved across crawls). Being crawled twice never grants
        # trust — this closes the TOFU manifest-injection hole.
        trusted = trusted_via_seed or bool(existing_peer and existing_peer.trusted)

        peer = Peer(
            url=base_url,
            name=hub_name,
            capabilities_count=_coerce_count(wk.get("capabilities_count", 0)),
            last_crawl=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            well_known_url=well_known_url,
            manifest_url=wk.get("manifest_url", f"{base_url}/ai-market/manifest"),
            public_key=pinned_key,
            depth=depth,
            discoverer=discoverer,
            categories=categories,
            trusted=trusted,
        )
        self.db.upsert_peer(peer)

        # 4. Index manifests from TRUSTED peers only. An unapproved peer (first contact or not)
        #    is recorded as pending; an operator must approve it (db.set_peer_trusted / admin
        #    endpoint) or pin its key via AIMARKET_SEED_PUBKEYS.
        if not trusted:
            logger.info(
                "Peer %s pending operator approval — manifest indexing skipped", base_url,
            )
            return {"capabilities_count": 0, "new_peer_urls": []}

        # Fetch and index manifest (trusted peers only)
        manifest_url = wk.get("manifest_url", f"{base_url}/ai-market/manifest")
        if not _url_is_safe(manifest_url):
            logger.warning("Manifest URL rejected as unsafe: %s", manifest_url[:60])
            return {"capabilities_count": 0, "new_peer_urls": self._extract_peer_urls(wk)}
        indexed = 0
        try:
            manifest_resp = await self._safe_get(manifest_url)
            manifest = manifest_resp.json()

            # Validate manifest structure
            m_errors = validate_manifest(manifest)
            if m_errors:
                logger.warning("Invalid manifest at %s: %s", manifest_url, m_errors)
                return None

            # Verify signature
            if not self.signer.verify_manifest_signature(manifest, pinned_key):
                logger.warning("Invalid manifest signature at %s", manifest_url)
                return None

            # Freshness: reject a stale/replayed (or far-future) signed manifest.
            if self._manifest_too_stale(manifest):
                logger.warning(
                    "Manifest at %s rejected as stale/replayed (generated_at=%s)",
                    manifest_url, manifest.get("generated_at"),
                )
                return {"capabilities_count": 0, "new_peer_urls": self._extract_peer_urls(wk)}

            # Index capabilities
            indexed = self._index_manifest_capabilities(manifest, base_url, hub_name)

        except Exception as exc:
            logger.warning("Failed to fetch/parse manifest at %s: %s", manifest_url, exc)
            # Still return peer data even if manifest fetch fails
            return {"capabilities_count": 0, "new_peer_urls": self._extract_peer_urls(wk)}

        return {
            "capabilities_count": indexed,
            "new_peer_urls": self._extract_peer_urls(wk),
        }

    def _index_manifest_capabilities(
        self, manifest: dict[str, Any], base_url: str, hub_name: str,
    ) -> int:
        """Index validated capabilities from a manifest into the database.

        All prices, latencies, and success rates are sanity-checked.
        Malicious values are clamped or cause the tool to be skipped.
        """
        count = 0
        tools = manifest.get("tools") or []
        if len(tools) > MAX_CAPABILITIES_PER_MANIFEST:
            logger.warning(
                "Manifest from %s has %d tools — limiting to %d",
                base_url, len(tools), MAX_CAPABILITIES_PER_MANIFEST,
            )
            tools = tools[:MAX_CAPABILITIES_PER_MANIFEST]

        for tool in tools:
            if not isinstance(tool, dict):
                continue

            # Validate price (EXP-29)
            price = float(tool.get("price_per_call_usd", 0.35))
            if price < MIN_PRICE_USD or price > MAX_PRICE_USD:
                logger.warning(
                    "Skipping tool %s: price %.2f out of range [%.2f, %.2f]",
                    tool.get("capability_id", "?"), price, MIN_PRICE_USD, MAX_PRICE_USD,
                )
                continue

            # Validate latency
            latency = int(tool.get("p50_latency_ms", 3000))
            if latency < MIN_LATENCY_MS or latency > MAX_LATENCY_MS:
                latency = max(MIN_LATENCY_MS, min(MAX_LATENCY_MS, latency))

            # Ignore peer-declared success_rate — we compute it ourselves from
            # observed invocations. Trusting peer-declared values lets a malicious
            # peer claim 99% trust and dominate routing on first index (NEW-7).
            # Start at a neutral baseline; trust_scorer will update over time.
            success = 0.5

            cap = Capability(
                capability_id=tool.get("capability_id", tool.get("name", "")),
                product_id=tool.get("product_id", ""),
                name=tool.get("name", tool.get("capability_id", "")),
                version="v1",
                description=tool.get("description", ""),
                input_schema=tool.get("input_schema", {}),
                output_schema=tool.get("output_schema", {}),
                price_per_call_usd=price,
                p50_latency_ms=latency,
                success_rate_30d=success,
                source_hub=base_url,
                source_hub_name=hub_name,
                routed_price_usd=self._routed_price(price),
                routing_fee_bps=self.config.routing_fee_bps,
                trust_score=0.5,
            )
            self.db.upsert_capability(cap)
            count += 1
        return count

    def _routed_price(self, base_price: float) -> float:
        # Use integer cents internally to avoid IEEE 754 drift
        cents = round(base_price * 100)
        routed_cents = round(cents * (1 + self.config.routing_fee_bps / 10000))
        return routed_cents / 100.0

    def _manifest_too_stale(self, manifest: dict[str, Any]) -> bool:
        """True if the manifest's signed `generated_at` is older than the
        configured max age (replay) or implausibly far in the future (clock
        spoof). Lenient: a missing/unparseable timestamp or disabled limit
        returns False so well-formed live peers are never rejected."""
        max_age = getattr(self.config, "manifest_max_age_s", 0) or 0
        if max_age <= 0:
            return False
        raw = manifest.get("generated_at")
        if not isinstance(raw, str) or not raw:
            return False
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age > max_age or age < -3600

    def _extract_peer_urls(self, well_known: dict[str, Any]) -> list[str]:
        """Extract peer hub URLs from a well-known response."""
        urls: list[str] = []
        peers = well_known.get("peers") or []
        for p in peers:
            if isinstance(p, dict) and p.get("url"):
                urls.append(p["url"])
        # Also crawl seed list entries from the federation block
        federation = well_known.get("federation") or {}
        seed_list = federation.get("seed_list") or []
        for s in seed_list:
            if isinstance(s, str) and s not in urls:
                urls.append(s.rsplit("/.well-known/", 1)[0])
        return urls

    async def close(self) -> None:
        await self._http.aclose()
