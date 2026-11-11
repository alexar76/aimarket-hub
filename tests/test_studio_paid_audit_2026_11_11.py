"""Audit 2026-11-11, satellites-1/-2: a Studio paid run whose process died (or whose request was
cancelled) while the buyer's payment was being verified stayed busy forever, and a paid step the
inner /invoke refused BEFORE running ended the run failed. Each is recoverable now."""
from __future__ import annotations

import asyncio
import json

import pytest

from aimarket_hub import settle
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer
from aimarket_hub.studio_paid import AdvanceRequest, PaidStudio, PreflightRequest

SELLER = "0x" + "12" * 20
WALLET = "0x" + "34" * 20
TX = "0x" + "56" * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_X402_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_X402_ACCEPT", "1")
    monkeypatch.setenv("AIMARKET_X402_CHAIN", "base")
    monkeypatch.delenv("AIMARKET_X402_ASSET", raising=False)
    monkeypatch.setenv("AIMARKET_MARKET_FEE_BPS", "0")
    monkeypatch.setenv("AIMARKET_SETTLE_RPC_URL", "http://127.0.0.1:1")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "key")
    db = HubDatabase(config.db_path)
    signer = Signer(config.signing_key_path)
    seen = []

    async def invoke(payload, headers, caller):
        seen.append(payload)
        return 200, {"success": True, "result": {"reading": 7}}

    db.upsert_capability(Capability(product_id="demo-product", capability_id="demo.read@v1",
                                    name="r", source_hub="local", payout_address=SELLER,
                                    price_per_call_usd=0.004, prompt_template="{}"))
    return config, db, signer, invoke, seen


def _studio(config, db, signer, invoke):
    return PaidStudio(db=db, config=config, signer=signer, invoke=invoke, route_ok=lambda c: True)


def _adv(svc, q, tx=None):
    return asyncio.run(svc.advance(q["run_id"], q["access_token"],
                                   AdvanceRequest(step_id="n0", wallet=WALLET, tx_hash=tx), "buyer"))


def _good_verify(**kw):
    t = kw["terms"]
    return dict(tx_hash=kw["tx_hash"], paid_units=t.seller_units, fee_units=t.fee_units,
                nonce=kw["require_nonce"], authorizer=WALLET, pay_to=t.pay_to, confirmations=2)


def test_a_run_a_dead_process_left_busy_before_dispatch_is_reclaimed(setup, monkeypatch):
    config, db, signer, invoke, seen = setup
    svc = _studio(config, db, signer, invoke)
    q = svc.preflight(PreflightRequest(nodes=[dict(product_id="demo-product",
                                                   capability_id="demo.read@v1", source_hub="local")]))
    _adv(svc, q)
    snapshot = {}

    class Killed(BaseException):
        pass

    def dying_verify(**kw):
        row = db._conn.execute("SELECT state_json, busy FROM studio_paid_runs WHERE run_id = ?",
                               (q["run_id"],)).fetchone()
        snapshot.update(state_json=row["state_json"], busy=row["busy"])
        raise Killed()

    monkeypatch.setattr(settle, "verify_transfer", dying_verify)
    with pytest.raises(Killed):
        _adv(svc, q, tx=TX)
    # what a SIGKILL leaves: the payment_pending save, busy=1, written a while ago
    db._conn.execute("UPDATE studio_paid_runs SET state_json = ?, busy = ?, updated_at = ? WHERE run_id = ?",
                     (snapshot["state_json"], 1, 0.0, q["run_id"]))
    db._conn.commit()
    monkeypatch.setattr(settle, "verify_transfer", _good_verify)
    out = _adv(_studio(config, db, signer, invoke), q, tx=TX)
    assert out["status"] == "completed" and len(seen) == 1


def test_a_cancellation_before_dispatch_leaves_the_run_active(setup, monkeypatch):
    config, db, signer, invoke, seen = setup
    svc = _studio(config, db, signer, invoke)
    q = svc.preflight(PreflightRequest(nodes=[dict(product_id="demo-product",
                                                   capability_id="demo.read@v1", source_hub="local")]))
    _adv(svc, q)

    def cancelled(**kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(settle, "verify_transfer", cancelled)
    with pytest.raises(asyncio.CancelledError):
        _adv(svc, q, tx=TX)
    monkeypatch.setattr(settle, "verify_transfer", _good_verify)
    out = _adv(svc, q, tx=TX)
    assert out["status"] == "completed" and len(seen) == 1


def test_a_paid_step_refused_before_running_is_retried_with_the_same_payment(setup, monkeypatch):
    config, db, signer, invoke, seen = setup
    calls = []

    async def refusing(payload, headers, caller):
        calls.append(headers)
        return 429, {"success": False, "error": "rate_limited", "detail": "invoke rate limit exceeded — slow down"}

    svc = _studio(config, db, signer, refusing)
    q = svc.preflight(PreflightRequest(nodes=[dict(product_id="demo-product",
                                                   capability_id="demo.read@v1", source_hub="local")]))
    _adv(svc, q)
    monkeypatch.setattr(settle, "verify_transfer", _good_verify)
    out = _adv(svc, q, tx=TX)
    assert out["status"] == "active"
    svc.invoke = invoke
    again = _adv(svc, q, tx=TX)
    assert again["status"] == "completed" and len(seen) == 1
