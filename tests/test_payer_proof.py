"""The CANONICAL channel-open payer proof — the interoperability contract.

Two channel stacks (aimarket-hub and web/backend/.../ai_market_protocol) each
require the depositor to prove control of the paying wallet, and each had invented
its OWN challenge for it: different domain string, different subject, different tx
normalisation. A signature valid at one door was invalid at the other, so no SDK
could be written against both.

These tests pin the single shared definition byte-for-byte. If the preimage has to
change, the version in the domain line changes with it — never the fields alone,
because two deployments on different versions must fail to verify rather than
silently disagree about what was signed.
"""

from __future__ import annotations

import pytest

import aimarket_hub.channels as ch


_PAYER = "0x" + "Ab" * 20            # EIP-55-style mixed case
_PAYER_LOWER = _PAYER.lower()

# The exact bytes an SDK must produce. Written out literally rather than built from
# the implementation, so a change to the format has to be made here too.
_EXPECTED = (
    "AIMarket-Payer-Proof/v1\n"
    "purpose:channel-open\n"
    "chain:base\n"
    "tx:0xdeadbeef\n"
    f"payer:{_PAYER_LOWER}\n"
    "amount_cents:500"
)


def _on_chain():
    """The shared on-chain module, or skip.

    Resolved through the production helper rather than ``importorskip`` on purpose:
    the hub's own test venv cannot initialise the web package (its ai_market_protocol
    __init__ pulls in commerce → passlib) even though ``on_chain`` itself imports
    fine, and a plain import therefore answers differently on the first call than on
    the second. ``_shared_on_chain`` is what production uses to make that
    deterministic, so the tests exercise the same path.
    """
    import sys

    if ch._shared_on_chain("channel_open_proof_message") is None:
        pytest.skip("web on_chain primitives not importable from this venv")
    return sys.modules[ch._ON_CHAIN_MODULE]


