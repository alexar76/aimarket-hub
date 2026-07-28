#!/usr/bin/env python3
"""AIMarket Hub CLI — crawl, search, invoke, publish, serve.

Usage:
  aimarket serve                     Start the hub API server
  aimarket publish capability.json   Publish a capability to the hub catalog
  aimarket crawl                     Run a federation crawl cycle
  aimarket search <query>            Search the federated catalog
  aimarket invoke <capability_id>    Invoke a capability
  aimarket peers                     List known peer hubs
  aimarket stats                     Show hub statistics
  aimarket trust <hub_url>           Show trust score for a hub
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.crawler import Crawler
from aimarket_hub.database import HubDatabase
from aimarket_hub.trust import TrustScorer

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def main() -> int:
    parser = argparse.ArgumentParser(description="AIMarket Hub CLI")
    sub = parser.add_subparsers(dest="command")

    # serve
    sub.add_parser("serve", help="Start the hub API server")

    # publish
    publish_p = sub.add_parser("publish", help="Publish a capability manifest")
    publish_p.add_argument(
        "manifest",
        nargs="?",
        default="capability.json",
        help="Path to capability manifest JSON",
    )
    publish_p.add_argument("--hub", default=None, help="Hub base URL (default: HubConfig.hub_url)")
    publish_p.add_argument("--token", default=None, help="AIMARKET_PUBLISH_TOKEN (or env)")

    # crawl
    sub.add_parser("crawl", help="Run federation crawl")

    # search
    search_p = sub.add_parser("search", help="Search capabilities")
    search_p.add_argument("query", nargs="?", default="", help="Search query")
    search_p.add_argument("--hub", default="any", help="Filter by source hub")
    search_p.add_argument("--limit", type=int, default=10)
    search_p.add_argument("--json", action="store_true")

    # invoke
    invoke_p = sub.add_parser("invoke", help="Invoke a capability")
    invoke_p.add_argument("capability_ref", help="product_id/capability_id")
    invoke_p.add_argument("--input", default="{}", help="JSON input payload")
    invoke_p.add_argument("--hub", default="local", help="Source hub")

    # peers
    sub.add_parser("peers", help="List known peers")

    # stats
    sub.add_parser("stats", help="Show hub statistics")

    # trust
    trust_p = sub.add_parser("trust", help="Show trust score")
    trust_p.add_argument("hub_url", help="Hub URL")

    args = parser.parse_args()

    if args.command == "serve":
        return _cmd_serve()
    elif args.command == "publish":
        return _cmd_publish(args)
    elif args.command == "crawl":
        return _cmd_crawl()
    elif args.command == "search":
        return _cmd_search(args)
    elif args.command == "invoke":
        return _cmd_invoke(args)
    elif args.command == "peers":
        return _cmd_peers()
    elif args.command == "stats":
        return _cmd_stats()
    elif args.command == "trust":
        return _cmd_trust(args)
    else:
        parser.print_help()
        return 0


def uvicorn_proxy_kwargs(raw: str, *, production: bool) -> dict[str, object]:
    """uvicorn keyword args that make the PROXIED client address trustworthy — and only
    from the proxy.

    ``uvicorn.run(app)`` was called with no proxy configuration at all, so uvicorn's
    default (``forwarded_allow_ips="127.0.0.1"``, or whatever ``FORWARDED_ALLOW_IPS``
    happened to say) decided the question implicitly. Behind
    ``deploy/nginx/modelmarket.dev.conf`` the hub's peer is the proxy on every request,
    so the per-IP channel cap and the invoke rate limiter saw ONE address for the whole
    internet — a single shared bucket, and nothing bounding the LRU eviction those
    limiters lean on. From a container the peer is the bridge gateway, which is not in
    uvicorn's default list either, so the forwarded header was ignored outright.

    Contract (same env var the app-side ``_client_address`` reads, so both layers agree):
      * ``AIMARKET_TRUSTED_PROXIES`` unset/empty → proxy headers are NOT processed at all.
        A directly exposed hub must never believe a caller's own ``X-Forwarded-For``.
      * set to the proxy address(es) → uvicorn trusts the header from exactly those peers.
      * ``*`` is refused: blanket trust lets every caller forge its own client address and
        mint a fresh rate-limit bucket per request. In production that is a hard startup
        failure (silently serving with forgeable rate-limit keys is worse than not
        starting); elsewhere it degrades to "no proxy trusted", loudly.
    """
    entries = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if "*" in entries:
        message = (
            "AIMARKET_TRUSTED_PROXIES=* is refused: blanket-trusting X-Forwarded-For "
            "lets every caller forge its own client address and defeat the per-IP rate "
            "limits. List the proxy address(es) explicitly (e.g. 127.0.0.1)."
        )
        if production:
            print(f"{RED}Production startup refused: {message}{RESET}", file=sys.stderr)
            raise SystemExit(1)
        print(f"{YELLOW}[proxy]{RESET} {message} Ignoring the header.")
        entries = []
    trusted = [e for e in entries if e != "*"]
    if not trusted:
        if production:
            print(
                f"{YELLOW}[proxy]{RESET} AIMARKET_TRUSTED_PROXIES not set — per-IP rate "
                "limits key on the immediate peer. Behind a reverse proxy that is the "
                "PROXY's address, so every caller shares one bucket."
            )
        return {"proxy_headers": False}
    print(f"{GREEN}[proxy]{RESET} trusting X-Forwarded-For from {', '.join(trusted)}")
    return {"proxy_headers": True, "forwarded_allow_ips": ",".join(trusted)}


def _cmd_serve() -> int:
    import os

    import uvicorn

    config = HubConfig()
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db)

    # Warn about active stub/dev flags at startup
    stub_flags = []
    if os.environ.get("AIMARKET_ZK_SIMULATED", "").strip() == "1":
        stub_flags.append("AIMARKET_ZK_SIMULATED=1 (ZK proofs are simulated — no cryptographic privacy)")
    if os.environ.get("AIFACTORY_PAYMENT_VERIFY_STUB", "").strip() == "1":
        stub_flags.append("AIFACTORY_PAYMENT_VERIFY_STUB=1 (all tx hashes accepted without on-chain check)")
    if os.environ.get("AIFACTORY_PAYMENT_TESTNET", "").strip() == "1":
        stub_flags.append("AIFACTORY_PAYMENT_TESTNET=1 (testnet mode — demo tx hashes accepted)")
    if os.environ.get("AIMARKET_TEE_SOFTWARE_OK", "").strip() == "1":
        stub_flags.append("AIMARKET_TEE_SOFTWARE_OK=1 (software TEE attestation — no hardware guarantee)")
    prod = os.environ.get("AIFACTORY_PROD", "").strip() == "1"
    if prod:
        stub_flags.append("AIFACTORY_PROD=1 (production mode — stub flags will HARD-BLOCK startup)")
    for flag in stub_flags:
        print(f"{YELLOW}[stub]{RESET} {flag}")

    # Enforce what the banner promises. Previously startup only WARNED and then
    # ran uvicorn regardless, so a standalone hub with AIFACTORY_PROD=1 and
    # AIFACTORY_PAYMENT_VERIFY_STUB=1 would start and credit payment channels for
    # free (no on-chain verification). Prefer the full production guard when the
    # security module is importable; otherwise enforce the payment/ZK stub gate
    # inline so the standalone package still fails closed.
    if prod:
        try:
            from security.prod_startup_guard import assert_production_startup_safe

            assert_production_startup_safe(exit_on_failure=True)
        except ImportError:
            dangerous = {
                "AIFACTORY_PAYMENT_VERIFY_STUB": "1",
                "AIFACTORY_PAYMENT_TESTNET": "1",
                "AIMARKET_ZK_SIMULATED": "1",
                "AIMARKET_TEE_SOFTWARE_OK": "1",
            }
            active = [k for k, v in dangerous.items() if os.environ.get(k, "").strip() == v]
            if active:
                print(
                    f"{RED}Production startup refused: AIFACTORY_PROD=1 with unsafe stub "
                    f"flag(s) active: {', '.join(active)}. Set them to 0 before running "
                    f"in production.{RESET}",
                    file=__import__("sys").stderr,
                )
                raise SystemExit(1)

            # Stub flags were the only thing this fallback checked, so a production hub
            # could boot with an Anvil recipient and route real deposits to a wallet whose
            # private key is public. The addresses live in aimarket_hub.config precisely
            # because the `security` package is absent from the standalone image.
            from aimarket_hub.config import is_dev_chain_address

            for var in ("AIMARKET_PAYMENT_RECIPIENT", "AIMARKET_ESCROW_EVM_ADDRESS"):
                value = os.environ.get(var, "").strip()
                if value and is_dev_chain_address(value):
                    print(
                        f"{RED}Production startup refused: {var}={value} is an Anvil/Hardhat "
                        f"dev-chain address. Its private key is public — real funds sent there "
                        f"are sweepable by anyone. Set a wallet you control.{RESET}",
                        file=__import__("sys").stderr,
                    )
                    raise SystemExit(1)

    proxy_kwargs = uvicorn_proxy_kwargs(
        os.environ.get("AIMARKET_TRUSTED_PROXIES", ""), production=prod
    )

    print(f"{GREEN}Hub API starting on http://0.0.0.0:9083{RESET}")
    uvicorn.run(app, host="0.0.0.0", port=9083, log_level="info", **proxy_kwargs)
    return 0


def _cmd_publish(args) -> int:
    import httpx

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"{YELLOW}Manifest not found: {manifest_path}{RESET}")
        return 1

    config = HubConfig()
    hub_url = (args.hub or config.hub_url).rstrip("/")
    token = (args.token or os.environ.get("AIMARKET_PUBLISH_TOKEN", "")).strip()
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"{BOLD}Publish → {hub_url}{RESET}")
    print(f"  {body.get('product_id')}/{body.get('capability_id')}")

    try:
        resp = httpx.post(
            f"{hub_url}/ai-market/v2/supply/register",
            json=body,
            headers=headers,
            timeout=30,
        )
        data = resp.json()
        if resp.status_code == 200:
            print(f"  {GREEN}✓ published{RESET}  ${data.get('price_per_call_usd', '?')}/call")
            print(f"  search: {data.get('search_hint', '')}")
            return 0
        print(f"  {YELLOW}{resp.status_code}{RESET} {data.get('detail', data)}")
        return 1
    except Exception as exc:
        print(f"  Error: {exc}")
        return 1


def _cmd_crawl() -> int:
    config = HubConfig()
    db = HubDatabase(config.db_path)
    crawler = Crawler(config=config, db=db)

    async def _run():
        try:
            return await crawler.crawl()
        finally:
            await crawler.close()

    stats = asyncio.run(_run())
    print(json.dumps(stats, indent=2))
    return 0


def _cmd_search(args) -> int:
    config = HubConfig()
    db = HubDatabase(config.db_path)
    results = db.search_capabilities(args.query, limit=args.limit)

    if args.json:
        print(json.dumps([{
            "capability_id": r.capability_id,
            "product_id": r.product_id,
            "source_hub": r.source_hub,
            "source_hub_name": r.source_hub_name,
            "price": r.price_per_call_usd,
            "routed_price": r.routed_price_usd,
            "trust": r.trust_score,
            "description": r.description,
        } for r in results], indent=2))
        return 0

    print(f"\n{BOLD}Search: \"{args.query}\"{RESET}\n")
    for i, r in enumerate(results[:args.limit], 1):
        hub_tag = f"{GREEN}[{r.source_hub_name or 'local'}]{RESET}"
        trust = r.trust_score
        price = r.routed_price_usd or r.price_per_call_usd
        print(f"  {i}. {BOLD}{r.capability_id}{RESET} @ {r.product_id}  {hub_tag}")
        print(f"     {r.description[:100]}")
        print(f"     ${price:.2f}  trust={trust:.2f}  latency={r.p50_latency_ms}ms\n")
    return 0


def _cmd_invoke(args) -> int:
    import httpx

    config = HubConfig()
    parts = args.capability_ref.split("/", 1)
    product_id = parts[0]
    capability_id = parts[1] if len(parts) > 1 else parts[0]
    inp = json.loads(args.input)

    print(f"{BOLD}Invoke: {product_id}/{capability_id}{RESET}")
    print(f"  Target hub: {args.hub}")

    try:
        resp = httpx.post(
            f"{config.hub_url}/ai-market/v2/invoke",
            json={
                "product_id": product_id,
                "capability_id": capability_id,
                "source_hub": args.hub,
                "input": inp,
            },
            timeout=60,
        )
        if resp.status_code == 402:
            print(f"  {DIM}402 Payment Required — see X-Payment-Required{RESET}")
            print(json.dumps(resp.json(), indent=2))
            return 0
        data = resp.json()
        ok = data.get("success", False)
        mark = "✓" if ok else "✗"
        color = GREEN if ok else RESET
        print(f"  {color}{mark} ${data.get('price_usd', '?')}  {data.get('latency_ms', '?')}ms{RESET}")
        if ok:
            print(json.dumps(data.get("result", {}), indent=2))
        return 0 if ok else 1
    except Exception as exc:
        print(f"  Error: {exc}")
        return 1


def _cmd_peers() -> int:
    config = HubConfig()
    db = HubDatabase(config.db_path)
    peers = db.list_peers()

    print(f"\n{BOLD}Known Peers ({len(peers)}){RESET}\n")
    for p in peers:
        print(f"  {BOLD}{p.name}{RESET}  {DIM}{p.url}{RESET}")
        print(f"    capabilities: {p.capabilities_count}  trust: {p.trust_score:.2f}  depth: {p.depth}")
        print(f"    last crawl: {p.last_crawl}\n")
    return 0


def _cmd_stats() -> int:
    config = HubConfig()
    db = HubDatabase(config.db_path)
    s = db.stats_summary()

    print(f"\n{BOLD}Hub Statistics{RESET}\n")
    for k, v in s.items():
        label = k.replace("_", " ").title()
        print(f"  {label}: {GREEN}{v}{RESET}")
    return 0


def _cmd_trust(args) -> int:
    config = HubConfig()
    db = HubDatabase(config.db_path)
    scorer = TrustScorer(db)
    score = scorer.compute_score(args.hub_url)
    details = scorer.score_details(args.hub_url)

    print(f"\n{BOLD}Trust Score: {args.hub_url}{RESET}\n")
    print(f"  Score: {GREEN}{score:.4f}{RESET}")
    print(f"  Weights: {details.get('weights', {})}")
    return 0


def _entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _entrypoint()
