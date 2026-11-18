"""Audit 2026-11-11, invoke-local-2: a buyer paid exactly the 402 quote, the listing was repriced
upward before the paid retry, and the hub refused the payment while the USDC sat with the seller.
The quote holds now."""
from __future__ import annotations

import pytest

from aimarket_hub.models import Capability
from tests.test_credits_invoke import _hub


def _paid_exactly(monkeypatch, quoted_price):
    from aimarket_hub import settle

    def _verify(*, tx_hash, terms, timeout=10.0, max_age_s=0, paid_before=None,
                require_nonce, rpc_url=""):
        paid = settle.to_units(quoted_price, terms.decimals)
        # mimic verify_transfer's own rule
        if paid < terms.seller_units:
            raise settle.PaymentError(f"paid {paid} base units, the seller's share is {terms.seller_units}")
        return {"tx_hash": tx_hash, "paid_units": paid, "authorizer": "0x" + "ab" * 20}

    monkeypatch.setattr(settle, "verify_transfer", _verify)


def _cap(price):
    return Capability(capability_id="demo.echo@v1", product_id="demo-echo", name="Echo",
                      description="d", price_per_call_usd=price, source_hub="local", invoke_url="",
                      prompt_template='{"answer": "echo", "ok": true}', payout_address="0x" + "cd" * 20)


@pytest.mark.parametrize("new_price", [0.005, 0.003, 0.0])
def test_a_quote_holds_when_the_listing_is_repriced_before_the_paid_retry(monkeypatch, tmp_path, new_price):
    with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="0", AIMARKET_X402_ACCEPT="1",
              AIFACTORY_CRYPTO_ENABLED="1", AIMARKET_PAYMENT_RECIPIENT="0x" + "cd" * 20,
              AIMARKET_ALLOW_DEMO_CREDIT="1", AIMARKET_AUTO_CRAWL="0") as (client, db):
        db.upsert_capability(_cap(0.004))
        body = {"product_id": "demo-echo", "capability_id": "demo.echo@v1", "input": {"text": "hi"},
                "source_hub": "local", "max_price_usd": 0.004}
        quote = client.post("/ai-market/v2/invoke", json=body)
        assert quote.status_code == 402
        _paid_exactly(monkeypatch, 0.004)          # buyer pays the quoted amount to the seller
        db.upsert_capability(_cap(new_price))
        headers = {
            "X-Payment": "0x" + "44" * 32, "X-Payment-Nonce": quote.json()["nonce"],
            "X-Payment-Secret": quote.json()["payment_secret"]}
        refused = client.post("/ai-market/v2/invoke", json={**body, "max_price_usd": 0.003}, headers=headers)
        assert refused.status_code == 409, refused.text
        assert refused.json()["current_price_usd"] == 0.004
        r = client.post("/ai-market/v2/invoke", json=body, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["price_usd"] == r.json()["receipt"]["price_usd"] == 0.004
        assert r.json()["list_price_usd"] == r.json()["receipt"]["list_price_usd"] == new_price
        payment = db._conn.execute(
            "SELECT amount_usd, amount_atomic FROM x402_payments WHERE nonce = ?", (quote.json()["nonce"],),
        ).fetchone()
        assert payment["amount_usd"] == 0.004
        assert payment["amount_atomic"] == "4000"