class TestCanonicalMessage:
    def test_hub_builds_the_pinned_canonical_bytes(self):
        oc = _on_chain()
        assert oc.channel_open_proof_message(
            chain="base", tx_hash="0xdeadbeef", payer=_PAYER_LOWER, amount_usd=5.0
        ) == _EXPECTED

    def test_hub_helper_matches_the_shared_primitive(self):
        oc = _on_chain()
        assert ch.payer_proof_challenge(
            payer=_PAYER, tx_hash="0xDEADBEEF", chain="Base", deposit_usd=5.0
        ) == oc.channel_open_proof_message(
            chain="base", tx_hash="0xdeadbeef", payer=_PAYER_LOWER, amount_usd=5.0
        )

    def test_every_field_is_canonicalised(self):
        """Casing that carries no information must not change the message.

        A client signing the checksummed rendering of its own address, or the
        upper-case rendering of its own tx hash, is signing the same deposit — and
        used to be refused because the two sides normalised differently.
        """
        oc = _on_chain()
        base = oc.channel_open_proof_message(
            chain="base", tx_hash="0xdeadbeef", payer=_PAYER_LOWER, amount_usd=5.0
        )
        for chain in ("Base", "BASE", "  base  "):
            for tx in ("0xDEADBEEF", "0xDeadBeef", " 0xdeadbeef "):
                for payer in (_PAYER, _PAYER.upper(), f"  {_PAYER_LOWER} "):
                    assert oc.channel_open_proof_message(
                        chain=chain, tx_hash=tx, payer=payer, amount_usd=5.0
                    ) == base, (chain, tx, payer)

    def test_the_0x_prefix_is_canonicalised_not_just_the_case(self):
        """A bare hex hash and its 0x rendering name the SAME transaction.

        Only one of the two stacks adds the prefix before building the challenge (the
        web package runs normalize_tx_hash first, the hub passes the request value
        through), so leaving a bare hash opaque made them sign different preimages for
        one deposit — the drift the canonical message exists to remove.
        """
        oc = _on_chain()
        real = "a1" * 32
        assert oc.canonical_proof_tx_hash(real.upper()) == "0x" + real
        assert oc.channel_open_proof_message(
            chain="base", tx_hash=real.upper(), payer=_PAYER_LOWER, amount_usd=5.0
        ) == oc.channel_open_proof_message(
            chain="base", tx_hash="0x" + real, payer=_PAYER_LOWER, amount_usd=5.0
        )

    def test_base58_ids_keep_their_case(self):
        """Solana signatures/addresses are base58 — case IS the identifier there."""
        oc = _on_chain()
        upper = oc.channel_open_proof_message(
            chain="solana", tx_hash="5KtPn1", payer="5KtPn1SolanaPayer", amount_usd=1.0
        )
        lower = oc.channel_open_proof_message(
            chain="solana", tx_hash="5ktpn1", payer="5KtPn1SolanaPayer", amount_usd=1.0
        )
        assert upper != lower

    @pytest.mark.parametrize(
        "field,kwargs",
        [
            ("chain", {"chain": "optimism"}),
            ("tx_hash", {"tx_hash": "0xfeed"}),
            ("payer", {"payer": "0x" + "cd" * 20}),
            ("amount", {"amount_usd": 5.01}),
        ],
    )
    def test_each_binding_field_changes_the_message(self, field, kwargs):
        """Every field is load-bearing: changing one must invalidate the proof."""
        oc = _on_chain()
        base_kwargs = {
            "chain": "base", "tx_hash": "0xdeadbeef",
            "payer": _PAYER_LOWER, "amount_usd": 5.0,
        }
        assert oc.channel_open_proof_message(**base_kwargs) != oc.channel_open_proof_message(
            **{**base_kwargs, **kwargs}
        ), field

    def test_amount_is_bound_in_integer_cents(self):
        """Floats that name the same money must produce the same preimage."""
        oc = _on_chain()
        assert oc.channel_open_proof_message(
            chain="base", tx_hash="0xa", payer=_PAYER_LOWER, amount_usd=5
        ) == oc.channel_open_proof_message(
            chain="base", tx_hash="0xa", payer=_PAYER_LOWER, amount_usd=5.00
        )
        assert "amount_cents:35" in oc.channel_open_proof_message(
            chain="base", tx_hash="0xa", payer=_PAYER_LOWER, amount_usd=0.35
        )

    def test_unusable_amount_cannot_produce_a_matchable_proof(self):
        """A non-finite amount must not silently render as 0 (a real deposit size)."""
        oc = _on_chain()
        for bad in (float("nan"), float("inf"), "five", None):
            assert "amount_cents:-1" in oc.channel_open_proof_message(
                chain="base", tx_hash="0xa", payer=_PAYER_LOWER, amount_usd=bad
            ), bad

    def test_domain_separated_from_the_legacy_challenges(self):
        """A proof collected for a UNI top-up or an invoke payment must not verify here."""
        oc = _on_chain()
        canonical = oc.channel_open_proof_message(
            chain="base", tx_hash="0xdeadbeef", payer=_PAYER_LOWER, amount_usd=5.0
        )
        for purpose in ("channel deposit", "invoke payment", "uni topup"):
            legacy = oc.payer_proof_message(
                purpose=purpose, subject=_PAYER_LOWER, tx_hash="0xdeadbeef", chain="base"
            )
            assert legacy != canonical, purpose


class TestRecovery:
    """Recovery is EIP-191 personal-sign so any ordinary wallet can produce it."""

    def test_roundtrip_recovers_the_signing_wallet(self):
        oc = _on_chain()
        account = pytest.importorskip("eth_account", reason="eth_account not installed")
        from eth_account.messages import encode_defunct

        key = "0x" + "42" * 32
        acct = account.Account.from_key(key)
        message = oc.channel_open_proof_message(
            chain="base", tx_hash="0xdeadbeef", payer=acct.address, amount_usd=5.0
        )
        signed = account.Account.sign_message(encode_defunct(text=message), private_key=key)
        sig = "0x" + signed.signature.hex().removeprefix("0x")

        recovered = oc.recover_channel_open_payer(
            chain="base", tx_hash="0xdeadbeef", payer=acct.address,
            amount_usd=5.0, signature=sig,
        )
        assert recovered is not None
        assert recovered.lower() == acct.address.lower()

        # ...and the SAME signature is accepted through the hub's own helper path,
        # which is the whole point of unifying the challenge.
        assert ch._recover_payer_address(
            payer=acct.address, tx_hash="0xDEADBEEF", chain="Base",
            deposit_usd=5.0, signature=sig,
        ).lower() == acct.address.lower()

    def test_a_proof_for_a_different_amount_does_not_verify(self):
        oc = _on_chain()
        account = pytest.importorskip("eth_account", reason="eth_account not installed")
        from eth_account.messages import encode_defunct

        key = "0x" + "43" * 32
        acct = account.Account.from_key(key)
        message = oc.channel_open_proof_message(
            chain="base", tx_hash="0xdeadbeef", payer=acct.address, amount_usd=5.0
        )
        signed = account.Account.sign_message(encode_defunct(text=message), private_key=key)
        sig = "0x" + signed.signature.hex().removeprefix("0x")

        recovered = oc.recover_channel_open_payer(
            chain="base", tx_hash="0xdeadbeef", payer=acct.address,
            amount_usd=50.0, signature=sig,
        )
        # A different preimage recovers SOME address, just never the payer's.
        assert recovered is None or recovered.lower() != acct.address.lower()

    @pytest.mark.parametrize("sig", ["", "   ", "0xnot-a-signature", "0x" + "00" * 65])
    def test_malformed_signatures_are_failed_proofs_not_errors(self, sig):
        oc = _on_chain()
        assert oc.recover_channel_open_payer(
            chain="base", tx_hash="0xdeadbeef", payer=_PAYER_LOWER,
            amount_usd=5.0, signature=sig,
        ) in (None, *([] if sig.strip() else [None]))


