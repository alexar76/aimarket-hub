"""Audit 2026-11-11, credits-1: a dust EIP-3009 authorization over someone else's top-up nonce.

The hub's own settle.verify_transfer, topup.redeem, credits ledger and x402 ledger on a migrated
SQLite database; only the chain RPC is stubbed with receipts. Before the fix the attacker's 10
base units closed the buyer's quote, in the same transaction or in a separate one redeemed first.
"""
from __future__ import annotations

import pytest

from aimarket_hub import credits, settle, topup
from aimarket_hub.database import HubDatabase

PAY_TO = "0x" + "a1" * 20
BUYER = "0x" + "b2" * 20
ATTACKER = "0x" + "c3" * 20
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _pad(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0")


def _auth(signer, nonce, units):
    return [{"address": USDC, "topics": [settle.AUTHORIZATION_USED_TOPIC, _pad(signer), nonce], "data": "0x"},
            {"address": USDC, "topics": [settle.TRANSFER_TOPIC, _pad(signer), _pad(PAY_TO)], "data": hex(units)}]


@pytest.fixture
def door(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_TOPUP_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("AIMARKET_X402_CHAIN", "base")
    monkeypatch.setenv("AIMARKET_TOPUP_VERIFY_DECIMALS", "0")
    monkeypatch.setenv("AIMARKET_SETTLE_RPC_URL", "http://stub.invalid")
    topup._VERIFY_LOG.clear()
    db = HubDatabase(str(tmp_path / "hub.db"))
    ledger = credits.CreditsLedger(db._conn)
    store = topup.TopupStore(db._conn)
    payments = settle.PaymentStore(db._conn)
    acct = ledger.create_account(label="buyer")["account_id"]
    receipts: dict[str, dict] = {}

    def fake_rpc(url, method, params, timeout):
        if method == "eth_getTransactionReceipt":
            return receipts[params[0]]
        return {"eth_chainId": hex(8453), "eth_blockNumber": "0x20"}[method]

    monkeypatch.setattr(settle, "_rpc", fake_rpc)

    def redeem(nonce, tx_hash, **kw):
        return topup.redeem(store=store, ledger=ledger, payments=payments, nonce=nonce, tx_hash=tx_hash,
                            claim_deposit=lambda **_: {"ok": True}, **kw)

    return store, ledger, acct, receipts, redeem


def test_a_dust_authorization_ahead_of_the_buyers_in_one_tx_does_not_bind(door):
    store, ledger, acct, receipts, redeem = door
    q, _body, _v2 = topup.quote(store=store, account_id=acct, amount_usd=5, hub_url="https://hub.test")
    tx = "0x" + "ee" * 32
    receipts[tx] = {"status": "0x1", "blockNumber": "0x10",
                    "logs": _auth(ATTACKER, q.nonce, 10) + _auth(BUYER, q.nonce, 5_000_000)}
    redeem(q.nonce, tx)
    after = store.get(q.nonce)
    assert after.status == topup.CREDITED and after.payer == BUYER and after.paid_units == 5_000_000
    assert ledger.balance(acct) == pytest.approx(5.0)


def test_a_dust_tx_redeemed_first_cannot_lock_the_buyer_out(door):
    store, ledger, acct, receipts, redeem = door
    q, _b, _v = topup.quote(store=store, account_id=acct, amount_usd=100, hub_url="https://hub.test")
    buyer_tx, attacker_tx = "0x" + "b1" * 32, "0x" + "a7" * 32
    receipts[buyer_tx] = {"status": "0x1", "blockNumber": "0x10", "logs": _auth(BUYER, q.nonce, 100_000_000)}
    receipts[attacker_tx] = {"status": "0x1", "blockNumber": "0x11", "logs": _auth(ATTACKER, q.nonce, 10)}
    with pytest.raises(topup.TopupError) as refused:
        redeem(q.nonce, attacker_tx, limit_key="ip:attacker")
    assert refused.value.error == "payment_invalid"
    assert store.get(q.nonce).status != topup.CREDITED
    redeem(q.nonce, buyer_tx, owner_account=acct)
    assert ledger.balance(acct) == pytest.approx(100.0)
