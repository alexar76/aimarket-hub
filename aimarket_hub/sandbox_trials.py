"""Widget sandbox trials — free invoke quota per visitor (escrow-free preview).

Used by embed widgets and demos: visitors get N complimentary invocations without
opening a payment channel. Production invocations still debit channels normally.

Env:
  AIMARKET_SANDBOX_ENABLED — "0" disables sandbox (default: enabled)
  AIMARKET_SANDBOX_MAX_PER_VISITOR — trials per visitor id (default 3)
  AIMARKET_SANDBOX_MAX_PER_IP_HOUR — rate cap per client IP (default 30)
  AIMARKET_SANDBOX_STUB_INVOKE — "1" returns deterministic demo output when factory is down
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_ENABLED = os.getenv("AIMARKET_SANDBOX_ENABLED", "1").strip().lower() not in ("0", "false", "no")
_MAX_PER_VISITOR = int(os.getenv("AIMARKET_SANDBOX_MAX_PER_VISITOR", "3"))
_MAX_PER_IP_HOUR = int(os.getenv("AIMARKET_SANDBOX_MAX_PER_IP_HOUR", "30"))
_DB_PATH = os.getenv("AIMARKET_SANDBOX_DB_PATH", "data/sandbox_trials.db")
def sandbox_stub_invoke_enabled() -> bool:
    return os.getenv("AIMARKET_SANDBOX_STUB_INVOKE", "0").strip().lower() in ("1", "true", "yes")


def sandbox_enabled() -> bool:
    return _ENABLED


def trial_policy() -> dict[str, Any]:
    """The free-trial terms, for publication in `.well-known/ai-market.json`.

    The tier existed and worked but was advertised nowhere, so a discovering agent read
    `payment_configured: true` next to a price list and concluded it needed a funded
    wallet to evaluate anything. Naming the header and the allowance here is the
    difference between "found the hub" and "tried a capability".

    Deliberately omits the per-IP cap: that is an abuse control, and publishing it just
    tells an abuser the shape of the limit.
    """
    if not _ENABLED:
        return {"enabled": False}
    return {
        "enabled": True,
        "max_invokes_per_visitor": _MAX_PER_VISITOR,
        "visitor_header": "X-AIMarket-Sandbox-Visitor",
        "how": (
            "Send X-AIMarket-Sandbox-Visitor with a stable id you choose and no payment "
            "channel; the invoke runs for free and returns a signed receipt. Results are "
            "marked sandbox=true. When the allowance is spent the hub answers 402."
        ),
        "clients": {"mcp": "pip install aimarket-mcp — market_search / market_invoke"},
    }


class SandboxTrialLedger:
    """SQLite-backed trial counters."""

    def __init__(self, db_path: str = "") -> None:
        self._path = Path(db_path or _DB_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS visitor_trials (
                    visitor_id TEXT PRIMARY KEY,
                    used INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ip_hits (
                    ip TEXT NOT NULL,
                    ts REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_hits_ts ON ip_hits(ts)")
            conn.commit()

    def quota(self, visitor_id: str) -> dict[str, Any]:
        if not _ENABLED:
            return {"enabled": False, "max_trials": 0, "used": 0, "remaining": 0}
        vid = _normalize_visitor(visitor_id)
        used = self._get_used(vid)
        remaining = max(0, _MAX_PER_VISITOR - used)
        return {
            "enabled": True,
            "max_trials": _MAX_PER_VISITOR,
            "used": used,
            "remaining": remaining,
            "visitor_id": vid,
        }

    def consume(self, visitor_id: str, client_ip: str = "") -> dict[str, Any]:
        """Reserve one trial. Returns ok/error dict."""
        if not _ENABLED:
            return {"error": "sandbox_disabled", "enabled": False}

        vid = _normalize_visitor(visitor_id)
        if not vid:
            # "missing" only when it really is. A present-but-rejected id (`keys-3` is six
            # characters) reported missing_visitor_id, sending the caller to look for a
            # header they had already sent — so say what the rule is instead.
            if (visitor_id or "").strip():
                return {
                    "error": "invalid_visitor_id",
                    "detail": (
                        "X-AIMarket-Sandbox-Visitor must be 8-64 characters of letters, "
                        "digits, '_' or '-'"
                    ),
                }
            return {"error": "missing_visitor_id"}

        if client_ip and not self._ip_within_limit(client_ip):
            return {"error": "rate_limit_exceeded", "detail": "too many sandbox tries from this network"}

        with self._lock, sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT used FROM visitor_trials WHERE visitor_id = ?", (vid,)
            ).fetchone()
            used = int(row[0]) if row else 0
            if used >= _MAX_PER_VISITOR:
                return {
                    "error": "trial_quota_exhausted",
                    "used": used,
                    "max_trials": _MAX_PER_VISITOR,
                    "remaining": 0,
                }
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if row:
                conn.execute(
                    "UPDATE visitor_trials SET used = ?, updated_at = ? WHERE visitor_id = ?",
                    (used + 1, now, vid),
                )
            else:
                conn.execute(
                    "INSERT INTO visitor_trials (visitor_id, used, updated_at) VALUES (?, 1, ?)",
                    (vid, now),
                )
            conn.commit()
            new_used = used + 1

        if client_ip:
            self._record_ip(client_ip)

        return {
            "ok": True,
            "used": new_used,
            "max_trials": _MAX_PER_VISITOR,
            "remaining": max(0, _MAX_PER_VISITOR - new_used),
            "visitor_id": vid,
        }

    def _get_used(self, visitor_id: str) -> int:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT used FROM visitor_trials WHERE visitor_id = ?", (visitor_id,)
            ).fetchone()
            return int(row[0]) if row else 0

    def _ip_within_limit(self, ip: str) -> bool:
        cutoff = time.time() - 3600
        with self._lock, sqlite3.connect(self._path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM ip_hits WHERE ip = ? AND ts > ?", (ip, cutoff)
            ).fetchone()[0]
            return count < _MAX_PER_IP_HOUR

    def _record_ip(self, ip: str) -> None:
        now = time.time()
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.execute("INSERT INTO ip_hits (ip, ts) VALUES (?, ?)", (ip, now))
            conn.execute("DELETE FROM ip_hits WHERE ts < ?", (now - 7200,))
            conn.commit()


_ledger: SandboxTrialLedger | None = None
_ledger_lock = threading.Lock()


def _get_ledger() -> SandboxTrialLedger:
    global _ledger
    if _ledger is None:
        with _ledger_lock:
            if _ledger is None:
                _ledger = SandboxTrialLedger()
    return _ledger


def sandbox_quota(visitor_id: str) -> dict[str, Any]:
    return _get_ledger().quota(visitor_id)


def consume_sandbox_trial(visitor_id: str, client_ip: str = "") -> dict[str, Any]:
    return _get_ledger().consume(visitor_id, client_ip=client_ip)


def new_visitor_id() -> str:
    return f"vis_{uuid.uuid4().hex[:20]}"


def _normalize_visitor(visitor_id: str) -> str:
    vid = (visitor_id or "").strip()
    if len(vid) < 8 or len(vid) > 64:
        return ""
    if not all(c.isalnum() or c in "_-" for c in vid):
        return ""
    return vid


def sandbox_demo_result(capability_id: str, product_id: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Deterministic preview payload when factory is unavailable (CI / offline demos)."""
    text = ""
    if isinstance(user_input, dict):
        text = str(user_input.get("text") or user_input.get("message") or user_input.get("task") or "")[:200]
    return {
        "output": {
            "mode": "sandbox_preview",
            "capability_id": capability_id,
            "product_id": product_id,
            "echo": text or "(empty input)",
            "note": "Sandbox trial — connect AIFACTORY_PUBLIC_URL for live execution.",
        },
        "sandbox": True,
    }