class TestStandaloneHubFailsClosed:
    """A hub deployed without the `web` package must refuse, never raise, never pass."""

    @pytest.fixture
    def no_web(self, monkeypatch):
        """Simulate a hub image that ships without the `web` package at all.

        Both halves matter: the module must be absent from sys.modules (otherwise the
        partial-import fallback legitimately finds it) AND the import itself must
        fail.
        """
        import builtins
        import sys

        monkeypatch.delitem(sys.modules, ch._ON_CHAIN_MODULE, raising=False)
        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name.startswith(ch._ON_CHAIN_MODULE):
                raise ImportError("no web package in this deployment")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)

    def test_challenge_returns_empty_instead_of_raising(self, no_web):
        # The bare `from web... import` used to raise ImportError straight out of the
        # helper, while every other cross-package import in channels.py degrades.
        assert ch.payer_proof_challenge(
            payer=_PAYER, tx_hash="0xabc", chain="base", deposit_usd=1.0
        ) == ""

    def test_recovery_returns_none_instead_of_raising(self, no_web):
        assert ch._recover_payer_address(
            payer=_PAYER, tx_hash="0xabc", chain="base", deposit_usd=1.0,
            signature="0xanything",
        ) is None

    def test_production_open_refuses_rather_than_crediting(self, no_web, tmp_path, monkeypatch):
        """The end state that matters: an unevaluatable proof grants nothing."""
        monkeypatch.setattr(ch, "_is_production_mode", lambda: True)
        monkeypatch.setattr(ch, "_VERIFY_STUB", False)
        monkeypatch.setattr(
            ch, "_verify_tx_onchain", lambda **kw: {"ok": True, "sender": _PAYER}
        )
        led = ch.ChannelLedger(db_path=str(tmp_path / "ch.db"))
        try:
            out = led.open(5.0, wallet=_PAYER, tx_hash="0xdep", payer_signature="0xsig")
            assert "error" in out and "payer proof" in out["error"]
            assert led.stats()["open_channels"] == 0
        finally:
            led.stop_sweep()


# ── Cross-stack single-use (PAYAUTH-002b) ────────────────────────────────────


