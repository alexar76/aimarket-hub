"""Supply stake hardening in production mode."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.supply_security import SupplySecurity


@pytest.fixture
def sec(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMARKET_SUPPLY_SECURITY_RELAXED", "0")
    db = HubDatabase(db_path=str(tmp_path / "hub.db"))
    return SupplySecurity(db, HubConfig(), signer=MagicMock(), slash_registry=None)


def test_stake_requires_tx_hash_in_prod(sec, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    sec.policy.min_stake_usd = 10.0
    with pytest.raises(ValueError, match="tx_hash required"):
        sec.stake("publisher-a", 25.0, tx_hash="")


def test_stake_accepts_verified_tx_hash_in_prod(sec, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    sec.policy.min_stake_usd = 10.0
    # On-chain verification is now required in prod — mock a verified deposit.
    monkeypatch.setattr("aimarket_hub.supply_security._verify_stake_tx", lambda tx, amt: True)
    out = sec.stake("publisher-a", 25.0, tx_hash="0xabc123")
    assert out["stake_usd"] >= 25.0


def test_stake_rejects_unverified_tx_in_prod(sec, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    sec.policy.min_stake_usd = 10.0
    monkeypatch.setattr("aimarket_hub.supply_security._verify_stake_tx", lambda tx, amt: False)
    with pytest.raises(ValueError, match="not verified on-chain"):
        sec.stake("publisher-a", 25.0, tx_hash="0xfabricated")


def test_stake_rejects_replayed_tx_in_prod(sec, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    sec.policy.min_stake_usd = 10.0
    monkeypatch.setattr("aimarket_hub.supply_security._verify_stake_tx", lambda tx, amt: True)
    sec.stake("publisher-a", 25.0, tx_hash="0xdeadbeef")
    # The same on-chain deposit cannot be claimed again (by anyone).
    with pytest.raises(ValueError, match="already recorded"):
        sec.stake("publisher-b", 25.0, tx_hash="0xdeadbeef")
