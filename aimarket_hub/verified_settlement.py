"""Pay-on-Verified settlement — escrow-held channel debits gated by a Metis verdict.

The buyer opts in per invoke (`verify` block in the v2 invoke body) and supplies the
task intent the delivered output is judged against. The provider's output is returned
immediately; the channel debit is deferred as a ledger HOLD (channels.hold_channel)
until Metis POST /v1/verify scores the output in the background:

    pass (status=success, score >= threshold)  -> capture_hold: debit recorded, settled
    fail (status=success, score <  threshold)  -> release_hold: refunded + signed
                                                  verification_rejection receipt
    engine error / needs_clarification         -> bounded re-runs, then the fail-open /
                                                  fail-closed policy (prod fail-closed
                                                  convention: AIFACTORY_PROD)
    transport failure                           -> retry with exponential backoff,
                                                  INDEFINITELY by default (no deadline)

An unresolved hold is buyer-safe by construction: the debit is never recorded until a
verdict lands (no-service-no-debit) — the party waiting on a stuck verify is the
provider. Pending rows persist in `verified_settlements` and are re-queued on startup,
so a hub restart never strands a hold.

Genuine verdicts (pass/fail) are emitted as self-signed reputation events
(`verify_passed` / `verify_failed`) — earned trust edges for LUMEN and search ranking.
Policy resolutions emit nothing: an unavailable verifier is not evidence about the
provider.

Env knobs are read dynamically (monkeypatchable, hub convention for prod gates):

    AIMARKET_VERIFY_ENABLED               1        master switch (per-invoke opt-in still required)
    AIMARKET_VERIFY_MIN_PRICE_USD         0.05     price floor — cheaper invokes are never taxed
    AIMARKET_VERIFY_SCORE_THRESHOLD       0.7      score needed to capture (matches factory gate)
    AIMARKET_VERIFY_COUNCIL_MIN_PRICE_USD 0.50     mode=auto: >= this -> council route, else fast
    AIMARKET_VERIFY_ATTEMPT_TIMEOUT_S     330      per-attempt HTTP timeout (> Metis 300s cap)
    AIMARKET_VERIFY_RETRY_BACKOFF_S       5        initial transport backoff (exp, cap 300)
    AIMARKET_VERIFY_ENGINE_RETRIES        2        re-runs after an engine-error envelope
    AIMARKET_VERIFY_MAX_WAIT_S            0        0 = no overall deadline (retry until verdict)
    AIMARKET_VERIFY_FAIL_CLOSED           derived  unset -> 1 iff AIFACTORY_PROD, else 0
    AIMARKET_VERIFY_METIS_URL             http://127.0.0.1:8080   (falls back to METIS_URL)
    AIMARKET_VERIFY_METIS_KEY             —        Bearer key (falls back to METIS_API_KEY)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from aimarket_hub.channels import capture_hold, release_hold
from aimarket_hub.models import ReputationEvent

logger = logging.getLogger(__name__)

VERIFIER_ID = "metis.verify@v1"

# Structural delimiters of the audit prompt a text-parsing verifier keys on. A
# buyer-supplied intent containing any of these could redirect the parse to an
# attacker-chosen payload, so plan() rejects an intent that carries them.
_RESERVED_VERIFY_MARKERS = (
    "Delivered result (JSON):",
    "Task (buyer intent):",
    "Judge whether",
)


def verifier_id() -> str:
    """Envelope `verifier` field. The verify slot is an interface — operators
    pointing AIMARKET_VERIFY_METIS_URL at a non-Metis verifier (e.g. GAIA's
    statistical plausibility service) should name it here so envelopes and
    receipts attribute the verdict honestly."""
    return os.environ.get("AIMARKET_VERIFY_VERIFIER_ID", "").strip() or VERIFIER_ID

# Metis rejects inputs over 200k chars; leave headroom for the instruction wrapper.
_MAX_OUTPUT_CHARS = 100_000

# Route cost order — used to clamp a buyer-named route to the price-justified ceiling.
_ROUTE_RANK = {"fast": 0, "thinking": 1, "council": 2, "agent": 3}


# ── Env knobs (dynamic reads — monkeypatchable, prod-gate convention) ─────────


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def verify_enabled() -> bool:
    return os.environ.get("AIMARKET_VERIFY_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def min_price_usd() -> float:
    return _env_float("AIMARKET_VERIFY_MIN_PRICE_USD", 0.05)


def score_threshold() -> float:
    return _env_float("AIMARKET_VERIFY_SCORE_THRESHOLD", 0.7)


def council_min_price_usd() -> float:
    return _env_float("AIMARKET_VERIFY_COUNCIL_MIN_PRICE_USD", 0.50)


def attempt_timeout_s() -> float:
    return _env_float("AIMARKET_VERIFY_ATTEMPT_TIMEOUT_S", 330.0)


def retry_backoff_s() -> float:
    return _env_float("AIMARKET_VERIFY_RETRY_BACKOFF_S", 5.0)


def engine_retries() -> int:
    return int(_env_float("AIMARKET_VERIFY_ENGINE_RETRIES", 2))


def max_wait_s() -> float:
    """Overall verdict deadline. 0 (default) = none: retry until a verdict lands."""
    return _env_float("AIMARKET_VERIFY_MAX_WAIT_S", 0.0)


def fail_closed() -> bool:
    """Policy for indeterminate outcomes (engine errors, elapsed max-wait).

    Explicit AIMARKET_VERIFY_FAIL_CLOSED wins; otherwise derived from the
    ecosystem prod convention: production refunds, dev settles.
    """
    explicit = os.environ.get("AIMARKET_VERIFY_FAIL_CLOSED", "").strip()
    if explicit:
        return _truthy(explicit)
    return os.environ.get("AIFACTORY_PROD", "").strip() == "1"


def metis_url() -> str:
    url = os.environ.get("AIMARKET_VERIFY_METIS_URL", "").strip() or \
        os.environ.get("METIS_URL", "").strip() or "http://127.0.0.1:8080"
    return url.rstrip("/")


def metis_key() -> str:
    return os.environ.get("AIMARKET_VERIFY_METIS_KEY", "").strip() or \
        os.environ.get("METIS_API_KEY", "").strip()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Plan: per-invoke eligibility ──────────────────────────────────────────────


@dataclass
class VerifyPlan:
    """Eligibility decision for one invoke's verify opt-in."""

    active: bool = False          # a verification will actually run
    paid: bool = False            # a ledger hold backs it (vs advisory)
    mode: str = "fast"
    intent: str = ""
    wait: bool = False
    wait_timeout_s: float = 300.0
    skipped_envelope: dict[str, Any] | None = None
    error: str = ""               # invalid opt-in -> 400


