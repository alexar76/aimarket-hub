"""SELLER-OP/1: what happens to a paid operation when something fails BEFORE the provider is called.

A failure before dispatch used to land in the blanket handler that marks an operation
"reconciliation_required" ("execution interrupted"), though nothing had been executed: the request
was then immutable, recovery needed a verified payment that was never stored, and a buyer who had
paid the seller was stuck at contact_operator for good.
"""
import asyncio
import threading
import time

import pytest
from fastapi import HTTPException

from aimarket_hub import settle
from aimarket_hub.seller_operations import ExecuteRequest, QuoteRequest, SellerOperations
from tests.test_studio_paid import TX, WALLET, listing, setup  # noqa: F401  (fixture)

OTHER = "0x" + "99" * 20


def pay(monkeypatch, authorizer):
    def verify(**kw):
        t = kw["terms"]
        return dict(tx_hash=kw["tx_hash"], paid_units=t.seller_units, fee_units=t.fee_units,
                    authorizer=authorizer, nonce=kw["require_nonce"], pay_to=t.pay_to)
    monkeypatch.setattr(settle, "verify_transfer", verify)


def operations(service):
    calls = []

    async def invoke(payload, headers, caller, *, operation_id):
        calls.append(operation_id)
        return 200, {"success": True, "result": {"reading": 7}, "receipt": {"nonce": "r"}}
    return SellerOperations(service, invoke), calls


def prepare(ops, wallet=None):
    body = {"product_id": "demo-product", "capability_id": "demo.read@v1", "max_price_usd": 1}
    if wallet:
        body["wallet"] = wallet
    q = ops.quote(QuoteRequest(**body))
    return q["offer"]["operation_id"], q["operation_token"]


def run(ops, op, token, **body):
    return asyncio.run(ops.execute(op, token, ExecuteRequest(input={}, tx_hash=TX, **body), "buyer.test"))


def test_an_open_offer_paid_without_a_declared_wallet_completes_and_replays(setup, monkeypatch):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    pay(monkeypatch, WALLET)
    first = run(ops, op, token)
    assert first["status"] == "completed" and first["wallet"] == WALLET.lower()
    # The same body again returns the stored result: no 409, no second dispatch.
    assert run(ops, op, token)["status"] == "completed"
    assert calls == [op]


def test_a_wrong_declared_wallet_on_an_open_offer_is_correctable(setup, monkeypatch):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    pay(monkeypatch, WALLET)
    with pytest.raises(HTTPException) as refused:
        run(ops, op, token, wallet=OTHER)
    assert refused.value.status_code == 409 and "nothing was dispatched" in refused.value.detail
    assert calls == []
    assert run(ops, op, token, wallet=WALLET)["status"] == "completed"
    assert calls == [op]


def test_a_wallet_scoped_offer_paid_by_another_wallet_fails_cleanly(setup, monkeypatch):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops, wallet=WALLET)
    pay(monkeypatch, OTHER)
    out = run(ops, op, token, wallet=WALLET)
    assert out["status"] == "failed" and "no provider dispatch" in out["detail"]
    assert out["recovery"]["action"] == "read_result"
    assert calls == []


def test_a_listing_that_breaks_after_payment_fails_without_dispatch(setup, monkeypatch):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    pay(monkeypatch, WALLET)
    monkeypatch.setattr(service, "describe", lambda *a, **k: (_ for _ in ()).throw(ValueError("route gone")))
    out = run(ops, op, token)
    assert out["status"] == "failed" and out["detail"].startswith("Listing changed")
    assert calls == []


def test_an_unexpected_error_before_dispatch_releases_the_claim(setup, monkeypatch):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    pay(monkeypatch, WALLET)
    real = service.describe
    monkeypatch.setattr(service, "describe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    with pytest.raises(RuntimeError):
        run(ops, op, token)
    assert ops.status(op, token)["status"] != "reconciliation_required"
    monkeypatch.setattr(service, "describe", real)
    assert run(ops, op, token)["status"] == "completed"
    assert calls == [op]


@pytest.mark.parametrize("price", [0, 0.01])
def test_the_paid_offer_holds_after_the_listing_price_changes(setup, monkeypatch, price):
    service, db, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    pay(monkeypatch, WALLET)
    listing(db, price=price)
    result = run(ops, op, token)
    assert result["status"] == "completed"
    assert run(ops, op, token) == result
    assert calls == [op]


@pytest.mark.asyncio
async def test_only_the_current_predispatch_claim_may_invoke(setup, monkeypatch):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    body = ExecuteRequest(input={}, tx_hash=TX)
    loop = asyncio.get_running_loop()
    checking = asyncio.Event()
    release = threading.Event()
    pay(monkeypatch, WALLET)
    verify = settle.verify_transfer

    def pause_first(**kwargs):
        if not checking.is_set():
            loop.call_soon_threadsafe(checking.set)
            assert release.wait(5)
        return verify(**kwargs)

    monkeypatch.setattr(settle, "verify_transfer", pause_first)
    first = asyncio.create_task(ops.execute(op, token, body, "buyer"))
    try:
        await asyncio.wait_for(checking.wait(), timeout=5)
        ops.conn.execute(
            "UPDATE seller_operations SET updated_at = ? WHERE operation_id = ?",
            (time.time() - ops.STALE_BUSY_S - 1, op),
        )
        ops.conn.commit()
        assert (await ops.recover(op, token))["recovery"]["action"] == "resume_same_operation"
        second = await ops.execute(op, token, body, "buyer")
    finally:
        release.set()
        first_result = await first
    assert second["status"] == "completed"
    assert first_result == second
    assert calls == [op]


@pytest.mark.parametrize("code,answer", [(429, {"detail": "slow down"}),
                                        (503, {"detail": "payment verifier unavailable"})])
def test_proven_predispatch_refusals_leave_the_same_paid_operation_retryable(setup, monkeypatch, code, answer):
    service, _, _ = setup
    ops, calls = operations(service)
    op, token = prepare(ops)
    pay(monkeypatch, WALLET)
    original = ops.invoke

    async def refused(*args, **kwargs):
        return code, answer

    ops.invoke = refused
    outcome = run(ops, op, token)
    assert outcome["status"] == "payment_pending"
    assert outcome["recovery"]["action"] == "resume_same_operation"
    assert calls == []
    ops.invoke = original
    assert run(ops, op, token)["status"] == "completed"
    assert calls == [op]
