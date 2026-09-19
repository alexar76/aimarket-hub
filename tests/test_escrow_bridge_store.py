"""C2 — the authorization store's invariants, and the acceptance checks in front of it.

The store is the only thing standing between "the buyer signed one debit" and "the hub
submitted two", so its invariants are tested as rules rather than as happy paths: identity
is the contract's own replay key, transitions are forward-only, and one (channel, nonce)
can hold exactly one authorization.
"""

from __future__ import annotations

import pytest

from aimarket_hub.escrow_bridge import authorization, chain, store
from aimarket_hub.escrow_bridge.eip712 import DebitAuthorization, crypto_available
from aimarket_hub.escrow_bridge.errors import AuthorizationRejected, BridgeConfigError

pytestmark = pytest.mark.skipif(
    not crypto_available(), reason="eth-utils/eth-account not installed (platon venv)"
)

_ESCROW = "0xe7f1725e7734ce288f8367e1bb143e90bb3f0512"
_HUB = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
_TOKEN = "0x5fbdb2315678afecb367f032d93f642f64180aa3"
_ESCROW_CHANNEL = "0x" + "11" * 32
_RECEIPT = "0x" + "22" * 32
_CHAIN_ID = 8453
_ZERO = "0x" + "00" * 20
_DEPOSITOR_KEY = "0x" + "42" * 32


@pytest.fixture
def st(tmp_path):
    s = store.AuthorizationStore(str(tmp_path / "bridge.db"))
    yield s
    s.close()


def _record(s, **over):
    args = dict(
        receipt_id=_RECEIPT, ledger_channel="ch_abc", escrow_channel=_ESCROW_CHANNEL,
        chain_id=_CHAIN_ID, escrow_address=_ESCROW, hub=_HUB, token=_TOKEN,
        depositor="0x" + "aa" * 20, amount_units=1_000_000, nonce=0,
        deadline=4_000_000_000, signature="0x" + "ab" * 65,
    )
    args.update(over)
    return s.record(**args)


class TestIdentityAndUniqueness:
    def test_a_receipt_is_stored_once(self, st):
        _record(st)
        with pytest.raises(store.StoreError, match="already stored"):
            _record(st)

    def test_a_second_authorization_for_the_same_nonce_is_refused(self, st):
        """Two rows at one nonce could never both be submitted — the contract takes one."""
        _record(st)
        with pytest.raises(store.StoreError, match="nonce"):
            _record(st, receipt_id="0x" + "33" * 32)

    def test_distinct_nonces_coexist(self, st):
        _record(st)
        _record(st, receipt_id="0x" + "33" * 32, nonce=1)
        assert len(st.unresolved()) == 2

    def test_the_same_nonce_on_another_channel_is_fine(self, st):
        _record(st)
        _record(st, receipt_id="0x" + "33" * 32, escrow_channel="0x" + "99" * 32)
        assert len(st.unresolved()) == 2