def plan(
    verify_block: Any,
    *,
    list_price: float,
    crypto_on: bool,
    sandbox_mode: bool,
    channel_id: str | None,
) -> VerifyPlan:
    """Decide what the buyer's verify block means for this invoke.

    Uses the LIST price for the floor and route tiering (crypto-off advisory runs
    should tier exactly like their paid twins would).
    """
    if not verify_block.requested:
        return VerifyPlan(active=False)
    intent = (verify_block.intent or "").strip()
    if not intent:
        return VerifyPlan(error="verify.intent is required when verification is requested")
    # The buyer intent is interpolated into the audit prompt the verifier parses;
    # a caller must not be able to smuggle the reserved structural delimiters into
    # it and redirect a text-parsing verifier (e.g. GAIA) to an attacker-chosen
    # payload. Reject them outright rather than silently escaping — a legitimate
    # task description never contains these markers.
    if any(mark in intent for mark in _RESERVED_VERIFY_MARKERS):
        return VerifyPlan(error="verify.intent may not contain reserved verification markers")

    # Route tiering as a price-justified CEILING, not just the `auto` default.
    # The council route can cost the operator a multi-minute cognition pass, so a
    # buyer must not be able to force it on a capability priced below the council
    # floor by naming the route explicitly (Metis cost-amplification).
    ceiling = "council" if list_price >= council_min_price_usd() else "fast"
    mode = verify_block.mode
    if mode == "auto":
        mode = ceiling
    elif _ROUTE_RANK.get(mode, 0) > _ROUTE_RANK[ceiling]:
        mode = ceiling

    common = dict(
        mode=mode, intent=intent,
        wait=bool(verify_block.wait),
        wait_timeout_s=float(verify_block.wait_timeout_s),
    )
    if not verify_enabled():
        return VerifyPlan(skipped_envelope=_skipped_envelope("verify_disabled", mode), **common)
    if list_price < min_price_usd():
        return VerifyPlan(skipped_envelope=_skipped_envelope("below_price_floor", mode), **common)

    paid = bool(crypto_on and not sandbox_mode and channel_id and list_price > 0)
    return VerifyPlan(active=True, paid=paid, **common)


