"""ACEX Agent IPO — off-chain reference ledger for the factory → hub → ACEX leg.

This is the missing leg of the "Agent IPO" north-star: when AI-Factory ships a
product and the Hub auto-lists its capabilities, this module floats the product
as an ACEX listing (CapShares cap table) and routes a configurable slice of every
*paid* invoke to a distributable revenue pool that shareholders can claim.

Semantics mirror the on-chain contracts (acex/contracts/evm):
  AgentListingRegistry  apply → audit (≥ MIN_AUDIT_SCORE_BPS) → approve → mint
  AgentShareToken       ERC-20 CapShares minted to the agent treasury, trading lock

The revenue-distribution layer below is the piece that does NOT yet exist on-chain;
it is the reference implementation that a future PulseDistributor contract mirrors.

All USD amounts are stored internally as integer micro-USD (1 USD = 1_000_000) so
pro-rata distribution is exact — no floating-point drift, and
sum(payouts) == pool always (dust is assigned to the largest holder).

Env:
  ACEX_AUTO_IPO                 "1" enables auto-float on auto-listing (default off)
  ACEX_REVENUE_SHARE_BPS        Shareholder cut of each paid invoke (default 5000 = 50%)
  ACEX_DEFAULT_MAX_SUPPLY       CapShares minted per listing (default 1_000_000)
  ACEX_TREASURY_HOLDER          Initial holder of all CapShares (default "factory-treasury")
  ACEX_MIN_AUDIT_SCORE_BPS      Minimum audit score to approve a listing (default 7000)
  ACEX_IPO_DB_PATH              SQLite path (default data/acex_ipo.db)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MICRO = 1_000_000

_AUTO_IPO = os.getenv("ACEX_AUTO_IPO", "0").strip().lower() in ("1", "true", "yes")
_DEFAULT_REVENUE_SHARE_BPS = int(os.getenv("ACEX_REVENUE_SHARE_BPS", "5000"))
_DEFAULT_MAX_SUPPLY = int(os.getenv("ACEX_DEFAULT_MAX_SUPPLY", "1000000"))
_DEFAULT_TREASURY = os.getenv("ACEX_TREASURY_HOLDER", "factory-treasury")
_MIN_AUDIT_SCORE_BPS = int(os.getenv("ACEX_MIN_AUDIT_SCORE_BPS", "7000"))


def _min_listing_revenue_usd() -> float:
    """Anti-Sybil: minimum prior paid-invoke revenue (USD) before floating CapShares.
    Read at call time so operators can tune it at runtime. 0 disables the gate (default)."""
    try:
        return max(0.0, float(os.getenv("ACEX_MIN_LISTING_REVENUE_USD", "0") or 0))
    except (TypeError, ValueError):
        return 0.0
_DB_PATH = os.getenv("ACEX_IPO_DB_PATH", "data/acex_ipo.db")

VALID_STATUSES = ("pending", "under_audit", "approved", "rejected", "delisted")


def _data_root() -> Path:
    """Stable base for relative data paths (mirrors factory_products_loader)."""
    env = os.environ.get("AIFACTORY_DATA_ROOT", "").strip()
    if env:
        return Path(env)
    try:
        from core.paths import data_root

        return data_root()
    except ImportError:
        # Last resort: the installed package's parent (…/aimarket-hub) so a
        # relative default never lands in an arbitrary CWD.
        return Path(__file__).resolve().parent.parent


def _resolve_db_path(db_path: str = "") -> Path:
    """Resolve the configured SQLite path to a stable absolute location.

    Absolute paths (the usual production config) are honoured as-is. Relative
    paths — including the ``data/acex_ipo.db`` default — are resolved against
    AIFACTORY_DATA_ROOT (or the package dir) so the ledger is never created in
    whatever happens to be the current working directory. The parent directory
    is created and the resolved absolute path is logged for operator visibility.
    """
    raw = (db_path or _DB_PATH or "data/acex_ipo.db").strip()
    p = Path(raw).expanduser()
    p = p.resolve() if p.is_absolute() else (_data_root() / p).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    logger.info("ACEX IPO ledger using SQLite DB at %s", p)
    return p


def ipo_enabled() -> bool:
    return _AUTO_IPO


def _to_micro(usd: float) -> int:
    return int(round(float(usd) * _MICRO))


def _usd(micro: int) -> float:
    return round(micro / _MICRO, 6)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _symbol_from_product(product_id: str) -> str:
    base = "".join(c for c in product_id.upper() if c.isalnum())[:6]
    return f"CAP{base}" if base else f"CAP{uuid.uuid4().hex[:4].upper()}"


class AcexIpoLedger:
    """SQLite-backed ACEX listing registry + CapShares cap table + revenue pool."""

    def __init__(self, db_path: str = "") -> None:
        self._path = _resolve_db_path(db_path)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    listing_id        TEXT PRIMARY KEY,
                    product_id        TEXT NOT NULL,
                    name              TEXT NOT NULL,
                    symbol            TEXT NOT NULL,
                    max_supply        INTEGER NOT NULL,
                    audit_score_bps   INTEGER NOT NULL DEFAULT 0,
                    revenue_share_bps INTEGER NOT NULL,
                    status            TEXT NOT NULL,
                    trading_enabled   INTEGER NOT NULL DEFAULT 0,
                    treasury          TEXT NOT NULL,
                    listed_at         TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS holdings (
                    listing_id TEXT NOT NULL,
                    holder     TEXT NOT NULL,
                    shares     INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (listing_id, holder)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS revenue (
                    listing_id          TEXT PRIMARY KEY,
                    accrued_micro       INTEGER NOT NULL DEFAULT 0,
                    distributed_micro   INTEGER NOT NULL DEFAULT 0,
                    gross_micro         INTEGER NOT NULL DEFAULT 0,
                    last_distribution_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS claims (
                    listing_id     TEXT NOT NULL,
                    holder         TEXT NOT NULL,
                    claimable_micro INTEGER NOT NULL DEFAULT 0,
                    claimed_micro   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (listing_id, holder)
                )
            """)
            conn.commit()

    # ── Listing lifecycle ───────────────────────────────────────

    def float_product(
        self,
        product_id: str,
        *,
        name: str | None = None,
        symbol: str | None = None,
        max_supply: int | None = None,
        treasury: str | None = None,
        audit_score_bps: int | None = None,
        revenue_share_bps: int | None = None,
        prior_revenue_usd: float = 0.0,
    ) -> dict[str, Any]:
        """Float a product as an ACEX listing (apply → audit → approve → mint).

        Idempotent: re-floating an existing product returns the current state with
        ``already_listed=True`` instead of erroring.
        """
        pid = (product_id or "").strip()
        if not pid:
            return {"error": "missing_product_id"}

        listing_id = pid
        name = (name or pid)[:80]
        symbol = (symbol or _symbol_from_product(pid))[:12]
        max_supply = int(max_supply or _DEFAULT_MAX_SUPPLY)
        treasury = (treasury or _DEFAULT_TREASURY).strip() or _DEFAULT_TREASURY
        revenue_share_bps = int(_DEFAULT_REVENUE_SHARE_BPS if revenue_share_bps is None else revenue_share_bps)
        audit_score_bps = int(_MIN_AUDIT_SCORE_BPS if audit_score_bps is None else audit_score_bps)

        if max_supply <= 0:
            return {"error": "invalid_max_supply"}
        if not (0 <= revenue_share_bps <= 10000):
            return {"error": "invalid_revenue_share_bps"}
        if audit_score_bps < _MIN_AUDIT_SCORE_BPS:
            return {
                "error": "audit_score_too_low",
                "audit_score_bps": audit_score_bps,
                "min_audit_score_bps": _MIN_AUDIT_SCORE_BPS,
            }
        # Anti-Sybil revenue gate (F7): a zero-revenue dummy agent cannot float shares.
        # `prior_revenue_usd` should be the agent's F4-verifiable paid-invoke revenue.
        # Disabled by default (0); operators set ACEX_MIN_LISTING_REVENUE_USD for mainnet.
        _min_revenue = _min_listing_revenue_usd()
        if _min_revenue > 0 and float(prior_revenue_usd) < _min_revenue:
            return {
                "error": "insufficient_prior_revenue",
                "prior_revenue_usd": round(float(prior_revenue_usd), 4),
                "min_listing_revenue_usd": _min_revenue,
            }

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT listing_id FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            if row:
                return {**self.listing_state(listing_id), "already_listed": True}

            now = _now()
            conn.execute(
                """INSERT INTO listings
                   (listing_id, product_id, name, symbol, max_supply, audit_score_bps,
                    revenue_share_bps, status, trading_enabled, treasury, listed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'approved', 1, ?, ?)""",
                (listing_id, pid, name, symbol, max_supply, audit_score_bps,
                 revenue_share_bps, treasury, now),
            )
            # Mint full supply to the agent treasury (matches AgentShareToken.mintTo)
            conn.execute(
                "INSERT INTO holdings (listing_id, holder, shares) VALUES (?, ?, ?)",
                (listing_id, treasury, max_supply),
            )
            conn.execute(
                "INSERT INTO revenue (listing_id) VALUES (?)", (listing_id,)
            )
            conn.commit()

        return {**self.listing_state(listing_id), "already_listed": False}

    def transfer_shares(
        self, listing_id: str, sender: str, recipient: str, shares: int
    ) -> dict[str, Any]:
        """Move CapShares between holders (secondary allocation / public float)."""
        shares = int(shares)
        if shares <= 0:
            return {"error": "invalid_share_amount"}
        sender = (sender or "").strip()
        recipient = (recipient or "").strip()
        if not sender or not recipient:
            return {"error": "missing_holder"}
        if sender == recipient:
            return {"error": "self_transfer"}

        with self._lock, self._connect() as conn:
            lst = conn.execute(
                "SELECT trading_enabled FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            if not lst:
                return {"error": "unknown_listing"}
            if not int(lst["trading_enabled"]):
                return {"error": "trading_locked"}

            srow = conn.execute(
                "SELECT shares FROM holdings WHERE listing_id = ? AND holder = ?",
                (listing_id, sender),
            ).fetchone()
            sender_shares = int(srow["shares"]) if srow else 0
            if sender_shares < shares:
                return {"error": "insufficient_shares", "have": sender_shares, "need": shares}

            conn.execute(
                "UPDATE holdings SET shares = shares - ? WHERE listing_id = ? AND holder = ?",
                (shares, listing_id, sender),
            )
            conn.execute(
                """INSERT INTO holdings (listing_id, holder, shares) VALUES (?, ?, ?)
                   ON CONFLICT(listing_id, holder) DO UPDATE SET shares = shares + ?""",
                (listing_id, recipient, shares, shares),
            )
            conn.execute(
                "DELETE FROM holdings WHERE listing_id = ? AND shares = 0", (listing_id,)
            )
            conn.commit()

        return {"ok": True, "listing_id": listing_id, "from": sender,
                "to": recipient, "shares": shares}

    # ── Revenue ─────────────────────────────────────────────────

    def accrue_revenue(self, listing_id: str, gross_usd: float) -> dict[str, Any]:
        """Add the shareholder slice of a paid invoke to the distributable pool.

        ``gross_usd`` is the full price charged; the listing's revenue_share_bps
        decides how much flows to shareholders (the remainder stays operating capital).
        """
        gross_micro = _to_micro(gross_usd)
        if gross_micro <= 0:
            return {"error": "non_positive_amount"}

        with self._lock, self._connect() as conn:
            lst = conn.execute(
                "SELECT revenue_share_bps FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            if not lst:
                return {"error": "unknown_listing"}
            bps = int(lst["revenue_share_bps"])
            pool_add = gross_micro * bps // 10000
            conn.execute(
                """UPDATE revenue
                   SET accrued_micro = accrued_micro + ?, gross_micro = gross_micro + ?
                   WHERE listing_id = ?""",
                (pool_add, gross_micro, listing_id),
            )
            conn.commit()

        return {
            "ok": True,
            "listing_id": listing_id,
            "gross_usd": _usd(gross_micro),
            "to_pool_usd": _usd(pool_add),
            "revenue_share_bps": bps,
        }

    def distribute(self, listing_id: str) -> dict[str, Any]:
        """Split the accrued pool pro-rata to current holders (exact, no drift)."""
        with self._lock, self._connect() as conn:
            rev = conn.execute(
                "SELECT accrued_micro FROM revenue WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            if not rev:
                return {"error": "unknown_listing"}
            pool = int(rev["accrued_micro"])
            if pool <= 0:
                return {"ok": True, "listing_id": listing_id, "distributed_usd": 0.0,
                        "payouts": [], "note": "nothing_to_distribute"}

            holders = conn.execute(
                "SELECT holder, shares FROM holdings WHERE listing_id = ? AND shares > 0 "
                "ORDER BY shares DESC, holder ASC",
                (listing_id,),
            ).fetchall()
            total_shares = sum(int(h["shares"]) for h in holders)
            if total_shares <= 0:
                return {"error": "no_shares_outstanding"}

            # Integer pro-rata; dust (rounding remainder) goes to the largest holder.
            payouts: list[dict[str, Any]] = []
            assigned = 0
            for h in holders:
                share_micro = pool * int(h["shares"]) // total_shares
                assigned += share_micro
                payouts.append({"holder": h["holder"], "shares": int(h["shares"]),
                                "_micro": share_micro})
            dust = pool - assigned
            if dust and payouts:
                payouts[0]["_micro"] += dust  # largest holder absorbs the dust

            now = _now()
            for p in payouts:
                conn.execute(
                    """INSERT INTO claims (listing_id, holder, claimable_micro)
                       VALUES (?, ?, ?)
                       ON CONFLICT(listing_id, holder)
                       DO UPDATE SET claimable_micro = claimable_micro + ?""",
                    (listing_id, p["holder"], p["_micro"], p["_micro"]),
                )
            conn.execute(
                """UPDATE revenue
                   SET accrued_micro = 0,
                       distributed_micro = distributed_micro + ?,
                       last_distribution_at = ?
                   WHERE listing_id = ?""",
                (pool, now, listing_id),
            )
            conn.commit()

        return {
            "ok": True,
            "listing_id": listing_id,
            "distributed_usd": _usd(pool),
            "holders": len(payouts),
            "payouts": [
                {"holder": p["holder"], "shares": p["shares"], "amount_usd": _usd(p["_micro"])}
                for p in payouts
            ],
        }

    def claim(self, listing_id: str, holder: str) -> dict[str, Any]:
        """Holder withdraws their claimable balance (zeroes it, returns amount)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT claimable_micro FROM claims WHERE listing_id = ? AND holder = ?",
                (listing_id, holder),
            ).fetchone()
            claimable = int(row["claimable_micro"]) if row else 0
            if claimable <= 0:
                return {"ok": True, "listing_id": listing_id, "holder": holder,
                        "claimed_usd": 0.0, "note": "nothing_to_claim"}
            conn.execute(
                """UPDATE claims SET claimable_micro = 0, claimed_micro = claimed_micro + ?
                   WHERE listing_id = ? AND holder = ?""",
                (claimable, listing_id, holder),
            )
            conn.commit()
        return {"ok": True, "listing_id": listing_id, "holder": holder,
                "claimed_usd": _usd(claimable)}

    # ── Views ───────────────────────────────────────────────────

    def cap_table(self, listing_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            lst = conn.execute(
                "SELECT max_supply FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
            if not lst:
                return {"error": "unknown_listing"}
            holders = conn.execute(
                "SELECT holder, shares FROM holdings WHERE listing_id = ? AND shares > 0 "
                "ORDER BY shares DESC, holder ASC",
                (listing_id,),
            ).fetchall()
        max_supply = int(lst["max_supply"])
        outstanding = sum(int(h["shares"]) for h in holders)
        return {
            "listing_id": listing_id,
            "max_supply": max_supply,
            "shares_outstanding": outstanding,
            "holders": [
                {
                    "holder": h["holder"],
                    "shares": int(h["shares"]),
                    "pct": round(100.0 * int(h["shares"]) / outstanding, 4) if outstanding else 0.0,
                }
                for h in holders
            ],
        }

    def revenue_state(self, listing_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            rev = conn.execute(
                "SELECT accrued_micro, distributed_micro, gross_micro, last_distribution_at "
                "FROM revenue WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
            if not rev:
                return {"error": "unknown_listing"}
            claims = conn.execute(
                "SELECT COALESCE(SUM(claimable_micro),0) AS c, COALESCE(SUM(claimed_micro),0) AS d "
                "FROM claims WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
        return {
            "listing_id": listing_id,
            "gross_revenue_usd": _usd(int(rev["gross_micro"])),
            "accrued_undistributed_usd": _usd(int(rev["accrued_micro"])),
            "distributed_usd": _usd(int(rev["distributed_micro"])),
            "claimable_usd": _usd(int(claims["c"])),
            "claimed_usd": _usd(int(claims["d"])),
            "last_distribution_at": rev["last_distribution_at"],
        }

    def listing_state(self, listing_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            lst = conn.execute(
                "SELECT * FROM listings WHERE listing_id = ?", (listing_id,)
            ).fetchone()
        if not lst:
            return {"error": "unknown_listing"}
        cap = self.cap_table(listing_id)
        rev = self.revenue_state(listing_id)
        return {
            "listing_id": lst["listing_id"],
            "product_id": lst["product_id"],
            "name": lst["name"],
            "symbol": lst["symbol"],
            "status": lst["status"],
            "trading_enabled": bool(lst["trading_enabled"]),
            "audit_score_bps": int(lst["audit_score_bps"]),
            "revenue_share_bps": int(lst["revenue_share_bps"]),
            "treasury": lst["treasury"],
            "listed_at": lst["listed_at"],
            "max_supply": cap["max_supply"],
            "shares_outstanding": cap["shares_outstanding"],
            "holder_count": len(cap["holders"]),
            "revenue": rev,
        }

    def holder_position(self, listing_id: str, holder: str) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            srow = conn.execute(
                "SELECT shares FROM holdings WHERE listing_id = ? AND holder = ?",
                (listing_id, holder),
            ).fetchone()
            crow = conn.execute(
                "SELECT claimable_micro, claimed_micro FROM claims "
                "WHERE listing_id = ? AND holder = ?",
                (listing_id, holder),
            ).fetchone()
        return {
            "listing_id": listing_id,
            "holder": holder,
            "shares": int(srow["shares"]) if srow else 0,
            "claimable_usd": _usd(int(crow["claimable_micro"])) if crow else 0.0,
            "claimed_usd": _usd(int(crow["claimed_micro"])) if crow else 0.0,
        }

    def holder_positions(self, holder: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT listing_id FROM holdings WHERE holder = ? AND shares > 0",
                (holder,),
            ).fetchall()
        return [self.holder_position(r["listing_id"], holder) for r in rows]

    def outstanding_claims(self, listing_id: str) -> list[dict[str, Any]]:
        """Holders with a positive claimable balance (amount in micro-USD)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT holder, claimable_micro FROM claims "
                "WHERE listing_id = ? AND claimable_micro > 0 ORDER BY holder ASC",
                (listing_id,),
            ).fetchall()
        return [{"holder": r["holder"], "amount": int(r["claimable_micro"])} for r in rows]

    def list_listings(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = min(max(1, int(limit)), 1000)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT listing_id FROM listings ORDER BY listed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.listing_state(r["listing_id"]) for r in rows]


# ── Module-level lazy singleton (mirrors sandbox_trials) ────────

_ledger: AcexIpoLedger | None = None
_ledger_lock = threading.Lock()


def _get_ledger() -> AcexIpoLedger:
    global _ledger
    if _ledger is None:
        with _ledger_lock:
            if _ledger is None:
                _ledger = AcexIpoLedger()
    return _ledger


def _reset_for_tests() -> None:
    """Drop the cached ledger so a new ACEX_IPO_DB_PATH takes effect (tests only)."""
    global _ledger
    with _ledger_lock:
        _ledger = None


def float_product(product_id: str, **kwargs: Any) -> dict[str, Any]:
    return _get_ledger().float_product(product_id, **kwargs)


def accrue_revenue(listing_id: str, gross_usd: float) -> dict[str, Any]:
    return _get_ledger().accrue_revenue(listing_id, gross_usd)


def distribute(listing_id: str) -> dict[str, Any]:
    return _get_ledger().distribute(listing_id)


def transfer_shares(listing_id: str, sender: str, recipient: str, shares: int) -> dict[str, Any]:
    return _get_ledger().transfer_shares(listing_id, sender, recipient, shares)


def claim(listing_id: str, holder: str) -> dict[str, Any]:
    return _get_ledger().claim(listing_id, holder)


def cap_table(listing_id: str) -> dict[str, Any]:
    return _get_ledger().cap_table(listing_id)


def revenue_state(listing_id: str) -> dict[str, Any]:
    return _get_ledger().revenue_state(listing_id)


def listing_state(listing_id: str) -> dict[str, Any]:
    return _get_ledger().listing_state(listing_id)


def holder_position(listing_id: str, holder: str) -> dict[str, Any]:
    return _get_ledger().holder_position(listing_id, holder)


def holder_positions(holder: str) -> list[dict[str, Any]]:
    return _get_ledger().holder_positions(holder)


def list_listings(limit: int = 200) -> list[dict[str, Any]]:
    return _get_ledger().list_listings(limit)


def outstanding_claims(listing_id: str) -> list[dict[str, Any]]:
    return _get_ledger().outstanding_claims(listing_id)


def build_onchain_claimset(listing_id: str, address_map: dict[str, str]) -> dict[str, Any]:
    """Build a Merkle claim set for on-chain settlement via PulseDistributor.

    ``address_map`` maps off-chain holder ids → 0x wallet addresses. Holders without
    a mapping are skipped. Amounts are micro-USD, which equal USDC base units (6 dp),
    so the result feeds ``PulseDistributor.postEpoch(root, total)`` and ``claim(...)``
    directly. The Merkle root/proofs match what the contract verifies on-chain.
    """
    from aimarket_hub import acex_merkle

    payouts = []
    for c in _get_ledger().outstanding_claims(listing_id):
        addr = address_map.get(c["holder"])
        if addr:
            payouts.append({"account": addr, "amount": c["amount"]})
    cs = acex_merkle.build_claimset(payouts)
    cs["listing_id"] = listing_id
    return cs
