#!/usr/bin/env python3
"""Load on the hub's money path, then an audit of the ledger it leaves behind.

The ecosystem's Locust mix (scripts/load/ in the monorepo) is read-only on purpose: it never
opens a paid invoke. This is the other half. It starts a real hub over HTTP on a throwaway
database (SQLite by default, PostgreSQL with --database-url), then, at the chosen concurrency:

* paid invokes on the credits rail from many accounts at once;
* the same paid invoke retried 3 times at once under one Idempotency-Key — what a client does
  after a timeout — which must be charged exactly once;
* self-serve signups racing for a small signup-grant budget;

and finally checks what no load test of the read path can: every account's held_mc equals
its held holds, no money was created or lost, nothing is left held, each idempotent group
was charged once, and the signup grants stopped at the budget.

    python scripts/load_money_path.py                          # SQLite, 400 invokes, 32 at once
    python scripts/load_money_path.py --invokes 2000 --concurrency 64
    python scripts/load_money_path.py --database-url postgresql:///scratch_db   # WIPES its schema

Exit 0 when every check holds. Nothing here touches a network beyond 127.0.0.1.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from statistics import quantiles
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PRICE_USD = 0.004
GRANT_USD = 0.05
BUDGET_USD = 0.25
ADMIN_TOKEN = "load-test-admin-token-not-for-production"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_hub(workdir: Path, database_url: str, port: int) -> Any:
    os.environ.update({
        "AIMARKET_ADMIN_TOKEN": ADMIN_TOKEN, "AIMARKET_CREDITS_ENABLED": "1",
        "AIMARKET_INVOKE_RATE_PER_MIN": "1000000", "AIMARKET_CREDITS_SIGNUPS_PER_HOUR": "1000000",
        "AIMARKET_SIGNUP_GRANT_USD": str(GRANT_USD), "AIMARKET_SIGNUP_GRANT_DAILY_USD": str(BUDGET_USD),
        "AIMARKET_HUB_URL": f"http://127.0.0.1:{port}",
        # Local only: no crawl, no federation seeds (an empty AIMARKET_SEED_LIST falls back to
        # the committed seeds file, so the config's list is emptied below as well).
        "AIMARKET_AUTO_CRAWL": "0",
    })
    import uvicorn

    from aimarket_hub.api import create_app
    from aimarket_hub.config import HubConfig
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.models import Capability
    from aimarket_hub.signing import Signer

    if database_url:
        import psycopg

        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
            conn.execute("CREATE SCHEMA public")
    config = HubConfig()
    config.hub_url = f"http://127.0.0.1:{port}"
    config.db_path = str(workdir / "hub.db")
    config.signing_key_path = str(workdir / "key")
    config.seed_list = []
    config.auto_crawl = False
    if database_url:
        config.database_url = database_url
    db = HubDatabase(workdir / "hub.db", database_url=database_url or None)
    db.upsert_capability(Capability(
        capability_id="load.echo@v1", product_id="load-echo", name="load.echo@v1",
        description="static pack for the money-path load test", price_per_call_usd=PRICE_USD,
        source_hub="local", invoke_url="", prompt_template='{"answer": "echo", "ok": true}',
    ))
    app = create_app(config=config, db=db, signer=Signer(workdir / "key"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread, db


async def _run(args: argparse.Namespace, base: str) -> dict[str, Any]:
    import httpx

    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=60, limits=limits) as client:
        for _ in range(200):
            try:
                if (await client.get("/ai-market/v2/health")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        keys = []
        for i in range(args.accounts):
            r = await client.post("/ai-market/v2/accounts", json={"label": f"load-{i}", "grant_usd": 10.0}, headers=admin)
            r.raise_for_status()
            keys.append(r.json()["api_key"])

        gate = asyncio.Semaphore(args.concurrency)
        latencies: list[float] = []
        outcomes: dict[str, int] = {}
        body = {"product_id": "load-echo", "capability_id": "load.echo@v1", "input": {"q": 1}, "source_hub": "local"}

        async def call(headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response | None:
            async with gate:
                t = time.perf_counter()
                try:
                    r = await client.post("/ai-market/v2/invoke", json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    outcomes[type(exc).__name__] = outcomes.get(type(exc).__name__, 0) + 1
                    return None
                latencies.append(time.perf_counter() - t)
                tag = f"{r.status_code} {r.headers.get('x-aimarket-idempotency', '')}".strip()
                outcomes[tag] = outcomes.get(tag, 0) + 1
                return r

        started = time.perf_counter()
        plain = [call({"X-API-Key": keys[i % len(keys)]}, {**body, "input": {"q": i}}) for i in range(args.invokes)]
        groups = [(keys[g % len(keys)], f"retry-{g}") for g in range(args.idempotent_groups)]
        retries = [call({"X-API-Key": k, "Idempotency-Key": idem}, {**body, "input": {"g": idem}})
                   for k, idem in groups for _ in range(3)]
        signups = [client.post("/ai-market/v2/accounts", json={"label": f"signup-{i}"}) for i in range(args.signups)]
        results = await asyncio.gather(*plain, *retries, *signups, return_exceptions=True)
        elapsed = time.perf_counter() - started
        signup_answers = [r for r in results[len(plain) + len(retries):] if not isinstance(r, BaseException)]
    return {"elapsed_s": elapsed, "latencies": latencies, "outcomes": outcomes, "keys": keys,
            "groups": groups, "signups": signup_answers}


def _audit(db: Any, run: dict[str, Any], args: argparse.Namespace) -> list[str]:
    from aimarket_hub import credits

    conn = db._conn
    problems: list[str] = []
    accounts = [dict(r) for r in conn.execute("SELECT * FROM credit_accounts").fetchall()]
    for a in accounts:
        held = conn.execute("SELECT COALESCE(SUM(amount_mc), 0) AS s FROM credit_holds WHERE account_id = ? "
                            "AND status = 'held'", (a["account_id"],)).fetchone()["s"]
        if int(a["held_mc"]) != int(held) or int(held) != 0:
            problems.append(f"{a['account_id']}: held_mc {a['held_mc']}, held holds {held} (both should be 0 now)")
        money_in = int(a["granted_mc"]) + int(a["topped_up_mc"] or 0)
        accounted = int(a["balance_mc"]) + int(a["held_mc"]) + int(a["spent_mc"]) + int(a["collateral_mc"])
        if money_in != accounted:
            problems.append(f"{a['account_id']}: {money_in} mc in, {accounted} mc accounted for")
    ok_plain = run["outcomes"].get("200", 0)
    stored = sum(v for k, v in run["outcomes"].items() if k == "200 stored")
    spent = sum(int(a["spent_mc"]) for a in accounts)
    expected = credits.usd_to_mc(PRICE_USD) * (ok_plain + stored)
    if spent != expected:
        problems.append(f"spent {spent} mc, expected {expected} mc for {ok_plain} plain + {stored} first idempotent answers")
    if stored != args.idempotent_groups:
        problems.append(f"{stored} idempotent groups ran, expected exactly {args.idempotent_groups} (one per key)")
    granted = credits.CreditsLedger(conn).granted_mc(since_hours=24)
    if args.signups * credits.usd_to_mc(GRANT_USD) >= credits.usd_to_mc(BUDGET_USD) and granted != credits.usd_to_mc(BUDGET_USD):
        problems.append(f"signup grants minted {granted} mc against a budget of {credits.usd_to_mc(BUDGET_USD)} mc")
    with_grant = sum(1 for s in run["signups"] if s.status_code == 200 and s.json().get("balance_usd", 0) > 0)
    run["signup_grants"] = with_grant
    conn.commit()
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--invokes", type=int, default=400)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--accounts", type=int, default=8)
    ap.add_argument("--idempotent-groups", type=int, default=40)
    ap.add_argument("--signups", type=int, default=30)
    ap.add_argument("--database-url", default="", help="PostgreSQL URL of a database this may wipe")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="hub-money-load-"))
    port = _free_port()
    server, thread, db = _start_hub(workdir, args.database_url, port)
    try:
        run = asyncio.run(_run(args, f"http://127.0.0.1:{port}"))
    finally:
        server.should_exit = True
        thread.join(timeout=20)
    problems = _audit(db, run, args)
    lat = sorted(run["latencies"])
    q = quantiles(lat, n=100) if len(lat) >= 2 else [0.0] * 99
    report = {
        "backend": "postgresql" if args.database_url else "sqlite",
        "requests": len(lat), "elapsed_s": round(run["elapsed_s"], 2),
        "rps": round(len(lat) / run["elapsed_s"], 1) if run["elapsed_s"] else 0.0,
        "latency_ms": {"p50": round(q[49] * 1000, 1), "p95": round(q[94] * 1000, 1), "p99": round(q[98] * 1000, 1),
                       "max": round((lat[-1] if lat else 0) * 1000, 1)},
        "outcomes": dict(sorted(run["outcomes"].items())),
        "signups": len(run["signups"]), "signup_grants": run.get("signup_grants"),
        "invariants": "hold" if not problems else problems,
    }
    print(json.dumps(report, indent=None if args.json else 2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