class TestForwardOnlyStateMachine:
    def test_the_happy_path(self, st):
        _record(st)
        assert st.get(_RECEIPT).status == store.PENDING
        st.record_plan(_RECEIPT, {"ok": True, "gas": 60000})
        assert st.get(_RECEIPT).status == store.PLANNED
        st.mark_submitted(_RECEIPT, "0xtx")
        assert st.get(_RECEIPT).status == store.SUBMITTED
        row = st.mark_confirmed(_RECEIPT)
        assert row.status == store.CONFIRMED and row.is_terminal and row.resolved_at

    def test_a_confirmed_row_can_never_be_reopened(self, st):
        """Reopening it would let the mirror submit an already-collected debit again."""
        _record(st)
        st.mark_submitted(_RECEIPT, "0xtx")
        st.mark_confirmed(_RECEIPT)
        for move in (
            lambda: st.mark_submitted(_RECEIPT, "0xtx2"),
            lambda: st.abandon(_RECEIPT, "changed my mind"),
            lambda: st.record_plan(_RECEIPT, {"ok": True}),
        ):
            with pytest.raises(store.StoreError, match="forward-only"):
                move()

    def test_an_abandoned_row_stays_abandoned(self, st):
        _record(st)
        st.abandon(_RECEIPT, "deadline passed unrecoverably")
        with pytest.raises(store.StoreError, match="forward-only"):
            st.mark_submitted(_RECEIPT, "0xtx")

    def test_submitted_cannot_walk_back_to_planned(self, st):
        _record(st)
        st.mark_submitted(_RECEIPT, "0xtx")
        # A plan recorded after submission is informational only; the status must hold.
        st.record_plan(_RECEIPT, {"ok": True})
        assert st.get(_RECEIPT).status == store.SUBMITTED

    def test_a_failed_simulation_does_not_demote_the_row(self, st):
        """Most reverts here are transient (an unfilled nonce gap, an unreachable node),
        and demoting would lose the queue position that keeps submissions ordered."""
        _record(st)
        st.record_plan(_RECEIPT, {"ok": True})
        row = st.record_plan(_RECEIPT, {"ok": False, "error": "execution reverted: nonce"})
        assert row.status == store.PLANNED
        assert "nonce" in row.last_error and row.attempts == 2

    def test_transitions_on_an_unknown_receipt_are_refused(self, st):
        with pytest.raises(store.StoreError, match="no authorization"):
            st.mark_submitted("0x" + "ff" * 32, "0xtx")


class TestQueueOrder:
    def test_next_is_the_lowest_unresolved_nonce(self, st):
        for i in (2, 0, 1):
            _record(st, receipt_id="0x" + f"{i:02x}" * 32, nonce=i)
        assert st.next_for_channel(_ESCROW_CHANNEL).nonce == 0

    def test_resolved_rows_leave_the_queue(self, st):
        for i in (0, 1):
            _record(st, receipt_id="0x" + f"{i:02x}" * 32, nonce=i)
        first = "0x" + "00" * 32
        st.mark_submitted(first, "0xtx")
        st.mark_confirmed(first)
        assert st.next_for_channel(_ESCROW_CHANNEL).nonce == 1

    def test_an_unknown_channel_has_no_next(self, st):
        assert st.next_for_channel("0x" + "77" * 32) is None


class TestReportingDoesNotLeak:
    def test_the_signature_is_never_rendered(self, st):
        """It is the buyer's credential for this debit; operator views have no use for it."""
        secret = "0x" + "cd" * 65
        row = _record(st, signature=secret)
        assert secret not in repr(row.as_dict())
        assert row.as_dict()["signature"].startswith("<")

    def test_stats_report_what_is_still_owed(self, st):
        _record(st)
        _record(st, receipt_id="0x" + "33" * 32, nonce=1, amount_units=500_000)
        assert st.stats()["unsubmitted_units"] == 1_500_000
        assert st.stats()["unsubmitted_usd"] == 1.5
        st.mark_submitted(_RECEIPT, "0xtx")
        st.mark_confirmed(_RECEIPT)
        assert st.stats()["unsubmitted_units"] == 500_000


