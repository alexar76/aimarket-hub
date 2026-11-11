"""Audit 2026-11-11: two appeal bonds the hub held but should not have.

mandates-a2a-3 — a publisher that turned on require_mandate still had a bond held on its balance,
and forfeited, by its bare API key. pay-on-verified-6 — a buyer's bond on an escrow-funded channel
was booked in the ledger but nothing signed it, so a forfeited bond was never collected on chain.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aimarket_hub import credits
from tests.test_verified_appeals import (
    _Court,
    _appeal,
    _build,
    _holds,
    _lookup,
    _provisional,
    _seller_account,
)


def test_a_require_mandate_publisher_cannot_bond_an_appeal_with_its_key(tmp_path, monkeypatch):
    h = _build(tmp_path, monkeypatch, _Court([("judged", False, 0.1)]),
               _Court([("judged", True, 0.9)]), credits_on=True)
    with TestClient(h.app) as client:
        _, _, nonce, _ = _provisional(h, client, first_passes=False)
        key = _seller_account(client, h)
        publisher = credits.ledger().resolve(key)
        h.db._conn.execute("INSERT INTO mandate_owners (did, account_id, require_mandate) VALUES (?, ?, 1)",
                           ("did:key:z6Mkowner", publisher))
        h.db._conn.commit()
        r = _appeal(client, nonce, api_key=key)
        assert r.status_code == 403 and r.json()["error"] == "mandate_required", r.text
        account = client.get("/ai-market/v2/account", headers={"X-API-Key": key}).json()
        assert account["held_usd"] == 0.0 and account["balance_usd"] == 1.0
        assert _lookup(client, nonce)["verification"]["status"] == "provisional"
        assert h.court.prompts == []


def test_a_buyer_cannot_bond_an_appeal_on_an_escrow_channel(tmp_path, monkeypatch):
    h = _build(tmp_path, monkeypatch, _Court([("judged", True, 0.95)]), _Court([("judged", False, 0.1)]))
    with TestClient(h.app) as client:
        channel_id, secret, nonce, _ = _provisional(h, client, first_passes=True)
        from aimarket_hub.settlement_service import appeals

        monkeypatch.setattr(appeals, "channel_escrow_binding",
                            lambda cid: "0x" + "e1" * 32 if cid == channel_id else "")
        r = _appeal(client, nonce, secret=secret)
        assert r.status_code == 409 and r.json()["error"] == "buyer_appeal_unavailable", r.text
        assert f"appeal_{nonce}" not in _holds(nonce)[0]
        assert _lookup(client, nonce)["verification"]["status"] == "provisional"
        assert h.court.prompts == []
