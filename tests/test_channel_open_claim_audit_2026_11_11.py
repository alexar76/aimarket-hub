"""Audit 2026-11-11, channels-4: a transfer-funded open that failed after the shared deposit claim
kept the claim, and every retry of that deposit was refused as "already used" with no channel and
no obligation behind it.

A failed channel write now hands the claim back. An open killed between the claim and its commit
leaves a committed intent row, by which the depositor's retry recognises the claim as this
ledger's own and takes it back.
"""
from __future__ import annotations

import pytest

from aimarket_hub import channels
from tests.test_channels import _PROOF, _prod_mode


@pytest.fixture(autouse=True)
def _crypto_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_DEPOSIT_CLAIMS_DIR", str(tmp_path / "claims"))


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    _prod_mode(monkeypatch, channels)
    led = channels.ChannelLedger(db_path=str(tmp_path / "open.db"))
    yield led
    led.stop_sweep()


def _boom(*a, **kw):
    raise OSError("disk I/O error")


def test_a_failed_channel_write_hands_the_claim_back(ledger, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(ledger, "_record_proven_payer", _boom)
        with pytest.raises(OSError):
            ledger.open(5.0, tx_hash="0x" + "d1" * 32, **_PROOF)
    retry = ledger.open(5.0, tx_hash="0x" + "d1" * 32, **_PROOF)
    assert "channel" in retry, retry
    replay = ledger.open(5.0, tx_hash="0x" + "d1" * 32, **_PROOF)
    assert "already used" in replay["error"]


def _strand_a_claim(ledger, monkeypatch, tx):
    """The process dies after the claim file is written and before the commit."""
    with monkeypatch.context() as m:
        m.setattr(ledger, "_record_proven_payer", _boom)
        m.setattr(channels, "_release_deposit_shared", lambda **kw: None)   # never got there
        with pytest.raises(OSError):
            ledger.open(5.0, tx_hash=tx, **_PROOF)


def test_a_claim_stranded_by_a_dead_open_is_taken_back_by_the_retry(ledger, monkeypatch):
    tx = "0x" + "d2" * 32
    _strand_a_claim(ledger, monkeypatch, tx)
    # Within the grace another worker may still be committing it: refused, as before.
    assert "already used" in ledger.open(5.0, tx_hash=tx, **_PROOF)["error"]
    with ledger._get_conn() as conn:
        conn.execute("UPDATE channel_deposit_intents SET created_at = created_at - 600")
        conn.commit()
    retry = ledger.open(5.0, tx_hash=tx, **_PROOF)
    assert "channel" in retry, retry
    assert "already used" in ledger.open(5.0, tx_hash=tx, **_PROOF)["error"]


def test_another_door_s_claim_is_never_taken_back(ledger, monkeypatch):
    tx = "0x" + "d3" * 32
    held = channels.claim_deposit_as("aimarket-hub-topup", chain="base", tx_hash=tx,
                                     claim_id="topup-1", amount_cents=500)
    assert held["ok"]
    for _ in range(2):
        assert "already used" in ledger.open(5.0, tx_hash=tx, **_PROOF)["error"]


def test_a_same_stack_claim_without_this_ledger_s_intent_is_kept(ledger, tmp_path, monkeypatch):
    """Another hub sharing the claims directory under the same stack name: not ours to free."""
    tx = "0x" + "d4" * 32
    held = channels.claim_deposit_as(channels._hub_channel_stack(), chain="base", tx_hash=tx,
                                     claim_id="ch_otherhub0000000", amount_cents=500)
    assert held["ok"]
    assert "already used" in ledger.open(5.0, tx_hash=tx, **_PROOF)["error"]


def test_an_overpaid_channel_deposit_is_credited_whole(ledger, monkeypatch):
    """deposits-x402-5: the verifier accepts paid >= asked, and only the asked amount was
    credited; the excess sat in the settlement wallet with no row anywhere."""
    from tests.test_channels import _EVM_PAYER

    monkeypatch.setattr(channels, "_verify_tx_onchain", lambda **kw: {
        "ok": True, "sender": _EVM_PAYER, "paid_units": 7_000_000, "expected_units": 5_000_000, "decimals": 6})
    opened = ledger.open(5.0, tx_hash="0x" + "d5" * 32, **_PROOF)["channel"]
    assert opened["balance_usd"] == 7.0 and opened["original_deposit_usd"] == 7.0
    s = ledger.close(opened["channel_id"], wallet=_EVM_PAYER, secret=opened["channel_secret"])["settlement"]
    assert s["refund_owed_usd"] == 7.0


def test_a_channel_deposit_above_the_cap_is_credited_to_the_cap(ledger, monkeypatch):
    from tests.test_channels import _EVM_PAYER

    monkeypatch.setattr(channels, "_verify_tx_onchain", lambda **kw: {
        "ok": True, "sender": _EVM_PAYER, "paid_units": 20_000_000_000, "expected_units": 5_000_000_000, "decimals": 6})
    opened = ledger.open(5_000.0, tx_hash="0x" + "d6" * 32, **_PROOF)["channel"]
    assert opened["balance_usd"] == 10_000.0


def test_a_sub_cent_ask_cannot_scale_the_credit_past_what_arrived(ledger, monkeypatch):
    """Review of 2026-11-11: the credit scaled the ROUNDED asked cents by paid/asked, so
    0.00500001 USD asked (1 cent) with 5,000 USDC paid credited $10,000. The credit is what
    arrived, in whole cents, and never more."""
    from tests.test_channels import _EVM_PAYER

    monkeypatch.setattr(channels, "_verify_tx_onchain", lambda **kw: {
        "ok": True, "sender": _EVM_PAYER, "paid_units": 5_000_000_000, "expected_units": 5_000, "decimals": 6})
    opened = ledger.open(0.00500001, tx_hash="0x" + "d7" * 32, **_PROOF)["channel"]
    assert opened["balance_usd"] == 5_000.0
    monkeypatch.setattr(channels, "_verify_tx_onchain", lambda **kw: {
        "ok": True, "sender": _EVM_PAYER, "paid_units": 1_500_000_000, "expected_units": 15_100, "decimals": 6})
    opened = ledger.open(0.0151, tx_hash="0x" + "d8" * 32, **_PROOF)["channel"]
    assert opened["balance_usd"] == 1_500.0


def test_without_decimals_nothing_beyond_the_ask_is_credited(ledger, monkeypatch):
    from tests.test_channels import _EVM_PAYER

    monkeypatch.setattr(channels, "_verify_tx_onchain", lambda **kw: {
        "ok": True, "sender": _EVM_PAYER, "paid_units": 9_000_000, "expected_units": 5_000_000})
    assert ledger.open(5.0, tx_hash="0x" + "d9" * 32, **_PROOF)["channel"]["balance_usd"] == 5.0
