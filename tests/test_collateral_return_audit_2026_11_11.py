"""Audit 2026-11-11, credits-2: a slash burned stake only in supply_stakes, and the operator's
collateral-return route capped returns by collateral_mc — so it handed the slashed money back."""
from __future__ import annotations

from aimarket_hub import credits
from tests._mandate_kit import ADMIN_TOKEN, funded_account, hub


def _staked(client, db, amount):
    account, _key = funded_account(client, db, amount)
    ledger = credits.CreditsLedger(db._conn)
    assert ledger.debit(account, amount, note="supply stake", as_collateral=True)["debited_usd"] == amount
    db.supply_stake_add(account, amount, f"credits:{account}:x")
    return account, ledger


def _return(client, account, amount):
    return client.post(f"/ai-market/v2/accounts/{account}/collateral/return",
                       headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, json={"amount_usd": amount})


def test_a_slashed_stake_cannot_be_returned(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path) as (client, db):
        account, ledger = _staked(client, db, 25.0)
        db.supply_stake_slash(account, 25.0)
        assert _return(client, account, 25.0).status_code == 400
        assert ledger.account(account)["balance_usd"] == 0.0


def test_only_the_unslashed_part_comes_back(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path) as (client, db):
        account, ledger = _staked(client, db, 25.0)
        db.supply_stake_slash(account, 10.0)
        assert _return(client, account, 20.0).status_code == 400
        assert _return(client, account, 15.0).status_code == 200
        assert ledger.account(account)["balance_usd"] == 15.0
