"""End-to-end regressions for the x402 bridge findings of the money-flow audit of 2026-11-11.

Each test is the audit's own reproduction, with the assertion turned round: what the audit showed the
hub doing is what these tests now forbid.
"""

from __future__ import annotations

import pytest

from aimarket_hub import credits
from tests.test_x402_bridge import (  # noqa: F401
    SETTLED, _Resp, _Seller, _buyer, _hub, _invoke, _offer, _payments, _v2_402, bridge_env,
)

PAYEES = ["0x" + f"{i:040x}" for i in range(1, 6)]  # five payees the seller publishes


def test_settle_then_refuse_cannot_drain_the_bridge_on_one_top_up(tmp_path, monkeypatch, bridge_env):
    """x402-bridge-1/-2: a seller settles every paid call and answers {"success": false}, rotating
    payTo across five payees. Before the fix one $0.05 top-up paid out $2.50 in a day."""
    bridge_env["usd"] = 100.0
    monkeypatch.setenv("AIMARKET_INVOKE_RATE_LIMIT", "100000")
    script = []
    for p in PAYEES:
        for _ in range(10):
            script.append(_Resp(402, {}, _v2_402(_offer(amount="50000", pay_to=p))))
            script.append(_Resp(200, {"success": False, "refuse_reason": "nope", "data": "the work"}, SETTLED))
    seller = _Seller(*script, well_known={"payment": {"enforced": True, "protocol": "x402", "accepts": [
        {"network": "base", "pay_to": p} for p in PAYEES]}})
    client, db = _hub(tmp_path, monkeypatch, seller, price=0.05)
    key, account = _buyer(topped_up=0.05)

    for _ in range(len(PAYEES) * 10):
        _invoke(client, key)
    rows = _payments(db)
    paid_out = sum(r["amount_usd"] for r in rows if r["status"] in ("undelivered", "unknown", "refused"))
    # The bridge's loss is bounded by what this buyer paid in, and the buyer still owes nothing.
    # (The scripted seller's later answers fall out of step once the bridge stops paying; what
    # matters is that exactly one payment was ever signed.)
    assert paid_out == pytest.approx(0.05)
    assert len(rows) == 1
    assert credits.ledger().account(account)["spent_usd"] == 0


def test_granted_credit_never_reaches_a_stranger_after_paid_credit_was_spent(tmp_path, monkeypatch, bridge_env):
    """x402-bridge-4: paid credit spent on this hub, then a grant — the grant must not become USDC."""
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(amount="50000"))),
                     _Resp(200, {"ok": True, "result": 1}, SETTLED))
    client, db = _hub(tmp_path, monkeypatch, seller, price=0.05)
    key, account = _buyer(topped_up=0.05)
    ledger = credits.ledger()
    assert not ledger.debit(account, 0.05, receipt_id="local_1", note="local capability").get("error")
    ledger.grant(account, 0.05, note="promo")
    r = _invoke(client, key)
    assert r.status_code == 402
    assert _payments(db) == []
    assert ledger.balance(account) == pytest.approx(0.05)


def test_a_json_array_answer_after_payment_is_delivered_and_charged_once(tmp_path, monkeypatch, bridge_env):
    """x402-bridge-3: a paid answer the federated path could not parse was thrown away (502), so the
    buyer retried and the bridge paid again. A paid 2xx answer is delivered, whatever its shape."""
    seller = _Seller(_Resp(402, {}, _v2_402(_offer(amount="1000"))), _Resp(200, [{"row": 1}], SETTLED))
    client, db = _hub(tmp_path, monkeypatch, seller, price=0.002)
    key, account = _buyer(topped_up=1.0)
    a = _invoke(client, key)
    assert a.status_code == 200, a.text
    assert '"row"' in a.text
    assert [p["status"] for p in _payments(db)] == ["delivered"]
    assert credits.ledger().account(account)["spent_usd"] == pytest.approx(0.001)
