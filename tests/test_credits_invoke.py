"""The credits rail as seen over HTTP — a fresh hub that can actually be paid.

The behaviour under test is the difference between a node with a P&L and a node without
one. Before the rail existed, `price = 0.0 if (sandbox_mode or not crypto_on)` meant every
default deployment served its whole priced catalogue for free, and the only alternative was
six configuration interlocks plus an escrow contract the operator had to deploy themselves.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer

ADMIN_TOKEN = "test-admin-token-not-for-production"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@contextmanager
def _hub(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", ADMIN_TOKEN)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / f"hub-{len(list(tmp_path.iterdir()))}"
    root.mkdir(parents=True, exist_ok=True)
    config = HubConfig()
    config.db_path = str(root / "hub.db")
    config.signing_key_path = str(root / "key")
    db = HubDatabase(root / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(root / "key"))
    with TestClient(app) as client:
        yield client, db


def _list_priced_capability(db: HubDatabase, price: float = 0.004) -> None:
    """A local capability with a static pack, so the invoke needs no provider.

    A `prompt_template` holding a JSON object is the hub's zero-dependency execution path
    (`fulfillment.has_execution_path`) — and the only way a fresh deployment has anything
    of its own to sell at all.
    """
    db.upsert_capability(Capability(
        capability_id="demo.echo@v1",
        product_id="demo-echo",
        name="Echo",
        description="Returns what it is given",
        price_per_call_usd=price,
        source_hub="local",
        invoke_url="",
        prompt_template='{"answer": "echo", "ok": true}',
    ))


class TestPricedInvokeWithoutAChain:
    def test_priced_capability_is_free_when_no_rail_is_on(self, monkeypatch, tmp_path):
        """The shipped default, pinned so the change in behaviour is deliberate and visible."""
        with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="0") as (client, db):
            _list_priced_capability(db)
            r = client.post("/ai-market/v2/invoke", json={
                "product_id": "demo-echo", "capability_id": "demo.echo@v1",
                "input": {"text": "hi"}, "source_hub": "local",
            })
            assert r.status_code != 402

    def test_priced_capability_demands_payment_once_credits_are_on(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="1") as (client, db):
            _list_priced_capability(db)
            r = client.post("/ai-market/v2/invoke", json={
                "product_id": "demo-echo", "capability_id": "demo.echo@v1",
                "input": {"text": "hi"}, "source_hub": "local",
            })
            assert r.status_code == 402
            body = r.json()
            # A 402 must name a rail the caller can actually reach — the old text named
            # only X-Payment-Channel, which on a chainless hub is a dead end.
            assert "X-API-Key" in body["detail"]
            assert any(w["rail"] == "credits" for w in body["payment_ways"])

    def test_an_unknown_key_is_refused_rather_than_served_free(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="1") as (client, db):
            _list_priced_capability(db)
            r = client.post(
                "/ai-market/v2/invoke",
                json={"product_id": "demo-echo", "capability_id": "demo.echo@v1",
                      "input": {"text": "hi"}, "source_hub": "local"},
                headers={"X-API-Key": "aimk_wrong"},
            )
            assert r.status_code == 401
            assert r.json()["error"] == "invalid_api_key"

    def test_signup_then_paid_invoke_moves_money(self, monkeypatch, tmp_path):
        with _hub(
            monkeypatch, tmp_path,
            AIMARKET_CREDITS_ENABLED="1", AIMARKET_CREDITS_FREE_GRANT_USD="0.05",
        ) as (client, db):
            _list_priced_capability(db, price=0.004)

            signup = client.post("/ai-market/v2/accounts", json={"label": "buyer"})
            assert signup.status_code == 200
            key = signup.json()["api_key"]
            assert signup.json()["balance_usd"] == pytest.approx(0.05)

            r = client.post(
                "/ai-market/v2/invoke",
                json={"product_id": "demo-echo", "capability_id": "demo.echo@v1",
                      "input": {"text": "hi"}, "source_hub": "local"},
                headers={"X-API-Key": key},
            )
            assert r.status_code == 200, r.text

            account = client.get("/ai-market/v2/account", headers={"X-API-Key": key}).json()
            assert account["spent_usd"] == pytest.approx(0.004)
            assert account["balance_usd"] == pytest.approx(0.046)
            assert account["held_usd"] == pytest.approx(0.0)

    def test_an_empty_balance_cannot_buy(self, monkeypatch, tmp_path):
        with _hub(
            monkeypatch, tmp_path,
            AIMARKET_CREDITS_ENABLED="1", AIMARKET_CREDITS_FREE_GRANT_USD="0",
        ) as (client, db):
            _list_priced_capability(db, price=0.004)
            key = client.post("/ai-market/v2/accounts", json={}).json()["api_key"]
            r = client.post(
                "/ai-market/v2/invoke",
                json={"product_id": "demo-echo", "capability_id": "demo.echo@v1",
                      "input": {"text": "hi"}, "source_hub": "local"},
                headers={"X-API-Key": key},
            )
            assert r.status_code == 402
            assert "insufficient credit" in r.json()["detail"]

    def test_operator_top_up_is_admin_only(self, monkeypatch, tmp_path):
        with _hub(
            monkeypatch, tmp_path,
            AIMARKET_CREDITS_ENABLED="1", AIMARKET_CREDITS_FREE_GRANT_USD="0",
        ) as (client, db):
            signup = client.post("/ai-market/v2/accounts", json={}).json()
            acct, key = signup["account_id"], signup["api_key"]

            assert client.post(
                f"/ai-market/v2/accounts/{acct}/credit", json={"amount_usd": 5},
            ).status_code in (401, 403)

            ok = client.post(
                f"/ai-market/v2/accounts/{acct}/credit",
                json={"amount_usd": 5, "note": "invoice 1", "reference": "invoice-1"}, headers=ADMIN_HEADERS,
            )
            assert ok.status_code == 200
            assert ok.json()["balance_usd"] == pytest.approx(5.0)

            replay = client.post(
                f"/ai-market/v2/accounts/{acct}/credit",
                json={"amount_usd": 5, "reference": "invoice-1"}, headers=ADMIN_HEADERS,
            )
            assert replay.status_code == 200
            assert replay.json()["idempotent_replay"] is True
            assert replay.json()["balance_usd"] == pytest.approx(5.0)

            entries = client.get(
                "/ai-market/v2/account/ledger", headers={"X-API-Key": key},
            ).json()["entries"]
            assert any(e["kind"] == "grant" and e["note"] == "invoice 1" for e in entries)


class TestOperatorVisibility:
    def test_the_manifest_advertises_a_reachable_rail(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="1") as (client, _db):
            rails = client.get("/.well-known/ai-market.json").json()["payment_rails"]
            assert rails["credits"]["enabled"] is True
            assert rails["credits"]["header"] == "X-API-Key"
            # Sub-cent pricing is the point: the channel ledger cannot express it.
            assert rails["credits"]["min_unit_usd"] < 0.01

    def test_live_stats_separate_earnings_from_money_held(self, monkeypatch, tmp_path):
        with _hub(
            monkeypatch, tmp_path,
            AIMARKET_CREDITS_ENABLED="1", AIMARKET_CREDITS_FREE_GRANT_USD="0",
        ) as (client, db):
            _list_priced_capability(db, price=0.004)
            signup = client.post("/ai-market/v2/accounts", json={}).json()
            client.post(
                f"/ai-market/v2/accounts/{signup['account_id']}/credit",
                json={"amount_usd": 1.0}, headers=ADMIN_HEADERS,
            )
            client.post(
                "/ai-market/v2/invoke",
                json={"product_id": "demo-echo", "capability_id": "demo.echo@v1",
                      "input": {"text": "hi"}, "source_hub": "local"},
                headers={"X-API-Key": signup["api_key"]},
            )
            summary = client.get("/ai-market/v2/stats/live").json()["summary"]
            assert summary["credits"]["credits_earned_usd"] == pytest.approx(0.004)
            assert summary["credits"]["outstanding_credit_usd"] == pytest.approx(0.996)


class TestSignupDoor:
    def test_closed_signup_refuses_strangers_but_not_the_operator(self, monkeypatch, tmp_path):
        with _hub(
            monkeypatch, tmp_path,
            AIMARKET_CREDITS_ENABLED="1", AIMARKET_CREDITS_OPEN_SIGNUP="0",
        ) as (client, _db):
            assert client.post("/ai-market/v2/accounts", json={}).status_code == 403
            r = client.post(
                "/ai-market/v2/accounts", json={"grant_usd": 2.5}, headers=ADMIN_HEADERS,
            )
            assert r.status_code == 200
            assert r.json()["balance_usd"] == pytest.approx(2.5)

    def test_open_signup_is_rate_limited_per_address(self, monkeypatch, tmp_path):
        with _hub(
            monkeypatch, tmp_path,
            AIMARKET_CREDITS_ENABLED="1", AIMARKET_CREDITS_SIGNUPS_PER_HOUR="2",
        ) as (client, _db):
            assert client.post("/ai-market/v2/accounts", json={}).status_code == 200
            assert client.post("/ai-market/v2/accounts", json={}).status_code == 200
            assert client.post("/ai-market/v2/accounts", json={}).status_code == 429

    def test_accounts_are_off_when_the_rail_is_off(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path, AIMARKET_CREDITS_ENABLED="0") as (client, _db):
            assert client.post("/ai-market/v2/accounts", json={}).status_code == 503


def test_open_signup_grants_until_the_budget_is_spent(monkeypatch):
    """The door stays open at zero once the day's grant budget is gone."""
    import os
    import tempfile

    from fastapi.testclient import TestClient

    from aimarket_hub.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("AIMARKET_DB_PATH", os.path.join(tmp, "hub.db"))
        monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
        monkeypatch.setenv("AIMARKET_CREDITS_OPEN_SIGNUP", "1")
        monkeypatch.setenv("AIMARKET_SIGNUP_GRANT_USD", "0.05")
        monkeypatch.setenv("AIMARKET_SIGNUP_GRANT_DAILY_USD", "0.05")
        monkeypatch.setenv("AIMARKET_CREDITS_SIGNUPS_PER_HOUR", "50")
        with TestClient(create_app()) as client:
            first = client.post("/ai-market/v2/accounts", json={"label": "first"})
            assert first.status_code == 200
            assert first.json()["balance_usd"] == pytest.approx(0.05)
            assert "grant_note" not in first.json()

            second = client.post("/ai-market/v2/accounts", json={"label": "second"})
            assert second.status_code == 200
            body = second.json()
            # Still a usable account — just not a funded one.
            assert body["balance_usd"] == 0.0
            assert body["api_key"]
            assert "daily" in body["grant_note"]

            rails = client.get("/.well-known/ai-market.json").json()["payment_rails"]
            assert rails["credits"]["free_grant_usd"] == pytest.approx(0.05)


