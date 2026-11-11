"""Audit 2026-11-11, federation-4/-6/-7 — regressions built from the audit's reproductions."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import aimarket_hub.hub_routes.invoke as invoke_routes
import aimarket_hub.channels as channels_mod
import aimarket_hub.outbound_http as ohttp
from aimarket_hub.api import create_app
from aimarket_hub.channels import ChannelLedger
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
from aimarket_hub.signing import Signer

ADMIN_TOKEN = "fed-admin-token"
PEER = "https://stranger-peer.example.com"


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code, self._p, self.text, self.headers = status_code, payload, "", {}

    def json(self):
        return self._p


class _Out:
    def __init__(self, price=0.004):
        self.headers, self.price = [], price

    async def safe_get(self, url, *, timeout=10.0):
        return _Resp(404, {})

    async def safe_post(self, url, *, json=None, headers=None, timeout=30.0, invoke=False, oracle_paid=False):
        self.headers.append(dict(headers or {}))
        return _Resp(200, {"success": True, "result": {"v": 1}, "price_usd": self.price})


def _build(tmp_path, monkeypatch, *, price, bps="100", credits=False):
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1" if credits else "0")
    monkeypatch.setenv("AIMARKET_CREDITS_FREE_GRANT_USD", "0")
    monkeypatch.setenv("AIMARKET_ALLOW_DEMO_CREDIT", "1")
    monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("AIFACTORY_PUBLIC_URL", "http://factory.test")
    monkeypatch.setenv("AIMARKET_ROUTING_FEE_BPS", bps)
    monkeypatch.setattr(channels_mod, "_ledger", ChannelLedger(db_path=str(tmp_path / "channels.db")))
    monkeypatch.setattr(invoke_routes, "channel_escrow_binding", lambda _cid: "")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    db.upsert_peer(Peer(url=PEER, name=PEER, capabilities_count=1, well_known_url="",
                        trusted=False))   # status defaults to active (e.g. auto-admitted)
    db.upsert_capability(Capability(capability_id="cap.x@v1", product_id="prod-x", name="x",
                                    price_per_call_usd=price, source_hub=PEER, source_hub_name=PEER,
                                    trust_score=0.5))
    return app, db, config


def _invoke(client, headers, **extra):
    return client.post("/ai-market/v2/invoke", json={
        "product_id": "prod-x", "capability_id": "cap.x@v1", "source_hub": PEER, "input": {}, **extra,
    }, headers=headers)


def test_the_hubs_peer_channel_goes_only_to_named_peers(tmp_path, monkeypatch):
    """federation-4: the hub's own channel id and secret went to every keyless admitted peer."""
    monkeypatch.setenv("AIMARKET_PEER_PAYMENT_CHANNEL", "ch_hub_owned_at_some_peer")
    monkeypatch.setenv("AIMARKET_PEER_PAYMENT_CHANNEL_SECRET", "s3cr3t-hub-channel")
    app, db, config = _build(tmp_path, monkeypatch, price=0.0)
    out = _Out(price=0.0)
    monkeypatch.setattr(ohttp, "safe_get", out.safe_get)
    monkeypatch.setattr(ohttp, "safe_post", out.safe_post)
    with TestClient(app) as client:
        assert _invoke(client, {}).status_code == 200
        assert "X-Payment-Channel-Secret" not in out.headers[0]
        monkeypatch.setenv("AIMARKET_PEER_PAYMENT_CHANNEL_PEERS", PEER)
        assert _invoke(client, {}).status_code == 200
        assert out.headers[-1]["X-Payment-Channel-Secret"] == "s3cr3t-hub-channel"


def test_a_channel_resale_respects_the_buyers_ceiling_in_whole_cents(tmp_path, monkeypatch):
    """federation-6: price and fee are two holds each rounded up to a cent; the ceiling now
    counts them that way and refuses instead of charging $0.02 against $0.01."""
    app, db, config = _build(tmp_path, monkeypatch, price=0.004, bps="100")
    monkeypatch.setattr(config, "peer_api_key", lambda _u: "aimk_key")
    out = _Out(price=0.004)
    monkeypatch.setattr(ohttp, "safe_get", out.safe_get)
    monkeypatch.setattr(ohttp, "safe_post", out.safe_post)
    with TestClient(app) as client:
        opened = channels_mod._ledger.open(5.0, with_secret=True)["channel"]
        before = channels_mod.channel_balance(opened["channel_id"])
        r = _invoke(client, {"X-Payment-Channel": opened["channel_id"],
                             "X-Payment-Channel-Secret": opened["channel_secret"]}, max_price_usd=0.01)
        after = channels_mod.channel_balance(opened["channel_id"])
    assert r.status_code == 409 and r.json()["current_price_usd"] == pytest.approx(0.02)
    assert before == after


@pytest.mark.parametrize("resell", [False, True])
def test_a_routing_fee_too_small_to_hold_does_not_block_a_credit_purchase(tmp_path, monkeypatch, resell):
    """federation-7: a fee under half a millicent could not be held on credits (402 always)."""
    app, db, config = _build(tmp_path, monkeypatch, price=0.0004, bps="100", credits=True)
    if resell:
        monkeypatch.setattr(config, "peer_api_key", lambda _u: "aimk_key")
    out = _Out(price=0.0004)
    monkeypatch.setattr(ohttp, "safe_get", out.safe_get)
    monkeypatch.setattr(ohttp, "safe_post", out.safe_post)
    with TestClient(app) as client:
        s = client.post("/ai-market/v2/accounts", json={"label": "b"}).json()
        client.post(f"/ai-market/v2/accounts/{s['account_id']}/credit", json={"amount_usd": 1.0},
                    headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        r = _invoke(client, {"X-API-Key": s["api_key"]})
    assert r.status_code == 200, r.text


def test_a_local_channel_call_respects_the_ceiling_in_whole_cents(tmp_path, monkeypatch):
    """invoke-local-6 / cross-cutting-5: a $0.004 local call billed $0.01 on a channel while
    max_price_usd=0.004 was checked against the list price."""
    app, db, config = _build(tmp_path, monkeypatch, price=0.0)
    db.upsert_capability(Capability(capability_id="loc.x@v1", product_id="loc-x", name="x",
                                    price_per_call_usd=0.004, source_hub="local", invoke_url="",
                                    prompt_template='{"answer": "x", "ok": true}', trust_score=0.5))
    with TestClient(app) as client:
        opened = channels_mod._ledger.open(5.0, with_secret=True)["channel"]
        r = client.post("/ai-market/v2/invoke", json={
            "product_id": "loc-x", "capability_id": "loc.x@v1", "source_hub": "local", "input": {},
            "max_price_usd": 0.004}, headers={"X-Payment-Channel": opened["channel_id"],
                                              "X-Payment-Channel-Secret": opened["channel_secret"]})
        assert r.status_code == 409 and r.json()["current_price_usd"] == pytest.approx(0.01)
        ok = client.post("/ai-market/v2/invoke", json={
            "product_id": "loc-x", "capability_id": "loc.x@v1", "source_hub": "local", "input": {},
            "max_price_usd": 0.01}, headers={"X-Payment-Channel": opened["channel_id"],
                                             "X-Payment-Channel-Secret": opened["channel_secret"]})
        assert ok.status_code == 200, ok.text
