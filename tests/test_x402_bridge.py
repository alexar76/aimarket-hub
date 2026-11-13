"""The x402 bridge: this hub pays an x402 seller from its own wallet for a buyer paying in credits.

What is pinned here, all against a scripted x402 seller and real EIP-3009 signatures:

- the money: the buyer is charged what the bridge actually paid (at most the listed price), and nothing
  when the seller refused, failed, answered for free, or the bridge would not pay;
- where the money goes: only a payee the seller itself published, never more than the price quoted;
- the limits: per call, per seller per day, per day, and the wallet balance minus payments in flight —
  under concurrent purchases too;
- free credit never becomes USDC in a stranger's wallet: only topped-up credit buys through the bridge;
- off by default, and a key file anyone else can read is refused.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aimarket_hub.api as api_mod  # noqa: F401  (create_app wiring)
import aimarket_hub.channels as channels_mod
import aimarket_hub.outbound_http as ohttp
from aimarket_hub import credits, settle, x402_bridge
from aimarket_hub.api import create_app
from aimarket_hub.channels import ChannelLedger
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.escrow_bridge import eip712
from aimarket_hub.models import Capability, Peer
from aimarket_hub.signing import Signer

pytest.importorskip("eth_account")
from eth_account import Account  # noqa: E402

BRIDGE_KEY = "0x" + "11" * 32
BRIDGE_ADDRESS = Account.from_key(BRIDGE_KEY).address
PAYEE = "0xc8Fceb0F3611BE99c41586eA2B83F6A0911d2Ff0"
OTHER = "0x1218ff36C5d2e3B6A565CdB1A8B1AcCFc606Ad0a"
PEER = "https://x402-seller.example.com"
ADMIN_TOKEN = "bridge-admin-token"


# ── fixtures ─────────────────────────────────────────────────────────────────


def _offer(amount="1000", pay_to=PAYEE, network="eip155:8453", asset=x402_bridge.USDC, scheme="exact"):
    return {"scheme": scheme, "network": network, "amount": amount, "asset": asset, "payTo": pay_to,
            "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}}


def _v2_402(*offers):
    doc = {"x402Version": 2, "error": "X-PAYMENT header is required",
           "resource": {"url": f"{PEER}/execute"}, "accepts": list(offers) or [_offer()]}
    return {"payment-required": base64.b64encode(json.dumps(doc).encode()).decode()}


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status_code, payload, dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class _Seller:
    """A scripted x402 seller: answers each POST with the next response, records what it got."""

    def __init__(self, *responses, well_known=None):
        self.responses = list(responses)
        self.well_known = well_known if well_known is not None else {
            "payment": {"enforced": True, "protocol": "x402", "asset": "USDC",
                        "accepts": [{"network": "base", "pay_to": PAYEE}]}}
        self.posts: list[tuple[str, dict, dict]] = []

    async def safe_get(self, url, *, timeout=10.0):
        return _Resp(200, self.well_known)

    async def safe_post(self, url, *, json=None, headers=None, timeout=30.0, invoke=False, oracle_paid=False):
        self.posts.append((url, json, dict(headers or {})))
        if not self.responses:
            raise AssertionError("the seller was called more often than scripted")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def payment(self, i=-1) -> dict:
        headers = self.posts[i][2]
        raw = headers.get("PAYMENT-SIGNATURE") or headers.get("X-PAYMENT")
        return json.loads(base64.b64decode(raw))


@pytest.fixture
def bridge_env(tmp_path, monkeypatch):
    key = tmp_path / "bridge.key"
    key.write_text(BRIDGE_KEY + "\n")
    os.chmod(key, 0o600)
    monkeypatch.setenv("AIMARKET_X402_BRIDGE", "1")
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_KEY_FILE", str(key))
    for name in ("AIMARKET_X402_BRIDGE_MAX_CALL_USD", "AIMARKET_X402_BRIDGE_SELLER_DAY_USD",
                 "AIMARKET_X402_BRIDGE_DAY_USD", "AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS",
                 "AIMARKET_SELLS_FOR", "AIMARKET_PEER_API_KEYS"):
        monkeypatch.delenv(name, raising=False)
    x402_bridge._key_cache.clear()
    x402_bridge._balance_cache.clear()
    balance = {"usd": 1.0}
    monkeypatch.setattr(x402_bridge, "wallet_balance_usd", lambda wallet, rpc=None, fresh=False: balance["usd"])
    return balance


def _hub(tmp_path: Path, monkeypatch, seller: _Seller, *, price=0.002, payout_address=""):
    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "0")
    monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(channels_mod, "_ledger", ChannelLedger(db_path=str(tmp_path / "channels.db")))
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    db.upsert_peer(Peer(url=PEER, name="seller", capabilities_count=1,
                        well_known_url=f"{PEER}/.well-known/ai-market.json", trusted=True))
    db.upsert_capability(Capability(capability_id="execute", product_id="exec", name="execute",
                                    price_per_call_usd=price, source_hub=PEER, source_hub_name=PEER,
                                    trust_score=0.5, payout_address=payout_address))
    monkeypatch.setattr(ohttp, "safe_get", seller.safe_get)
    monkeypatch.setattr(ohttp, "safe_post", seller.safe_post)
    return TestClient(app), db


def _buyer(*, topped_up=1.0, granted=0.0):
    ledger = credits.ledger()
    acct = ledger.create_account("bridge buyer", grant_usd=granted)
    if topped_up:
        assert not ledger.deposit(acct["account_id"], credits.usd_to_mc(topped_up),
                                  reference=f"test:{acct['account_id']}").get("error")
    return acct["api_key"], acct["account_id"]


def _invoke(client, key):
    return client.post("/ai-market/v2/invoke", headers={"X-API-Key": key}, json={
        "product_id": "exec", "capability_id": "execute", "source_hub": PEER, "input": {"source": "print(1)"}})


def _payments(db):
    return [dict(r) for r in db._conn.execute(
        "SELECT status, amount_usd, pay_to, tx_hash, http_status, buyer_account FROM x402_bridge_payments "
        "ORDER BY created_at").fetchall()]


SETTLED = {"payment-response": base64.b64encode(json.dumps(
    {"success": True, "transaction": "0x" + "ab" * 32, "network": "eip155:8453"}).encode()).decode()}


# ── through the federation ───────────────────────────────────────────────────


def test_a_credit_buyer_buys_an_x402_seller_and_pays_what_the_bridge_paid(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(amount="1000"))),
                     _Resp(200, {"outcome": "completed", "stdout": "1\n"}, SETTLED))
    client, db = _hub(tmp_path, monkeypatch, seller, price=0.002)
    key, account = _buyer(topped_up=1.0)

    r = _invoke(client, key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stdout"] == "1\n"
    assert body["price_usd"] == pytest.approx(0.001)
    assert body["paid_via"]["via"] == "x402-bridge" and body["paid_via"]["pay_to"] == PAYEE
    assert body["paid_via"]["tx_hash"] == "0x" + "ab" * 32
    # Charged what was paid ($0.001), not the $0.002 that was held; the rest went back.
    assert credits.ledger().balance(account) == pytest.approx(0.999)
    acct = credits.ledger().account(account)
    assert acct["held_usd"] == 0 and acct["spent_usd"] == pytest.approx(0.001)

    # The payment the seller received is a valid EIP-3009 authorization from the bridge wallet,
    # for exactly the quoted amount, to the published payee, under both header names.
    assert len(seller.posts) == 2
    sent = seller.posts[1][2]
    assert sent["PAYMENT-SIGNATURE"] == sent["X-PAYMENT"]
    assert "X-AIMarket-Sandbox-Visitor" not in sent
    paid = seller.payment()
    assert paid["x402Version"] == 2 and paid["accepted"]["payTo"] == PAYEE
    auth = paid["payload"]["authorization"]
    assert auth["from"] == BRIDGE_ADDRESS and auth["to"] == PAYEE and auth["value"] == "1000"
    digest = eip712.transfer_authorization_digest(
        sender=auth["from"], recipient=auth["to"], value=int(auth["value"]),
        valid_after=int(auth["validAfter"]), valid_before=int(auth["validBefore"]), nonce=auth["nonce"],
        token_name="USD Coin", token_version="2", chain_id=8453, token_address=x402_bridge.USDC)
    assert eip712.addresses_equal(eip712.recover_transfer_signer(paid["payload"]["signature"], digest),
                                  BRIDGE_ADDRESS)
    assert int(auth["validBefore"]) - time.time() <= 300 + 5

    [row] = _payments(db)
    assert row["status"] == "delivered" and row["buyer_account"] == account
    assert row["tx_hash"] == "0x" + "ab" * 32


def test_a_v1_body_offer_is_paid_with_an_x_payment_header(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {"x402Version": 1, "accepts": [_offer(amount="1500", network="base")]}),
                     _Resp(200, {"ok": True}))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 200, r.text
    paid = seller.payment()
    assert paid["x402Version"] == 1 and paid["scheme"] == "exact"
    assert credits.ledger().balance(account) == pytest.approx(0.9985)
    # No settlement header: delivered all the same (the buyer got the work), tx unknown.
    assert _payments(db)[0]["status"] == "delivered"


def test_a_price_above_the_listing_is_not_paid_and_costs_nothing(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(amount="3000"))))
    client, db = _hub(tmp_path, monkeypatch, seller, price=0.002)
    key, account = _buyer()
    r = _invoke(client, key)
    # What the buyer got before the bridge: the seller's own 402, payable from their own wallet,
    # with a note saying why this hub did not pay it.
    assert r.status_code == 402
    note = r.json()["x402_bridge"]
    assert note["paid"] is False and "above" in note["detail"]
    assert r.json()["accepts"][0]["payTo"] == PAYEE and r.headers.get("payment-required")
    assert len(seller.posts) == 1          # nothing was signed or sent
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db) == []


def test_a_payee_the_seller_never_published_is_not_paid(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(pay_to=OTHER))))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 402 and "recipient other than" in r.json()["x402_bridge"]["detail"]
    assert len(seller.posts) == 1
    assert credits.ledger().balance(account) == pytest.approx(1.0)


def test_the_signed_listing_payee_counts_as_published(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(pay_to=OTHER))), _Resp(200, {"ok": True}),
                     well_known={"payment_rails": {"seller_direct": {"enabled": True}}})
    client, db = _hub(tmp_path, monkeypatch, seller, payout_address=OTHER)
    key, _ = _buyer()
    assert _invoke(client, key).status_code == 200


def test_a_peer_that_does_not_bill_over_x402_is_not_bridged(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {"error": "pay"}), well_known={"name": "no payment declared"})
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    # The old broker shape: no price here, the peer's own 402 goes back to the buyer.
    assert r.status_code == 402
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db) == []


def test_a_seller_that_refuses_the_payment_costs_the_buyer_nothing(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(402, {}, _v2_402()))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 402 and r.json()["x402_bridge"]["refusal"] == "seller_refused"
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db)[0]["status"] == "refused"


def test_a_seller_that_takes_the_payment_and_fails_costs_the_buyer_nothing(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(500, {"error": "boom"}, SETTLED))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 502
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    [row] = _payments(db)
    assert row["status"] == "undelivered" and row["tx_hash"] == "0x" + "ab" * 32


def test_a_refusal_in_the_body_after_payment_is_not_charged(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(200, {"success": False, "error": "bad input"}))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 200 and r.json()["success"] is False
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db)[0]["status"] == "unknown"


def test_a_transport_failure_after_paying_is_recorded_and_not_charged(tmp_path, monkeypatch, bridge_env):
    import httpx

    seller = _Seller(_Resp(402, {}, _v2_402()), httpx.ConnectError("reset"))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 502
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db)[0]["status"] == "unknown"


def test_a_free_answer_is_not_charged_and_nothing_is_paid(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(200, {"ok": True, "free": True}))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 200 and r.json()["price_usd"] == 0.0
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db) == []


def test_signup_credit_never_pays_a_stranger(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer(topped_up=0, granted=1.0)
    r = _invoke(client, key)
    assert r.status_code == 402 and r.json()["x402_bridge"]["refusal"] == "bridge_paid_only"
    assert len(seller.posts) == 1
    assert credits.ledger().balance(account) == pytest.approx(1.0)


def test_the_operator_can_let_its_own_granted_account_use_the_bridge(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(200, {"ok": True}))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer(topped_up=0, granted=1.0)
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", f"other,{account}")
    assert _invoke(client, key).status_code == 200
    assert credits.ledger().balance(account) == pytest.approx(0.999)


def test_topped_up_credit_bounds_bridge_spending_even_with_a_grant_on_top(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(amount="1000"))), _Resp(200, {"ok": True}),
                     _Resp(402, {}, _v2_402(_offer(amount="1000"))))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer(topped_up=0.001, granted=5.0)
    assert _invoke(client, key).status_code == 200
    r = _invoke(client, key)          # the paid-in $0.001 is spent; the $5 grant cannot follow
    assert r.status_code == 402 and r.json()["x402_bridge"]["refusal"] == "bridge_paid_only"


def test_an_empty_wallet_refuses_before_signing(tmp_path, monkeypatch, bridge_env):
    bridge_env["usd"] = 0.0005
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(amount="1000"))))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    r = _invoke(client, key)
    assert r.status_code == 402 and r.json()["x402_bridge"]["refusal"] == "bridge_empty"
    assert len(seller.posts) == 1


def test_a_top_up_of_the_wallet_raises_the_limit_at_once(tmp_path, monkeypatch, bridge_env):
    bridge_env["usd"] = 0.0
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(402, {}, _v2_402()), _Resp(200, {"ok": True}))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, _ = _buyer()
    assert _invoke(client, key).status_code == 402
    bridge_env["usd"] = 1.0           # the operator sends USDC to the bridge address
    assert _invoke(client, key).status_code == 200


def test_the_bridge_is_off_by_default(tmp_path, monkeypatch, bridge_env):
    monkeypatch.delenv("AIMARKET_X402_BRIDGE")
    seller = _Seller(_Resp(402, {}, _v2_402()))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    assert _invoke(client, key).status_code == 402
    assert _payments(db) == []
    status = client.get("/ai-market/v2/x402-bridge").json()
    assert status["enabled"] is False and "off" in status["reason"]


def test_the_status_page_names_the_wallet_and_todays_spending(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(200, {"ok": True}, SETTLED))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer()
    assert _invoke(client, key).status_code == 200
    status = client.get("/ai-market/v2/x402-bridge").json()
    assert status["enabled"] is True and status["wallet"] == BRIDGE_ADDRESS
    assert status["balance_usd"] == 1.0
    assert status["today"]["spent_usd"] == pytest.approx(0.001) and status["today"]["delivered"] == 1
    assert account not in json.dumps(status)     # no buyer identity on the public page
    assert BRIDGE_KEY[2:] not in json.dumps(status)


# ── the module on its own ────────────────────────────────────────────────────


def test_a_key_file_others_can_read_is_refused(tmp_path, monkeypatch, bridge_env):
    key = Path(os.environ["AIMARKET_X402_BRIDGE_KEY_FILE"])
    os.chmod(key, 0o644)
    x402_bridge._key_cache.clear()
    assert "readable by others" in x402_bridge.unavailable_reason()
    assert not x402_bridge.enabled()


def test_keygen_writes_an_owner_only_file_and_never_overwrites(tmp_path, capsys):
    path = tmp_path / "new.key"
    assert x402_bridge.main(["keygen", str(path)]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("0x") and len(printed) == 42
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text().strip()[2:] not in printed
    assert x402_bridge.main(["keygen", str(path)]) == 2


def test_choose_offer_takes_the_cheapest_payable_offer():
    offers = [_offer(amount="2000"), _offer(amount="900", network="solana:5eyk"),
              _offer(amount="1500"), _offer(amount="1000", scheme="upto")]
    chosen = x402_bridge.choose_offer({"x402Version": 2, "accepts": offers}, {}, limit_usd=0.01,
                                      expected_pay_to=[PAYEE])
    assert chosen.amount_atomic == 1500


def test_choose_offer_refuses_without_a_published_payee():
    with pytest.raises(x402_bridge.BridgeRefused, match="no Base payee"):
        x402_bridge.choose_offer({"accepts": [_offer()]}, {}, limit_usd=1, expected_pay_to=None)


@pytest.mark.parametrize("bad", [
    _offer(amount="-1"), _offer(amount="1e3"), _offer(amount="0"), _offer(amount=True),
    _offer(asset="0x0000000000000000000000000000000000000001"), _offer(pay_to="0xnot"),
])
def test_choose_offer_refuses_malformed_offers(bad):
    with pytest.raises(x402_bridge.BridgeRefused):
        x402_bridge.choose_offer({"accepts": [bad]}, {}, limit_usd=1, expected_pay_to=[PAYEE])


def test_wallet_balance_reads_usdc_balance_of(bridge_env):
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return hex(1_234_567)

    # The fixture replaced the function; read the real one.
    import importlib

    real = importlib.reload(x402_bridge).wallet_balance_usd
    assert real(BRIDGE_ADDRESS, rpc=rpc, fresh=True) == pytest.approx(1.234567)
    method, params = calls[0]
    assert method == "eth_call" and params[0]["to"] == x402_bridge.USDC
    assert params[0]["data"] == "0x70a08231" + BRIDGE_ADDRESS[2:].lower().zfill(64)


def _ledger_db(tmp_path):
    from aimarket_hub.db_backend import SQLiteBackend
    from aimarket_hub.migrations import Migrations

    backend = SQLiteBackend(str(tmp_path / "ledger.db"))
    Migrations(backend).apply()
    return backend


def _reserve(book, amount_atomic, *, pay_to=PAYEE, account="acct", now=None, seller=PEER, receipt="r"):
    offer = x402_bridge.Offer(x402_version=2, accept=_offer(amount=str(amount_atomic), pay_to=pay_to),
                              resource=None, pay_to=pay_to, amount_atomic=amount_atomic, max_timeout_s=300)
    return book.reserve(offer=offer, buyer_account=account, receipt_id=receipt, seller=seller, route="x",
                        balance_usd=1.0, now=now or time.time())


def test_caps_per_call_per_seller_and_per_day(tmp_path, monkeypatch, bridge_env):
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", "acct")
    book = x402_bridge.BridgeLedger(_ledger_db(tmp_path))
    with pytest.raises(x402_bridge.BridgeRefused, match="per-call cap"):
        _reserve(book, 60_000)
    for _ in range(10):
        _reserve(book, 50_000)
    with pytest.raises(x402_bridge.BridgeRefused, match="seller reached"):
        _reserve(book, 1)
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_DAY_USD", "0.6")
    other_seller = "https://another-seller.example.org"
    _reserve(book, 50_000, pay_to=OTHER, seller=other_seller)
    _reserve(book, 50_000, pay_to=OTHER, seller=other_seller)
    with pytest.raises(x402_bridge.BridgeRefused, match="bridge reached its daily cap"):
        _reserve(book, 1, pay_to=OTHER, seller=other_seller)


def test_seller_cap_counts_the_seller_not_only_the_payee(tmp_path, monkeypatch, bridge_env):
    """Audit 2026-11-11, x402-bridge-2: one seller rotating payTo across its own payees got a fresh
    $0.50 for every address it published. The cap is the seller's, whatever payee a 402 names, and
    a second seller that shares a payee is still held by the payee's own count."""
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", "acct")
    book = x402_bridge.BridgeLedger(_ledger_db(tmp_path))
    for _ in range(10):
        _reserve(book, 50_000, pay_to=PAYEE)
    with pytest.raises(x402_bridge.BridgeRefused, match="seller reached"):
        _reserve(book, 1, pay_to=OTHER)                               # same seller, new payee
    with pytest.raises(x402_bridge.BridgeRefused, match="seller reached"):
        _reserve(book, 1, pay_to=OTHER, seller=PEER.upper() + "/x/")  # same origin, other spelling
    with pytest.raises(x402_bridge.BridgeRefused, match="seller reached"):
        _reserve(book, 1, pay_to=PAYEE, seller="https://other.example.net")  # other seller, same payee
    _reserve(book, 1, pay_to=OTHER, seller="https://other.example.net")


