"""Pre-funded payment channels for off-chain capability invocation.

Implements channel/open, channel/close from Protocol v1/v2 spec.

Storage: SQLite-backed (survives process restart).
Amounts: integer cents internally (no IEEE 754 drift).
Rate limiting: per-wallet caps on open/close (see "Rate limiting" below).
Background sweep: expires stale channels and reaps orphaned holds every 5 min.

Settlement model — this ledger is the ONLY channel state (no on-chain mirror)
    This module does not mirror channel state on-chain and does not hold funds in
    escrow. A deposit is an ordinary on-chain transfer to the PLATFORM SETTLEMENT
    WALLET (on_chain.platform_recipient), verified after the fact; from that moment
    the money is in the operator's custody and every debit, balance and remainder
    below is bookkeeping in this SQLite ledger.

    Nothing in the repository calls ``AIMarketEscrow.debitChannel``. If an operator
    ALSO opens a contract channel for the same funds, its on-chain ``usedAmount``
    stays 0 no matter how much is consumed here, so ``refundChannel`` /
    ``expireChannel`` would return a fully-consumed deposit in full. The two rails
    are therefore mutually exclusive today: the contract has been exercised
    end-to-end by hand (docs/onchain-journal.md), but not from this code path.
    Tracked as KI-11 in docs/known-issues.md; the honest consequences for refunds
    are spelled out under "Refunds are OBLIGATIONS" below.

Deposit binding (PAYAUTH-003)
    A deposit is verified with the SENDER-returning verifier
    (on_chain.verify_tx_payment_details) and the channel is credited ONLY to the
    wallet that actually paid on-chain. Verifying recipient/amount alone lets anyone
    watching the shared settlement wallet claim someone else's inbound transfer by
    quoting its tx hash first. If the verifier cannot report a sender, the deposit is
    refused (fail closed). A caller that supplies no wallet gets the channel bound to
    the on-chain payer — an on-chain-funded channel is never anonymous.

    Matching the claimed wallet against the on-chain sender is NOT sufficient on its
    own: the payer's address is public — it is printed in the very transaction the
    claimant quotes — so a front-runner simply names the victim's address, the match
    succeeds, and open() hands THEM the channel_secret that authorizes debits (the
    invoke path presents only channel id + secret, no wallet). The claimant must
    therefore PROVE control of the paying wallet with an EIP-191 signature over
    payer_proof_challenge(), recovered via on_chain.recover_channel_open_payer.

    That challenge is the CANONICAL, versioned one defined in
    web/backend/services/ai_market_protocol/on_chain.py and shared byte-for-byte with
    the web v1 channel stack — each side used to have its own incompatible message,
    so one signature could not satisfy both and no SDK could target both doors. The
    preimage binds domain+version, purpose, chain, tx hash, payer and amount; read
    that module's CANONICAL PAYER PROOF block for why each field is in it. Non-EVM
    chains have no implemented proof scheme and are refused rather than credited
    unproven.

Single-use deposits are claimed under a CANONICAL key (PAYAUTH-002)
    An EVM tx hash and a chain name are case-insensitive at the RPC layer, so
    ("Base", "0xABC…") and ("base", "0xabc…") are the same transaction. Claiming them
    verbatim let one real deposit be re-claimed once per capitalisation and fund N
    funded channels; the claim is now keyed on the normalised pair, and the lookup is
    case-insensitive so rows written before this fix still block a replay. Base58
    Solana signatures are case-SIGNIFICANT and are left byte-exact.

Refunds are OBLIGATIONS, not transfers (ACCT-001)
    Deposits are paid to the platform's own settlement wallet, so the unspent
    remainder at close/expiry is already in the operator's custody and NOTHING in this
    module can send value. The remainder is therefore recorded as an explicit,
    queryable payout obligation (`channel_payout_obligations`, migration 017) and the
    settlement dict reports `refund_executed_usd` (always 0.0 here) separately from
    `refund_owed_usd`. Operator payouts happen out-of-band and are attested back with
    mark_obligation_paid().

Rate limiting
    Per-wallet sliding windows held in-process: with N worker processes the effective
    cap is N x the configured value, and wallet-less ("anonymous") opens share ONE
    bounded bucket instead of being exempt. The tracking table is bounded by LRU
    EVICTION, not by refusal — the key is caller-supplied, so refusing when full let
    ~50k junk wallet strings lock every real depositor out of open and close for an
    hour. The flood itself is capped at the HTTP layer, per client IP (api.py), where
    the identity is not attacker-chosen.

Env knobs (read dynamically so they are monkeypatchable, hub convention):
    AIMARKET_CHANNELS_DB_PATH             data/channels.db  ledger database
    AIMARKET_CHANNEL_ALLOW_UNPROVEN_PAYER 0     opt OUT of the payer proof-of-control
                                                requirement above (transition only —
                                                leaves deposit front-running open)
    AIMARKET_CHANNEL_ANON_OPENS_PER_HOUR  200   shared cap for wallet-less opens
    AIMARKET_CHANNEL_ANON_CLOSES_PER_HOUR 600   shared cap for wallet-less closes
    AIMARKET_CHANNEL_HOLD_REAP_AFTER_SECS 86400 release a hold stuck 'held' this long
                                                with no live verification (0 = off)
    AIMARKET_VERIFY_SETTLEMENTS_DB_PATH   —     where verified_settlements lives
                                                (defaults to AIMARKET_DB_PATH /
                                                data/hub.db); the reaper refuses to
                                                release anything it cannot read
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import math
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# ── Env-driven defaults ──────────────────────────────────────────

_DEFAULT_CHAIN = os.getenv("AIMARKET_PAYMENT_CHAIN", "base")
_DEFAULT_TOKEN = os.getenv("AIMARKET_PAYMENT_TOKEN", "USDC")
_RECIPIENT = os.getenv("AIMARKET_PAYMENT_RECIPIENT", "")
_VERIFY_STUB = os.getenv("AIFACTORY_PAYMENT_VERIFY_STUB", "0") == "1"
_MIN_CONFIRMATIONS = int(os.getenv("AIFACTORY_PAYMENT_MIN_CONFIRMATIONS", "2"))
_DB_PATH = os.getenv("AIMARKET_CHANNELS_DB_PATH", "data/channels.db")

# Rate limits
_MAX_OPENS_PER_WALLET_PER_HOUR = 20
_MAX_CLOSES_PER_WALLET_PER_HOUR = 60
_DEFAULT_ANON_OPENS_PER_HOUR = 200
_DEFAULT_ANON_CLOSES_PER_HOUR = 600
# One shared bucket for every wallet-less caller. Exempting them (the old behaviour)
# made a public hub effectively unlimited: an attacker just omits the wallet field.
_ANON_RATE_KEY = "\x00anonymous"
# Upper bound on tracked buckets. Without it, one request per fresh wallet string
# grows the in-process dicts without limit (memory DoS). Full table => LRU eviction
# (see _check_rate for why refusing instead was an availability DoS).
_MAX_RATE_TRACKED_KEYS = 50_000
_DEFAULT_MAX_CHANNELS = 10_000
_CHANNEL_EXPIRY_SECS = 86400  # 24h
_SWEEP_INTERVAL_SECS = 300     # 5 min

# Orphaned-hold reaper (see module docstring)
_DEFAULT_HOLD_REAP_AFTER_SECS = 86400  # 24h
_HOLD_REAP_SCAN_LIMIT = 500            # bounded work per pass

# Cents per USD (no floats for money)
_CENTS_PER_USD = 100


def _validate_recipient() -> None:
    if not _RECIPIENT:
        logger.warning(
            "AIMARKET_PAYMENT_RECIPIENT is not set. "
            "Payment channels will work in demo mode but real USDT/USDC "
            "transfers will fail. Set it in .env or environment."
        )


_validate_recipient()


def _is_production_mode() -> bool:
    """Mirror security.prod_startup_guard.is_production_mode with an env fallback.

    The hub also ships as a standalone package where the security module may not
    be importable, so we fall back to the AIFACTORY_PROD env contract used
    elsewhere in the hub (signing.py, capability_nft.py).
    """
    try:
        from security.prod_startup_guard import is_production_mode

        return is_production_mode()
    except Exception:
        return os.environ.get("AIFACTORY_PROD", "").strip() == "1"


def _allow_demo_credit() -> bool:
    """Whether to credit a channel WITHOUT on-chain verification (dev/demo only).

    Read dynamically (not a module constant) so it's monkeypatchable in tests. Fail-closed by
    default: a non-production deploy that hasn't explicitly opted in will REFUSE to credit an
    unverified deposit, so a public hub that simply forgot AIFACTORY_PROD can't hand out free
    channels.
    """
    return os.environ.get("AIMARKET_ALLOW_DEMO_CREDIT", "").strip() == "1"


def _verify_tx_onchain(
    *, tx_hash: str, amount_usd: float, chain: str, token: str
) -> dict[str, Any]:
    """Verify a deposit tx on-chain before crediting a channel (fail-closed).

    Returns ``{"ok": True, "sender": <payer address or "">}`` only when an on-chain
    verifier confirms the transaction paid the configured recipient the expected
    amount/token with sufficient confirmations. Returns ``{"ok": False, "error": ...}``
    otherwise.

    Uses the SENDER-returning verifier (verify_tx_payment_details) because recipient +
    amount alone do not identify WHO paid: every deposit lands in the same platform
    settlement wallet, so a plain recipient/amount check lets anyone quote a stranger's
    tx hash and have the channel credited to their own wallet (PAYAUTH-003). ``sender``
    is empty when the verifier cannot attribute the payment (e.g. the dev demo-bypass
    path, which has no chain behind it) — callers MUST refuse to credit then.

    If the verifier is not reachable (standalone hub deploy with no chain access) we
    MUST NOT credit in production — we reject with "on-chain verification unavailable"
    so credit is never granted on an unverified tx.
    """
    tx = (tx_hash or "").strip()
    if not tx:
        return {"ok": False, "error": "tx_hash is required for on-chain verification"}

    verify_tx_payment_details = _shared_on_chain("verify_tx_payment_details")
    if verify_tx_payment_details is None:  # verifier not reachable from this deployment
        return {
            "ok": False,
            "error": "on-chain verification unavailable — refusing to credit channel "
            "in production without a verified transaction",
        }

    try:
        verified, sender = verify_tx_payment_details(
            tx_hash=tx, amount_usd=amount_usd, chain=chain, token=token
        )
    except Exception as exc:
        logger.error("On-chain verification raised for tx %s: %s", tx[:12], exc)
        return {
            "ok": False,
            "error": "on-chain verification unavailable — verifier error, "
            "refusing to credit channel",
        }

    if not verified:
        return {
            "ok": False,
            "error": "on-chain verification failed — transaction does not match "
            "expected recipient/amount/token or has insufficient confirmations",
        }
    return {"ok": True, "sender": str(sender or "").strip()}


# ── Payer proof-of-control (PAYAUTH-003b) ────────────────────────


_ON_CHAIN_MODULE = "web.backend.services.ai_market_protocol.on_chain"


def _shared_on_chain(attr: str) -> Any:
    """One primitive from the CANONICAL on-chain module, or None if unavailable.

    Every cross-package reach into the web stack goes through here so a hub that
    cannot see it degrades identically everywhere (verification, challenge, recovery)
    instead of one call raising ImportError while its neighbour returns an error dict.

    The ``sys.modules`` fallback is not belt-and-braces, it is the whole point: the
    PARENT package's ``__init__`` imports optional web-app dependencies, so on a hub
    where one of those is missing the first ``import …on_chain`` raises even though
    ``on_chain`` itself imported cleanly and is already registered. Without the
    fallback the answer depended on how many times it had been asked — the first
    channel open after boot failed closed and an identical retry succeeded.
    """
    try:
        import web.backend.services.ai_market_protocol.on_chain  # noqa: F401
    except Exception as exc:
        cached = sys.modules.get(_ON_CHAIN_MODULE)
        if cached is None:
            logger.error(
                "Shared on-chain primitives unavailable (%s import failed): %s",
                _ON_CHAIN_MODULE, exc,
            )
            return None
        logger.warning(
            "%s imported, but its parent package failed to initialise (%s) — using "
            "the loaded module", _ON_CHAIN_MODULE, exc,
        )
    return getattr(sys.modules.get(_ON_CHAIN_MODULE), attr, None)


def _allow_unproven_payer() -> bool:
    """Whether to credit an on-chain deposit WITHOUT proof the caller controls the payer.

    Default OFF. Matching the caller's claimed wallet against the on-chain sender proves
    nothing, because the sender address is public in the transaction being quoted: a
    front-runner names the victim's own address, the match passes, and open() returns
    the channel_secret to the attacker. This escape hatch exists only so an operator can
    keep the legacy flow alive while the HTTP layer is wired to forward the signature;
    every use is logged.
    """
    return os.environ.get("AIMARKET_CHANNEL_ALLOW_UNPROVEN_PAYER", "").strip() == "1"


def payer_proof_challenge(
    *, payer: str, tx_hash: str, chain: str, deposit_usd: float
) -> str:
    """The exact message the paying wallet must sign to claim a deposit.

    Delegates to the CANONICAL, versioned challenge
    (on_chain.channel_open_proof_message) — the single definition shared with the
    web v1 channel stack, so one signature is valid at both doors and one
    normalisation rule decides whether `0xABC` and `0xabc` are the same deposit.
    The amount is part of the preimage, which is why it is a required argument here.

    Returns "" when the shared primitive is not importable (a standalone hub deploy
    with no `web` package). It is NOT a bare ImportError: every other cross-package
    import in this module degrades to a refusal, and this one used to be the sole
    exception — it raised straight out of the helper. "" means "this deployment
    cannot state or evaluate payer proofs", and it is safe because the matching
    recovery below fails closed for exactly the same reason, so open() refuses every
    on-chain deposit rather than crediting one on an unevaluatable proof.
    """
    build = _shared_on_chain("channel_open_proof_message")
    if build is None:
        logger.error(
            "Payer proof challenge unavailable — this hub cannot accept "
            "on-chain-funded channel opens"
        )
        return ""
    return build(chain=chain, tx_hash=tx_hash, payer=payer, amount_usd=deposit_usd)


def _recover_payer_address(
    *, payer: str, tx_hash: str, chain: str, deposit_usd: float, signature: str
) -> str | None:
    """Address that signed the canonical payer challenge for this deposit, or None.

    None on ANY failure — no signature, malformed signature, or the recovery primitive
    not being importable from this deployment. The caller treats None as "unproven" and
    refuses to credit, so a hub that cannot evaluate the proof never grants one.
    """
    if not (signature or "").strip():
        return None
    recover = _shared_on_chain("recover_channel_open_payer")
    if recover is None:
        logger.error(
            "Payer proof unavailable (recover_channel_open_payer not importable) — "
            "refusing to credit an unproven deposit"
        )
        return None
    try:
        return recover(
            chain=chain,
            tx_hash=tx_hash,
            payer=payer,
            amount_usd=deposit_usd,
            signature=signature.strip(),
        )
    except Exception as exc:
        logger.error("Payer proof recovery raised: %s — treating as unproven", exc)
        return None


# ── Helpers ──────────────────────────────────────────────────────

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# Chains whose payout tx ids are 0x + 32 bytes of hex (see _payout_tx_shape_error).
_EVM_PAYOUT_CHAINS = frozenset(
    {"base", "ethereum", "eth", "mainnet", "arbitrum", "optimism", "polygon", "bsc", "avalanche"}
)


def _normalize_chain(chain: str) -> str:
    """Canonical chain id for the single-use deposit claim (chain names are ASCII)."""
    return (chain or "").strip().lower()


def _normalize_tx_hash(tx_hash: str) -> str:
    """Canonical transaction id for the single-use deposit claim.

    EVM hashes are hex and case-insensitive at the JSON-RPC layer, so `0xABC…` and
    `0xabc…` name the SAME transaction. Storing the claim verbatim let one real deposit
    be claimed once per capitalisation and mint a fully funded channel each time.
    Anything that is not 0x-prefixed hex (a base58 Solana signature) is case-SIGNIFICANT
    and must be left byte-exact.
    """
    tx = (tx_hash or "").strip()
    body = tx[2:] if tx[:2].lower() == "0x" else ""
    if body and all(c in _HEX_DIGITS for c in body):
        return "0x" + body.lower()
    return tx


def _claims_fallback_dir(db_path: str) -> str:
    """Stack-local directory used only when no shared registry is configured.

    Derived from the LEDGER'S OWN database path, not the module default: two ledgers
    pointed at different files (a test's tmp db, a second hub instance) must not share
    a claim namespace, or one instance's deposits silently block another's. A
    standalone hub — which has no second door to race with — keeps working, and the
    registry logs a warning whenever it falls back, because a fallback means the doors
    are NOT sharing a record.
    """
    from pathlib import Path

    return str(Path(db_path or _DB_PATH).resolve().parent / "deposit_claims")


def _claim_deposit_shared(
    *, chain: str, tx_hash: str, channel_id: str, amount_cents: int, db_path: str = ""
) -> dict[str, Any]:
    """Claim a deposit in the registry BOTH settlement doors consult. Fails closed.

    Prefer the canonical web ``on_chain.claim_deposit`` when that package is importable.
    Hub-only images do not ship ``web`` — fall back to :mod:`aimarket_hub.deposit_claims`
    so escrow-funded opens still work (stack-local exclusivity).
    """
    claim_deposit = _shared_on_chain("claim_deposit")
    stack = _shared_on_chain("DEPOSIT_STACK_HUB")
    fallback = _claims_fallback_dir(db_path)
    if claim_deposit is None or not stack:
        from aimarket_hub.deposit_claims import (
            DEPOSIT_STACK_HUB as _LOCAL_STACK,
            claim_deposit as _local_claim,
        )

        logger.warning(
            "web on_chain deposit registry unavailable — using hub-local claims at %s",
            fallback,
        )
        return _local_claim(
            chain=chain, tx_hash=tx_hash, stack=_LOCAL_STACK,
            claim_id=channel_id, amount_cents=amount_cents,
            fallback_dir=fallback,
        )
    try:
        return claim_deposit(
            chain=chain, tx_hash=tx_hash, stack=stack,
            claim_id=channel_id, amount_cents=amount_cents,
            fallback_dir=fallback,
        )
    except Exception as exc:
        logger.error("shared deposit claim raised for %s: %s", tx_hash[:12], exc)
        return {"ok": False, "error": "deposit_registry_unavailable"}


def _release_deposit_shared(
    *, chain: str, tx_hash: str, channel_id: str, db_path: str = ""
) -> None:
    """Hand a claim back when the channel it was taken for is not created.

    Best-effort by design: a claim left behind blocks one deposit's re-use, which is
    a stuck deposit, not a double credit — so a failure here must never turn into a
    failed open. It is logged so a leak is visible.
    """
    fallback = _claims_fallback_dir(db_path)
    try:
        release = _shared_on_chain("release_deposit_claim")
        stack = _shared_on_chain("DEPOSIT_STACK_HUB")
        if release is None or not stack:
            from aimarket_hub.deposit_claims import (
                DEPOSIT_STACK_HUB as _LOCAL_STACK,
                release_deposit_claim as _local_release,
            )

            _local_release(
                chain=chain, tx_hash=tx_hash, stack=_LOCAL_STACK,
                claim_id=channel_id, fallback_dir=fallback,
            )
            return
        release(
            chain=chain, tx_hash=tx_hash, stack=stack,
            claim_id=channel_id, fallback_dir=fallback,
        )
    except Exception as exc:
        logger.error(
            "could not release shared deposit claim for %s (%s) — the deposit stays "
            "blocked until an operator clears it", tx_hash[:12], exc,
        )



def _payout_tx_shape_error(tx: str, chain: str) -> str:
    """Why ``tx`` cannot be a payout transaction id on ``chain`` — "" if it can.

    Not a chain lookup (this module never talks to an RPC) — a SHAPE check, so an
    operator attestation always points at something an auditor can actually resolve.
    Accepting any non-empty string meant "done" cleared a real depositor debt.
    """
    if _normalize_chain(chain) in _EVM_PAYOUT_CHAINS:
        body = tx[2:] if tx[:2].lower() == "0x" else ""
        if len(body) != 64 or not all(c in _HEX_DIGITS for c in body):
            return (
                "payout_tx_hash must be a 0x-prefixed 32-byte hex transaction hash "
                f"on {chain} — refusing to mark a debt paid against an unresolvable id"
            )
        return ""
    # Non-EVM (base58 Solana signatures are 86-88 chars); accept only something long
    # enough to be a real id rather than a word.
    if len(tx) < 32:
        return (
            "payout_tx_hash does not look like a transaction id — refusing to mark a "
            "debt paid against an unresolvable id"
        )
    return ""


def _admin_token() -> str:
    """The operator token, read dynamically so it is monkeypatchable (hub convention)."""
    return os.environ.get("AIMARKET_ADMIN_TOKEN", "").strip()


def _require_operator(operator_token: str, action: str) -> None:
    """Raise PermissionError unless ``operator_token`` is the configured admin token.

    The obligation surface is not merely privileged, it is the operator's LIABILITY
    LEDGER: the readers return depositor wallets, owed amounts and deposit tx hashes,
    and the writer flips a real debt to 'paid'. The HTTP routes gate it, but the
    module-level exports are importable by any in-process plugin, so gating only the
    routes would leave the export as the weak spot. Fail CLOSED: with no admin token
    configured there is nothing to authenticate against, so nobody is authorised.
    """
    expected = _admin_token()
    if not expected:
        raise PermissionError(
            f"{action} is unavailable: AIMARKET_ADMIN_TOKEN is not configured, so the "
            "operator obligation ledger cannot authenticate anyone"
        )
    if not hmac.compare_digest((operator_token or "").strip(), expected):
        raise PermissionError(f"{action} requires the operator (admin) token")


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int env knob, ignoring garbage (keeps the default)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r, using %d", name, raw, default)
        return default
    return value if value >= 0 else default


def _anon_opens_per_hour() -> int:
    return _env_int("AIMARKET_CHANNEL_ANON_OPENS_PER_HOUR", _DEFAULT_ANON_OPENS_PER_HOUR)


def _anon_closes_per_hour() -> int:
    return _env_int("AIMARKET_CHANNEL_ANON_CLOSES_PER_HOUR", _DEFAULT_ANON_CLOSES_PER_HOUR)


def _hold_reap_after_secs() -> int:
    return _env_int("AIMARKET_CHANNEL_HOLD_REAP_AFTER_SECS", _DEFAULT_HOLD_REAP_AFTER_SECS)


def _settlements_db_path() -> str:
    """Where `verified_settlements` (the hub index DB) lives.

    The ledger keeps its own database file, so the orphaned-hold reaper has to look
    across at the hub DB to tell an ABANDONED hold from one a live verification still
    owns. Same resolution order the hub itself uses (HubConfig.db_path).
    """
    explicit = os.environ.get("AIMARKET_VERIFY_SETTLEMENTS_DB_PATH", "").strip()
    if explicit:
        return explicit
    return os.environ.get("AIMARKET_DB_PATH", "").strip() or "data/hub.db"


def _is_evm_address(value: str) -> bool:
    # The "0x" prefix carries no checksum information, so an address written "0X…" is
    # the same address. Requiring the exact lowercase prefix made it read as an opaque
    # non-EVM handle: the owner was then denied by _wallet_matches and, on the deposit
    # path, refused as a chain with no proof-of-control scheme.
    s = (value or "").strip()
    if len(s) != 42 or s[:2].lower() != "0x":
        return False
    try:
        int(s[2:], 16)
    except ValueError:
        return False
    return True


def _wallet_matches(a: str, b: str) -> bool:
    """Wallet equality for authorization / deposit binding.

    EVM addresses are case-insensitive — the mixed case of EIP-55 is a checksum, not
    identity — so the lowercase form of an address must not read as a different (or
    unauthorized) wallet. Anything else (base58 Solana ids, opaque handles) compares
    exactly, because case IS significant there.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if _is_evm_address(a) and _is_evm_address(b):
        return a.lower() == b.lower()
    return a == b


def _dollars_to_cents(usd: float) -> int:
    return round(usd * _CENTS_PER_USD)


def _dollars_to_cents_bill(usd: float) -> int:
    """Cents to DEBIT for a positively-priced invoke.

    Rounds UP to the smallest chargeable unit so a sub-cent price (e.g. a
    $0.004 capability) still bills 1 cent instead of debiting nothing and
    serving the paid invoke for free (BILLING-001). The ``round(.., 6)`` first
    strips binary-float noise (0.35 * 100 == 34.999999999999996) so exact-cent
    prices are not pushed up a whole cent.
    """
    if usd <= 0:
        return 0
    return max(1, math.ceil(round(usd * _CENTS_PER_USD, 6)))


def _cents_to_dollars(cents: int) -> float:
    return cents / _CENTS_PER_USD


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _expiry_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + _CHANNEL_EXPIRY_SECS))


