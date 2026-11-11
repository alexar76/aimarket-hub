"""Audit 2026-11-11, deposits-x402-4: with a market fee enabled the invoke 402 named the SELLER as
payTo while verification required the payment through the splitter, so every x402 buyer paid the
seller in full and was refused. The 402 names the splitter now; the invoice keeps the seller."""
from __future__ import annotations

from aimarket_hub.models import Capability
from tests.test_credits_invoke import _hub

SELLER = "0x" + "cd" * 20
SPLITTER = "0x" + "99" * 20
FEE_TO = "0x" + "fe" * 20


def _cap():
    return Capability(capability_id="demo.echo@v1", product_id="demo-echo", name="Echo",
                      description="d", price_per_call_usd=1.0, source_hub="local", invoke_url="",
                      prompt_template='{"answer": "echo", "ok": true}', payout_address=SELLER)


BODY = {"product_id": "demo-echo", "capability_id": "demo.echo@v1", "input": {"text": "hi"}, "source_hub": "local"}


def test_with_a_fee_the_402_names_the_splitter(monkeypatch, tmp_path):
    with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="0", AIMARKET_X402_ACCEPT="1",
              AIFACTORY_CRYPTO_ENABLED="1", AIMARKET_PAYMENT_RECIPIENT=FEE_TO, AIMARKET_AUTO_CRAWL="0",
              AIMARKET_MARKET_FEE_BPS="100", AIMARKET_MARKET_FEE_TO=FEE_TO,
              AIMARKET_MARKET_SPLITTER=SPLITTER) as (client, db):
        db.upsert_capability(_cap())
        r = client.post("/ai-market/v2/invoke", json=BODY)
        assert r.status_code == 402, r.text
        body = r.json()
        assert body["pay_to"].lower() == SPLITTER
        assert body["seller_pay_to"].lower() == SELLER
        assert all(a["payTo"].lower() == SPLITTER for a in body["accepts"])
        # contracts-2: a stock client paying payTo directly strands the USDC in the splitter,
        # so the offer says how the contract is paid.
        how = [a["extra"].get("splitter") for a in body["accepts"]]
        assert how and all(h and h["method"].startswith("payWithAuthorization(") for h in how)
        assert all(h["authorization"] == "ReceiveWithAuthorization" and h["seller"].lower() == SELLER
                   for h in how)
        invoice = db._conn.execute("SELECT pay_to FROM settle_invoices WHERE nonce = ?",
                                   (body["nonce"],)).fetchone()
        assert invoice["pay_to"].lower() == SELLER


def test_without_a_fee_the_402_still_names_the_seller(monkeypatch, tmp_path):
    with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="0", AIMARKET_X402_ACCEPT="1",
              AIFACTORY_CRYPTO_ENABLED="1", AIMARKET_PAYMENT_RECIPIENT=FEE_TO,
              AIMARKET_AUTO_CRAWL="0") as (client, db):
        db.upsert_capability(_cap())
        body = client.post("/ai-market/v2/invoke", json=BODY).json()
        assert body["pay_to"].lower() == SELLER and "seller_pay_to" not in body
        assert all("splitter" not in a.get("extra", {}) for a in body["accepts"])
