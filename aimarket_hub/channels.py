"""Pre-funded payment channels for off-chain capability invocation.

Implements channel/open, channel/close from Protocol v1/v2 spec.

Storage: SQLite-backed (survives process restart).
Amounts: integer cents internally (no IEEE 754 drift).
Rate limiting: per-wallet caps on open/close.
Background sweep: auto-refunds expired channels every 5 min.

In production, channel state is mirrored on-chain via AIMarketEscrow contract.
The ledger holds authoritative state; the contract holds funds in escrow.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import secrets
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
_DEFAULT_MAX_CHANNELS = 10_000
_CHANNEL_EXPIRY_SECS = 86400  # 24h
_SWEEP_INTERVAL_SECS = 300     # 5 min

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

    Returns ``{"ok": True}`` only when an on-chain verifier confirms the
    transaction paid the configured recipient the expected amount/token with
    sufficient confirmations. Returns ``{"ok": False, "error": ...}`` otherwise.

    The monorepo verifier (web.backend...on_chain.verify_tx_payment) checks
    recipient, amount, token and confirmations internally. If it is not
    reachable (standalone hub deploy with no chain access) we MUST NOT credit
    in production — we reject with "on-chain verification unavailable" so credit
    is never granted on an unverified tx.
    """
    tx = (tx_hash or "").strip()
    if not tx:
        return {"ok": False, "error": "tx_hash is required for on-chain verification"}

    try:
        from web.backend.services.ai_market_protocol.on_chain import verify_tx_payment
    except Exception as exc:  # verifier not reachable from this deployment
        logger.error(
            "On-chain verification unavailable (verifier import failed): %s", exc
        )
        return {
            "ok": False,
            "error": "on-chain verification unavailable — refusing to credit channel "
            "in production without a verified transaction",
        }

    try:
        verified = verify_tx_payment(
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
    return {"ok": True}


# ── Helpers ──────────────────────────────────────────────────────

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


# ── SQLite Channel Ledger ────────────────────────────────────────

class ChannelLedger:
    """Payment channel ledger — SQLite or PostgreSQL via DATABASE_URL.

    All amounts stored as integer cents. Public API accepts float USD
    and returns float USD for backward compatibility.
    """

    def __init__(self, db_path: str = "", database_url: str = ""):
        from aimarket_hub.db_backend import create_backend
        from aimarket_hub.migrations import Migrations

        self._backend = create_backend(
            database_url=database_url,
            db_path=db_path or _DB_PATH,
        )
        self._lock = threading.Lock()
        self._rate_state: dict[str, list[float]] = {}  # wallet -> [open_timestamps]
        self._close_rate: dict[str, list[float]] = {}   # wallet -> [close_timestamps]
        self._max_channels = _DEFAULT_MAX_CHANNELS
        self._sweep_thread: threading.Thread | None = None
        self._running = False
        Migrations(self._backend).apply(target_version=14)  # incl. 007_channel_secret, 012_consumed_deposits, 014_channel_holds
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
                    max_per_hour: int) -> bool:
        """Return True if within rate limit, False if exceeded."""
        if not wallet:
            return True  # anonymous channels not rate-limited
        now = time.time()
        window = now - 3600
        rate_store[wallet] = [t for t in rate_store.get(wallet, []) if t > window]
        if len(rate_store[wallet]) >= max_per_hour:
            return False
        rate_store[wallet].append(now)
        return True

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

    def _sweep_expired(self) -> int:
        """Auto-refund all expired open channels. Returns count."""
        now = _now_iso()
        with self._lock, self._get_conn() as conn:
            # Defer expiry of channels that still have a held Pay-on-Verified hold:
            # the reserved cents are in neither balance nor used, so expiring now
            # would strand them. The verified-settlement worker always resolves the
            # hold eventually (capture/release), and a later sweep pass then expires
            # the channel with correct accounting. Mirrors close()'s pending-holds
            # guard.
            rows = conn.execute(
                "SELECT channel_id, balance_cents, used_cents, wallet "
                "FROM channels WHERE status = 'open' AND expires_at < ? "
                "AND channel_id NOT IN "
                "(SELECT channel_id FROM channel_holds WHERE status = 'held')",
                (now,),
            ).fetchall()

            count = 0
            for row in rows:
                total_cents = row["balance_cents"] + row["used_cents"]
                conn.execute(
                    "UPDATE channels SET status = 'expired', closed_at = ? "
                    "WHERE channel_id = ?",
                    (now, row["channel_id"]),
                )
                count += 1
                logger.info(
                    "Channel %s expired — %d cents refunded to %s",
                    row["channel_id"], total_cents, row["wallet"][:10] or "anonymous",
                )

            if count:
                conn.commit()
                logger.info("Swept %d expired channels", count)
            return count

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
    ) -> dict[str, Any]:
        """Open a pre-funded payment channel.

        with_secret: mint a one-time debit secret (returned ONCE as `channel_secret`); debit
            then requires it. The public entry point (open_channel) defaults this ON so every
            production/HTTP-opened channel is secured; the low-level ledger primitive defaults
            OFF so trusted internal callers/tests aren't forced through the secret.
        """
        if deposit_usd <= 0 or deposit_usd > 10_000:
            return {"error": "deposit must be between 0 and 10,000 USD"}

        if not self._check_rate(wallet, self._rate_state, _MAX_OPENS_PER_WALLET_PER_HOUR):
            return {"error": "rate limit exceeded — max 20 channel opens per hour per wallet"}

        deposit_cents = _dollars_to_cents(deposit_usd)
        token = token or _DEFAULT_TOKEN
        chain = chain or _DEFAULT_CHAIN

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
                tx = (tx_hash or "").strip()
                already = conn.execute(
                    "SELECT 1 FROM consumed_deposits WHERE chain = ? AND tx_hash = ?",
                    (chain, tx),
                ).fetchone()
                if already:
                    logger.warning(
                        "Rejected channel open: deposit tx %s on %s already consumed",
                        tx[:12], chain,
                    )
                    return {"error": "deposit transaction already used to fund a channel"}
                try:
                    conn.execute(
                        "INSERT INTO consumed_deposits "
                        "(chain, tx_hash, channel_id, amount_cents, consumed_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (chain, tx, channel_id, deposit_cents, now),
                    )
                except Exception as exc:  # UNIQUE/PK violation → concurrent claim won
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

            dict(conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone())

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

    def close(
        self,
        channel_id: str,
        settle_tx_hash: str = "",
        wallet: str = "",
    ) -> dict[str, Any]:
        """Close a channel and compute settlement.

        Args:
            wallet: Must match the wallet that opened the channel.
        """
        if not self._check_rate(wallet, self._close_rate, _MAX_CLOSES_PER_WALLET_PER_HOUR):
            return {"error": "rate limit exceeded — max 60 close per hour per wallet"}

        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return {"error": "channel not found"}
            if row["status"] != "open":
                return {"error": f"channel is {row['status']}"}

            # Authorization: wallet must match (anonymous channels cannot be closed by strangers)
            if row["wallet"] != wallet:
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
            conn.commit()

            used = _cents_to_dollars(row["used_cents"])
            refund = _cents_to_dollars(row["balance_cents"])
            original = _cents_to_dollars(row["original_deposit_cents"])

        return {
            "settlement": {
                "channel_id": channel_id,
                "used_usd": used,
                "refund_usd": refund,
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
            if requester_wallet and row["wallet"] and row["wallet"] != requester_wallet:
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
            if requester_wallet and row["wallet"] and row["wallet"] != requester_wallet:
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

    def refund(self, channel_id: str, amount_usd: float) -> dict[str, Any]:
        """Refund to a channel (on failure/abort).

        Only allowed on open channels. Amount capped at original deposit
        minus current balance to prevent unbounded refund.
        """
        amount_cents = _dollars_to_cents(amount_usd)

        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if not row:
                return {"error": "channel not found"}
            if row["status"] != "open":
                return {"error": f"channel is {row['status']} — refund only allowed on open channels"}

            # Cap refund at original deposit minus current balance
            max_refund = row["original_deposit_cents"] - row["balance_cents"]
            amount_cents = min(amount_cents, max_refund)
            if amount_cents <= 0:
                return {"error": "refund amount would exceed original deposit"}

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
        """
        wallet = (wallet or "").strip()
        if not wallet:
            return None
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(used_cents), 0) AS used "
                "FROM channels WHERE wallet = ?",
                (wallet,),
            ).fetchone()
        if not row or not row["n"]:
            return None
        return _cents_to_dollars(row["used"])

    def stats(self) -> dict[str, Any]:
        """Return aggregate statistics."""
        with self._get_conn() as conn:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM channels WHERE status = 'open'"
            ).fetchone()[0]
            settled = conn.execute(
                "SELECT COUNT(*), SUM(used_cents) FROM channels WHERE status = 'settled'"
            ).fetchone()
            expired = conn.execute(
                "SELECT COUNT(*) FROM channels WHERE status = 'expired'"
            ).fetchone()[0]

        return {
            "open_channels": open_count,
            "settled_channels": settled[0] or 0,
            "settled_volume_cents": settled[1] or 0,
            "settled_volume_usd": _cents_to_dollars(settled[1] or 0),
            "expired_channels": expired,
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
) -> dict[str, Any]:
    if not _crypto_enabled():
        return dict(_CRYPTO_DISABLED)
    # Public/HTTP entry point → secure-by-default: mint a debit secret so the invoke path
    # (X-Payment-Channel) can't be drained with just a leaked channel id.
    return _ledger.open(
        deposit_usd=deposit_usd,
        token=token or _DEFAULT_TOKEN,
        chain=chain or _DEFAULT_CHAIN,
        wallet=wallet,
        tx_hash=tx_hash,
        with_secret=with_secret,
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


def refund_channel(channel_id: str, amount_usd: float) -> dict[str, Any]:
    return _ledger.refund(channel_id, amount_usd)


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