def _hash_secret(secret: str) -> str:
    return hashlib.sha256((secret or "").encode()).hexdigest()


def _parse_ts(value: Any) -> float | None:
    """Epoch seconds for a stored timestamp, or None if unparseable.

    Rows written by this module use ``%Y-%m-%dT%H:%M:%SZ``; rows written by a column
    DEFAULT use SQLite's ``%Y-%m-%d %H:%M:%S``. Both are UTC, and the reaper must be
    able to age EITHER — comparing the two formats as text silently mis-orders them
    (' ' sorts before 'T'), which would age a fresh hold out immediately.
    """
    s = str(value or "").strip()
    if len(s) < 19:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return calendar.timegm(time.strptime(s[:19], fmt))
        except ValueError:
            continue
    return None


# ── SQLite Channel Ledger ────────────────────────────────────────

class ChannelLedger:
    """Payment channel ledger — SQLite or PostgreSQL via DATABASE_URL.

    All amounts stored as integer cents. Public API accepts float USD
    and returns float USD for backward compatibility.
    """

    def __init__(self, db_path: str = "", database_url: str = ""):
        from aimarket_hub.db_backend import create_backend
        from aimarket_hub.migrations import Migrations

        self._db_path = db_path or _DB_PATH
        self._backend = create_backend(
            database_url=database_url,
            db_path=self._db_path,
        )
        self._lock = threading.Lock()
        self._rate_state: dict[str, list[float]] = {}  # wallet -> [open_timestamps]
        self._close_rate: dict[str, list[float]] = {}   # wallet -> [close_timestamps]
        self._max_channels = _DEFAULT_MAX_CHANNELS
        self._sweep_thread: threading.Thread | None = None
        self._running = False
        # The ledger owns a SEPARATE database file, so it applies only the migrations
        # that touch channel-ledger tables. `subsystem=` derives that set from the
        # registered DDL (migrations.channel_ledger_versions) instead of a hand-written
        # target_version: the old `target_version=14` + "incl. ..." comment would have
        # silently skipped every later channels migration the moment one was added.
        Migrations(self._backend).apply(subsystem="channels")
        self._start_sweep()

    # ── Database ──────────────────────────────────────────────────

    def _init_db(self) -> None:
        pass  # Handled by Migrations in __init__

    def _legacy_init_db(self) -> None:
        self._backend.dir.mkdir(parents=True, exist_ok=True) if hasattr(self._backend, 'dir') else None
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    balance_cents INTEGER NOT NULL,
                    original_deposit_cents INTEGER NOT NULL,
                    used_cents INTEGER NOT NULL DEFAULT 0,
                    token TEXT NOT NULL DEFAULT 'USDT',
                    chain TEXT NOT NULL DEFAULT 'base',
                    wallet TEXT NOT NULL DEFAULT '',
                    tx_hash TEXT NOT NULL DEFAULT '',
                    recipient TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    opened_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    settle_tx_hash TEXT NOT NULL DEFAULT '',
                    closed_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS debited_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_channels_status
                ON channels(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_channels_wallet
                ON channels(wallet)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_channels_expires
                ON channels(expires_at) WHERE status = 'open'
            """)
            conn.commit()

    def _get_conn(self):
        """Return a context-managed DB connection (SQLite or PG)."""
        return self._backend.get_connection()

    # ── Rate limiting ─────────────────────────────────────────────

    def _check_rate(self, wallet: str, rate_store: dict[str, list[float]],
                    max_per_hour: int, anon_max_per_hour: int | None = None) -> bool:
        """Return True if within rate limit, False if exceeded.

        Wallet-less callers are NOT exempt (they used to be, which made a public hub
        unlimited to anyone who simply omitted the wallet field): they share one bucket
        with its own, larger, configurable cap. Note the window is per-process — with N
        workers the effective cap is N x max_per_hour.

        The bucket table is bounded, and a FULL table evicts instead of refusing. The
        previous "fail closed when full" turned a memory DoS into a total availability
        outage: the key is caller-supplied and is charged before any verification, so
        ~50k cheap requests carrying distinct made-up wallet strings filled the table
        and then EVERY new wallet — every real depositor — was refused both open and
        close until the buckets aged out an hour later. Evicting the
        least-recently-active bucket is the right trade: the worst an attacker buys by
        forcing eviction is a reset window for a wallet that had *almost* stopped
        transacting, and the real cap on flooding lives at the HTTP layer (per-IP,
        api.py) where the identity is not attacker-chosen.
        """
        key = (wallet or "").strip() or _ANON_RATE_KEY
        if key == _ANON_RATE_KEY and anon_max_per_hour is not None:
            limit = anon_max_per_hour   # 0 means "no allowance", not "no limit"
        else:
            limit = max_per_hour
        if limit <= 0:
            return False  # explicitly disabled → refuse rather than allow
        now = time.time()
        window = now - 3600
        hits = [t for t in rate_store.get(key, ()) if t > window]
        if len(hits) >= limit:
            rate_store[key] = hits
            return False
        hits.append(now)
        rate_store[key] = hits
        if len(rate_store) > _MAX_RATE_TRACKED_KEYS:
            self._evict_rate_store(rate_store, window, keep=key)
        return True

    @staticmethod
    def _prune_rate_store(rate_store: dict[str, list[float]], window: float) -> None:
        """Drop buckets whose newest hit fell out of the window (they hold no state)."""
        stale = [k for k, hits in rate_store.items() if not hits or max(hits) <= window]
        for k in stale:
            del rate_store[k]

    @classmethod
    def _evict_rate_store(
        cls, rate_store: dict[str, list[float]], window: float, *, keep: str,
    ) -> None:
        """Bound the table in place: expired buckets first, then least-recently-active.

        Expired buckets carry no state, so they go for free. Dropping only those is not
        a bound though — enough distinct keys inside one window keeps every bucket live
        — so anything still over the cap is evicted LRU. ``keep`` is the bucket just
        charged; evicting it would hand this very caller a fresh window.
        """
        # `keep` was charged microseconds ago, so pruning can never drop it.
        cls._prune_rate_store(rate_store, window)
        excess = len(rate_store) - _MAX_RATE_TRACKED_KEYS
        if excess <= 0:
            return
        by_age = sorted(
            (k for k in rate_store if k != keep and k != _ANON_RATE_KEY),
            key=lambda k: max(rate_store[k]) if rate_store[k] else 0.0,
        )
        for k in by_age[:excess]:
            del rate_store[k]
        if len(rate_store) > _MAX_RATE_TRACKED_KEYS:
            logger.error(
                "Rate-limit table still over cap after eviction (%d keys) — the shared "
                "anonymous bucket and the active caller are never evicted",
                len(rate_store),
            )

    # ── Background sweep ──────────────────────────────────────────

    def _start_sweep(self) -> None:
        """Start a background thread that sweeps expired channels."""
        self._running = True
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()

    def _sweep_loop(self) -> None:
        while self._running:
            time.sleep(_SWEEP_INTERVAL_SECS)
            try:
                self._sweep_expired()
            except Exception as exc:
                logger.error("Channel sweep error: %s", exc)
            try:
                self.reap_stale_holds()
            except Exception as exc:
                logger.error("Channel hold-reaper error: %s", exc)

    def _sweep_expired(self) -> int:
        """Expire stale open channels, recording the unspent remainder as a payout
        obligation. Returns count.

        NOTE: nothing is transferred here — the deposit was paid to the platform's own
        settlement wallet, so the remainder is a DEBT to the depositor, not a refund
        this process can perform. The previous log line claimed "%d cents refunded",
        and the number it printed was balance + used (i.e. the whole deposit, including
        what the buyer actually spent).
        """
        now = _now_iso()
        with self._lock, self._get_conn() as conn:
            # Defer expiry of channels that still have a held Pay-on-Verified hold:
            # the reserved cents are in neither balance nor used, so expiring now
            # would strand them. The verified-settlement worker always resolves the
            # hold eventually (capture/release, or the reaper below), and a later
            # sweep pass then expires the channel with correct accounting. Mirrors
            # close()'s pending-holds guard.
            rows = conn.execute(
                "SELECT * FROM channels WHERE status = 'open' AND expires_at < ? "
                "AND channel_id NOT IN "
                "(SELECT channel_id FROM channel_holds WHERE status = 'held')",
                (now,),
            ).fetchall()

            count = 0
            for row in rows:
                conn.execute(
                    "UPDATE channels SET status = 'expired', closed_at = ? "
                    "WHERE channel_id = ?",
                    (now, row["channel_id"]),
                )
                obligation = self._record_payout_obligation(
                    conn, row, kind="expiry_remainder", now=now,
                )
                count += 1
                logger.info(
                    "Channel %s expired — %d cents unspent, %s (no funds moved)",
                    row["channel_id"], row["balance_cents"],
                    (
                        "recorded as payout obligation owed to %s"
                        % (row["wallet"][:10] or "an unidentified depositor")
                        if obligation else "nothing owed"
                    ),
                )

            if count:
                conn.commit()
                logger.info("Swept %d expired channels", count)
            return count

    # ── Payout obligations (honest accounting, never a transfer) ───

    def _record_payout_obligation(
        self, conn: Any, row: Any, *, kind: str, now: str,
    ) -> dict[str, Any] | None:
        """Record the channel's unspent remainder as an explicit debt to the depositor.

        Returns the obligation dict, or None when nothing is owed. Idempotent: the
        table is keyed by channel_id, so a re-run (close after a crashed close, or a
        second sweep pass) cannot double-book the same remainder.

        Only balance_cents is owed — used_cents was genuinely spent on delivered
        invokes, and held cents belong to an unresolved hold (callers guarantee there
        are none at this point).

        Escrow-funded channels record NOTHING. The debt exists because on the transfer
        path the deposit landed in the platform's settlement wallet, so the operator
        physically holds money that is not theirs. An escrow channel is the opposite:
        the deposit never leaves AIMarketEscrow, and `settleChannel` pays the remainder
        straight back to the depositor from the contract. Booking a debt there invents
        one — the live hub was publishing `outstanding_obligations_usd: 3.84` as its
        public solvency figure while all four remainders had already been returned
        on-chain (two Settled, two Refunded). Real debt was zero.
        """
        remainder = int(row["balance_cents"] or 0)
        if remainder <= 0:
            return None
        if str(row["escrow_channel"] or "").strip():
            logger.info(
                "channel %s is escrow-funded — the contract returns the remainder, "
                "no operator obligation recorded", row["channel_id"],
            )
            return None
        channel_id = row["channel_id"]
        existing = conn.execute(
            "SELECT * FROM channel_payout_obligations WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if existing:
            return self._obligation_dict(existing)
        try:
            conn.execute(
                "INSERT INTO channel_payout_obligations "
                "(channel_id, wallet, chain, token, amount_cents, kind, status, "
                " deposit_tx_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'owed', ?, ?)",
                (channel_id, row["wallet"], row["chain"], row["token"],
                 remainder, kind, row["tx_hash"], now),
            )
        except Exception as exc:  # PK violation → a concurrent writer booked it first
            msg = str(exc).lower()
            if not any(k in msg for k in ("unique", "constraint", "duplicate")):
                raise
            existing = conn.execute(
                "SELECT * FROM channel_payout_obligations WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            return self._obligation_dict(existing) if existing else None
        if not row["wallet"]:
            # An unbound (stub/demo-only) channel leaves a debt with no payee. Loud on
            # purpose: in production every channel is bound to its on-chain payer.
            logger.warning(
                "Channel %s owes %d cents but carries no wallet — obligation has no payee",
                channel_id, remainder,
            )
        return {
            "channel_id": channel_id,
            "wallet": row["wallet"],
            "chain": row["chain"],
            "token": row["token"],
            "amount_usd": _cents_to_dollars(remainder),
            "amount_cents": remainder,
            "kind": kind,
            "status": "owed",
            "created_at": now,
            "payout_tx_hash": "",
            "settled_at": "",
        }

    @staticmethod
    def _obligation_dict(row: Any) -> dict[str, Any]:
        return {
            "channel_id": row["channel_id"],
            "wallet": row["wallet"],
            "chain": row["chain"],
            "token": row["token"],
            "amount_usd": _cents_to_dollars(row["amount_cents"]),
            "amount_cents": row["amount_cents"],
            "kind": row["kind"],
            "status": row["status"],
            "created_at": row["created_at"],
            "payout_tx_hash": row["payout_tx_hash"],
            "settled_at": row["settled_at"],
        }

    def obligations(self, status: str = "owed", limit: int = 500) -> list[dict[str, Any]]:
        """Outstanding (or, with status='', all) payout obligations, newest first."""
        limit = max(1, min(int(limit or 1), 5_000))
        with self._get_conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM channel_payout_obligations WHERE status = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM channel_payout_obligations "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._obligation_dict(r) for r in rows]

    def obligations_total(self, status: str = "owed") -> dict[str, Any]:
        """Count + total of payout obligations in a given state (default: still owed)."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents "
                "FROM channel_payout_obligations WHERE status = ?",
                (status,),
            ).fetchone()
        cents = int(row["cents"] or 0)
        return {
            "status": status,
            "count": int(row["n"] or 0),
            "total_cents": cents,
            "total_usd": _cents_to_dollars(cents),
        }

    def mark_obligation_paid(self, channel_id: str, payout_tx_hash: str) -> dict[str, Any]:
        """Record that the operator has paid an obligation OUT-OF-BAND.

        This is an operator attestation, not a transfer and not an on-chain proof: this
        module never moves funds. A tx hash is REQUIRED, and it must be SHAPED like a
        real transaction id for the obligation's own chain — the previous check was
        "non-empty string", so `"paid"` cleared a depositor's debt and left an audit
        trail that can never be checked against any chain.
        """
        tx = (payout_tx_hash or "").strip()
        if not tx:
            return {"error": "payout_tx_hash is required to mark an obligation paid"}
        now = _now_iso()
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channel_payout_obligations WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            if not row or row["status"] != "owed":
                return {"error": "no outstanding obligation for this channel"}
            shape_error = _payout_tx_shape_error(tx, row["chain"])
            if shape_error:
                return {"error": shape_error}
            cur = conn.execute(
                "UPDATE channel_payout_obligations SET status = 'paid', "
                "payout_tx_hash = ?, settled_at = ? "
                "WHERE channel_id = ? AND status = 'owed'",
                (tx, now, channel_id),
            )
            if getattr(cur, "rowcount", 0) != 1:
                # A concurrent writer settled it between the SELECT and the UPDATE.
                return {"error": "no outstanding obligation for this channel"}
            conn.commit()
        logger.warning(
            "Obligation on channel %s marked PAID out-of-band by the operator "
            "(payout tx %s) — attestation only, no funds moved by this process",
            channel_id, tx[:14],
        )
        return {"ok": True, "channel_id": channel_id, "payout_tx_hash": tx, "settled_at": now}

    # ── Orphaned-hold reaper ──────────────────────────────────────

    def _unresolved_settlement_nonces(self, receipt_ids: list[str]) -> set[str] | None:
        """Which of these hold receipts a live Pay-on-Verified verification still owns.

        Returns None when ownership cannot be determined — the caller must then reap
        NOTHING: releasing a hold a running verification later captures would let the
        same cents be spent twice (capture only adds to used_cents, it does not
        re-check the balance).

        The holds live in the ledger DB while `verified_settlements` lives in the hub
        index DB, so on SQLite this is a read-only cross-file lookup.
        """
        if not receipt_ids:
            return set()

        def _query(conn: Any) -> set[str]:
            found: set[str] = set()
            for i in range(0, len(receipt_ids), 200):
                chunk = receipt_ids[i:i + 200]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT nonce FROM verified_settlements "
                    f"WHERE status IN ('pending', 'verifying') AND nonce IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                found.update(r["nonce"] for r in rows)
            return found

        backend_type = getattr(self._backend, "backend_type", "sqlite")
        target = _settlements_db_path()
        own_path = str(getattr(self._backend, "db_path", "") or "")
        try:
            if backend_type != "sqlite" or (own_path and os.path.abspath(own_path) == os.path.abspath(target)):
                # Same database (PostgreSQL, or the hub and ledger sharing one file):
                # our own connection can see verified_settlements.
                with self._get_conn() as conn:
                    return _query(conn)
            conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=10)
            try:
                conn.row_factory = sqlite3.Row
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'verified_settlements'"
                ).fetchone()
                if not exists:
                    logger.error(
                        "Hold reaper: no verified_settlements table in %s — refusing to "
                        "release holds whose ownership cannot be checked", target,
                    )
                    return None
                return _query(conn)
            finally:
                conn.close()
        except Exception as exc:
            logger.error(
                "Hold reaper: cannot read verified_settlements from %s (%s) — "
                "refusing to release any hold", target, exc,
            )
            return None

    def reap_stale_holds(self, max_age_secs: float | None = None) -> dict[str, Any]:
        """Release holds stuck in 'held' with no live verification behind them.

        hold() commits to the ledger DB while the Pay-on-Verified row is committed to a
        different database; a hard kill between the two leaves a 'held' hold no
        reconciler will ever resolve, and close() refuses while a hold is pending — the
        buyer's balance is frozen forever. This reaper is the missing bound.

        Safety rules:
          * only holds older than the configured age (0 disables the reaper entirely);
          * only holds NOT owned by a pending/verifying settlement row — and if that
            cannot be read, nothing is reaped (fail closed);
          * never a hold whose receipt already appears in debited_receipts (a capture is
            partially recorded; crediting the balance back would double-count it);
          * bounded work per pass, and idempotent: the status flip is row-count guarded,
            so a concurrent capture_hold/release_hold wins and the reaper skips.

        Releasing returns the cents to the channel balance (buyer-safe direction) — no
        funds move, and the buyer can then close and be owed the remainder as usual.
        """
        age = float(_hold_reap_after_secs() if max_age_secs is None else max_age_secs)
        if age <= 0:
            return {"reaped": 0, "released_usd": 0.0, "skipped": 0, "disabled": True}

        cutoff = time.time() - age
        with self._get_conn() as conn:
            held = conn.execute(
                "SELECT receipt_id, channel_id, amount_cents, created_at "
                "FROM channel_holds WHERE status = 'held' "
                "ORDER BY created_at LIMIT ?",
                (_HOLD_REAP_SCAN_LIMIT,),
            ).fetchall()

        candidates = []
        undatable = 0
        for row in held:
            created = _parse_ts(row["created_at"])
            if created is None:
                # Cannot age it → cannot prove it is abandoned → leave it alone.
                undatable += 1
                continue
            if created <= cutoff:
                candidates.append(row)

        if not candidates:
            return {"reaped": 0, "released_usd": 0.0, "skipped": undatable, "scanned": len(held)}

        owned = self._unresolved_settlement_nonces([r["receipt_id"] for r in candidates])
        if owned is None:
            return {
                "reaped": 0, "released_usd": 0.0, "skipped": len(candidates) + undatable,
                "scanned": len(held),
                "error": "settlement ownership unavailable — refusing to release holds",
            }

        now = _now_iso()
        reaped = 0
        released_cents = 0
        skipped = undatable
        inconsistent = 0
        with self._lock, self._get_conn() as conn:
            for row in candidates:
                receipt_id = row["receipt_id"]
                if receipt_id in owned:
                    skipped += 1
                    continue
                captured = conn.execute(
                    "SELECT 1 FROM debited_receipts WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if captured:
                    # A capture is (at least partially) recorded for this receipt.
                    # Returning the cents to the balance would credit the buyer for an
                    # invoke already billed — refuse and surface it for the operator.
                    inconsistent += 1
                    logger.error(
                        "Hold %s is 'held' but already has a debit receipt — refusing to "
                        "release; ledger needs operator reconciliation", receipt_id,
                    )
                    continue
                channel = conn.execute(
                    "SELECT status FROM channels WHERE channel_id = ?",
                    (row["channel_id"],),
                ).fetchone()
                if not channel or channel["status"] != "open":
                    # A hold is only ever created on an open channel, and close/sweep
                    # both refuse while one is held — so this is an inconsistency, not a
                    # routine case. Crediting a closed channel's balance would resurrect
                    # money after settlement, so refuse.
                    inconsistent += 1
                    logger.error(
                        "Hold %s belongs to channel %s in state %s — refusing to release",
                        receipt_id, row["channel_id"],
                        channel["status"] if channel else "missing",
                    )
                    continue
                note = f"reaped: no live verification after {int(age)}s"
                cur = conn.execute(
                    "UPDATE channel_holds SET status = 'reaped', resolved_at = ?, "
                    "resolution_note = ? WHERE receipt_id = ? AND status = 'held'",
                    (now, note, receipt_id),
                )
                if getattr(cur, "rowcount", 0) != 1:
                    # capture_hold/release_hold resolved it between the scan and now.
                    skipped += 1
                    continue
                conn.execute(
                    "UPDATE channels SET balance_cents = balance_cents + ? "
                    "WHERE channel_id = ?",
                    (row["amount_cents"], row["channel_id"]),
                )
                reaped += 1
                released_cents += int(row["amount_cents"] or 0)
                logger.warning(
                    "Reaped orphaned hold %s on channel %s — %d cents returned to "
                    "balance (%s)", receipt_id, row["channel_id"],
                    row["amount_cents"], note,
                )
            if reaped:
                conn.commit()

        result = {
            "reaped": reaped,
            "released_usd": _cents_to_dollars(released_cents),
            "skipped": skipped,
            "scanned": len(held),
        }
        if inconsistent:
            result["inconsistent"] = inconsistent
        return result

    def stop_sweep(self) -> None:
        self._running = False

    # ── Channel operations ────────────────────────────────────────

    def open(
        self,
        deposit_usd: float,
        token: str = "",
        chain: str = "",
        wallet: str = "",
        tx_hash: str = "",
        with_secret: bool = False,
        payer_signature: str = "",
        escrow_channel_id: str = "",
    ) -> dict[str, Any]:
        """Open a pre-funded payment channel.

        with_secret: mint a one-time debit secret (returned ONCE as `channel_secret`); debit
            then requires it. The public entry point (open_channel) defaults this ON so every
            production/HTTP-opened channel is secured; the low-level ledger primitive defaults
            OFF so trusted internal callers/tests aren't forced through the secret.
        payer_signature: EIP-191 signature by the PAYING wallet over
            payer_proof_challenge(payer=..., tx_hash=..., chain=...). Required on the
            on-chain-verified path: without it the channel_secret goes to whoever quotes
            the (public) deposit tx first. See the module docstring.
        escrow_channel_id: an AIMarketEscrow channel that already holds the funds. Only
            honoured when the (opt-in, default-off) escrow bridge is enabled. It replaces
            the transfer-hash path entirely: the contract records the depositor, so there
            is no public tx hash for a bystander to quote and no EIP-191 proof to collect,
            and the remainder is refundable BY THE CONTRACT instead of becoming an
            operator payout obligation.
        """
        # NaN/inf first: `nan <= 0` and `nan > 10_000` are BOTH False, so a non-finite
        # deposit slipped through the range check and blew up in round(nan * 100) —
        # a 500 from the HTTP endpoint instead of a 400.
        try:
            deposit_usd = float(deposit_usd)
        except (TypeError, ValueError):
            return {"error": "deposit must be a number"}
        if not math.isfinite(deposit_usd):
            return {"error": "deposit must be a finite number"}
        if deposit_usd <= 0 or deposit_usd > 10_000:
            return {"error": "deposit must be between 0 and 10,000 USD"}

        if not self._check_rate(
            wallet, self._rate_state, _MAX_OPENS_PER_WALLET_PER_HOUR,
            _anon_opens_per_hour(),
        ):
            return {
                "error": (
                    f"rate limit exceeded — max {_MAX_OPENS_PER_WALLET_PER_HOUR} channel "
                    "opens per hour per wallet"
                )
            }

        deposit_cents = _dollars_to_cents(deposit_usd)
        token = token or _DEFAULT_TOKEN
        chain = chain or _DEFAULT_CHAIN

        # ── Escrow-backed funding (opt-in bridge) ────────────────────────────
        # Verified by READING the contract rather than by trusting a transfer hash, so
        # the depositor is bound by construction. Deliberately an early, separate branch:
        # it must never fall through into the transfer path, and the transfer path must be
        # byte-for-byte unchanged for every hub that has not enabled the bridge.
        escrow_channel_id = (escrow_channel_id or "").strip()
        if escrow_channel_id:
            ok, err, claim_id = self._verify_escrow_funding(
                escrow_channel_id=escrow_channel_id,
                wallet=wallet,
                deposit_usd=deposit_usd,
            )
            if not ok:
                return {"error": err}
            return self._insert_channel(
                deposit_cents=deposit_cents, token=token, chain=chain, wallet=wallet,
                tx_hash="", with_secret=with_secret, escrow_channel=escrow_channel_id,
                claim_identifier=claim_id, deposit_usd=deposit_usd,
            )

        # On-chain verification (fail-closed). In stub mode (dev) any tx_hash is
        # accepted — unchanged. With stub OFF and AIFACTORY_PROD=1 we require a
        # real, verified deposit transaction before crediting the channel: the
        # tx must pay the configured recipient the expected amount/token with
        # enough confirmations. If no verifier is reachable, we reject rather
        # than silently credit an unverified tx (PAYAUTH-001).
        # Whether this channel is being funded by a real, on-chain-verified deposit.
        # Only such deposits are single-use-guarded (see consumed_deposits claim
        # below): stub/demo channels carry no real tx to replay.
        verified_onchain = False
        if not _VERIFY_STUB:
            if _is_production_mode():
                result = _verify_tx_onchain(
                    tx_hash=tx_hash,
                    amount_usd=deposit_usd,
                    chain=chain,
                    token=token,
                )
                if not result.get("ok"):
                    logger.warning(
                        "Rejected channel open for wallet %s: %s",
                        (wallet[:10] or "anonymous"),
                        result.get("error"),
                    )
                    return {"error": result.get("error", "on-chain verification failed")}

                # Bind the deposit to the wallet that ACTUALLY paid (PAYAUTH-003).
                # Every deposit lands in the same platform settlement wallet, so
                # recipient+amount proves only that *someone* paid: without this check
                # anyone watching inbound transfers could quote a stranger's tx hash and
                # have the deposit credited to their own channel.
                payer = str(result.get("sender") or "").strip()
                if not payer:
                    logger.error(
                        "Rejected channel open: verifier confirmed tx %s but reported no "
                        "sender — deposit cannot be bound to a payer",
                        (tx_hash or "")[:12],
                    )
                    return {
                        "error": (
                            "on-chain verification did not report the paying wallet — "
                            "refusing to credit a deposit that cannot be bound to its payer"
                        )
                    }
                claimed = (wallet or "").strip()
                if claimed and not _wallet_matches(claimed, payer):
                    logger.warning(
                        "Rejected channel open: deposit tx %s was sent by %s, not by the "
                        "claimed wallet %s",
                        (tx_hash or "")[:12], payer[:10], claimed[:10],
                    )
                    return {
                        "error": (
                            "deposit sender does not match the claimed wallet — a channel "
                            "is credited only to the wallet that paid on-chain"
                        )
                    }
                # Proof of CONTROL, not just of identity (PAYAUTH-003b). The payer's
                # address is public — it is printed in the transaction the claimant is
                # quoting — so "claimed == payer" is satisfied by anyone who read the
                # chain. Whoever calls open() receives the channel_secret, and the
                # invoke path authorizes debits on that secret alone, so without a
                # signature a front-runner spends the victim's whole deposit and the
                # victim is locked out by the single-use guard.
                if _allow_unproven_payer():
                    logger.warning(
                        "AIMARKET_CHANNEL_ALLOW_UNPROVEN_PAYER=1 — crediting deposit %s "
                        "to %s without proof of control (deposit front-running is OPEN)",
                        (tx_hash or "")[:12], payer[:10],
                    )
                elif not _is_evm_address(payer):
                    # No proof scheme is implemented for base58/Solana payers; refuse
                    # rather than credit an unproven deposit (mirrors uni_wallet).
                    logger.error(
                        "Rejected channel open: payer %s is not an EVM address and has "
                        "no implemented proof-of-control scheme", payer[:10],
                    )
                    return {
                        "error": (
                            "proof of control over the paying wallet is only implemented "
                            "for EVM addresses — contact the operator"
                        )
                    }
                else:
                    recovered = _recover_payer_address(
                        payer=payer, tx_hash=tx_hash, chain=chain,
                        deposit_usd=deposit_usd, signature=payer_signature,
                    )
                    if not recovered or not _wallet_matches(recovered, payer):
                        logger.warning(
                            "Rejected channel open: deposit %s claimed for payer %s "
                            "without a valid payer proof (recovered %s)",
                            (tx_hash or "")[:12], payer[:10],
                            (recovered or "none")[:10],
                        )
                        # Hand back the exact text to sign: the challenge is amount- and
                        # tx-bound, so a client that has to reconstruct it by hand gets
                        # it wrong and reads the refusal as "proofs are broken".
                        return {
                            "error": (
                                "missing or invalid payer proof — sign the challenge from "
                                "payer_proof_challenge(payer, tx_hash, chain, deposit_usd) "
                                "with the wallet that paid and resend it as payer_signature"
                            ),
                            "challenge": payer_proof_challenge(
                                payer=payer, tx_hash=tx_hash, chain=chain,
                                deposit_usd=deposit_usd,
                            ),
                        }

                # Store the on-chain payer (the verified truth). An empty claim is not a
                # bypass: the channel is bound to the payer, never left anonymous.
                wallet = payer
                verified_onchain = True
            elif not _allow_demo_credit():
                # Not production, not stub, not explicitly demo → fail closed. A misconfigured
                # public hub must never credit an unverified deposit (free channels).
                logger.warning("Rejected channel open: unverified deposit and demo credit not allowed")
                return {
                    "error": (
                        "refusing to credit channel without on-chain verification — set "
                        "AIFACTORY_PROD=1 to verify deposits, or AIMARKET_ALLOW_DEMO_CREDIT=1 "
                        "for dev/demo"
                    )
                }

        with self._lock, self._get_conn() as conn:
            # DoS check
            open_count = conn.execute(
                "SELECT COUNT(*) FROM channels WHERE status = 'open'"
            ).fetchone()[0]
            if open_count >= self._max_channels:
                return {"error": "too many open channels, try again later"}

            channel_id = f"ch_{uuid.uuid4().hex[:16]}"
            # Per-channel debit secret — returned ONCE to the owner; only its hash is stored.
            # Debit requires it, so a leaked channel id alone cannot drain the channel.
            channel_secret = secrets.token_urlsafe(24) if with_secret else ""
            secret_hash = _hash_secret(channel_secret) if with_secret else ""
            now = _now_iso()
            expires = _expiry_iso()

            # Single-use deposit guard (PAYAUTH-002). A verified on-chain deposit may
            # fund exactly one channel. Claim (chain, tx_hash) atomically before the
            # channel INSERT so one real deposit can't be replayed to mint unlimited
            # funded channels. SELECT-then-INSERT under self._lock is atomic for the
            # single-process SQLite deployment; the PRIMARY KEY guards the rare
            # multi-process/PG race (its INSERT then fails and we reject cleanly).
            if verified_onchain:
                # Canonical claim key: an EVM hash/chain differing only in case is the
                # SAME deposit, and claiming it verbatim let one transaction fund a new
                # fully-credited channel per capitalisation. The lookup stays
                # case-insensitive (not just the insert) so rows written before this fix
                # still block the replay; base58 Solana ids keep their case, where it is
                # part of the identifier.
                tx = _normalize_tx_hash(tx_hash)
                claim_chain = _normalize_chain(chain)
                if tx.startswith("0x"):
                    already = conn.execute(
                        "SELECT 1 FROM consumed_deposits "
                        "WHERE LOWER(chain) = ? AND LOWER(tx_hash) = ?",
                        (claim_chain, tx),
                    ).fetchone()
                else:
                    already = conn.execute(
                        "SELECT 1 FROM consumed_deposits "
                        "WHERE LOWER(chain) = ? AND tx_hash = ?",
                        (claim_chain, tx),
                    ).fetchone()
                if already:
                    logger.warning(
                        "Rejected channel open: deposit tx %s on %s already consumed",
                        tx[:12], chain,
                    )
                    return {"error": "deposit transaction already used to fund a channel"}

                # CROSS-STACK single-use (PAYAUTH-002b). consumed_deposits above is
                # THIS ledger's store; the factory's v1 channel door keeps its own. Each
                # enforced single-use over its own rows only, so one real transfer bought
                # a funded channel at BOTH doors — $5 paid, $10 credited. The shared
                # registry is the one record both doors write before crediting, so the
                # claim is exclusive system-wide.
                #
                # Taken BEFORE the local INSERT deliberately: every rejection below this
                # point returns without committing, and a claim taken after a local write
                # would have to unwind one of them. Nothing local is written yet here, so
                # a refusal costs nothing; the two failure paths that CAN still fire after
                # the claim (local race, unexpected error) release it explicitly.
                claim = _claim_deposit_shared(
                    chain=chain, tx_hash=tx, channel_id=channel_id,
                    amount_cents=deposit_cents, db_path=self._db_path,
                )
                if not claim.get("ok"):
                    logger.warning(
                        "Rejected channel open: deposit tx %s on %s not claimable (%s)",
                        tx[:12], chain, claim.get("error"),
                    )
                    if claim.get("error") == "deposit_registry_unavailable":
                        return {
                            "error": (
                                "shared deposit registry unavailable — refusing to credit "
                                "a channel that cannot be made exclusive across settlement "
                                "doors (set AIMARKET_DEPOSIT_CLAIMS_DIR)"
                            )
                        }
                    return {"error": "deposit transaction already used to fund a channel"}

                try:
                    conn.execute(
                        "INSERT INTO consumed_deposits "
                        "(chain, tx_hash, channel_id, amount_cents, consumed_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (chain, tx, channel_id, deposit_cents, now),
                    )
                except Exception as exc:  # UNIQUE/PK violation → concurrent claim won
                    _release_deposit_shared(
                        chain=chain, tx_hash=tx, channel_id=channel_id,
                        db_path=self._db_path,
                    )
                    msg = str(exc).lower()
                    if any(k in msg for k in ("unique", "constraint", "duplicate")):
                        logger.warning(
                            "Rejected channel open: deposit tx %s on %s already consumed (race)",
                            tx[:12], chain,
                        )
                        return {"error": "deposit transaction already used to fund a channel"}
                    raise

            conn.execute(
                """INSERT INTO channels
                   (channel_id, balance_cents, original_deposit_cents, used_cents,
                    token, chain, wallet, tx_hash, recipient, status,
                    opened_at, expires_at, secret_hash)
                   VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
                (channel_id, deposit_cents, deposit_cents,
                 token, chain, wallet, tx_hash, _RECIPIENT, now, expires,
                 secret_hash),
            )
            conn.commit()

        balance_usd = _cents_to_dollars(deposit_cents)
        channel: dict[str, Any] = {
            "channel_id": channel_id,
            "balance_usd": balance_usd,
            "original_deposit_usd": balance_usd,
            "used_usd": 0.0,
            "token": token,
            "chain": chain,
            "wallet": wallet,
            "tx_hash": tx_hash,
            "recipient": _RECIPIENT,
            "verify_stub": _VERIFY_STUB,
            "status": "open",
            "opened_at": now,
            "expires_at": expires,
        }
        if channel_secret:
            # Returned ONCE — the owner must store it and present it on debit
            # (X-Payment-Channel-Secret). Never persisted in plaintext.
            channel["channel_secret"] = channel_secret
        return {"channel": channel}

    # ── Escrow-backed funding (opt-in bridge) ────────────────────────────────

    def _verify_escrow_funding(
        self, *, escrow_channel_id: str, wallet: str, deposit_usd: float
    ) -> tuple[bool, str, str]:
        """Verify an AIMarketEscrow channel backs this credit. Returns (ok, error, claim_id).

        Fails closed on every axis: the bridge being off, the module being unavailable,
        the chain being unreadable, or the contract's state not matching. None of those
        are reasons to fall back to the transfer path — a caller that supplied an escrow
        channel id is telling us where the money is, and if we cannot confirm it there we
        must not credit at all.
        """
        try:
            from aimarket_hub.escrow_bridge import config as bridge_config
            from aimarket_hub.escrow_bridge import escrow_verify
            from aimarket_hub.escrow_bridge.errors import BridgeError
        except Exception as exc:
            logger.error("escrow-backed open requested but the bridge is unavailable: %s", exc)
            return False, "escrow settlement is not available on this hub", ""

        if not bridge_config.enabled():
            return (
                False,
                "escrow-backed channels are disabled on this hub "
                "(AIMARKET_ESCROW_BRIDGE_ENABLED=0)",
                "",
            )
        try:
            funding = escrow_verify.verify_funding(
                channel_id=escrow_channel_id,
                claimed_wallet=wallet,
                deposit_usd=deposit_usd,
            )
        except BridgeError as exc:
            logger.warning(
                "Rejected escrow-backed open for %s: %s", escrow_channel_id[:14], exc
            )
            return False, str(exc), ""
        return True, "", funding.claim_id

    def _insert_channel(
        self,
        *,
        deposit_cents: int,
        token: str,
        chain: str,
        wallet: str,
        tx_hash: str,
        with_secret: bool,
        escrow_channel: str,
        claim_identifier: str,
        deposit_usd: float,
    ) -> dict[str, Any]:
        """Create the ledger row for an escrow-backed channel.

        Separate from the transfer path's tail on purpose: the two funding models differ in
        what must be claimed (an escrow channelId, not a tx hash) and in what a refund
        means (the contract's job, not an operator obligation), and entangling them is how
        one model's guard silently stops covering the other.
        """
        with self._lock, self._get_conn() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM channels WHERE status = 'open'"
            ).fetchone()[0]
            if open_count >= self._max_channels:
                return {"error": "too many open channels, try again later"}

            already = conn.execute(
                "SELECT 1 FROM channels WHERE escrow_channel = ? AND status = 'open'",
                (escrow_channel,),
            ).fetchone()
            if already:
                return {"error": "this escrow channel already backs an open payment channel"}

            channel_id = f"ch_{uuid.uuid4().hex[:16]}"
            channel_secret = secrets.token_urlsafe(24) if with_secret else ""
            secret_hash = _hash_secret(channel_secret) if with_secret else ""
            now = _now_iso()
            expires = _expiry_iso()

            # Same system-wide exclusivity the transfer path gets, keyed on the escrow
            # channelId (namespaced by the bridge so it cannot collide with a tx hash).
            claim = _claim_deposit_shared(
                chain=chain, tx_hash=claim_identifier, channel_id=channel_id,
                amount_cents=deposit_cents, db_path=self._db_path,
            )
            if not claim.get("ok"):
                if claim.get("error") == "deposit_registry_unavailable":
                    return {
                        "error": (
                            "shared deposit registry unavailable — refusing to credit a "
                            "channel that cannot be made exclusive across settlement doors"
                        )
                    }
                return {"error": "this escrow channel has already funded a payment channel"}

            try:
                conn.execute(
                    """INSERT INTO channels
                       (channel_id, balance_cents, original_deposit_cents, used_cents,
                        token, chain, wallet, tx_hash, recipient, status,
                        opened_at, expires_at, secret_hash, escrow_channel)
                       VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
                    (channel_id, deposit_cents, deposit_cents, token, chain, wallet,
                     tx_hash, _RECIPIENT, now, expires, secret_hash, escrow_channel),
                )
                conn.commit()
            except Exception:
                _release_deposit_shared(
                    chain=chain, tx_hash=claim_identifier, channel_id=channel_id,
                    db_path=self._db_path,
                )
                raise

        balance_usd = _cents_to_dollars(deposit_cents)
        channel: dict[str, Any] = {
            "channel_id": channel_id,
            "balance_usd": balance_usd,
            "original_deposit_usd": balance_usd,
            "used_usd": 0.0,
            "token": token,
            "chain": chain,
            "wallet": wallet,
            "tx_hash": "",
            "recipient": _RECIPIENT,
            "verify_stub": False,
            "status": "open",
            "opened_at": now,
            "expires_at": expires,
            "escrow_channel": escrow_channel,
            # Unlike a transfer-funded channel, the remainder here is still the
            # depositor's on chain: they refund it themselves, and no operator debt is
            # recorded at close.
            "refund_source": "escrow_contract",
        }
        if channel_secret:
            channel["channel_secret"] = channel_secret
        return {"channel": channel}

    def escrow_channel_for(self, channel_id: str) -> str:
        """The escrow channel backing a ledger channel, or "" for a transfer-funded one."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT escrow_channel FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        if not row:
            return ""
        try:
            return str(row["escrow_channel"] or "")
        except (KeyError, IndexError):  # pre-018 row shape
            return ""

    def close(
        self,
        channel_id: str,
        settle_tx_hash: str = "",
        wallet: str = "",
    ) -> dict[str, Any]:
        """Close a channel and compute settlement.

        Args:
            wallet: Must match the wallet that opened the channel. An anonymous channel
                (opened with no wallet — only reachable in stub/demo mode, since an
                on-chain-verified deposit is bound to its payer) has no owner to
                authenticate: any caller that likewise presents no wallet may close it.
                That is acceptable because closing moves no funds — it records the
                unspent remainder as an obligation to the (unknown) depositor.

        Settlement is bookkeeping only: `refund_executed_usd` is always 0.0 here and the
        remainder is reported as `refund_owed_usd` plus a durable obligation row.
        """
        if not self._check_rate(
            wallet, self._close_rate, _MAX_CLOSES_PER_WALLET_PER_HOUR,
            _anon_closes_per_hour(),
        ):
            return {
                "error": (
                    f"rate limit exceeded — max {_MAX_CLOSES_PER_WALLET_PER_HOUR} closes "
                    "per hour per wallet"
                )
            }

        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return {"error": "channel not found"}
            if row["status"] != "open":
                return {"error": f"channel is {row['status']}"}

            # Authorization: an owned channel requires its owner (case-insensitively for
            # EVM addresses — EIP-55 case is a checksum, not identity). An anonymous
            # channel has no owner to authenticate and is closable by any caller that
            # also presents no wallet; see the docstring for why that is safe. Stated
            # explicitly because the old `row["wallet"] != wallet` check silently allowed
            # exactly that while the comment claimed strangers were locked out.
            owner = (row["wallet"] or "").strip()
            claimed = (wallet or "").strip()
            authorized = _wallet_matches(owner, claimed) if owner else not claimed
            if not authorized:
                return {"error": "unauthorized: wallet does not match channel owner"}

            # Outstanding Pay-on-Verified holds must resolve (capture or release)
            # before close — the reserved cents are in neither balance nor used yet,
            # so settling now would strand them.
            pending_holds = conn.execute(
                "SELECT COUNT(*) FROM channel_holds "
                "WHERE channel_id = ? AND status = 'held'",
                (channel_id,),
            ).fetchone()[0]
            if pending_holds:
                return {
                    "error": (
                        f"channel has {pending_holds} pending verified settlement(s) — "
                        "retry close after they resolve"
                    )
                }

            now = _now_iso()
            conn.execute(
                "UPDATE channels SET status = 'settled', settle_tx_hash = ?, "
                "closed_at = ? WHERE channel_id = ?",
                (settle_tx_hash, now, channel_id),
            )
            # The remainder is a DEBT, not a transfer: the deposit was paid to the
            # platform's settlement wallet and this process never sends value.
            obligation = self._record_payout_obligation(
                conn, row, kind="close_remainder", now=now,
            )
            conn.commit()

            used = _cents_to_dollars(row["used_cents"])
            refund = _cents_to_dollars(row["balance_cents"])
            original = _cents_to_dollars(row["original_deposit_cents"])

        return {
            "settlement": {
                "channel_id": channel_id,
                "used_usd": used,
                # Kept for API back-compat: the unspent remainder. It is NOT proof of a
                # transfer — read refund_executed_usd / refund_owed_usd for that.
                "refund_usd": refund,
                "refund_executed_usd": 0.0,
                "refund_owed_usd": refund,
                "refund_status": "owed" if refund > 0 else "none",
                "obligation": obligation,
                "original_deposit_usd": original,
                "status": "settled",
                "settle_tx_hash": settle_tx_hash,
            }
        }

    def debit(
        self,
        channel_id: str,
        amount_usd: float,
        receipt_id: str = "",
        requester_wallet: str = "",
        secret: str = "",
    ) -> dict[str, Any]:
        """Deduct from a channel (called during invoke).

        Args:
            receipt_id: Unique ID for replay protection.
            requester_wallet: authenticated caller wallet (defense-in-depth; must match owner
                when supplied).
            secret: the per-channel debit secret returned at open. REQUIRED for channels that
                have one (opened after migration 007) — so a leaked channel id alone cannot
                drain the channel. Legacy channels (no stored secret) skip this check.
        """
        # Bill with a ceiling so a positive sub-cent price is never served free.
        amount_cents = _dollars_to_cents_bill(amount_usd)

        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return {"error": "channel not found"}
            if row["status"] != "open":
                return {"error": "channel not open"}

            # Authorization: a channel that carries a debit secret REQUIRES it (constant-time
            # compare) — this is the primary auth. Legacy channels (empty hash) are exempt for
            # back-compat but still get the wallet defense below.
            stored_hash = (row["secret_hash"] if "secret_hash" in row.keys() else "") or ""
            if stored_hash and not hmac.compare_digest(stored_hash, _hash_secret(secret)):
                return {"error": "unauthorized: invalid or missing channel secret"}

            # Defense-in-depth: if the caller's wallet is known, it must own the channel.
            if requester_wallet and row["wallet"] and not _wallet_matches(
                row["wallet"], requester_wallet
            ):
                return {"error": "unauthorized: wallet does not match channel owner"}

            balance_cents = row["balance_cents"]
            if amount_cents > balance_cents:
                return {
                    "error": "insufficient balance",
                    "needed": amount_usd,
                    "balance": _cents_to_dollars(balance_cents),
                }

            # Replay protection
            if receipt_id:
                existing = conn.execute(
                    "SELECT 1 FROM debited_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                ).fetchone()
                if existing:
                    return {"error": "receipt already processed (replay rejected)"}
                conn.execute(
                    "INSERT INTO debited_receipts (receipt_id, channel_id, amount_cents, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (receipt_id, channel_id, amount_cents, _now_iso()),
                )

            new_balance = balance_cents - amount_cents
            new_used = row["used_cents"] + amount_cents
            conn.execute(
                "UPDATE channels SET balance_cents = ?, used_cents = ? "
                "WHERE channel_id = ?",
                (new_balance, new_used, channel_id),
            )
            conn.commit()

            remaining = _cents_to_dollars(new_balance)

        return {"ok": True, "channel_id": channel_id, "remaining_balance": remaining}

    def hold(
        self,
        channel_id: str,
        amount_usd: float,
        receipt_id: str,
        requester_wallet: str = "",
        secret: str = "",
    ) -> dict[str, Any]:
        """Reserve funds for a Pay-on-Verified invoke (auth leg of auth/capture).

        Same authorization as debit (channel secret + wallet defense) — the buyer
        authorizes the reservation while their secret is in-request, so the later
        capture/release can run from a background worker WITHOUT the secret.
        Balance drops immediately: a pending verification can never be double-spent.
        """
        if not receipt_id:
            return {"error": "receipt_id is required for a hold"}
        amount_cents = _dollars_to_cents_bill(amount_usd)

        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return {"error": "channel not found"}
            if row["status"] != "open":
                return {"error": "channel not open"}

            stored_hash = (row["secret_hash"] if "secret_hash" in row.keys() else "") or ""
            if stored_hash and not hmac.compare_digest(stored_hash, _hash_secret(secret)):
                return {"error": "unauthorized: invalid or missing channel secret"}
            if requester_wallet and row["wallet"] and not _wallet_matches(
                row["wallet"], requester_wallet
            ):
                return {"error": "unauthorized: wallet does not match channel owner"}

            balance_cents = row["balance_cents"]
            if amount_cents > balance_cents:
                return {
                    "error": "insufficient balance",
                    "needed": amount_usd,
                    "balance": _cents_to_dollars(balance_cents),
                }

            # Replay protection — a receipt nonce may be used by at most one
            # debit OR one hold, ever.
            for table in ("debited_receipts", "channel_holds"):
                existing = conn.execute(
                    f"SELECT 1 FROM {table} WHERE receipt_id = ?", (receipt_id,)
                ).fetchone()
                if existing:
                    return {"error": "receipt already processed (replay rejected)"}

            conn.execute(
                "INSERT INTO channel_holds (receipt_id, channel_id, amount_cents, status, created_at) "
                "VALUES (?, ?, ?, 'held', ?)",
                (receipt_id, channel_id, amount_cents, _now_iso()),
            )
            new_balance = balance_cents - amount_cents
            conn.execute(
                "UPDATE channels SET balance_cents = ? WHERE channel_id = ?",
                (new_balance, channel_id),
            )
            conn.commit()
            remaining = _cents_to_dollars(new_balance)

        return {
            "ok": True,
            "channel_id": channel_id,
            "remaining_balance": remaining,
            "held_usd": _cents_to_dollars(amount_cents),
        }

    def capture_hold(self, receipt_id: str) -> dict[str, Any]:
        """Capture a hold after a passing verdict: reservation becomes a recorded debit.

        No secret needed — the buyer pre-authorized the amount at hold time. Works
        regardless of current channel status (the cents were reserved while open).
        """
        with self._lock, self._get_conn() as conn:
            hold = conn.execute(
                "SELECT * FROM channel_holds WHERE receipt_id = ? AND status = 'held'",
                (receipt_id,),
            ).fetchone()
            if not hold:
                return {"error": "hold not found or already resolved"}

            conn.execute(
                "INSERT INTO debited_receipts (receipt_id, channel_id, amount_cents, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (receipt_id, hold["channel_id"], hold["amount_cents"], _now_iso()),
            )
            conn.execute(
                "UPDATE channels SET used_cents = used_cents + ? WHERE channel_id = ?",
                (hold["amount_cents"], hold["channel_id"]),
            )
            conn.execute(
                "UPDATE channel_holds SET status = 'captured', resolved_at = ? "
                "WHERE receipt_id = ?",
                (_now_iso(), receipt_id),
            )
            conn.commit()

        return {
            "ok": True,
            "channel_id": hold["channel_id"],
            "captured_usd": _cents_to_dollars(hold["amount_cents"]),
        }

    def release_hold(self, receipt_id: str) -> dict[str, Any]:
        """Release a hold after a failing verdict: reservation returns to balance."""
        with self._lock, self._get_conn() as conn:
            hold = conn.execute(
                "SELECT * FROM channel_holds WHERE receipt_id = ? AND status = 'held'",
                (receipt_id,),
            ).fetchone()
            if not hold:
                return {"error": "hold not found or already resolved"}

            conn.execute(
                "UPDATE channels SET balance_cents = balance_cents + ? WHERE channel_id = ?",
                (hold["amount_cents"], hold["channel_id"]),
            )
            conn.execute(
                "UPDATE channel_holds SET status = 'released', resolved_at = ? "
                "WHERE receipt_id = ?",
                (_now_iso(), receipt_id),
            )
            row = conn.execute(
                "SELECT balance_cents FROM channels WHERE channel_id = ?",
                (hold["channel_id"],),
            ).fetchone()
            conn.commit()

        return {
            "ok": True,
            "channel_id": hold["channel_id"],
            "released_usd": _cents_to_dollars(hold["amount_cents"]),
            "remaining_balance": _cents_to_dollars(row["balance_cents"] if row else 0),
        }

    def refund(
        self,
        channel_id: str,
        amount_usd: float,
        requester_wallet: str = "",
        secret: str = "",
    ) -> dict[str, Any]:
        """Reverse a recorded debit back into the channel's spendable balance.

        In-ledger correction only — no funds move. Only allowed on open channels.

        Authorization mirrors debit(): a channel that carries a debit secret REQUIRES it
        (constant-time compare), and a supplied requester_wallet must own the channel.
        Without that, the only thing standing between a leaked channel id and a
        rewritten balance was the deposit cap.
        """
        try:
            amount_usd = float(amount_usd)
        except (TypeError, ValueError):
            return {"error": "refund amount must be a number"}
        if not math.isfinite(amount_usd):
            return {"error": "refund amount must be a finite number"}
        amount_cents = _dollars_to_cents(amount_usd)

        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return {"error": "channel not found"}
            if row["status"] != "open":
                return {"error": f"channel is {row['status']} — refund only allowed on open channels"}

            stored_hash = (row["secret_hash"] if "secret_hash" in row.keys() else "") or ""
            if stored_hash and not hmac.compare_digest(stored_hash, _hash_secret(secret)):
                return {"error": "unauthorized: invalid or missing channel secret"}
            if requester_wallet and row["wallet"] and not _wallet_matches(
                row["wallet"], requester_wallet
            ):
                return {"error": "unauthorized: wallet does not match channel owner"}

            # Cap at what was actually SPENT. `original - balance` (the old cap) also
            # counted cents reserved by an unresolved hold: refunding those put them back
            # in the spendable balance while the hold could still be captured, minting
            # value the deposit never covered.
            max_refund = row["used_cents"]
            amount_cents = min(amount_cents, max_refund)
            if amount_cents <= 0:
                return {"error": "refund amount would exceed recorded spend"}

            new_balance = row["balance_cents"] + amount_cents
            new_used = max(0, row["used_cents"] - amount_cents)
            conn.execute(
                "UPDATE channels SET balance_cents = ?, used_cents = ? "
                "WHERE channel_id = ?",
                (new_balance, new_used, channel_id),
            )
            conn.commit()

            remaining = _cents_to_dollars(new_balance)

        return {"ok": True, "channel_id": channel_id, "remaining_balance": remaining}

    def get(self, channel_id: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "channel_id": row["channel_id"],
                "balance_usd": _cents_to_dollars(row["balance_cents"]),
                "original_deposit_usd": _cents_to_dollars(row["original_deposit_cents"]),
                "used_usd": _cents_to_dollars(row["used_cents"]),
                "token": row["token"],
                "chain": row["chain"],
                "wallet": row["wallet"],
                "tx_hash": row["tx_hash"],
                "recipient": row["recipient"],
                "status": row["status"],
                "opened_at": row["opened_at"],
                "expires_at": row["expires_at"],
                "settle_tx_hash": row["settle_tx_hash"],
                "closed_at": row["closed_at"],
            }

    def recorded_spend_usd(self, wallet: str) -> float | None:
        """Total USD the ledger has actually debited across a wallet's channels.

        Returns None when the wallet owns no channels on record — i.e. the hub
        holds no settlement data for it. Used to ground self-bond slashing in
        hub-recorded settlement rather than a caller-supplied number.

        EVM addresses are compared case-insensitively: since deposit binding stores the
        VERIFIER's rendering of the payer (EIP-55 checksummed), an exact match against a
        self-bond that registered the same address in lower case found nothing and the
        slash route refused every real production channel with "no hub-recorded
        settlement". Case is significant for base58 ids, which stay exact.
        """
        wallet = (wallet or "").strip()
        if not wallet:
            return None
        with self._get_conn() as conn:
            if _is_evm_address(wallet):
                row = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(used_cents), 0) AS used "
                    "FROM channels WHERE LOWER(wallet) = ?",
                    (wallet.lower(),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(used_cents), 0) AS used "
                    "FROM channels WHERE wallet = ?",
                    (wallet,),
                ).fetchone()
        if not row or not row["n"]:
            return None
        return _cents_to_dollars(row["used"])

    def stats(self) -> dict[str, Any]:
        """Return aggregate statistics.

        Volume is reported for expired channels too: summing used_cents only over
        'settled' rows silently dropped everything spent through a channel that timed
        out instead of being closed, understating real settled volume.
        """
        with self._get_conn() as conn:
            open_row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(used_cents), 0) AS used "
                "FROM channels WHERE status = 'open'"
            ).fetchone()
            settled = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(used_cents), 0) AS used "
                "FROM channels WHERE status = 'settled'"
            ).fetchone()
            expired = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(used_cents), 0) AS used "
                "FROM channels WHERE status = 'expired'"
            ).fetchone()
        owed = self.obligations_total("owed")

        settled_cents = int(settled["used"] or 0)
        expired_cents = int(expired["used"] or 0)
        closed_cents = settled_cents + expired_cents
        return {
            "open_channels": int(open_row["n"] or 0),
            "open_used_cents": int(open_row["used"] or 0),
            "settled_channels": int(settled["n"] or 0),
            "settled_volume_cents": settled_cents,
            "settled_volume_usd": _cents_to_dollars(settled_cents),
            "expired_channels": int(expired["n"] or 0),
            "expired_volume_cents": expired_cents,
            "expired_volume_usd": _cents_to_dollars(expired_cents),
            "closed_volume_cents": closed_cents,
            "closed_volume_usd": _cents_to_dollars(closed_cents),
            "outstanding_obligations": owed["count"],
            "outstanding_obligations_usd": owed["total_usd"],
            "max_channels": self._max_channels,
        }


# ── Global instance ──────────────────────────────────────────────

_ledger = ChannelLedger()

# Master crypto switch (default OFF). Standalone read — same env/contract as the
# rest of the ecosystem. When off, payment channels are disabled entirely and
# capabilities are served free; signing/sandbox/manifests are unaffected.
_CRYPTO_DISABLED = {"error": "payment channels disabled by operator (crypto off — AIFACTORY_CRYPTO_ENABLED=0)"}


def _crypto_enabled() -> bool:
    return os.environ.get("AIFACTORY_CRYPTO_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def open_channel(
    deposit_usd: float,
    token: str | None = None,
    chain: str | None = None,
    wallet: str = "",
    tx_hash: str = "",
    with_secret: bool = True,
    payer_signature: str = "",
    escrow_channel_id: str = "",
) -> dict[str, Any]:
    """Open a channel from the public/HTTP entry point.

    `payer_signature` must be forwarded by the transport for on-chain deposits — it is
    what stops a front-runner from claiming a stranger's (public) deposit tx and walking
    away with the channel_secret. See ChannelLedger.open / the module docstring.
    """
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    # Public/HTTP entry point → secure-by-default: mint a debit secret so the invoke path
    # (X-Payment-Channel) can't be drained with just a leaked channel id.
    return _ledger.open(
        deposit_usd=deposit_usd,
        escrow_channel_id=escrow_channel_id,
        token=token or _DEFAULT_TOKEN,
        chain=chain or _DEFAULT_CHAIN,
        wallet=wallet,
        tx_hash=tx_hash,
        with_secret=with_secret,
        payer_signature=payer_signature,
    )


def close_channel(channel_id: str, settle_tx_hash: str = "", wallet: str = "") -> dict[str, Any]:
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    return _ledger.close(channel_id, settle_tx_hash=settle_tx_hash, wallet=wallet)


def debit_channel(
    channel_id: str, amount_usd: float, receipt_id: str = "",
    requester_wallet: str = "", secret: str = "",
) -> dict[str, Any]:
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    return _ledger.debit(
        channel_id, amount_usd, receipt_id=receipt_id,
        requester_wallet=requester_wallet, secret=secret,
    )


def refund_channel(
    channel_id: str, amount_usd: float,
    requester_wallet: str = "", secret: str = "",
) -> dict[str, Any]:
    """Reverse a recorded debit (in-ledger only, no funds move).

    Crypto-gated like debit: when the operator has payment channels switched off there
    is no ledger to correct. Authorization is delegated to the primitive (channel secret
    / owner wallet) — the export used to be an unauthenticated, ungated balance writer
    whose only limit was the deposit cap. There is no HTTP route for it: the safety-abort
    path builds its own receipt (safety_gate.refund_channel) and internal callers hold
    the channel secret.
    """
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    return _ledger.refund(
        channel_id, amount_usd,
        requester_wallet=requester_wallet, secret=secret,
    )


def hold_channel(
    channel_id: str, amount_usd: float, receipt_id: str,
    requester_wallet: str = "", secret: str = "",
) -> dict[str, Any]:
    """Reserve funds pending a Pay-on-Verified verdict (crypto-gated like debit)."""
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    return _ledger.hold(
        channel_id, amount_usd, receipt_id=receipt_id,
        requester_wallet=requester_wallet, secret=secret,
    )


def capture_hold(receipt_id: str) -> dict[str, Any]:
    """Capture a verified hold. NOT crypto-gated: an in-flight hold must resolve
    even if the operator flips the master switch off mid-settlement (mirrors
    refund_channel, which is likewise ungated)."""
    return _ledger.capture_hold(receipt_id)


def release_hold(receipt_id: str) -> dict[str, Any]:
    """Release a failed-verdict hold back to the channel balance (ungated, see capture_hold)."""
    return _ledger.release_hold(receipt_id)


def channel_stats() -> dict[str, Any]:
    return _ledger.stats()


def reap_stale_holds(max_age_secs: float | None = None) -> dict[str, Any]:
    """Release holds abandoned by a crash between the hold and its settlement row.

    Ungated like capture_hold/release_hold: an in-flight hold must be resolvable even if
    the operator flips the master switch off, otherwise the buyer's balance stays frozen.
    """
    return _ledger.reap_stale_holds(max_age_secs)


def channel_obligations(
    status: str = "owed", limit: int = 500, *, operator_token: str = "",
) -> list[dict[str, Any]]:
    """Payout obligations the operator owes depositors — OPERATOR ONLY.

    A closed/expired channel's unspent remainder is recorded here rather than
    transferred (see the module docstring); nothing in this module can pay them.
    Each row carries the depositor's wallet, what they are owed and the deposit tx
    hash, which is a map of who is holding money with the platform — so the export
    demands the operator token and raises PermissionError otherwise.
    """
    _require_operator(operator_token, "listing payout obligations")
    return _ledger.obligations(status=status, limit=limit)


def channel_obligations_total(
    status: str = "owed", *, operator_token: str = "",
) -> dict[str, Any]:
    """Count + USD total of payout obligations — OPERATOR ONLY (see channel_obligations).

    The aggregate alone leaks how much customer money the platform is sitting on, so
    it is gated with the detail view rather than left as the way around it. The
    unauthenticated summary lives on /stats/live, which reports the same total openly
    and on purpose (solvency transparency) without naming any depositor.
    """
    _require_operator(operator_token, "reading the payout obligation total")
    return _ledger.obligations_total(status=status)


def mark_obligation_paid(
    channel_id: str, payout_tx_hash: str, *, operator_token: str = "",
) -> dict[str, Any]:
    """Record an out-of-band operator payout against an obligation (attestation only).

    OPERATOR ONLY, and crypto-gated. This writes off a real debt to a real depositor
    on nothing but the operator's word; before this gate any in-process caller could
    clear the whole liability ledger. The crypto gate is deliberate too: with the
    master switch off the platform is asserting there is no payment rail at all, so
    an on-chain payout hash cannot be honest evidence — flip AIFACTORY_CRYPTO_ENABLED
    back on to settle debts recorded while it was on.
    """
    _require_operator(operator_token, "marking an obligation paid")
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    return _ledger.mark_obligation_paid(channel_id, payout_tx_hash)


def wallet_recorded_spend_usd(wallet: str) -> float | None:
    """USD the ledger has actually debited for a wallet, or None if it owns no channels.

    Grounds self-bond slashing in hub-recorded settlement: a claimed overspend can only
    be honoured up to what the hub itself observed, so a forged observed_spend cannot
    drain a bond. Read-only, so it is NOT gated by the crypto master switch.
    """
    return _ledger.recorded_spend_usd(wallet)


def channel_balance(channel_id: str) -> float | None:
    """Current USD balance of an open channel, or None if it is unknown.

    Used for a pre-authorization check before running a billable capability, so a
    depleted channel doesn't get paid upstream work done for free (the debit
    happens only after execution).
    """
    ch = _ledger.get(channel_id)
    if not ch or ch.get("status") != "open":
        return None
    return float(ch.get("balance_usd") or 0.0)


def channel_escrow_binding(channel_id: str) -> str:
    """Escrow channel backing a ledger channel, or "" when it was transfer-funded.

    Read-only, so it is NOT gated by the crypto master switch: the invoke path needs it to
    decide whether a DebitAuthorization is even applicable to this channel.
    """
    return _ledger.escrow_channel_for(channel_id)
