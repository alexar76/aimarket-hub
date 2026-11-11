"""Pay-on-Verified regressions from the money-flow audit of 2026-11-11."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import verified_settlement as vs_mod
from tests.test_verified_settlement import (  # noqa: F401
    VERIFY, _build, _channel_state, _invoke, _MetisScript, _open_channel,
)


def test_a_pass_after_the_escrow_authorization_died_releases_instead_of_capturing(tmp_path, monkeypatch):
    """escrow-5 / pay-on-verified-4: a verdict that lands after the buyer's escrow authorization
    expired captured revenue nothing could ever collect on chain."""
    monkeypatch.setattr(vs_mod.VerifiedSettlementService, "_escrow_uncollectable",
                        staticmethod(lambda receipt_id: True))
    app, _db = _build(tmp_path, monkeypatch, _MetisScript([("judged", True, 0.95)]))
    with TestClient(app) as client:
        channel_id, secret = _open_channel(client)
        r = _invoke(client, channel_id, secret, verify={**VERIFY, "wait": True})
        assert r.status_code == 200, r.text
        ch = _channel_state(client, channel_id=channel_id)
        assert ch["used_usd"] == pytest.approx(0.0)


def test_uncollectable_reads_the_bridge_row(monkeypatch):
    from aimarket_hub.escrow_bridge import store as bridge_store

    class _Row:
        def __init__(self, status, last_error="", expired=False):
            self.status, self.last_error, self._expired = status, last_error, expired

        def expired(self, now=None):
            return self._expired

    rows = {
        "superseded": _Row(bridge_store.ABANDONED, bridge_store.SUPERSEDED_REASON),
        "abandoned": _Row(bridge_store.ABANDONED, "authorization deadline passed before submission"),
        "pending_live": _Row(bridge_store.PENDING),
        "pending_expired": _Row(bridge_store.PENDING, expired=True),
        "confirmed": _Row(bridge_store.CONFIRMED),
    }

    class _Store:
        def __init__(self, *a, **kw):
            pass

        def get(self, rid):
            return rows.get(rid)

        def close(self):
            pass

    monkeypatch.setattr(bridge_store, "AuthorizationStore", _Store)
    check = vs_mod.VerifiedSettlementService._escrow_uncollectable
    assert [check(r) for r in ("superseded", "abandoned", "pending_live", "pending_expired",
                               "confirmed", "missing")] == [False, True, False, True, False, False]


@pytest.fixture
def _claims(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("AIMARKET_DEPOSIT_CLAIMS_DIR", str(tmp_path_factory.mktemp("claims")))


def _publisher(client, db):
    from tests.test_verified_settlement import CAP

    acct = client.post("/ai-market/v2/accounts", json={"label": "seller"}).json()
    db._conn.execute("UPDATE capabilities SET publisher_id = ? WHERE capability_id = ?",
                     (acct["account_id"], CAP["capability_id"]))
    db._conn.commit()
    return acct


def _balance(client, key):
    return client.get("/ai-market/v2/account", headers={"X-API-Key": key}).json()["balance_usd"]


def test_a_verified_sale_pays_the_seller_the_same_share_as_a_plain_one(tmp_path, monkeypatch, _claims):
    """pay-on-verified-2: the plain channel sale credited the seller 70%, the verified one 0."""
    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_CREDITS_FREE_GRANT_USD", "0")
    monkeypatch.setenv("AIMARKET_SIGNUP_GRANT_USD", "0")
    app, db = _build(tmp_path, monkeypatch, _MetisScript([("judged", True, 0.95)]))
    with TestClient(app) as client:
        seller = _publisher(client, db)
        channel_id, secret = _open_channel(client)
        assert _invoke(client, channel_id, secret).status_code == 200
        after_plain = _balance(client, seller["api_key"])
        r = _invoke(client, channel_id, secret, verify={**VERIFY, "wait": True})
        assert r.status_code == 200 and r.json()["verification"]["status"] == "settled"
        after_verified = _balance(client, seller["api_key"])
    assert after_plain == pytest.approx(0.28)
    assert after_verified == pytest.approx(0.56)


def test_the_jury_sees_the_request_input(tmp_path, monkeypatch, _claims):
    """pay-on-verified-1: the prompt carried the intent and the output only."""
    script = _MetisScript([("judged", True, 0.95)])
    app, db = _build(tmp_path, monkeypatch, script)
    with TestClient(app) as client:
        channel_id, secret = _open_channel(client)
        r = _invoke(client, channel_id, secret, verify={**VERIFY, "wait": True})
        assert r.status_code == 200, r.text
    prompts = [p for p in getattr(script, "prompts", [])] or [
        row["input_json"] for row in db._conn.execute("SELECT input_json FROM verified_settlements")]
    assert any('"text": "paid"' in p or '"text":"paid"' in p for p in prompts)
    composed = vs_mod.VerifiedSettlementService._compose_input(
        "Write a sonnet", '{"r": "pagado"}', "a" * 24, request_input='{"text": "paid"}')
    assert "<<<REQUEST-INPUT-" in composed and "cannot be held against the seller" in composed
    # A row from before the column existed gets exactly the old prompt.
    assert vs_mod.VerifiedSettlementService._compose_input("i", "{}", "a" * 24) == \
        vs_mod.VerifiedSettlementService._compose_input("i", "{}", "a" * 24, request_input="")


def test_one_wallet_counts_once_however_many_channels(monkeypatch):
    """pay-on-verified-1: the slash ladder's distinct consumers were channel ids."""
    from aimarket_hub import channels

    class _Ledger:
        def get(self, cid):
            return {"wallet": "0xABC" if cid in ("ch_1", "ch_2") else ""}

    monkeypatch.setattr(channels, "_ledger", _Ledger())
    of = vs_mod.VerifiedSettlementService._consumer_of
    assert of("ch_1") == of("ch_2") == "wallet:0xabc"
    assert of("ch_3") == "ch_3" and of("") == ""


def test_a_failed_advisory_verdict_is_not_signed_as_a_refund(tmp_path, monkeypatch):
    """pay-on-verified-3: nothing was held, so nothing was refunded."""
    import json

    from aimarket_hub.database import HubDatabase
    from aimarket_hub.signing import Signer
    from tests.test_verified_settlement import AID, _insert_verifying_row

    db = HubDatabase(str(tmp_path / "a.db"))
    svc = vs_mod.VerifiedSettlementService(db, Signer(str(tmp_path / "k")))
    row = _insert_verifying_row(db, nonce="adv1")
    svc._resolve_verdict(row, {"answer": "", "status": "success", "verified": True, "verify_score": 0.1,
                               "verify_performed": True, "trace_id": "t",
                               "delivery_verdict": {"fulfils": False, "score": 0.1, "reasons": ["no"]},
                               vs_mod._AUDIT_ID_KEY: AID}, 5)
    env = json.loads(db._conn.execute("SELECT envelope_json FROM verified_settlements WHERE nonce='adv1'")
                     .fetchone()["envelope_json"])
    assert env["status"] == "advisory_failed" and env["refunded"] is False


def test_a_misconfigured_verifier_is_not_retried_forever(monkeypatch):
    """pay-on-verified-5: 3xx/403/404/405/422 were retried forever without a log line; the
    overall wait had no default."""
    monkeypatch.delenv("AIMARKET_VERIFY_MAX_WAIT_S", raising=False)
    assert vs_mod.max_wait_s() == 86_400.0
    monkeypatch.setenv("AIMARKET_VERIFY_MAX_WAIT_S", "0")
    assert vs_mod.max_wait_s() == 0.0