class TestOneDepositFundsOneChannelSystemWide:
    """The two settlement doors must not each credit the same deposit.

    aimarket-hub guards single-use with its ``consumed_deposits`` table and the web v1
    package with its own tx-hash store, so before the shared claim registry one real
    transfer plus one signature bought a funded channel at BOTH doors: $5 paid, $10
    credited. The registry (one O_EXCL file per deposit, consulted by both) is the only
    thing that can make the claim exclusive across two independent ledgers — and it is
    only load-bearing if both doors actually WRITE it, which is what these pin. The
    mechanism shipped once with neither door calling it.
    """

    # Real 32-byte hex, because case-canonicalisation only applies to hex ids: a
    # non-hex (base58/Solana) id is case-SIGNIFICANT, so "0xSHARED"/"0xshared" would
    # correctly be two different deposits and would prove nothing here.
    TX1 = "0x" + "11" * 32
    TX2 = "0x" + "ab" * 32
    TX3 = "0x" + "33" * 32
    TX4 = "0x" + "44" * 32

    def _prod_ledger(self, tmp_path, monkeypatch, name):
        monkeypatch.setattr(ch, "_is_production_mode", lambda: True)
        monkeypatch.setattr(ch, "_VERIFY_STUB", False)
        monkeypatch.setattr(
            ch, "_verify_tx_onchain", lambda **kw: {"ok": True, "sender": _PAYER}
        )
        monkeypatch.setattr(
            ch, "_recover_payer_address",
            lambda *, payer, tx_hash, chain, deposit_usd, signature: _PAYER,
        )
        return ch.ChannelLedger(db_path=str(tmp_path / name))

    def test_the_hub_writes_the_shared_registry(self, tmp_path, monkeypatch):
        oc = _on_chain()
        monkeypatch.setenv("AIMARKET_DEPOSIT_CLAIMS_DIR", str(tmp_path / "claims"))
        led = self._prod_ledger(tmp_path, monkeypatch, "a.db")
        try:
            out = led.open(5.0, wallet=_PAYER, tx_hash=self.TX1, payer_signature="0xsig")
            assert "channel" in out, out
            key = oc.deposit_claim_key("base", self.TX1)
            assert (tmp_path / "claims" / f"{key}.json").exists(), (
                "the hub credited a channel without claiming the deposit in the shared "
                "registry — the other door can still fund a second channel from it"
            )
        finally:
            led.stop_sweep()

    def test_a_deposit_claimed_by_the_other_door_cannot_fund_a_hub_channel(
        self, tmp_path, monkeypatch
    ):
        oc = _on_chain()
        claims = tmp_path / "claims"
        monkeypatch.setenv("AIMARKET_DEPOSIT_CLAIMS_DIR", str(claims))
        # The factory's v1 door gets there first with the very same transaction.
        first = oc.claim_deposit(
            chain="base", tx_hash=self.TX2.upper().replace("0X", "0x"), stack=oc.DEPOSIT_STACK_WEB_V1,
            claim_id="ch_web_1", amount_cents=500,
        )
        assert first.get("ok") is True, first

        led = self._prod_ledger(tmp_path, monkeypatch, "b.db")
        try:
            out = led.open(5.0, wallet=_PAYER, tx_hash=self.TX2, payer_signature="0xsig")
            assert "error" in out, out
            assert "already used" in out["error"]
            assert led.stats()["open_channels"] == 0
        finally:
            led.stop_sweep()

    def test_an_unavailable_registry_refuses_rather_than_crediting(
        self, tmp_path, monkeypatch
    ):
        """Fail closed: exclusivity that cannot be recorded must not be assumed."""
        _on_chain()
        monkeypatch.setattr(
            ch, "_claim_deposit_shared",
            lambda **kw: {"ok": False, "error": "deposit_registry_unavailable"},
        )
        led = self._prod_ledger(tmp_path, monkeypatch, "c.db")
        try:
            out = led.open(5.0, wallet=_PAYER, tx_hash=self.TX3, payer_signature="0xsig")
            assert "error" in out and "registry unavailable" in out["error"]
            assert led.stats()["open_channels"] == 0
        finally:
            led.stop_sweep()

    def test_a_local_race_loser_hands_the_claim_back(self, tmp_path, monkeypatch):
        """A claim taken for a channel that is never created must not strand the deposit."""
        oc = _on_chain()
        monkeypatch.setenv("AIMARKET_DEPOSIT_CLAIMS_DIR", str(tmp_path / "claims"))
        led = self._prod_ledger(tmp_path, monkeypatch, "d.db")
        try:
            first = led.open(5.0, wallet=_PAYER, tx_hash=self.TX4, payer_signature="0xsig")
            assert "channel" in first, first
            # Same deposit again: the local consumed_deposits row now rejects it. The
            # shared claim it took on the way in belongs to the FIRST channel, so it must
            # still name that channel rather than having been freed or overwritten.
            again = led.open(5.0, wallet=_PAYER, tx_hash=self.TX4, payer_signature="0xsig")
            assert "error" in again and "already used" in again["error"]
            key = oc.deposit_claim_key("base", self.TX4)
            import json
            body = json.loads((tmp_path / "claims" / f"{key}.json").read_text())
            assert body.get("claim_id") == first["channel"]["channel_id"]
        finally:
            led.stop_sweep()
