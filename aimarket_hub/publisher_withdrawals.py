"""Reservations and finalized Base USDC transfers for publisher earnings."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from decimal import Decimal
from typing import Any

from aimarket_hub import credits, deposit_watch, realm, settle, x402
from aimarket_hub.db_backend import atomic, returning_one
from aimarket_hub.publisher_withdrawal_chain import (
    CHAIN_ID, USDC_ADDRESS, ZERO_ADDRESS, PayoutChainError, PublisherWithdrawalChain, SignedPayout,
)

WALLET_PROOF_TTL = 600
UNITS_PER_MC = 10
_REQUEST_ID = re.compile(r"[a-zA-Z0-9_-]{16,80}")
_AMOUNT = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,5})?")
FINAL_STATES = frozenset({"paid", "reverted", "cancelled"})
logger = logging.getLogger(__name__)


class WithdrawalError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def configured_sender() -> str:
    address = os.getenv("AIMARKET_PUBLISHER_PAYOUT_FROM", "").strip().lower()
    if realm.is_uni() or not settle.is_address(address) or address in (ZERO_ADDRESS, USDC_ADDRESS.lower()):
        return ""
    # This signer owns its nonce sequence. Other rails may send or attest transfers from their own wallets.
    other_wallets = (x402.recipient(), os.getenv("AIMARKET_PAYMENT_RECIPIENT", ""), deposit_watch.dedicated_wallet())
    return "" if address in {value.strip().lower() for value in other_wallets} else address


def terms(body: dict[str, Any], sender: str) -> tuple[str, int, str]:
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise WithdrawalError(400, "request_id must be 16 to 80 letters, digits, hyphens or underscores")
    amount = str(body.get("amount_usdc", ""))
    if not _AMOUNT.fullmatch(amount):
        raise WithdrawalError(400, "amount_usdc must be a positive decimal with at most five fractional digits")
    amount_mc = int(Decimal(amount) * credits.MILLICENTS_PER_DOLLAR)
    if not amount_mc:
        raise WithdrawalError(400, "amount_usdc must be positive")
    recipient = str(body.get("recipient") or "").strip().lower()
    if not settle.is_address(recipient) or recipient in (ZERO_ADDRESS, USDC_ADDRESS.lower(), sender):
        raise WithdrawalError(400, "recipient must be a nonzero Base wallet distinct from the payout sender")
    return request_id, amount_mc, recipient


def wallet_message(*, hub_url: str, account: str, sender: str, request_id: str,
                   amount_mc: int, recipient: str, issued_at: int) -> str:
    return "\n".join((
        "AIMarket publisher withdrawal v1", f"hub: {hub_url.rstrip('/')}", f"account: {account}",
        f"request: {request_id}", f"chain: eip155:{CHAIN_ID}", f"token: {USDC_ADDRESS.lower()}",
        f"amount_atomic: {amount_mc * UNITS_PER_MC}", f"recipient: {recipient}", f"sender: {sender}",
        f"issued_at: {issued_at}",
    ))


def public(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("withdrawal_id", "request_id", "recipient", "sender", "status", "created_at", "updated_at", "tx_hash")
    return {**{key: row[key] for key in keys}, "amount_usdc": credits.mc_to_usd(row["amount_mc"]),
            "amount_atomic": str(row["amount_mc"] * UNITS_PER_MC), "chain_id": CHAIN_ID, "token": USDC_ADDRESS}


class PublisherWithdrawals:
    def __init__(self, ledger: credits.CreditsLedger):
        self.ledger = ledger
        self.conn = ledger._conn

    def get(self, withdrawal_id: str, *, account: str | None = None) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM publisher_withdrawals WHERE withdrawal_id = ?",
                                (withdrawal_id,)).fetchone()
        return dict(row) if row and (account is None or row["account_id"] == account) else None

    def by_request(self, account: str, request_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM publisher_withdrawals WHERE account_id = ? AND request_id = ?",
                                (account, request_id)).fetchone()
        return dict(row) if row else None

    def pending(self, *, signed_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        condition = "status IN ('signed', 'broadcast')" if signed_only else "status IN ('requested', 'signed', 'broadcast')"
        rows = self.conn.execute(
            f"SELECT * FROM publisher_withdrawals WHERE {condition} ORDER BY created_at, withdrawal_id LIMIT ?", (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def next_for_sender(self, sender: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT w.* FROM publisher_withdrawals w JOIN credit_accounts a ON a.account_id = w.account_id "
            "WHERE w.sender = ? AND (w.status IN ('signed', 'broadcast') "
            "OR (w.status = 'requested' AND a.status = 'active')) "
            "ORDER BY CASE WHEN w.status = 'requested' THEN 1 ELSE 0 END, w.created_at, w.withdrawal_id LIMIT 1",
            (sender,),
        ).fetchone()
        return dict(row) if row else None

    def summary(self, account: str, limit: int) -> dict[str, Any]:
        balance = self.conn.execute(
            "SELECT balance_mc, publisher_cash_mc, publisher_payout_wallet FROM credit_accounts WHERE account_id = ?",
            (account,),
        ).fetchone()
        rows = self.conn.execute(
            "SELECT * FROM publisher_withdrawals WHERE account_id = ? ORDER BY created_at DESC, withdrawal_id DESC LIMIT ?",
            (account, limit),
        ).fetchall()
        return {"withdrawable_usdc": credits.mc_to_usd(min(balance["balance_mc"], balance["publisher_cash_mc"])),
                "payout_wallet": balance["publisher_payout_wallet"] or None,
                "withdrawals": [public(dict(row)) for row in rows]}

    def reserve(self, *, account: str, request_id: str, amount_mc: int, recipient: str,
                sender: str) -> dict[str, Any]:
        if type(amount_mc) is not int or amount_mc <= 0:
            raise WithdrawalError(400, "amount_usdc must be positive")
        with atomic(self.conn):
            # Serialize withdrawals, purchases and payout-wallet enrollment against this account.
            locked = returning_one(self.conn,
                "UPDATE credit_accounts SET balance_mc = balance_mc WHERE account_id = ? AND status = 'active' "
                "RETURNING balance_mc, publisher_cash_mc, publisher_payout_wallet", (account,),
            )
            if not locked:
                raise WithdrawalError(403, "account is disabled or unknown")
            previous = self.by_request(account, request_id)
            if previous:
                return self.replay(previous, amount_mc, recipient)
            if locked["publisher_payout_wallet"] not in ("", recipient):
                raise WithdrawalError(409, "recipient differs from this account's verified payout wallet")
            if min(locked["balance_mc"], locked["publisher_cash_mc"]) < amount_mc:
                raise WithdrawalError(409, "insufficient withdrawable publisher earnings")
            self.conn.execute(
                "UPDATE credit_accounts SET balance_mc = balance_mc - ?, publisher_cash_mc = publisher_cash_mc - ?, "
                "publisher_payout_wallet = ? WHERE account_id = ?", (amount_mc, amount_mc, recipient, account),
            )
            withdrawal_id = "pwd_" + uuid.uuid4().hex
            now = int(time.time())
            row = returning_one(self.conn,
                "INSERT INTO publisher_withdrawals (withdrawal_id, account_id, request_id, amount_mc, "
                "recipient, sender, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?) RETURNING *",
                (withdrawal_id, account, request_id, amount_mc, recipient, sender, now, now),
            )
            self.ledger._log(account, "publisher_withdraw_requested", amount_mc, receipt_id=withdrawal_id)
            return dict(row)

    @staticmethod
    def replay(row: dict[str, Any], amount_mc: int, recipient: str) -> dict[str, Any]:
        if (row["amount_mc"], row["recipient"]) != (amount_mc, recipient):
            raise WithdrawalError(409, "request_id already used with different withdrawal terms")
        return row

    def next_nonce(self, sender: str, pending_nonce: int) -> int:
        rows = self.conn.execute(
            "SELECT MAX(nonce) AS last, MAX(CASE WHEN status IN ('signed', 'broadcast') THEN 1 ELSE 0 END) AS active "
            "FROM publisher_withdrawals WHERE sender = ?", (sender,),
        ).fetchone()
        if rows["active"]:
            raise WithdrawalError(409, "another payout is awaiting reconciliation for this sender")
        last = rows["last"]
        return max(pending_nonce, last + 1) if last is not None else pending_nonce

    def save_signed(self, row: dict[str, Any], nonce: int, payout: SignedPayout) -> dict[str, Any]:
        sender, withdrawal_id = row["sender"], row["withdrawal_id"]
        with atomic(self.conn):
            self.conn.execute("INSERT INTO publisher_withdrawal_signers (sender) VALUES (?) ON CONFLICT DO NOTHING",
                              (sender,))
            self.conn.execute("UPDATE publisher_withdrawal_signers SET version = version + 1 WHERE sender = ?", (sender,))
            current = self.get(withdrawal_id)
            if current["status"] != "requested":
                return current
            active = returning_one(self.conn,
                "UPDATE credit_accounts SET balance_mc = balance_mc WHERE account_id = ? AND status = 'active' "
                "RETURNING account_id", (current["account_id"],),
            )
            if not active:
                raise WithdrawalError(403, "account is disabled or unknown")
            if nonce != self.next_nonce(sender, nonce):
                raise WithdrawalError(409, "sender nonce advanced while preparing this payout; retry")
            updated = returning_one(self.conn,
                "UPDATE publisher_withdrawals SET status = 'signed', nonce = ?, tx_hash = ?, raw_tx = ?, updated_at = ? "
                "WHERE withdrawal_id = ? AND status = 'requested' RETURNING *",
                (nonce, payout.tx_hash, payout.raw_tx, int(time.time()), withdrawal_id),
            )
            return dict(updated) if updated is not None else self.get(withdrawal_id)

    def mark_broadcast(self, withdrawal_id: str, tx_hash: str) -> None:
        with atomic(self.conn):
            self.conn.execute("UPDATE publisher_withdrawals SET status = 'broadcast', updated_at = ? "
                              "WHERE withdrawal_id = ? AND tx_hash = ? AND status = 'signed'",
                              (int(time.time()), withdrawal_id, tx_hash))

    def _restore_reservation(self, row: Any) -> None:
        self.conn.execute("UPDATE credit_accounts SET balance_mc = balance_mc + ?, "
                          "publisher_cash_mc = publisher_cash_mc + ? WHERE account_id = ?",
                          (row["amount_mc"], row["amount_mc"], row["account_id"]))

    def cancel(self, withdrawal_id: str, account: str) -> dict[str, Any]:
        with atomic(self.conn):
            self.conn.execute("UPDATE credit_accounts SET balance_mc = balance_mc WHERE account_id = ?", (account,))
            row = returning_one(self.conn,
                "UPDATE publisher_withdrawals SET status = 'cancelled', updated_at = ? "
                "WHERE withdrawal_id = ? AND account_id = ? AND status = 'requested' RETURNING *",
                (int(time.time()), withdrawal_id, account),
            )
            if not row:
                current = self.get(withdrawal_id, account=account)
                if not current:
                    raise WithdrawalError(404, "unknown withdrawal")
                if current["status"] == "cancelled":
                    return current
                raise WithdrawalError(409, "withdrawal can only be cancelled before signing")
            self._restore_reservation(row)
            self.ledger._log(account, "publisher_withdraw_cancelled", row["amount_mc"], receipt_id=withdrawal_id)
            return dict(row)

    def finish(self, withdrawal_id: str, tx_hash: str, verdict: str) -> dict[str, Any]:
        if verdict not in ("paid", "reverted"):
            raise ValueError("withdrawal is not finalized")
        with atomic(self.conn):
            row = returning_one(self.conn,
                "UPDATE publisher_withdrawals SET status = ?, updated_at = ?, raw_tx = '' "
                "WHERE withdrawal_id = ? AND tx_hash = ? AND status IN ('signed', 'broadcast') RETURNING *",
                (verdict, int(time.time()), withdrawal_id, tx_hash),
            )
            if not row:
                return self.get(withdrawal_id)
            account, amount_mc = row["account_id"], row["amount_mc"]
            if verdict == "reverted":
                self._restore_reservation(row)
            self.ledger._log(account, f"publisher_withdraw_{verdict}", amount_mc,
                             receipt_id=withdrawal_id, note=tx_hash)
            return dict(row)

    def process(self, withdrawal_id: str, chain: PublisherWithdrawalChain, *, private_key: str | None = None,
                broadcast: bool = False) -> dict[str, Any]:
        row = self.get(withdrawal_id)
        if not row:
            raise WithdrawalError(404, "unknown withdrawal")
        if row["status"] in FINAL_STATES:
            return row
        if row["sender"] != chain.sender:
            raise WithdrawalError(409, "payout sender differs from the reserved withdrawal")
        if row["status"] == "requested" and broadcast:
            if not private_key:
                raise WithdrawalError(503, "configure the payout signing key on the operator host")
            nonce = self.next_nonce(chain.sender, chain.pending_nonce())
            payout = chain.prepare(recipient=row["recipient"], amount_units=row["amount_mc"] * UNITS_PER_MC,
                                   nonce=nonce, private_key=private_key)
            row = self.save_signed(row, nonce, payout)
        if row["status"] not in ("signed", "broadcast"):
            return row
        tx_hash = row["tx_hash"]
        verdict = chain.reconcile(tx_hash=tx_hash, recipient=row["recipient"], amount_units=row["amount_mc"] * UNITS_PER_MC)
        if verdict in ("paid", "reverted"):
            return self.finish(withdrawal_id, tx_hash, verdict)
        if verdict == "mismatch":
            raise WithdrawalError(409, "finalized transaction does not match the reserved payout; funds remain reserved")
        if broadcast:
            try:
                chain.broadcast(SignedPayout(row["raw_tx"], tx_hash), recipient=row["recipient"],
                                amount_units=row["amount_mc"] * UNITS_PER_MC, nonce=row["nonce"])
            except PayoutChainError:
                # A timeout or 'already known' can follow successful broadcast. Keep the exact bytes for retry.
                latest = self.get(withdrawal_id)
                return latest if latest["status"] in FINAL_STATES else {**latest, "broadcast_unconfirmed": True}
            self.mark_broadcast(withdrawal_id, tx_hash)
        return self.get(withdrawal_id)

    def reconcile_pending(self) -> None:
        for row in self.pending(signed_only=True, limit=25):
            try:
                self.process(row["withdrawal_id"], PublisherWithdrawalChain(row["sender"]))
            except Exception as exc:  # noqa: BLE001 - preserve this reservation and inspect the other payouts
                logger.warning("publisher withdrawal %s: reconciliation did not resolve: %s", row["withdrawal_id"], exc)
