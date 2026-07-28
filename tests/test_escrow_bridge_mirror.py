"""C3 — the mirror and its signing strategies.

The single most important assertion in this file is negative: under the shipped defaults,
NOTHING reaches a broadcast. It is enforced with a signer whose submit() fails the test if
it is ever called, so a future refactor that quietly makes the default capable of sending
breaks here rather than on someone's mainnet.

Everything else tests the three guards in front of a submission — queue order, the ledger's
own record, and the contract's simulated verdict — because each one exists to refuse a
specific way of collecting money the hub is not owed.
"""

from __future__ import annotations

import sqlite3

import pytest

from aimarket_hub.escrow_bridge import chain, config, mirror, signer as signer_mod, store
from aimarket_hub.escrow_bridge.eip712 import crypto_available
from aimarket_hub.escrow_bridge.errors import BridgeDisabled, SubmissionRefused

pytestmark = pytest.mark.skipif(
    not crypto_available(), reason="eth-utils/eth-account not installed (platon venv)"
)

_ESCROW = "0xe7f1725e7734ce288f8367e1bb143e90bb3f0512"
_HUB = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
_TOKEN = "0x5fbdb2315678afecb367f032d93f642f64180aa3"
_CHANNEL = "0x" + "11" * 32
_RECEIPT = "0x" + "22" * 32
_SIG = "0x" + "ab" * 65
_NOW = 1_000_000


class _ExplodingSigner(signer_mod.Signer):
    """A signer that must never be reached. Named so the failure reads clearly."""

    name = "exploding"

    def submit(self, tx):
        raise AssertionError(
            "the mirror attempted to BROADCAST a transaction under a configuration that "
            "must never broadcast"
        )


class _RecordingSigner(signer_mod.Signer):
    name = "recording"

    def __init__(self, tx_hash="0x" + "ee" * 32, fail=None):
        self.sent: list[signer_mod.UnsignedTx] = []
        self._hash = tx_hash
        self._fail = fail

    @property
    def sender(self):
        return _HUB

    def submit(self, tx):
        if self._fail:
            raise SubmissionRefused(self._fail)
        self.sent.append(tx)
        return self._hash


