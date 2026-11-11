"""Audit 2026-11-11, channels-5: the per-wallet open/close buckets were charged before any proof.

The wallet is caller-supplied and public, so 60 junk closes (or 20 junk opens) naming a victim's
address locked the victim's real close (or open) for an hour. The buckets are now looked at first
and charged only once the caller has shown it owns the wallet.
"""
from __future__ import annotations

import time

import pytest

from aimarket_hub import channels

VICTIM = "0xVictimWallet"


@pytest.fixture(autouse=True)
def _crypto_enabled(monkeypatch):
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")


@pytest.fixture
def ledger(tmp_path):
    led = channels.ChannelLedger(db_path=str(tmp_path / "rate.db"))
    led._MAX_CHANNELS = 10_000
    yield led
    led.stop_sweep()


def test_junk_closes_naming_a_wallet_do_not_lock_its_owner_out(ledger):
    opened = ledger.open(1.0, wallet=VICTIM)["channel"]
    for _ in range(channels._MAX_CLOSES_PER_WALLET_PER_HOUR + 5):
        junk = ledger.close("xxxxxxxx", wallet=VICTIM)          # no such channel, no secret
        assert junk["error"] == "channel not found"
        wrong = ledger.close(opened["channel_id"], wallet=VICTIM, secret="guess")
        assert wrong["error"].startswith("unauthorized")
    real = ledger.close(opened["channel_id"], wallet=VICTIM, secret=opened["channel_secret"])
    assert "settlement" in real, real


def test_refused_opens_naming_a_wallet_do_not_spend_its_bucket(ledger):
    for _ in range(channels._MAX_OPENS_PER_WALLET_PER_HOUR + 5):
        refused = ledger.open(-1.0, wallet=VICTIM)
        assert "error" in refused and "rate limit" not in refused["error"]
        refused = ledger.open(1.0, wallet=VICTIM, escrow_channel_id="0x" + "ab" * 32)
        assert "error" in refused and "rate limit" not in refused["error"]
    assert "channel" in ledger.open(1.0, wallet=VICTIM)


def test_the_owner_s_own_opens_and_closes_still_count(ledger):
    made = [ledger.open(0.10, wallet=VICTIM)["channel"]
            for _ in range(channels._MAX_OPENS_PER_WALLET_PER_HOUR)]
    assert "rate limit" in ledger.open(0.10, wallet=VICTIM)["error"]
    ledger._close_rate[VICTIM] = [time.time()] * (channels._MAX_CLOSES_PER_WALLET_PER_HOUR - 1)
    assert "settlement" in ledger.close(made[0]["channel_id"], wallet=VICTIM, secret=made[0]["channel_secret"])
    assert "rate limit" in ledger.close(made[1]["channel_id"], wallet=VICTIM,
                                        secret=made[1]["channel_secret"])["error"]


def test_an_escrow_channel_close_says_the_contract_returns_the_remainder(tmp_path, monkeypatch):
    """cross-cutting-7: close on an escrow channel sends nothing on-chain and the operator owes
    nothing; the answer used to call the remainder 'owed' and the spec/SDKs called it a refund."""
    led = channels.ChannelLedger(db_path=str(tmp_path / "escrow.db"))
    monkeypatch.setattr(led, "_verify_escrow_funding",
                        lambda **kw: (True, "", "escrow:" + kw["escrow_channel_id"]))
    escrow = "0x" + "e1" * 32
    try:
        ch = led.open(2.0, wallet=VICTIM, escrow_channel_id=escrow)["channel"]
        s = led.close(ch["channel_id"], wallet=VICTIM, secret=ch["channel_secret"])["settlement"]
        assert s["refund_usd"] == 2.0 and s["refund_executed_usd"] == 0.0
        assert s["refund_owed_usd"] == 0.0 and s["obligation"] is None
        assert s["refund_status"] == "on_chain" and s["escrow_channel_id"] == escrow
        assert "requestClose" in s["refund_how"] and "settleChannel" in s["refund_how"]
    finally:
        led.stop_sweep()


def test_a_rate_charge_that_fails_after_the_commit_does_not_fail_the_open(ledger, monkeypatch):
    """Review of 2026-11-11: the open's bucket is charged after the channel is committed, from a
    worker thread; a failure there must not turn a funded open into a 500."""
    def boom(*a, **kw):
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(ledger, "_charge_rate", boom)
    assert "channel" in ledger.open(1.0, wallet=VICTIM)


def test_opens_from_many_threads_keep_the_bucket_table_consistent(ledger, monkeypatch):
    import threading

    monkeypatch.setattr(channels, "_MAX_RATE_TRACKED_KEYS", 8)
    errors: list[BaseException] = []

    def worker(n):
        try:
            for i in range(10):
                assert "channel" in ledger.open(0.10, wallet=f"0xW{n}-{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(ledger._rate_state) <= 8