def _account(conn, account_id, *, topped_up_mc=0, spent_mc=0, held_mc=0, granted_mc=0, collateral_mc=0):
    balance = topped_up_mc + granted_mc - spent_mc - held_mc - collateral_mc
    conn.execute(
        "INSERT INTO credit_accounts (account_id, key_hash, balance_mc, held_mc, spent_mc, granted_mc, "
        "collateral_mc, topped_up_mc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, "h_" + account_id, balance, held_mc, spent_mc, granted_mc, collateral_mc, topped_up_mc),
    )
    conn.commit()


def _hold(conn, receipt, account_id, amount_mc, status="held"):
    conn.execute("INSERT INTO credit_holds (receipt_id, account_id, amount_mc, status) VALUES (?, ?, ?, ?)",
                 (receipt, account_id, amount_mc, status))
    conn.commit()


def test_uncharged_bridge_losses_count_against_the_buyer(tmp_path, monkeypatch, bridge_env):
    """Audit 2026-11-11, x402-bridge-1: payments a seller took without delivering cost the buyer no
    credit and did not count against what the buyer paid in, so one $0.05 top-up paid out without
    bound. Now every uncharged payment stays counted until reconcile() shows it unused."""
    conn = _ledger_db(tmp_path)
    book = x402_bridge.BridgeLedger(conn)
    _account(conn, "buyer", topped_up_mc=5_000, held_mc=5_000)       # $0.05 paid in, held for this call
    _hold(conn, "r1", "buyer", 5_000)
    pid = _reserve(book, 50_000, account="buyer", receipt="r1")       # the hold of r1 is its own
    book.mark(pid, "undelivered", tx_hash="0x" + "ab" * 32)
    conn.execute("UPDATE credit_holds SET status = 'released' WHERE receipt_id = 'r1'")
    conn.execute("UPDATE credit_accounts SET held_mc = 0, balance_mc = 5000 WHERE account_id = 'buyer'")
    conn.commit()
    _hold(conn, "r2", "buyer", 5_000)
    conn.execute("UPDATE credit_accounts SET held_mc = 5000, balance_mc = 0 WHERE account_id = 'buyer'")
    conn.commit()
    assert book.buyer_bridge_left_mc("buyer", own_receipt="r2") == 0
    with pytest.raises(x402_bridge.BridgeRefused, match="topped-up credit"):
        _reserve(book, 50_000, account="buyer", receipt="r2")
    for status in ("unknown", "refused"):
        book.mark(pid, status)
        with pytest.raises(x402_bridge.BridgeRefused, match="topped-up credit"):
            _reserve(book, 1, account="buyer", receipt="r2")
    book.mark(pid, x402_bridge.VOID)                                   # proved unused: allowance comes back
    _reserve(book, 50_000, account="buyer", receipt="r2")