@pytest.fixture
def st(tmp_path):
    s = store.AuthorizationStore(str(tmp_path / "bridge.db"))
    yield s
    s.close()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A stand-in channel ledger DB holding the debit the hub actually recorded."""
    path = tmp_path / "channels.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE debited_receipts (receipt_id TEXT PRIMARY KEY, channel_id TEXT, "
        "amount_cents INTEGER, timestamp TEXT)"
    )
    conn.commit()
    conn.close()
    from aimarket_hub import channels as ch_mod

    monkeypatch.setattr(ch_mod, "_DB_PATH", str(path))

    def _debit(receipt_id=_RECEIPT, cents=100):
        c = sqlite3.connect(path)
        c.execute(
            "INSERT OR REPLACE INTO debited_receipts VALUES (?, ?, ?, '')",
            (receipt_id, "ch_abc", cents),
        )
        c.commit()
        c.close()

    return _debit


@pytest.fixture
def sim(monkeypatch):
    """Control what the simulated contract says."""
    state = {"ok": True, "error": "", "gas": 60_000}

    def _simulate(*, to, data, sender):
        return dict(state)

    monkeypatch.setattr(chain, "simulate", _simulate)
    return state


def _row(st, **over):
    args = dict(
        receipt_id=_RECEIPT, ledger_channel="ch_abc", escrow_channel=_CHANNEL,
        chain_id=8453, escrow_address=_ESCROW, hub=_HUB, token=_TOKEN,
        depositor="0x" + "aa" * 20, amount_units=1_000_000, nonce=0,
        deadline=_NOW + 3600, signature=_SIG,
    )
    args.update(over)
    return st.record(**args)


def _mirror(st, signer, **kw):
    return mirror.Mirror(authorizations=st, signer=signer, require_enabled=False, **kw)


class TestTheDefaultCannotBroadcast:
    def test_plan_only_is_the_default_signer(self, monkeypatch):
        for var in ("AIMARKET_ESCROW_SUBMIT_STRATEGY", "AIMARKET_ESCROW_SUBMIT_CONFIRM",
                    "AIMARKET_ESCROW_PRIVATE_KEY", "AIMARKET_ESCROW_SIGNER_URL"):
            monkeypatch.delenv(var, raising=False)
        assert isinstance(signer_mod.build_signer(), signer_mod.PlanOnlySigner)

    def test_a_full_pass_under_defaults_never_reaches_a_signer(self, st, ledger, sim):
        """The load-bearing negative test of the whole bridge."""
        ledger(cents=100)
        _row(st)
        report = _mirror(st, _ExplodingSigner.__new__(_ExplodingSigner)).run(now=_NOW)
        # The exploding signer would have raised; getting here means submit() was not called.
        assert report.outcomes.get(mirror.OUTCOME_SUBMITTED, 0) == 0

    def test_plan_mode_still_does_the_useful_work(self, st, ledger, sim):
        """Plan mode proves the authorization WOULD be accepted, and says the gas cost."""
        ledger(cents=100)
        _row(st)
        report = _mirror(st, signer_mod.PlanOnlySigner()).run(now=_NOW)
        assert report.dry_run is True
        assert report.outcomes == {mirror.OUTCOME_PLANNED: 1}
        assert report.rows[0]["gas"] == 60_000
        assert st.get(_RECEIPT).status == store.PLANNED

    def test_an_under_configured_strategy_degrades_to_plan(self, monkeypatch):
        """Naming a strategy is not enough; the confirmation phrase is a second act."""
        monkeypatch.setenv("AIMARKET_ESCROW_SUBMIT_STRATEGY", "external")
        monkeypatch.setenv("AIMARKET_ESCROW_SIGNER_URL", "https://signer.invalid")
        monkeypatch.delenv("AIMARKET_ESCROW_SUBMIT_CONFIRM", raising=False)
        assert isinstance(signer_mod.build_signer(), signer_mod.PlanOnlySigner)

    def test_a_fully_configured_strategy_is_honoured(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_ESCROW_SUBMIT_STRATEGY", "external")
        monkeypatch.setenv("AIMARKET_ESCROW_SIGNER_URL", "https://signer.invalid")
        monkeypatch.setenv("AIMARKET_ESCROW_SUBMIT_CONFIRM", config.SUBMIT_CONFIRM_PHRASE)
        assert isinstance(signer_mod.build_signer(), signer_mod.ExternalSigner)

    def test_the_mirror_refuses_to_run_when_the_bridge_is_off(self, monkeypatch, st):
        monkeypatch.delenv("AIMARKET_ESCROW_BRIDGE_ENABLED", raising=False)
        with pytest.raises(BridgeDisabled):
            mirror.Mirror(authorizations=st, signer=signer_mod.PlanOnlySigner())


class TestNonceOrder:
    def test_a_later_nonce_waits_behind_an_earlier_one(self, st, ledger, sim):
        """The contract only accepts the current nonce, so skipping would strand a row."""
        ledger(receipt_id="0x" + "01" * 32, cents=100)
        ledger(receipt_id="0x" + "02" * 32, cents=100)
        _row(st, receipt_id="0x" + "01" * 32, nonce=0)
        _row(st, receipt_id="0x" + "02" * 32, nonce=1)
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        by_receipt = {r["receipt_id"]: r for r in report.rows}
        assert by_receipt["0x" + "01" * 32]["outcome"] == mirror.OUTCOME_SUBMITTED
        assert by_receipt["0x" + "02" * 32]["outcome"] == mirror.OUTCOME_BLOCKED
        assert "waiting behind nonce 0" in by_receipt["0x" + "02" * 32]["detail"]

    def test_the_queue_advances_once_the_earlier_row_resolves(self, st, ledger, sim):
        ledger(receipt_id="0x" + "01" * 32, cents=100)
        ledger(receipt_id="0x" + "02" * 32, cents=100)
        first = _row(st, receipt_id="0x" + "01" * 32, nonce=0)
        _row(st, receipt_id="0x" + "02" * 32, nonce=1)
        st.mark_submitted(first.receipt_id, "0xtx")
        st.mark_confirmed(first.receipt_id)
        rec = _RecordingSigner()
        report = _mirror(st, rec).run(now=_NOW)
        assert report.outcomes.get(mirror.OUTCOME_SUBMITTED) == 1
        assert len(rec.sent) == 1

    def test_an_already_submitted_row_is_not_resubmitted(self, st, ledger, sim):
        """Re-broadcasting only spends gas to hit the contract's own replay guard."""
        ledger(cents=100)
        _row(st)
        st.mark_submitted(_RECEIPT, "0xtx")
        rec = _RecordingSigner()
        _mirror(st, rec).run(now=_NOW)
        assert rec.sent == []