def test_grant_usd_is_refused_rather_than_ignored(monkeypatch):
    """A privileged field must never be silently dropped.

    This is the bug that hid a wrong admin token for a whole build: a caller
    asked for a zero-balance account, got the signup grant instead, and found
    out it was not admin only when it later tried to credit a paid buyer.
    """
    import os
    import tempfile

    from fastapi.testclient import TestClient

    from aimarket_hub.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("AIMARKET_DB_PATH", os.path.join(tmp, "hub.db"))
        monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
        monkeypatch.setenv("AIMARKET_CREDITS_OPEN_SIGNUP", "1")
        monkeypatch.setenv("AIMARKET_SIGNUP_GRANT_USD", "0.05")
        monkeypatch.setenv("AIMARKET_SIGNUP_GRANT_DAILY_USD", "10")
        monkeypatch.setenv("AIMARKET_CREDITS_SIGNUPS_PER_HOUR", "50")
        monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", "a" * 40)
        with TestClient(create_app()) as client:
            refused = client.post("/ai-market/v2/accounts", json={"label": "x", "grant_usd": 0})
            assert refused.status_code == 403
            assert "operator-only" in refused.json()["detail"]

            # A wrong bearer is no better than none.
            wrong = client.post(
                "/ai-market/v2/accounts",
                json={"label": "x", "grant_usd": 0},
                headers={"Authorization": "Bearer " + "b" * 40},
            )
            assert wrong.status_code == 403

            # Asking for nothing still works and still gets the advertised grant.
            plain = client.post("/ai-market/v2/accounts", json={"label": "x"})
            assert plain.status_code == 200
            assert plain.json()["balance_usd"] == pytest.approx(0.05)

            # The operator keeps full control of the opening balance.
            admin = client.post(
                "/ai-market/v2/accounts",
                json={"label": "enterprise", "grant_usd": 25},
                headers={"Authorization": "Bearer " + "a" * 40},
            )
            assert admin.status_code == 200
            assert admin.json()["balance_usd"] == pytest.approx(25.0)