def test_granted_credit_spent_elsewhere_never_frees_paid_credit(tmp_path, monkeypatch, bridge_env):
    """Audit 2026-11-11, x402-bridge-4: $0.05 topped up and spent on this hub, then a $0.05 grant —
    the bridge must not turn the grant into USDC for a stranger."""
    conn = _ledger_db(tmp_path)
    book = x402_bridge.BridgeLedger(conn)
    _account(conn, "buyer", topped_up_mc=5_000, spent_mc=5_000, granted_mc=5_000, held_mc=5_000)
    _hold(conn, "r1", "buyer", 5_000)
    with pytest.raises(x402_bridge.BridgeRefused, match="topped-up credit"):
        _reserve(book, 50_000, account="buyer", receipt="r1")
    # Two concurrent purchases of a buyer with enough paid in: each hold counts once, not twice.
    _account(conn, "rich", topped_up_mc=20_000, held_mc=10_000)
    _hold(conn, "a", "rich", 5_000)
    _hold(conn, "b", "rich", 5_000)
    first = _reserve(book, 50_000, account="rich", receipt="a")
    book.mark(first, "signed", nonce="0x" + "11" * 32, valid_before=int(time.time()) + 300)
    _reserve(book, 50_000, account="rich", receipt="b")


def test_reconcile_voids_unused_and_keeps_used(tmp_path, monkeypatch, bridge_env):
    """Rows a dead process or a refusing seller left behind are settled from authorizationState."""
    conn = _ledger_db(tmp_path)
    book = x402_bridge.BridgeLedger(conn)
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", "acct")
    now = time.time()
    stale = now - x402_bridge.IN_FLIGHT_WINDOW_S - 10
    never_signed = _reserve(book, 1_000, now=stale)
    used, unused, fresh = (_reserve(book, 1_000, now=stale) for _ in range(3))
    expired = int(now) - x402_bridge.RECONCILE_GRACE_S - 5
    book.mark(used, "refused", nonce="0x" + "aa" * 32, valid_before=expired)
    book.mark(unused, "signed", nonce="0x" + "bb" * 32, valid_before=expired)
    book.mark(fresh, "unknown", nonce="0x" + "cc" * 32, valid_before=int(now) + 300)

    def rpc(method, params):
        assert method == "eth_call"
        data = params[0]["data"]
        return "0x" + ("1" if data.endswith("aa" * 32) else "0").zfill(64)

    assert book.reconcile(now=now, wallet="0x" + "de" * 20, rpc=rpc) == 3
    status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM x402_bridge_payments")}
    assert status[never_signed] == x402_bridge.VOID
    assert status[used] == "undelivered"
    assert status[unused] == x402_bridge.VOID
    assert status[fresh] == "unknown"                     # still inside its validity window

    def broken(method, params):
        raise RuntimeError("rpc down")

    book.mark(fresh, "unknown", valid_before=expired)
    assert book.reconcile(now=now, wallet="0x" + "de" * 20, rpc=broken) == 0
    assert conn.execute("SELECT status FROM x402_bridge_payments WHERE id = ?", (fresh,)).fetchone()[0] == "unknown"