class TestTheLedgerGuard:
    def test_a_receipt_the_ledger_never_debited_is_refused(self, st, ledger, sim):
        """The hub must be able to show it charged for what it collects."""
        _row(st)                     # no ledger row for it
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert report.outcomes == {mirror.OUTCOME_BLOCKED: 1}
        assert "no debit for this receipt" in report.rows[0]["detail"]

    def test_collecting_more_than_the_ledger_debited_is_refused(self, st, ledger, sim):
        """Guards against a hub bug or a tampered store over-collecting on chain."""
        ledger(cents=10)             # $0.10 charged off chain
        _row(st, amount_units=1_000_000)   # $1.00 signed
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert report.outcomes == {mirror.OUTCOME_BLOCKED: 1}
        assert "over-collect" in report.rows[0]["detail"]

    def test_collecting_less_than_the_ledger_debited_is_allowed(self, st, ledger, sim):
        """Under-collecting only costs the hub, so it is not the bridge's job to refuse."""
        ledger(cents=100)
        _row(st, amount_units=500_000)
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert report.outcomes.get(mirror.OUTCOME_SUBMITTED) == 1

    def test_an_unreadable_ledger_blocks_rather_than_assumes(self, st, sim, monkeypatch):
        from aimarket_hub import channels as ch_mod

        monkeypatch.setattr(ch_mod, "_DB_PATH", "/nonexistent/channels.db")
        _row(st)
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert report.outcomes == {mirror.OUTCOME_BLOCKED: 1}


class TestSimulationGuard:
    def test_a_reverting_simulation_blocks_and_is_recorded(self, st, ledger, sim):
        ledger(cents=100)
        _row(st)
        sim.update(ok=False, error="execution reverted: ReceiptAlreadyUsed", gas=None)
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert report.outcomes == {mirror.OUTCOME_BLOCKED: 1}
        row = st.get(_RECEIPT)
        assert "ReceiptAlreadyUsed" in row.last_error
        # A revert is usually transient here, so the row keeps its queue position.
        assert row.status == store.PENDING

    def test_a_passed_deadline_is_abandoned_not_retried_forever(self, st, ledger, sim):
        """The signature can never be submitted again, so the debit is uncollectable."""
        ledger(cents=100)
        _row(st, deadline=_NOW - 1)
        report = _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert report.outcomes == {mirror.OUTCOME_REJECTED: 1}
        assert st.get(_RECEIPT).status == store.ABANDONED

    def test_gas_is_padded_above_the_estimate(self, st, ledger, sim):
        """State can shift between estimate and inclusion; a tight limit reverts."""
        ledger(cents=100)
        _row(st)
        rec = _RecordingSigner()
        _mirror(st, rec).run(now=_NOW)
        assert rec.sent[0].gas > 60_000


class TestSubmissionAndConfirmation:
    def test_a_refusal_leaves_the_row_retryable(self, st, ledger, sim):
        ledger(cents=100)
        _row(st)
        report = _mirror(st, _RecordingSigner(fail="signer offline")).run(now=_NOW)
        assert report.outcomes == {mirror.OUTCOME_REFUSED: 1}
        assert st.get(_RECEIPT).status == store.PLANNED   # not terminal, retryable

    def test_confirmation_reads_the_chain_not_the_signer(self, st, ledger, sim, monkeypatch):
        """A signer that claims success proves nothing; a receipt does."""
        ledger(cents=100)
        _row(st)
        _mirror(st, _RecordingSigner()).run(now=_NOW)
        assert st.get(_RECEIPT).status == store.SUBMITTED

        class _Pool:
            def call(self, method, params=None):
                assert method == "eth_getTransactionReceipt"
                return {"status": "0x1"}

        monkeypatch.setattr(chain, "_pool", lambda: _Pool())
        report = _mirror(st, _RecordingSigner()).confirm()
        assert report.outcomes == {mirror.OUTCOME_CONFIRMED: 1}
        assert st.get(_RECEIPT).status == store.CONFIRMED

    def test_an_unmined_transaction_stays_submitted(self, st, ledger, sim, monkeypatch):
        ledger(cents=100)
        _row(st)
        _mirror(st, _RecordingSigner()).run(now=_NOW)

        class _Pool:
            def call(self, method, params=None):
                return None

        monkeypatch.setattr(chain, "_pool", lambda: _Pool())
        report = _mirror(st, _RecordingSigner()).confirm()
        assert report.outcomes == {mirror.OUTCOME_BLOCKED: 1}
        assert st.get(_RECEIPT).status == store.SUBMITTED

    def test_a_reverted_receipt_is_not_terminal(self, st, ledger, sim, monkeypatch):
        """Usually a nonce the chain moved past — re-simulate, do not discard a valid sig."""
        ledger(cents=100)
        _row(st)
        _mirror(st, _RecordingSigner()).run(now=_NOW)

        class _Pool:
            def call(self, method, params=None):
                return {"status": "0x0"}

        monkeypatch.setattr(chain, "_pool", lambda: _Pool())
        _mirror(st, _RecordingSigner()).confirm()
        row = st.get(_RECEIPT)
        assert row.status == store.SUBMITTED and "reverted" in row.last_error


