"""Audit 2026-11-11, federation-2/-3 (cross-cutting-6): resale to a peer hub. The peer charged the
routing hub more than the buyer was quoted, and a peer that answered after the router gave up kept
the payment while the buyer was refunded."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
from aimarket_hub.signing import Signer

ADMIN = "test-admin-token-not-for-production"
UPSTREAM_URL = "https://upstream.example.com"


def _build(tmp_path, monkeypatch, name, hub_url, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    config = HubConfig()
    config.db_path = str(root / "hub.db")
    config.signing_key_path = str(root / "key")
    config.hub_url = hub_url
    db = HubDatabase(root / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(root / "key"))
    return app, db, config


def _cap(price, source_hub="local", **kw):
    return Capability(
        capability_id="up.answer@v1", product_id="up-answer", name="Upstream answer",
        description="Static pack", price_per_call_usd=price, source_hub=source_hub,
        invoke_url="", prompt_template=json.dumps({"answer": "from upstream"}), **kw,
    )


def _setup(tmp_path, monkeypatch, *, upstream_price, router_catalogue_price, mode="ok"):
    monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_ORACLE_FAMILY_URL", "off")
    monkeypatch.setenv("AIMARKET_CREDITS_FREE_GRANT_USD", "0")
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    up_app, up_db, _ = _build(tmp_path, monkeypatch, "upstream", UPSTREAM_URL)
    up_db.upsert_capability(_cap(upstream_price))
    up = TestClient(up_app)
    up.__enter__()
    signup = up.post("/ai-market/v2/accounts", json={"label": "router hub"}).json()
    up.post(f"/ai-market/v2/accounts/{signup['account_id']}/credit",
            json={"amount_usd": 5.0}, headers={"Authorization": f"Bearer {ADMIN}"})
    router_app, router_db, _ = _build(
        tmp_path, monkeypatch, "router", "https://router.example.com",
        AIMARKET_ROUTING_FEE_BPS="1000",
        AIMARKET_PEER_API_KEYS=f"{UPSTREAM_URL}={signup['api_key']}",
    )
    router_db.upsert_peer(Peer(url=UPSTREAM_URL, name="Upstream", capabilities_count=1,
                               trust_score=0.9, categories=["test"],
                               well_known_url=f"{UPSTREAM_URL}/.well-known/ai-market.json"))
    router_db.upsert_capability(_cap(router_catalogue_price, source_hub=UPSTREAM_URL,
                                     source_hub_name="Upstream", trust_score=0.9))
    router = TestClient(router_app)
    router.__enter__()

    import aimarket_hub.outbound_http as outbound
    import aimarket_hub.peer_routing as peer_routing

    calls = []

    async def _safe_post(url, **kw):
        r = up.post(str(url).replace(UPSTREAM_URL, ""), json=kw.get("json"),
                    headers=kw.get("headers") or {})
        calls.append(kw.get("headers") or {})
        if mode == "timeout_once" and len(calls) == 1:
            # the peer did the work and charged; the router's read timed out
            raise httpx.ReadTimeout("simulated: peer answered after the router gave up")
        return r

    async def _safe_get(url, **kw):
        return up.get(str(url).replace(UPSTREAM_URL, ""), headers=kw.get("headers") or {})

    monkeypatch.setattr(outbound, "safe_post", _safe_post)
    monkeypatch.setattr(outbound, "safe_get", _safe_get, raising=False)
    monkeypatch.setattr(peer_routing, "_peer_endpoint_cache", {}, raising=False)
    return router, up, signup, calls


def _buyer(client, usd):
    s = client.post("/ai-market/v2/accounts", json={"label": "buyer"}).json()
    client.post(f"/ai-market/v2/accounts/{s['account_id']}/credit", json={"amount_usd": usd},
                headers={"Authorization": f"Bearer {ADMIN}"})
    return s["api_key"]


def _invoke(client, key, **extra):
    return client.post("/ai-market/v2/invoke", headers={"X-API-Key": key}, json={
        "product_id": "up-answer", "capability_id": "up.answer@v1",
        "source_hub": UPSTREAM_URL, "input": {}, **extra})


def _router_account_at_upstream(up, key):
    return up.get("/ai-market/v2/account", headers={"X-API-Key": key}).json()


def test_a_peer_that_repriced_cannot_charge_the_router_more_than_the_buyer_paid(tmp_path, monkeypatch):
    router, up, signup, _calls = _setup(tmp_path, monkeypatch, upstream_price=0.50, router_catalogue_price=0.02)
    try:
        key = _buyer(router, 1.0)
        r = _invoke(router, key, max_price_usd=0.03)
        buyer = router.get("/ai-market/v2/account", headers={"X-API-Key": key}).json()
        acct = _router_account_at_upstream(up, signup["api_key"])
        assert r.status_code == 502                       # the peer answered 409 to the price bound
        assert buyer["spent_usd"] == pytest.approx(0.0)
        assert acct["spent_usd"] == pytest.approx(0.0)
    finally:
        router.__exit__(None, None, None)
        up.__exit__(None, None, None)


def test_a_late_peer_answer_is_fetched_once_not_paid_twice_or_lost(tmp_path, monkeypatch):
    router, up, signup, calls = _setup(tmp_path, monkeypatch, upstream_price=0.02,
                                       router_catalogue_price=0.02, mode="timeout_once")
    try:
        key = _buyer(router, 1.0)
        r = _invoke(router, key)
        buyer = router.get("/ai-market/v2/account", headers={"X-API-Key": key}).json()
        acct = _router_account_at_upstream(up, signup["api_key"])
        assert r.status_code == 200, r.text
        assert len(calls) == 2 and calls[0].get("Idempotency-Key") == calls[1].get("Idempotency-Key")
        assert acct["spent_usd"] == pytest.approx(0.02)   # the peer charged once
        assert buyer["spent_usd"] == pytest.approx(0.022) # and the buyer paid for what was delivered
    finally:
        router.__exit__(None, None, None)
        up.__exit__(None, None, None)
