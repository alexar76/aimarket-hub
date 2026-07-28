"""C3 — the reconciler that turns stored authorizations into on-chain debits.

This is what actually closes audit finding #9: without it ``usedAmount`` stays 0 forever
and a depositor can reclaim a fully consumed deposit. Everything here is a background /
operator action, never part of a request: an invoke records an authorization and returns,
and the mirror catches up afterwards.

Order is the load-bearing property. The escrow accepts a debit only at the channel's
CURRENT nonce, so authorizations must go out strictly in ascending nonce per channel. A gap
therefore BLOCKS rather than skips — skipping would leave a permanently unsubmittable row
behind, i.e. money the hub can never collect.

Three guards sit in front of every submission, in this order, because each is cheaper than
the next and each can only refuse:

    1. the store's queue position   — is this the next nonce for its channel?
    2. the LEDGER                   — did the hub actually debit this receipt, for this much?
    3. the chain                    — would the contract accept it right now? (simulation)

Guard 2 matters more than it looks: the store holds what the BUYER signed, and the ledger
holds what the hub actually charged. Submitting the signed amount without checking would
let a hub bug (or a tampered store) collect more on chain than it billed off chain.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from aimarket_hub.escrow_bridge import chain, config, escrow_verify, signer as signer_mod, store
from aimarket_hub.escrow_bridge.eip712 import DebitAuthorization
from aimarket_hub.escrow_bridge.errors import (
    BridgeDisabled,
    SubmissionRefused,
)

logger = logging.getLogger(__name__)

# Outcomes reported per row. "blocked" and "refused" are not failures of the row — they
# describe the state of the world around it — so they never move it in the store.
OUTCOME_PLANNED = "planned"
OUTCOME_SUBMITTED = "submitted"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_REFUSED = "refused"
OUTCOME_REJECTED = "rejected"


@dataclass
class MirrorReport:
    """What one pass did, in a shape an operator can read without a database client."""

    dry_run: bool
    strategy: str
    scanned: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def note(self, receipt_id: str, outcome: str, detail: str = "", **extra: Any) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.rows.append(
            {"receipt_id": receipt_id, "outcome": outcome, "detail": detail, **extra}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "strategy": self.strategy,
            "scanned": self.scanned,
            "outcomes": dict(self.outcomes),
            "rows": self.rows,
        }


def _ledger_debited_cents(receipt_id: str) -> int | None:
    """Cents the CHANNEL LEDGER recorded for this receipt, or None if it has no record.

    Read defensively straight from the ledger's database: ``aimarket_hub.channels`` exposes
    no per-receipt accessor, and the bridge must not become a reason to widen that module's
    public surface. Any failure returns None, and None means "refuse to submit" — an
    unverifiable charge is not a charge worth collecting.
    """
    try:
        from aimarket_hub import channels as ch_mod

        path = getattr(ch_mod, "_DB_PATH", "")
        if not path:
            return None
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT amount_cents FROM debited_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("could not read the ledger debit for %s: %s", receipt_id[:14], exc)
        return None
    return int(row["amount_cents"]) if row else None


class Mirror:
    """One pass over the unresolved authorizations."""

    def __init__(
        self,
        *,
        authorizations: store.AuthorizationStore | None = None,
        signer: signer_mod.Signer | None = None,
        require_enabled: bool = True,
    ):
        if require_enabled and not config.enabled():
            raise BridgeDisabled(
                "the escrow bridge is disabled (AIMARKET_ESCROW_BRIDGE_ENABLED=0) — "
                "nothing to mirror"
            )
        self.store = authorizations or store.AuthorizationStore()
        self.signer = signer or signer_mod.build_signer()
        self._dry_run = isinstance(self.signer, signer_mod.PlanOnlySigner)

    # ── one row ──────────────────────────────────────────────────────────────

    def _authorization_of(self, row: store.AuthorizationRow) -> DebitAuthorization:
        return DebitAuthorization(
            channel_id=row.escrow_channel, hub=row.hub, token=row.token,
            amount=row.amount_units, receipt_id=row.receipt_id, nonce=row.nonce,
            deadline=row.deadline,
        )

    def _preflight(self, row: store.AuthorizationRow, report: MirrorReport, *, now: float) -> bool:
        """Guards 1 and 2. Returns False (and records why) if the row must not go out."""
        nxt = self.store.next_for_channel(row.escrow_channel)
        if nxt is not None and nxt.receipt_id != row.receipt_id:
            report.note(
                row.receipt_id, OUTCOME_BLOCKED,
                f"waiting behind nonce {nxt.nonce}; the contract only accepts the "
                "channel's current nonce",
                nonce=row.nonce,
            )
            return False

        if row.expired(now=now):
            # The signature can never be submitted again, so the money is uncollectable.
            # Recording that plainly is more useful than retrying it forever.
            self.store.abandon(row.receipt_id, "authorization deadline passed before submission")
            report.note(
                row.receipt_id, OUTCOME_REJECTED,
                "deadline passed before submission — this debit can no longer be collected "
                "on chain",
                nonce=row.nonce,
            )
            return False

        recorded = _ledger_debited_cents(row.receipt_id)
        if recorded is None:
            report.note(
                row.receipt_id, OUTCOME_BLOCKED,
                "the channel ledger has no debit for this receipt — refusing to collect a "
                "charge the hub cannot show it made",
                nonce=row.nonce,
            )
            return False
        allowed_units = recorded * (10 ** (escrow_verify.TOKEN_DECIMALS - 2))
        if row.amount_units > allowed_units:
            report.note(
                row.receipt_id, OUTCOME_BLOCKED,
                f"authorization is for {row.amount_units} units but the ledger only "
                f"debited {allowed_units} — refusing to over-collect",
                nonce=row.nonce,
            )
            return False
        return True

    def process(self, row: store.AuthorizationRow, report: MirrorReport, *, now: float) -> None:
        """Plan, and submit if the policy allows. Never raises for a normal refusal."""
        if not self._preflight(row, report, now=now):
            return

        auth = self._authorization_of(row)
        data = chain.encode_debit_channel(auth, row.signature)
        sender = self.signer.sender or row.hub
        plan = chain.simulate(to=row.escrow_address, data=data, sender=sender)
        # Recorded whatever the answer: a revert here is the most useful diagnostic the
        # bridge produces, and plan mode exists to collect exactly this.
        self.store.record_plan(row.receipt_id, {**plan, "sender": sender})
        if not plan.get("ok"):
            report.note(
                row.receipt_id, OUTCOME_BLOCKED,
                f"simulation says the contract would reject it: {plan.get('error', '')}",
                nonce=row.nonce,
            )
            return

        if self._dry_run:
            report.note(
                row.receipt_id, OUTCOME_PLANNED,
                "would be accepted; not sent (plan mode)",
                nonce=row.nonce, gas=plan.get("gas"),
                amount_usd=escrow_verify.base_units_to_usd(row.amount_units),
            )
            return

        gas = int(plan.get("gas") or 0)
        tx = signer_mod.UnsignedTx(
            to=row.escrow_address, data=data, chain_id=row.chain_id,
            # A simulated estimate is a floor, not a promise: state can shift between the
            # estimate and inclusion, and an under-funded gas limit reverts the debit.
            gas=int(gas * 1.25) if gas else 250_000,
        )
        try:
            tx_hash = self.signer.submit(tx)
        except SubmissionRefused as exc:
            report.note(row.receipt_id, OUTCOME_REFUSED, str(exc), nonce=row.nonce)
            return
        self.store.mark_submitted(row.receipt_id, tx_hash)
        report.note(
            row.receipt_id, OUTCOME_SUBMITTED, "broadcast; awaiting a receipt",
            nonce=row.nonce, tx_hash=tx_hash,
            amount_usd=escrow_verify.base_units_to_usd(row.amount_units),
        )

    # ── passes ───────────────────────────────────────────────────────────────

    def run(self, *, limit: int = 200, now: float | None = None) -> MirrorReport:
        """Plan (and possibly submit) every unresolved authorization, in nonce order."""
        clock = time.time() if now is None else now
        report = MirrorReport(dry_run=self._dry_run, strategy=self.signer.name)
        rows = self.store.unresolved(limit=limit)
        report.scanned = len(rows)
        for row in rows:
            if row.status == store.SUBMITTED:
                # Already broadcast — the confirm pass owns it, and re-submitting would
                # only spend gas to hit the contract's own usedReceipts guard.
                continue
            try:
                self.process(row, report, now=clock)
            except Exception as exc:  # one bad row must not stall the queue
                logger.warning("mirror: %s failed: %s", row.receipt_id[:14], exc)
                report.note(row.receipt_id, OUTCOME_BLOCKED, f"{type(exc).__name__}: {exc}")
        return report

    def confirm(self, *, limit: int = 200) -> MirrorReport:
        """Resolve broadcast rows by READING the chain, never by trusting the signer.

        A signer (or a node) that claims success proves nothing; a receipt does. A reverted
        receipt is left non-terminal on purpose: the usual cause is a nonce the chain moved
        past, and the next pass re-simulates against current state instead of abandoning a
        buyer's still-valid signature.
        """
        report = MirrorReport(dry_run=self._dry_run, strategy=self.signer.name)
        rows = [r for r in self.store.unresolved(limit=limit) if r.status == store.SUBMITTED]
        report.scanned = len(rows)
        for row in rows:
            if not row.tx_hash:
                report.note(row.receipt_id, OUTCOME_BLOCKED, "submitted without a tx hash")
                continue
            try:
                receipt = chain._pool().call("eth_getTransactionReceipt", [row.tx_hash])
            except Exception as exc:
                report.note(row.receipt_id, OUTCOME_BLOCKED, f"receipt unreadable: {exc}")
                continue
            if not receipt:
                report.note(row.receipt_id, OUTCOME_BLOCKED, "not mined yet")
                continue
            status = str((receipt or {}).get("status", "")).lower()
            if status in ("0x1", "1"):
                self.store.mark_confirmed(row.receipt_id, row.tx_hash)
                report.note(
                    row.receipt_id, OUTCOME_CONFIRMED, "the contract recorded the debit",
                    tx_hash=row.tx_hash,
                    amount_usd=escrow_verify.base_units_to_usd(row.amount_units),
                )
            else:
                self.store.record_plan(
                    row.receipt_id,
                    {"ok": False, "error": f"transaction reverted on chain (status {status})"},
                )
                report.note(
                    row.receipt_id, OUTCOME_BLOCKED,
                    "the transaction reverted; it will be re-simulated against current state",
                    tx_hash=row.tx_hash,
                )
        return report

    def status(self) -> dict[str, Any]:
        """Operator snapshot: configuration, queue, and what is still owed on chain."""
        return {
            "config": config.describe(),
            "signer": self.signer.name,
            "dry_run": self._dry_run,
            "store": self.store.stats(),
        }
