"""Audit 2026-11-11, invoke-local-4: a seller-direct claim committed at admission by a process that
then died stayed consumed with no receipt for good; the buyer had paid the seller and the retry was
refused as "already used". The stale-claim sweep gives it back; a delivered claim is never touched."""
from __future__ import annotations

import time

from aimarket_hub import settle
from aimarket_hub.models import Capability
from tests.test_credits_invoke import _hub

ENV = dict(AIMARKET_CREDITS_ENABLED="0", AIMARKET_X402_ACCEPT="1", AIFACTORY_CRYPTO_ENABLED="1",
           AIMARKET_PAYMENT_RECIPIENT="0x" + "cd" * 20, AIMARKET_AUTO_CRAWL="0")
BODY = {"product_id": "demo-echo", "capability_id": "demo.echo@v1", "input": {"text": "hi"},
        "source_hub": "local"}


def _cap():
    return Capability(capability_id="demo.echo@v1", product_id="demo-echo", name="Echo",
                      description="d", price_per_call_usd=0.004, source_hub="local", invoke_url="",
                      prompt_template='{"answer": "echo", "ok": true}', payout_address="0x" + "cd" * 20)


def _verify(*, tx_hash, terms, timeout=10.0, max_age_s=0, paid_before=None, require_nonce, rpc_url=""):
    return {"tx_hash": tx_hash, "paid_units": terms.amount_units, "authorizer": "0x" + "ab" * 20}


def _pay(client, quote, tx):
    return client.post("/ai-market/v2/invoke", json=BODY, headers={
        "X-Payment": tx, "X-Payment-Nonce": quote["nonce"], "X-Payment-Secret": quote["payment_secret"]})


def test_a_claim_a_dead_call_left_behind_is_given_back(monkeypatch, tmp_path):
    tx = "0x" + "44" * 32
    with _hub(monkeypatch, tmp_path, **ENV) as (client, db):
        db.upsert_capability(_cap())
        quote = client.post("/ai-market/v2/invoke", json=BODY).json()
        monkeypatch.setattr(settle, "verify_transfer", _verify)
        store = settle.PaymentStore(db._conn)
        assert store.claim_invoice(        # what admission commits before the provider runs ...
            tx_hash=tx, pay_to="0x" + "cd" * 20, payer="0x" + "ab" * 20, amount_usd=0.004, amount_atomic=4000,
            asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", network="base",
            capability_id="demo.echo@v1", nonce=quote["nonce"])
        # ... and the process dies: no unwind, no receipt.
        assert store.release_stale_claims(older_than_s=900) == 0            # too fresh to tell
        assert store.release_stale_claims(older_than_s=900, now=time.time() + 901) == 1
        r = _pay(client, quote, tx)
        assert r.status_code == 200, r.text


def test_a_delivered_claim_is_never_given_back(monkeypatch, tmp_path):
    tx = "0x" + "45" * 32
    with _hub(monkeypatch, tmp_path, **ENV) as (client, db):
        db.upsert_capability(_cap())
        quote = client.post("/ai-market/v2/invoke", json=BODY).json()
        monkeypatch.setattr(settle, "verify_transfer", _verify)
        assert _pay(client, quote, tx).status_code == 200
        store = settle.PaymentStore(db._conn)
        assert store.release_stale_claims(older_than_s=900, now=time.time() + 10_000) == 0
        again = _pay(client, quote, tx)
        assert again.status_code == 402 and "already been used" in again.text