# ── Envelope builders ─────────────────────────────────────────────────────────


def _base_envelope(mode: str) -> dict[str, Any]:
    return {
        "requested": True,
        "status": "pending",
        "performed": False,
        "verified": None,
        "verify_score": None,
        "verdict": "",
        "threshold": score_threshold(),
        "trace_id": None,
        "verifier": verifier_id(),
        "mode": mode,
        "settled": False,
        "reason": None,
        "timestamp": _now_iso(),
    }


def _skipped_envelope(reason: str, mode: str) -> dict[str, Any]:
    env = _base_envelope(mode)
    # A skipped verification settles like a legacy invoke: money moves immediately.
    env.update(status="skipped", settled=True, reason=reason)
    return env


def skipped_envelope_for(verify_block: Any, *, reason: str) -> dict[str, Any]:
    """Skipped envelope for a verify block outside the local-settlement path.

    Used by the federated invoke branch, which has no price context — an `auto`
    mode simply resolves to `fast` for display.
    """
    mode = getattr(verify_block, "mode", "fast")
    if mode == "auto":
        mode = "fast"
    return _skipped_envelope(reason, mode)


def pending_envelope(nonce: str, capability_id: str, mode: str, *, advisory: bool) -> dict[str, Any]:
    env = _base_envelope(mode)
    env.update(nonce=nonce, capability_id=capability_id)
    if advisory:
        # Nothing is held (sandbox / crypto off / free capability): the verdict is
        # informational + reputation-feeding only.
        env["reason"] = "advisory"
    return env


# ── Service ───────────────────────────────────────────────────────────────────