class TestSignerStrategies:
    def test_plan_only_explains_itself(self):
        with pytest.raises(SubmissionRefused, match="NOT sent"):
            signer_mod.PlanOnlySigner().submit(
                signer_mod.UnsignedTx(to=_ESCROW, data="0x", chain_id=1, gas=1)
            )

    def test_external_without_a_url_refuses(self):
        with pytest.raises(SubmissionRefused, match="SIGNER_URL"):
            signer_mod.ExternalSigner(url="").submit(
                signer_mod.UnsignedTx(to=_ESCROW, data="0x", chain_id=1, gas=1)
            )

    def test_external_rejects_a_response_without_a_hash(self, monkeypatch):
        """A signer that answers but reports nothing usable must not look like success."""
        import io
        import urllib.request

        class _Ctx(io.BytesIO):
            """urlopen's context-manager shape, with a body that parses but says nothing."""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Ctx(b'{"ok": true}'))
        with pytest.raises(SubmissionRefused, match="did not return a transaction hash"):
            signer_mod.ExternalSigner(url="https://signer.invalid").submit(
                signer_mod.UnsignedTx(to=_ESCROW, data="0x", chain_id=1, gas=1)
            )

    def test_external_never_sends_the_key_because_it_has_none(self):
        s = signer_mod.ExternalSigner(url="https://signer.invalid", token="tok")
        assert not hasattr(s, "_key")

    def test_env_without_a_key_refuses(self):
        with pytest.raises(SubmissionRefused, match="PRIVATE_KEY"):
            signer_mod.EnvKeySigner(key="").submit(
                signer_mod.UnsignedTx(to=_ESCROW, data="0x", chain_id=1, gas=1)
            )

    def test_env_refuses_a_key_that_is_committed_to_the_repository(self, monkeypatch):
        """A committed key is already public; using it anyway makes hub funds spendable."""
        signer = signer_mod.EnvKeySigner(key="0x" + "42" * 32)

        class _Found:
            returncode = 0

        monkeypatch.setattr(signer_mod.subprocess, "run", lambda *a, **k: _Found())
        with pytest.raises(SubmissionRefused, match="found in the repository"):
            signer.submit(signer_mod.UnsignedTx(to=_ESCROW, data="0x", chain_id=1, gas=1))

    def test_a_missing_git_does_not_silently_pass_or_hard_fail(self, monkeypatch, caplog):
        """Cannot check ≠ safe, but refusing would break a legitimate container deploy."""
        signer = signer_mod.EnvKeySigner(key="0x" + "42" * 32)

        def _boom(*a, **k):
            raise FileNotFoundError("git")

        monkeypatch.setattr(signer_mod.subprocess, "run", _boom)
        with caplog.at_level("WARNING"):
            signer._guard_committed_key()
        assert "not in version control" in caplog.text

    def test_a_signing_error_never_echoes_the_payload(self, monkeypatch):
        """A signing exception can carry the transaction, which sits next to the key."""
        signer = signer_mod.EnvKeySigner(key="0x" + "42" * 32)
        monkeypatch.setattr(signer_mod.subprocess, "run",
                           lambda *a, **k: type("R", (), {"returncode": 1})())

        class _Pool:
            def call(self, method, params=None):
                return "0x1"

        import eth_account

        monkeypatch.setattr(
            eth_account.Account, "sign_transaction",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret-ish 0x4242"))),
        )
        signer._pool = _Pool()
        with pytest.raises(SubmissionRefused) as err:
            signer.submit(signer_mod.UnsignedTx(to=_ESCROW, data="0xdead", chain_id=1, gas=1))
        assert "0x4242" not in str(err.value) and "RuntimeError" in str(err.value)


class TestStatusReport:
    def test_status_reports_configuration_and_debt_without_secrets(self, st, monkeypatch):
        monkeypatch.setenv("AIMARKET_ESCROW_PRIVATE_KEY", "0x" + "ab" * 32)
        _row(st)
        out = _mirror(st, signer_mod.PlanOnlySigner()).status()
        assert out["dry_run"] is True
        assert out["store"]["unsubmitted_usd"] == 1.0
        assert "ab" * 32 not in repr(out)