class TestAcceptance:
    """The checks in front of the store: everything the contract will ask, asked early."""

    def _account(self):
        from eth_account import Account

        return Account.from_key(_DEPOSITOR_KEY)

    @pytest.fixture
    def wired(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_ESCROW_CONTRACT", _ESCROW)
        monkeypatch.setenv("AIMARKET_ESCROW_HUB_ADDRESS", _HUB)
        depositor = self._account().address

        def _install(**over):
            state = dict(nonce=0, balance=5_000_000, status=chain.STATUS_OPEN,
                         depositor=depositor, token=_TOKEN)
            state.update(over)
            ch = chain.EscrowChannel(
                channel_id=_ESCROW_CHANNEL, depositor=state["depositor"], hub=_ZERO,
                token=state["token"], deposit_amount=5_000_000, balance=state["balance"],
                used_amount=0, expires_at=4_000_000_000, nonce=state["nonce"],
                status=state["status"],
            )
            monkeypatch.setattr(chain, "chain_id", lambda: _CHAIN_ID)
            monkeypatch.setattr(chain, "escrow_address", lambda: _ESCROW)
            monkeypatch.setattr(chain, "read_channel", lambda cid, address=None: ch)
            return ch

        return _install

    # One hour past the fixed `now` used below: inside the default max TTL, so the
    # deadline check is exercised by the tests that target it rather than by every test.
    _NOW = 1_000_000
    _DEADLINE = _NOW + 3600

    def _payload(self, *, amount=1_000_000, nonce=0, deadline=_DEADLINE,
                 receipt=_RECEIPT, channel=_ESCROW_CHANNEL, hub=_HUB, token=_TOKEN,
                 signer_key=_DEPOSITOR_KEY, sign_over=None):
        from eth_account import Account

        auth = DebitAuthorization(channel_id=channel, hub=hub, token=token, amount=amount,
                                  receipt_id=receipt, nonce=nonce, deadline=deadline)
        from aimarket_hub.escrow_bridge import eip712

        digest = eip712.debit_digest(
            sign_over or auth, chain_id=_CHAIN_ID, verifying_contract=_ESCROW
        )
        sig = Account._sign_hash(digest, Account.from_key(signer_key).key).signature.hex()
        return {**auth.as_message(), "signature": sig if sig.startswith("0x") else "0x" + sig}

    def _accept(self, st, payload, **over):
        args = dict(
            payload=payload, ledger_channel_id="ch_abc", escrow_channel_id=_ESCROW_CHANNEL,
            expected_amount_usd=1.0, expected_receipt_id=_RECEIPT, authorizations=st,
            now=self._NOW,
        )
        args.update(over)
        return authorization.verify_and_store(**args)

    def test_a_correct_authorization_is_accepted_and_stored(self, st, wired):
        wired()
        out = self._accept(st, self._payload())
        assert out.signer.lower() == self._account().address.lower()
        assert st.get(_RECEIPT).status == store.PENDING
        assert out.row.amount_units == 1_000_000

    def test_snake_case_field_names_are_accepted(self, st, wired):
        """Three SDKs spell these differently; a buyer should not have to care."""
        wired()
        p = self._payload()
        p["channel_id"] = p.pop("channelId")
        p["receipt_id"] = p.pop("receiptId")
        assert self._accept(st, p).row.receipt_id == _RECEIPT

    def test_a_signature_from_another_wallet_is_refused(self, st, wired):
        wired()
        payload = self._payload(signer_key="0x" + "77" * 32)
        with pytest.raises(AuthorizationRejected, match="depositor"):
            self._accept(st, payload)

    def test_an_amount_that_does_not_match_the_debit_is_refused(self, st, wired):
        """Authorising a cent and being served a dollar's work is the attack here."""
        wired()
        with pytest.raises(AuthorizationRejected, match="amount"):
            self._accept(st, self._payload(amount=10_000), expected_amount_usd=1.0)

    def test_an_amount_above_the_escrow_balance_is_refused(self, st, wired):
        wired(balance=500_000)
        with pytest.raises(AuthorizationRejected, match="balance"):
            self._accept(st, self._payload(amount=1_000_000))

    def test_a_receipt_for_another_invoke_is_refused(self, st, wired):
        wired()
        other = "0x" + "44" * 32
        with pytest.raises(AuthorizationRejected, match="receiptId"):
            self._accept(st, self._payload(receipt=other), expected_receipt_id=_RECEIPT)

    def test_a_stale_nonce_is_refused(self, st, wired):
        wired(nonce=3)
        with pytest.raises(AuthorizationRejected, match="nonce"):
            self._accept(st, self._payload(nonce=2))

    def test_a_future_nonce_is_refused(self, st, wired):
        """It would sit unsubmittable behind a gap the contract will never skip."""
        wired(nonce=0)
        with pytest.raises(AuthorizationRejected, match="nonce"):
            self._accept(st, self._payload(nonce=1))

    def test_an_authorization_for_another_channel_is_refused(self, st, wired):
        wired()
        with pytest.raises(AuthorizationRejected, match="channelId"):
            self._accept(st, self._payload(channel="0x" + "88" * 32))

    def test_an_authorization_bound_to_another_hub_is_refused(self, st, wired):
        wired()
        with pytest.raises(AuthorizationRejected, match="hub"):
            self._accept(st, self._payload(hub="0x" + "99" * 20))

    def test_a_token_mismatch_is_refused(self, st, wired):
        wired()
        with pytest.raises(AuthorizationRejected, match="token"):
            self._accept(st, self._payload(token="0x" + "cc" * 20))

    def test_an_expired_deadline_is_refused(self, st, wired):
        wired()
        with pytest.raises(AuthorizationRejected, match="deadline"):
            self._accept(st, self._payload(deadline=self._NOW - 1), now=self._NOW)

    def test_an_absurdly_distant_deadline_is_refused(self, st, wired):
        """A deadline caps how long the hub may hold a claim; unbounded is a licence."""
        wired()
        with pytest.raises(AuthorizationRejected, match="deadline"):
            self._accept(st, self._payload(deadline=4_000_000_000), now=1_000)

    def test_a_closed_escrow_channel_is_refused(self, st, wired):
        wired(status=chain.STATUS_SETTLED)
        with pytest.raises(AuthorizationRejected, match="not open"):
            self._accept(st, self._payload())

    def test_a_replay_of_a_stored_receipt_is_refused_as_client_error(self, st, wired):
        wired()
        self._accept(st, self._payload())
        with pytest.raises(AuthorizationRejected, match="already stored"):
            self._accept(st, self._payload())

    @pytest.mark.parametrize("payload", [
        None, "string", 42, {}, {"signature": "0xab"},
        {"channelId": _ESCROW_CHANNEL, "hub": _HUB, "token": _TOKEN, "amount": "abc",
         "receiptId": _RECEIPT, "nonce": 0, "deadline": 1, "signature": "0xab"},
    ])
    def test_malformed_payloads_are_client_errors_not_crashes(self, st, wired, payload):
        wired()
        with pytest.raises(AuthorizationRejected):
            self._accept(st, payload)

    def test_a_missing_hub_address_is_an_operator_error(self, st, wired, monkeypatch):
        """Not guessable: the contract binds the hub into the digest."""
        wired()
        monkeypatch.delenv("AIMARKET_ESCROW_HUB_ADDRESS", raising=False)
        with pytest.raises(BridgeConfigError, match="HUB_ADDRESS"):
            self._accept(st, self._payload())

    def test_a_tampered_amount_after_signing_is_refused(self, st, wired):
        """Sign for 0.10, submit a payload claiming 1.00: the digest no longer recovers."""
        wired()
        honest = DebitAuthorization(
            channel_id=_ESCROW_CHANNEL, hub=_HUB, token=_TOKEN, amount=100_000,
            receipt_id=_RECEIPT, nonce=0, deadline=self._DEADLINE,
        )
        payload = self._payload(amount=1_000_000, sign_over=honest)
        with pytest.raises(AuthorizationRejected, match="depositor|recovered"):
            self._accept(st, payload)


class TestStorePathDefault:
    def test_it_lands_beside_the_channel_ledger(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AIMARKET_ESCROW_BRIDGE_DB_PATH", raising=False)
        from aimarket_hub import channels as ch_mod

        monkeypatch.setattr(ch_mod, "_DB_PATH", str(tmp_path / "sub" / "channels.db"))
        assert store.default_db_path() == str(tmp_path / "sub" / "escrow_bridge.db")

    def test_an_explicit_path_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AIMARKET_ESCROW_BRIDGE_DB_PATH", str(tmp_path / "custom.db"))
        assert store.default_db_path() == str(tmp_path / "custom.db")