class VerifiedSettlementService:
    """Owns pending verified settlements: persistence, the background Metis worker
    loop, hold capture/release, envelope signing, and reputation emission."""

    def __init__(self, db: Any, signer: Any, consumer_hub: str = "operator_self"):
        self._db = db
        self._signer = signer
        self._consumer_hub = consumer_hub
        self._supply_security: Any = None
        self._tasks: set[asyncio.Task] = set()
        self._events: dict[str, asyncio.Event] = {}
        # Bound concurrent Metis calls so a restart with many pending rows (or a
        # burst of verified invokes) can't open hundreds of sockets at once.
        cap = int(_env_float("AIMARKET_VERIFY_MAX_CONCURRENCY", 8))
        self._sem = asyncio.Semaphore(max(1, cap))

    def attach_supply_security(self, supply_security: Any) -> None:
        """Verify-first escalation hook: genuine verdict failures are reported to
        SupplySecurity so REPEAT verified failures can escalate to a calibrated
        slash (the refund itself already made the buyer whole). Setter injection —
        SupplySecurity is constructed after this service in the app factory."""
        self._supply_security = supply_security

    # ── Registration (called inline from the invoke handler) ──────────────

    def register(
        self,
        *,
        nonce: str,
        product_id: str,
        capability_id: str,
        channel_id: str,
        provider_id: str,
        price_usd: float,
        intent: str,
        output: Any,
        mode: str,
        receipt: dict[str, Any],
        advisory: bool,
    ) -> dict[str, Any]:
        """Persist a pending settlement and schedule its background verification.

        Returns the pending envelope (already attached to the receipt by the caller).
        """
        env = pending_envelope(nonce, capability_id, mode, advisory=advisory)
        # Mutate the caller's receipt in place so the invoke response and the
        # stored copy both carry the pending envelope from the start.
        receipt["verification"] = env
        try:
            output_json = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            output_json = json.dumps(str(output), ensure_ascii=False)
        # Bound the stored row: _compose_input only ever sends the first
        # _MAX_OUTPUT_CHARS to Metis anyway, so a giant provider payload need not
        # bloat the DB or per-task resident memory.
        if len(output_json) > _MAX_OUTPUT_CHARS:
            output_json = output_json[:_MAX_OUTPUT_CHARS] + "…[truncated]"
        self._db._conn.execute(
            "INSERT INTO verified_settlements "
            "(nonce, product_id, capability_id, channel_id, provider_id, price_usd, "
            " intent, output_json, mode, status, envelope_json, receipt_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                nonce, product_id, capability_id,
                channel_id if not advisory else "",
                provider_id, price_usd, intent, output_json, mode,
                json.dumps(env, ensure_ascii=False),
                json.dumps(receipt, ensure_ascii=False),
                _now_iso(),
            ),
        )
        self._db._conn.commit()
        self._schedule(nonce)
        return env

    def _schedule(self, nonce: str) -> None:
        task = asyncio.create_task(self._run(nonce))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Lookup / wait ──────────────────────────────────────────────────────

    def lookup(self, nonce: str) -> dict[str, Any] | None:
        row = self._db._conn.execute(
            "SELECT * FROM verified_settlements WHERE nonce = ?", (nonce,)
        ).fetchone()
        if not row:
            return None
        out: dict[str, Any] = {
            "verification": json.loads(row["envelope_json"] or "{}"),
            "receipt": json.loads(row["receipt_json"] or "{}"),
        }
        if row["rejection_json"]:
            out["rejection_receipt"] = json.loads(row["rejection_json"])
        return out

    async def wait_for(self, nonce: str, timeout: float) -> dict[str, Any] | None:
        """Wait (bounded) for the verdict. Returns the resolved lookup dict, or
        None on timeout — the caller then responds with the pending envelope."""
        rec = self.lookup(nonce)
        if rec and rec["verification"].get("status") not in ("", "pending"):
            return rec
        ev = self._events.setdefault(nonce, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(1.0, timeout))
        except asyncio.TimeoutError:
            return None
        return self.lookup(nonce)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def reconcile(self) -> int:
        """Re-queue unresolved settlements (startup: restarts never strand holds).

        Picks up both `pending` rows and `verifying` rows: a `verifying` row means a
        previous process claimed it and died mid-verify, so at startup it is stale
        and must be re-run. `_run` re-claims each with a row-count-guarded UPDATE, so
        even if two processes reconcile the same shared DB only one wins the row.
        """
        rows = self._db._conn.execute(
            "SELECT nonce FROM verified_settlements WHERE status IN ('pending', 'verifying')"
        ).fetchall()
        for row in rows:
            self._schedule(row["nonce"])
        if rows:
            logger.info("verified-settlement: re-queued %d unresolved verification(s)", len(rows))
        return len(rows)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ── Worker ─────────────────────────────────────────────────────────────

    async def _run(self, nonce: str) -> None:
        # Row-count-guarded claim: flip pending|verifying → verifying and proceed
        # only if THIS call won the transition. On a shared DB (uvicorn --workers N
        # / Postgres replicas) this makes exactly one worker resolve each nonce, so
        # capture/release + the final envelope can't be double-applied.
        claim = self._db._conn.execute(
            "UPDATE verified_settlements SET status = 'verifying' "
            "WHERE nonce = ? AND status IN ('pending', 'verifying')",
            (nonce,),
        )
        self._db._conn.commit()
        if getattr(claim, "rowcount", 0) != 1:
            return
        row = self._db._conn.execute(
            "SELECT * FROM verified_settlements WHERE nonce = ?", (nonce,)
        ).fetchone()
        if not row:
            return

        started = time.monotonic()
        attempts = int(row["attempts"] or 0)
        engine_attempts = int(row["engine_attempts"] or 0)
        backoff = retry_backoff_s()
        last_env: dict[str, Any] | None = None

        while True:
            t0 = time.time()
            async with self._sem:  # cap simultaneous Metis calls across all rows
                kind, payload = await self._attempt(row)
            verify_latency_ms = int((time.time() - t0) * 1000)
            attempts += 1
            self._persist_progress(nonce, attempts, engine_attempts)

            if kind == "verdict":
                self._resolve_verdict(row, payload, verify_latency_ms)
                return
            if kind == "fatal":
                logger.warning("verified-settlement %s: fatal verify error (%s) — applying policy", nonce, payload)
                self._resolve_policy(row, last_env)
                return
            if kind == "engine":
                last_env = payload
                engine_attempts += 1
                self._persist_progress(nonce, attempts, engine_attempts)
                if engine_attempts > engine_retries():
                    logger.warning(
                        "verified-settlement %s: %d engine-error envelope(s) — applying policy",
                        nonce, engine_attempts,
                    )
                    self._resolve_policy(row, last_env)
                    return
            # kind in ("transport", "engine"): retry. No overall deadline by default —
            # an unresolved hold is buyer-safe (debit never recorded until a verdict).
            deadline = max_wait_s()
            if deadline > 0 and (time.monotonic() - started) > deadline:
                logger.warning("verified-settlement %s: max wait %.0fs elapsed — applying policy", nonce, deadline)
                self._resolve_policy(row, last_env)
                return
            # Jittered backoff de-synchronises a startup herd of re-queued rows so
            # they don't all retry Metis on the same tick.
            await asyncio.sleep(backoff * random.uniform(0.5, 1.5))
            backoff = min(backoff * 2, 300.0)

    async def _attempt(self, row: Any) -> tuple[str, Any]:
        """One Metis /v1/verify attempt.

        Returns (kind, payload): "verdict" -> success envelope; "engine" -> error /
        needs_clarification envelope (a definitive Metis response, retried a bounded
        number of times because each re-run costs a fresh cognition pass);
        "transport" -> no envelope at all (retried forever); "fatal" -> config/input
        error retrying cannot fix (401/400/413).
        """
        headers = {"Content-Type": "application/json"}
        key = metis_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "input": self._compose_input(row["intent"], row["output_json"]),
            "route": row["mode"],
            "min_verify_score": score_threshold(),
        }
        try:
            async with httpx.AsyncClient(timeout=attempt_timeout_s(), follow_redirects=False) as client:
                resp = await client.post(f"{metis_url()}/v1/verify", json=payload, headers=headers)
        except httpx.RequestError as exc:
            # Never log raw provider errors (secret-leak guard, metis_gate convention).
            return "transport", type(exc).__name__

        if resp.status_code == 429:
            return "transport", "rate_limited"
        if resp.status_code in (400, 401, 413):
            return "fatal", f"metis_http_{resp.status_code}"
        if resp.status_code != 200:
            return "transport", f"http_{resp.status_code}"
        try:
            env = resp.json()
        except ValueError:
            return "transport", "invalid_json"
        # Metis returns HTTP 200 + status:"error" for engine failures/timeouts;
        # needs_clarification can never be settled against either.
        if env.get("status") == "success":
            return "verdict", env
        return "engine", env

    @staticmethod
    def _compose_input(intent: str, output_json: str) -> str:
        output = output_json
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "…[truncated]"
        return (
            "You are auditing a paid AI service delivery.\n"
            f"Task (buyer intent):\n{intent}\n\n"
            f"Delivered result (JSON):\n{output}\n\n"
            "Judge whether the delivered result correctly and completely fulfils the task."
        )

    # ── Resolution ─────────────────────────────────────────────────────────

    def _resolve_verdict(self, row: Any, metis_env: dict[str, Any], verify_latency_ms: int) -> None:
        score = float(metis_env.get("verify_score") or 0.0)
        # Defence-in-depth: capture only when the verifier says verified AND the
        # score clears the operator threshold. The hub owns its own money-movement
        # bar rather than delegating it entirely to the verifier's boolean, so a
        # buggy/compromised verifier returning verified=true with a sub-threshold
        # score cannot capture.
        passed = bool(metis_env.get("verified")) and score >= score_threshold()
        trace_id = metis_env.get("trace_id")
        verdict = "passed" if passed else "failed"
        reason = None if passed else "verify_failed"
        won = self._finalize(
            row, verdict=verdict, performed=True, verified=passed,
            verify_score=score, trace_id=trace_id, reason=reason,
        )
        # Reputation is a one-shot side-effect too: only emit it if this call actually
        # resolved the row (a re-run after a crash-mid-finalize must not double-emit).
        if won:
            self._emit_reputation(row, passed=passed, verify_latency_ms=verify_latency_ms)

    def _resolve_policy(self, row: Any, metis_env: dict[str, Any] | None) -> None:
        """Indeterminate outcome: money moves by operator policy, never by verdict.
        No reputation event — an unavailable verifier is not evidence about the provider."""
        refund = fail_closed()
        score = float((metis_env or {}).get("verify_score") or 0.0)
        trace_id = (metis_env or {}).get("trace_id")
        reason = "metis_error_fail_closed" if refund else "metis_error_fail_open"
        self._finalize(
            row, verdict="indeterminate", performed=metis_env is not None,
            verified=False if refund else None, verify_score=score,
            trace_id=trace_id, reason=reason, force_refund=refund,
        )

    def _finalize(
        self,
        row: Any,
        *,
        verdict: str,
        performed: bool,
        verified: bool | None,
        verify_score: float,
        trace_id: Any,
        reason: str | None,
        force_refund: bool = False,
    ) -> bool:
        """Resolve one settlement row. Returns True iff THIS call won the terminal
        transition (and therefore fired the one-shot side-effects); False if the row
        was already resolved by a prior finalize (crash-recovery / double-schedule)."""
        nonce = row["nonce"]
        paid = bool(row["channel_id"]) and float(row["price_usd"] or 0) > 0
        refunding = force_refund or verdict == "failed"

        settled = False
        ledger_note = None
        if paid:
            if refunding:
                res = release_hold(nonce)
                if res.get("error"):
                    ledger_note = f"release_failed:{res['error']}"
                    logger.error("verified-settlement %s: release failed: %s", nonce, res["error"])
            else:
                res = capture_hold(nonce)
                if res.get("error"):
                    ledger_note = f"capture_failed:{res['error']}"
                    logger.error("verified-settlement %s: capture failed: %s", nonce, res["error"])
                else:
                    settled = True
                    self._accrue_acex(row)
        else:
            # Advisory: nothing was held; the invoke settled (freely) at response time.
            settled = not refunding

        env = json.loads(row["envelope_json"] or "{}")
        env.update(
            status="refunded" if refunding else "settled",
            performed=performed,
            verified=verified,
            verify_score=round(verify_score, 4),
            verdict=verdict,
            trace_id=trace_id,
            settled=settled,
            reason=ledger_note or reason,
            timestamp=_now_iso(),
        )
        with contextlib.suppress(Exception):
            env["signature"] = self._signer.sign_verification(env)

        rejection_json = ""
        rejection: dict[str, Any] | None = None
        if refunding:
            rejection = {
                "type": "verification_rejection",
                "product_id": row["product_id"],
                "capability_id": row["capability_id"],
                "channel_id": row["channel_id"] or None,
                "reason": env["reason"],
                "verify_score": env["verify_score"],
                "trace_id": trace_id,
                "timestamp": env["timestamp"],
                "refunded": paid,
                "nonce": f"vfail_{int(time.time())}_{row['product_id'][:8]}",
            }
            with contextlib.suppress(Exception):
                rejection["signature"] = self._signer.sign_receipt(rejection)
            rejection_json = json.dumps(rejection, ensure_ascii=False)

        receipt = json.loads(row["receipt_json"] or "{}")
        receipt["verification"] = env

        # Exactly-once gate: the terminal transition is the source of truth. The worker's
        # claim (_run) flips pending→verifying, so only the run that flips verifying→terminal
        # here may fire the one-shot side-effects (escalation, reputation, waiter wakeup).
        # A crash between money-movement above and this UPDATE leaves the row 'verifying';
        # reconcile re-runs it, and this guard makes the re-run — not a phantom double —
        # do the honors, so record_verified_failure can never double-count a fault.
        cur = self._db._conn.execute(
            "UPDATE verified_settlements SET status = ?, envelope_json = ?, "
            "rejection_json = ?, receipt_json = ?, resolved_at = ? "
            "WHERE nonce = ? AND status = 'verifying'",
            (
                env["status"], json.dumps(env, ensure_ascii=False), rejection_json,
                json.dumps(receipt, ensure_ascii=False), env["timestamp"], nonce,
            ),
        )
        self._db._conn.commit()
        won = getattr(cur, "rowcount", 0) == 1
        if not won:
            # A prior finalize already resolved this row: do NOT re-move reputation/stake
            # (money was protected by the single-use hold). Still wake any waiter.
            ev = self._events.pop(nonce, None)
            if ev:
                ev.set()
            logger.info("verified-settlement %s: finalize skipped (already resolved)", nonce)
            return False

        # Verify-first escalation — now that we own the terminal transition, so it runs
        # exactly once. Only a genuine verdict "failed" counts (a policy refund /
        # indeterminate verdict is not evidence about the provider); `paid` + the channel
        # (consumer) id gate it further so an advisory verdict never touches stake and one
        # buyer cannot slash alone. Errors are logged, not swallowed, so a wiring/type
        # regression surfaces instead of vanishing.
        if verdict == "failed" and rejection is not None and self._supply_security is not None and (row["provider_id"] or "").strip():
            try:
                self._supply_security.record_verified_failure(
                    publisher_id=row["provider_id"],
                    product_id=row["product_id"],
                    capability_id=row["capability_id"],
                    consumer_id=row["channel_id"] or "",
                    paid=paid,
                    rejection=rejection,
                )
            except Exception as exc:
                logger.warning(
                    "verified-settlement %s: verify-first escalation failed: %s", nonce, exc
                )

        ev = self._events.pop(nonce, None)
        if ev:
            ev.set()
        logger.info(
            "verified-settlement %s: %s (verdict=%s score=%.4f trace=%s)",
            nonce, env["status"], verdict, env["verify_score"] or 0.0, trace_id,
        )
        return True

    def _persist_progress(self, nonce: str, attempts: int, engine_attempts: int) -> None:
        with contextlib.suppress(Exception):
            self._db._conn.execute(
                "UPDATE verified_settlements SET attempts = ?, engine_attempts = ? WHERE nonce = ?",
                (attempts, engine_attempts, nonce),
            )
            self._db._conn.commit()

    def _accrue_acex(self, row: Any) -> None:
        """Revenue accruals deferred from invoke time: only CAPTURED money feeds
        CapShares pools / audit rewards (a refunded invoke earned nothing)."""
        price = float(row["price_usd"] or 0)
        product_id = row["product_id"]
        if price <= 0:
            return
        try:
            from aimarket_hub import acex_ipo
            acex_ipo.accrue_revenue(product_id, price)
        except Exception as exc:
            logger.warning("ACEX accrue failed for %s: %s", product_id, exc)
        try:
            from aimarket_hub import acex_audit
            acex_audit.accrue_audit_rewards(product_id, price)
        except Exception as exc:
            logger.warning("ACEX audit accrue failed for %s: %s", product_id, exc)

    def _emit_reputation(self, row: Any, *, passed: bool, verify_latency_ms: int) -> None:
        """Self-signed reputation event per genuine verdict (hub-local write path —
        the HTTP endpoint requires federation-peer signatures)."""
        try:
            event_type = "verify_passed" if passed else "verify_failed"
            timestamp = _now_iso()
            price_usd = float(row["price_usd"] or 0)
            canonical = (
                f"type:{event_type}"
                f"|provider_hub:{row['provider_id'] or 'local'}"
                f"|timestamp:{timestamp}"
                f"|price_usd:{price_usd}"
                f"|latency_ms:{verify_latency_ms}"
            )
            sig = {
                "algorithm": "ed25519",
                "public_key": self._signer.public_key_b64,
                "value": self._signer.sign_canonical(canonical),
            }
            self._db.record_reputation_event(ReputationEvent(
                event_type=event_type,
                provider_hub=row["provider_id"] or "local",
                capability_id=row["capability_id"],
                timestamp=timestamp,
                price_usd=price_usd,
                latency_ms=verify_latency_ms,
                consumer_hub=self._consumer_hub,
                signature=json.dumps(sig),
            ))
        except Exception as exc:
            logger.warning("verified-settlement %s: reputation emit failed: %s", row["nonce"], exc)