def test_only_admin_may_move_money_into_an_account(monkeypatch):
    """/credit is the boundary the wrong token finally hit. Pin it."""
    import os
    import tempfile

    from fastapi.testclient import TestClient

    from aimarket_hub.api import create_app

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("AIMARKET_DB_PATH", os.path.join(tmp, "hub.db"))
        monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
        monkeypatch.setenv("AIMARKET_CREDITS_OPEN_SIGNUP", "1")
        monkeypatch.setenv("AIMARKET_CREDITS_SIGNUPS_PER_HOUR", "50")
        monkeypatch.setenv("AIMARKET_SIGNUP_GRANT_USD", "0")
        monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", "a" * 40)
        with TestClient(create_app()) as client:
            account = client.post("/ai-market/v2/accounts", json={"label": "buyer"}).json()
            assert account["balance_usd"] == 0.0
            body = {"amount_usd": 5, "reference": "probe-1"}

            assert client.post(f"/ai-market/v2/accounts/{account['account_id']}/credit",
                               json=body).status_code in (401, 403)
            assert client.post(f"/ai-market/v2/accounts/{account['account_id']}/credit", json=body,
                               headers={"Authorization": "Bearer " + "b" * 40}).status_code == 403

            credited = client.post(
                f"/ai-market/v2/accounts/{account['account_id']}/credit", json=body,
                headers={"Authorization": "Bearer " + "a" * 40})
            assert credited.status_code == 200
            assert credited.json()["balance_usd"] == pytest.approx(5.0)