def test_a_refused_payment_still_counts_until_it_expires(tmp_path, monkeypatch, bridge_env):
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", "acct")
    book = x402_bridge.BridgeLedger(_ledger_db(tmp_path))
    now = time.time()
    pid = _reserve(book, 50_000, now=now)
    book.mark(pid, "refused")
    assert book.in_flight(now) == pytest.approx(0.05)
    assert book.spent_today(now, PAYEE) == pytest.approx(0.05)
    assert book.in_flight(now + x402_bridge.IN_FLIGHT_WINDOW_S + 1) == 0


def test_concurrent_reservations_never_exceed_the_wallet(tmp_path, monkeypatch, bridge_env):
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", "acct")
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_SELLER_DAY_USD", "100")
    book = x402_bridge.BridgeLedger(_ledger_db(tmp_path))
    offer = x402_bridge.Offer(x402_version=2, accept=_offer(amount="10000"), resource=None, pay_to=PAYEE,
                              amount_atomic=10_000, max_timeout_s=300)
    won, lost = [], []

    def buy():
        try:
            won.append(book.reserve(offer=offer, buyer_account="acct", receipt_id="r", seller=PEER, route="x",
                                    balance_usd=0.10, now=time.time()))
        except x402_bridge.BridgeRefused:
            lost.append(1)

    threads = [threading.Thread(target=buy) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 10 and len(lost) == 30
    assert book.in_flight(time.time()) == pytest.approx(0.10)


def test_partial_capture_returns_the_rest_of_the_hold(tmp_path):
    from aimarket_hub.credits import CreditsLedger

    ledger = CreditsLedger(_ledger_db(tmp_path))
    acct = ledger.create_account("t", grant_usd=1.0)
    assert not ledger.hold(acct["account_id"], 0.5, "h1").get("error")
    out = ledger.capture_hold("h1", amount_mc=credits.usd_to_mc(0.2))
    assert out["captured_usd"] == pytest.approx(0.2)
    row = ledger.account(acct["account_id"])
    assert row["balance_usd"] == pytest.approx(0.8) and row["held_usd"] == 0
    assert row["spent_usd"] == pytest.approx(0.2)
    # Never more than the hold, and once only.
    assert not ledger.hold(acct["account_id"], 0.1, "h2").get("error")
    assert ledger.capture_hold("h2", amount_mc=credits.usd_to_mc(5))["captured_usd"] == pytest.approx(0.1)
    assert ledger.capture_hold("h2", amount_mc=1).get("already") == "captured"


# ── observed sellers (spec §2.7) through POST /observed/invoke ────────────────

SHOP = "https://shop.example.com"
ROUTE = f"{SHOP}/api/text"


class _Shop:
    """safe_request_capped for an observed x402 seller: scripted (status, json, headers) replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict | None, dict]] = []

    async def __call__(self, method, url, *, json=None, content=None, headers=None, timeout=30.0,
                       max_bytes=0, invoke=False):
        self.calls.append((method, url, json, dict(headers or {})))
        status, body, headers_out = self.replies.pop(0)
        raw = b"" if body is None else __import__("json").dumps(body).encode()
        return status, raw, {k.lower(): v for k, v in (headers_out or {}).items()}


def _observed_hub(tmp_path, monkeypatch, shop, *, price=0.002, pinned=""):
    client, db = _hub(tmp_path, monkeypatch, _Seller())
    db.upsert_observed_seller(SHOP, consent={"by": "seller", "at": "2026-11-08"}, pinned_pay_to=pinned)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    db.save_observed_seller_observation(SHOP, {
        "name": "Shop", "observed_at": now, "pinned_pay_to": pinned, "routes": [{
            "url": ROUTE, "method": "POST", "listed": True, "result": "quoted", "price_usd": price,
            "amount_atomic": str(int(price * 1e6)), "asset": x402_bridge.USDC, "network": "eip155:8453",
            "pay_to": PAYEE, "probed_at": now}]})
    monkeypatch.setattr(ohttp, "safe_request_capped", shop)
    return client, db


def _buy(client, key, **extra):
    return client.post("/ai-market/v2/observed/invoke", headers={"X-API-Key": key},
                       json={"url": ROUTE, "input": {"text": "hello"}, **extra})


def test_a_credit_buyer_buys_an_observed_route(tmp_path, monkeypatch, bridge_env):
    shop = _Shop((402, None, _v2_402(_offer(amount="1500"))), (200, {"words": 1}, SETTLED))
    client, db = _observed_hub(tmp_path, monkeypatch, shop)
    key, account = _buyer()
    r = _buy(client, key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == {"words": 1} and body["price_usd"] == pytest.approx(0.0015)
    assert body["paid_via"]["tx_hash"] == "0x" + "ab" * 32
    assert r.headers.get("payment-response")
    assert credits.ledger().balance(account) == pytest.approx(0.9985)
    method, url, sent_json, sent_headers = shop.calls[1]
    assert (method, url, sent_json) == ("POST", ROUTE, {"text": "hello"})
    assert sent_headers["PAYMENT-SIGNATURE"]
    assert _payments(db)[0]["status"] == "delivered"


@pytest.mark.parametrize("receipt", [SETTLED, {}, {"payment-response": base64.b64encode(json.dumps({
    "success": True, "transaction": "0x" + "aa" * 32, "network": "eip155:56",
}).encode()).decode()}])
def test_observed_fixed_get_pays_only_the_exact_registered_request(tmp_path, monkeypatch, bridge_env, receipt):
    from aimarket_hub.observed_sellers import EXPLICIT_GET_SOURCE

    fixed = f"{ROUTE}?id=1,1027"
    terms = {"x402Version": 2, "resource": {"url": ROUTE}, "accepts": [
        _offer(amount="100", network="eip155:56"),
        _offer(amount="1500"),
    ]}
    challenge = {"payment-required": base64.b64encode(json.dumps(terms).encode()).decode()}
    shop = _Shop((402, None, challenge), (200, {"quotes": [1, 1027]}, receipt), (402, None, challenge))
    client, db = _observed_hub(tmp_path, monkeypatch, shop, pinned=PAYEE)
    monkeypatch.setenv("AIMARKET_OBSERVED_EXPLICIT_GET_ORIGINS", SHOP)
    seller = db.get_observed_seller(SHOP)
    db.upsert_observed_seller(
        SHOP, consent={**seller["consent"], "confirmed_pay_to": PAYEE},
        allowed_routes=[fixed], pinned_pay_to=PAYEE,
    )
    db.save_observed_seller_observation(SHOP, {**seller, "routes": [{
        **seller["routes"][0], "url": fixed, "method": "GET", "source": EXPLICIT_GET_SOURCE,
    }]})
    key, account = _buyer()
    assert _buy(client, key, url=f"{ROUTE}?id=999", input={}).status_code == 404
    assert _buy(client, key, url=fixed, input={"id": "999"}).status_code == 400
    assert shop.calls == []

    result = _buy(client, key, url=fixed, input={}, max_price_usd=0.002)
    assert [call[:3] for call in shop.calls] == [("GET", fixed, None), ("GET", fixed, None)]
    assert "PAYMENT-SIGNATURE" not in shop.calls[0][3]
    signed = json.loads(base64.b64decode(shop.calls[1][3]["PAYMENT-SIGNATURE"]))
    assert signed["accepted"]["network"] == "eip155:8453"
    assert signed["payload"]["authorization"]["value"] == "1500"
    if receipt == SETTLED:
        assert result.status_code == 200, result.text
        assert result.json()["result"] == {"quotes": [1, 1027]}
        assert credits.ledger().balance(account) == pytest.approx(0.9985)
        assert _payments(db)[0]["status"] == "delivered"
    else:
        assert result.status_code == 502, result.text
        assert result.json()["error"] == "seller_settlement_unconfirmed"
        assert credits.ledger().balance(account) == pytest.approx(1.0)
        assert _payments(db)[0]["status"] == "unknown"
        repeated = _buy(client, key, url=fixed, input={}, max_price_usd=0.002)
        assert repeated.status_code == 502 and repeated.json()["bridge"]["refusal"] == "bridge_pending"
        assert len(_payments(db)) == 1
        assert "PAYMENT-SIGNATURE" not in shop.calls[2][3]


@pytest.mark.parametrize("case", [
    "verified", "opaque_network_id", "wrong_query", "wrong_status", "wrong_header_network", "wrong_network_id",
    "invalid_hash", "conflicting_hash", "wrong_header_payer", "wrong_header_amount", "failed_status",
    "wrong_chain", "wrong_block", "wrong_nonce", "wrong_amount", "wrong_payee", "wrong_payer", "wrong_token",
    "wrong_log_block", "nonadjacent_transfer", "duplicate_authorization", "receipt_unavailable",
])
def test_cmc_fixed_get_receipt_binds_the_exact_authorization(tmp_path, monkeypatch, bridge_env, case):
    from aimarket_hub.observed_sellers import EXPLICIT_GET_SOURCE

    fixed = x402_bridge._CMC_FIXED_GET
    endpoint = fixed.split("?", 1)[0]
    origin = "https://pro-api.coinmarketcap.com"
    if case == "wrong_query":
        fixed = endpoint + "?id=999"
    tx = "0x" + "ab" * 32
    block_hash = "0x" + "cd" * 32
    alias = {"success": True, "txHash": tx, "networkId": 8453, "x402FlowId": "fixture"}
    alias.update({
        "opaque_network_id": {"networkId": "opaque-vendor-id"},
        "wrong_header_network": {"network": "eip155:1"}, "wrong_network_id": {"networkId": "0x1"},
        "invalid_hash": {"txHash": "0x12"}, "conflicting_hash": {"transaction": block_hash},
        "wrong_header_payer": {"payer": OTHER}, "wrong_header_amount": {"amount": 9999},
        "failed_status": {"success": False},
    }.get(case, {}))
    challenge = {"x402Version": 2, "resource": {"url": endpoint}, "accepts": [_offer(amount="10000")]}
    headers = {"payment-required": base64.b64encode(json.dumps(challenge).encode()).decode()}
    receipt_header = {"payment-response": base64.b64encode(json.dumps(alias).encode()).decode()}
    shop = _Shop((402, None, headers), (201 if case == "wrong_status" else 200, {"quotes": [1, 1027]}, receipt_header),
                 (402, None, headers))
    client, db = _observed_hub(tmp_path, monkeypatch, shop)
    monkeypatch.setenv("AIMARKET_OBSERVED_EXPLICIT_GET_ORIGINS", origin)
    consent = {"by": "fixture", "at": "2026-11-13", "confirmed_pay_to": PAYEE}
    db.upsert_observed_seller(origin, consent=consent, allowed_routes=[fixed], pinned_pay_to=PAYEE)
    source = db.get_observed_seller(SHOP)
    db.save_observed_seller_observation(origin, {**source, "origin": origin, "pinned_pay_to": PAYEE, "routes": [{
        **source["routes"][0], "url": fixed, "method": "GET", "source": EXPLICIT_GET_SOURCE,
        "price_usd": 0.01, "amount_atomic": "10000",
    }]})
    calls = []

    def rpc(_url, method, params, _timeout):
        calls.append(method)
        if method == "eth_chainId":
            return "0x1" if case == "wrong_chain" else "0x2105"
        if method == "eth_getBlockByNumber":
            assert params == ["0x40", False]
            return {"number": "0x40", "hash": tx if case == "wrong_block" else block_hash}
        assert method == "eth_getTransactionReceipt" and params == [tx]
        if case == "receipt_unavailable":
            return None
        signed = json.loads(base64.b64decode(shop.calls[1][3]["PAYMENT-SIGNATURE"]))
        nonce = signed["payload"]["authorization"]["nonce"]

        def topic(address):
            return "0x" + address[2:].lower().zfill(64)

        payer = topic(OTHER if case == "wrong_payer" else BRIDGE_ADDRESS)
        payee = topic(OTHER if case == "wrong_payee" else PAYEE)
        facts = {"address": x402_bridge.USDC, "transactionHash": tx, "blockHash": block_hash,
                 "blockNumber": "0x40", "removed": False}
        used = {**facts, "logIndex": "0x4", "data": "0x", "topics": [settle.AUTHORIZATION_USED_TOPIC, payer,
                tx if case == "wrong_nonce" else nonce]}
        moved = {**facts, "logIndex": "0x6" if case == "nonadjacent_transfer" else "0x5",
                 "data": f"0x{(10001 if case == 'wrong_amount' else 10000):064x}",
                 "topics": [settle.TRANSFER_TOPIC, payer, payee]}
        if case == "wrong_token":
            moved["address"] = OTHER
        if case == "wrong_log_block":
            moved["blockNumber"] = "0x41"
        logs = [used, moved]
        if case == "duplicate_authorization":
            logs.append({**used, "logIndex": "0x6"})
        return {**facts, "status": "0x1", "logs": logs}

    monkeypatch.setattr(settle, "rpc_urls", lambda chain_id: ["https://cmc-test.invalid"])
    monkeypatch.setattr(settle, "_rpc", rpc)
    key, account = _buyer()
    result = _buy(client, key, url=fixed, input={}, max_price_usd=0.01)
    assert [call[:3] for call in shop.calls] == [("GET", fixed, None), ("GET", fixed, None)], result.text
    assert "PAYMENT-SIGNATURE" not in shop.calls[0][3] and "PAYMENT-SIGNATURE" in shop.calls[1][3]
    [row] = _payments(db)
    if case in {"verified", "opaque_network_id"}:
        assert result.status_code == 200, result.text
        assert result.json()["paid_via"]["tx_hash"] == tx
        assert row["status"] == "delivered" and row["tx_hash"] == tx
        assert credits.ledger().balance(account) == pytest.approx(0.99)
        assert calls == ["eth_chainId", "eth_getTransactionReceipt", "eth_getBlockByNumber"]
        return

    assert result.status_code == 502 and result.json()["error"] == "seller_settlement_unconfirmed", result.text
    assert row["status"] == "unknown" and not row["tx_hash"]
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    repeated = _buy(client, key, url=fixed, input={}, max_price_usd=0.01)
    assert repeated.status_code == 502 and repeated.json()["bridge"]["refusal"] == "bridge_pending"
    assert len(_payments(db)) == 1 and "PAYMENT-SIGNATURE" not in shop.calls[2][3]


def test_an_observed_route_is_not_paid_above_its_observed_price(tmp_path, monkeypatch, bridge_env):
    shop = _Shop((402, None, _v2_402(_offer(amount="2500"))))
    client, db = _observed_hub(tmp_path, monkeypatch, shop, price=0.002)
    key, account = _buyer()
    r = _buy(client, key)
    assert r.status_code == 502 and r.json()["error"] == "upstream_unpaid"
    assert len(shop.calls) == 1 and credits.ledger().balance(account) == pytest.approx(1.0)


def test_an_observed_route_pays_only_the_pinned_payee(tmp_path, monkeypatch, bridge_env):
    shop = _Shop((402, None, _v2_402(_offer(pay_to=PAYEE))))
    client, db = _observed_hub(tmp_path, monkeypatch, shop, pinned=OTHER)
    key, account = _buyer()
    r = _buy(client, key)
    assert r.status_code == 502 and "recipient other than" in r.json()["detail"]
    assert credits.ledger().balance(account) == pytest.approx(1.0)


def test_a_url_this_hub_does_not_serve_is_refused_without_a_call(tmp_path, monkeypatch, bridge_env):
    shop = _Shop()
    client, db = _observed_hub(tmp_path, monkeypatch, shop)
    key, _ = _buyer()
    r = client.post("/ai-market/v2/observed/invoke", headers={"X-API-Key": key},
                    json={"url": "http://169.254.169.254/latest/meta-data", "input": {}})
    assert r.status_code == 404 and shop.calls == []


def test_observed_invoke_needs_a_key_and_a_bridge(tmp_path, monkeypatch, bridge_env):
    shop = _Shop()
    client, db = _observed_hub(tmp_path, monkeypatch, shop)
    assert client.post("/ai-market/v2/observed/invoke", json={"url": ROUTE}).status_code == 401
    monkeypatch.delenv("AIMARKET_X402_BRIDGE")
    key, _ = _buyer()
    assert _buy(client, key).status_code == 503


def test_observed_invoke_respects_max_price(tmp_path, monkeypatch, bridge_env):
    shop = _Shop()
    client, db = _observed_hub(tmp_path, monkeypatch, shop, price=0.002)
    key, _ = _buyer()
    r = _buy(client, key, max_price_usd=0.001)
    assert r.status_code == 409 and shop.calls == []


def test_an_observed_seller_that_fails_after_payment_is_not_charged(tmp_path, monkeypatch, bridge_env):
    shop = _Shop((402, None, _v2_402()), (503, {"error": "down"}, SETTLED))
    client, db = _observed_hub(tmp_path, monkeypatch, shop)
    key, account = _buyer()
    r = _buy(client, key)
    assert r.status_code == 502 and r.json()["error"] == "seller_error"
    assert credits.ledger().balance(account) == pytest.approx(1.0)
    assert _payments(db)[0]["status"] == "undelivered"


def test_a_broke_buyer_gets_no_hub_payment_offer(tmp_path, monkeypatch, bridge_env):
    shop = _Shop()
    client, db = _observed_hub(tmp_path, monkeypatch, shop)
    key, _ = _buyer(topped_up=0.0001)
    r = _buy(client, key)
    assert r.status_code == 402 and "accepts" not in r.json() and "payment-required" not in r.headers
    assert shop.calls == []


# ── review fixes (2026-11-08) ────────────────────────────────────────────────


def test_an_unreadable_key_turns_the_bridge_off_not_the_hub(tmp_path, monkeypatch, bridge_env):
    key = Path(os.environ["AIMARKET_X402_BRIDGE_KEY_FILE"])
    os.chmod(key, 0o000)
    x402_bridge._key_cache.clear()
    try:
        if os.access(key, os.R_OK):          # root reads anything: nothing to test
            pytest.skip("running as root")
        assert "not readable" in x402_bridge.unavailable_reason()
        seller = _Seller(_Resp(402, {}, _v2_402()))
        client, db = _hub(tmp_path, monkeypatch, seller)
        k, _ = _buyer()
        assert _invoke(client, k).status_code == 402      # the old passthrough, not a 500
    finally:
        os.chmod(key, 0o600)


def test_the_signing_domain_is_usdcs_whatever_the_offer_says(bridge_env):
    offer = x402_bridge.Offer(x402_version=2, accept=_offer(), resource=None, pay_to=PAYEE, amount_atomic=1000,
                              max_timeout_s=300, extra={"name": "Not USDC", "version": "9"})
    _, value, auth = x402_bridge.sign_payment(offer)
    sig = json.loads(base64.b64decode(value))["payload"]["signature"]
    digest = eip712.transfer_authorization_digest(
        sender=auth["from"], recipient=auth["to"], value=1000, valid_after=int(auth["validAfter"]),
        valid_before=int(auth["validBefore"]), nonce=auth["nonce"], token_name="USD Coin", token_version="2",
        chain_id=8453, token_address=x402_bridge.USDC)
    assert eip712.addresses_equal(eip712.recover_transfer_signer(sig, digest), BRIDGE_ADDRESS)


def test_a_just_settled_payment_still_counts_against_the_wallet(tmp_path, monkeypatch, bridge_env):
    monkeypatch.setenv("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS", "acct")
    book = x402_bridge.BridgeLedger(_ledger_db(tmp_path))
    now = time.time()
    pid = _reserve(book, 40_000, now=now)
    book.mark(pid, "delivered")
    assert book.in_flight(time.time()) == pytest.approx(0.04)
    assert book.in_flight(time.time() + x402_bridge.SETTLED_LAG_S + 1) == 0


def test_the_operators_own_accounts_may_spend_granted_credit(tmp_path, monkeypatch, bridge_env):
    seller = _Seller(_Resp(402, {}, _v2_402()), _Resp(200, {"ok": True}))
    client, db = _hub(tmp_path, monkeypatch, seller)
    key, account = _buyer(topped_up=0, granted=1.0)
    monkeypatch.setenv("AIMARKET_OPERATOR_ACCOUNTS", account)
    from aimarket_hub import ecosystem

    ecosystem._clear_cache()
    try:
        assert _invoke(client, key).status_code == 200
    finally:
        ecosystem._clear_cache()


def test_a_seller_direct_payee_in_the_well_known_counts_as_published():
    from aimarket_hub.peer_routing import _declared_x402

    machina = {"payment_rails": {"seller_direct": {"enabled": True, "network": "eip155:8453", "payTo": OTHER}}}
    assert _declared_x402(machina) == (True, frozenset({OTHER.lower()}))
    elsewhere = {"payment_rails": {"seller_direct": {"enabled": True, "network": "solana", "payTo": OTHER}}}
    assert _declared_x402(elsewhere) == (True, frozenset())
    agent_exec = {"payment": {"enforced": True, "protocol": "x402",
                              "accepts": [{"network": "base", "pay_to": PAYEE}, {"network": "solana", "pay_to": "G"}]}}
    assert _declared_x402(agent_exec) == (True, frozenset({PAYEE.lower()}))
    assert _declared_x402({"name": "no billing"}) == (False, frozenset())
