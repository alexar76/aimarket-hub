"""Run publisher payout operations on the operator host, separate from the Hub API."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat

from aimarket_hub import credits, realm
from aimarket_hub.database import HubDatabase
from aimarket_hub.publisher_withdrawal_chain import PayoutChainError, PublisherWithdrawalChain
from aimarket_hub.publisher_withdrawals import FINAL_STATES, PublisherWithdrawals, WithdrawalError, configured_sender, public


def _read_key() -> str:
    path = os.getenv("AIMARKET_PUBLISHER_PAYOUT_KEY_FILE", "").strip()
    if not path:
        raise WithdrawalError(503, "set AIMARKET_PUBLISHER_PAYOUT_KEY_FILE on the operator host")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as key_file:
            mode = os.fstat(key_file.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError
            key = key_file.read(80).decode("ascii").strip()
            if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key):
                raise ValueError
    except (OSError, ValueError):
        raise WithdrawalError(503, "payout key must be a readable private key in an owner-only regular file") from None
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="list pending publisher payouts")
    commands.add_parser("run-once", help="process one payout from the configured sender's queue")
    commands.add_parser("send", help="sign and broadcast, or retry the exact persisted transaction").add_argument(
        "withdrawal_id",
    )
    commands.add_parser("reconcile", help="check finality and apply the receipt without broadcasting").add_argument(
        "withdrawal_id",
    )
    args = parser.parse_args()
    if realm.is_uni():
        parser.exit(1, "Base publisher withdrawals are disabled in UNI\n")
    db = HubDatabase(os.getenv("AIMARKET_DB_PATH") or "data/hub.db", database_url=os.getenv("DATABASE_URL", ""))
    try:
        store = PublisherWithdrawals(credits.CreditsLedger(db._conn))
        if args.action == "list":
            result = [public(row) for row in store.pending()]
        else:
            send = args.action in ("run-once", "send")
            sender = configured_sender() if send else ""
            if args.action == "run-once":
                if not sender:
                    raise WithdrawalError(503, "configure a dedicated Base publisher payout sender")
                row = store.next_for_sender(sender)
                if not row:
                    print("{}")
                    return
            else:
                row = store.get(args.withdrawal_id)
            if not row:
                raise WithdrawalError(404, "unknown withdrawal")
            if send and row["status"] not in FINAL_STATES and row["sender"] != sender:
                raise WithdrawalError(409, "configured publisher payout sender does not match this withdrawal")
            key = _read_key() if send and row["status"] == "requested" else None
            processed = store.process(row["withdrawal_id"], PublisherWithdrawalChain(row["sender"]),
                                      private_key=key, broadcast=send)
            result = public(processed)
            if processed.get("broadcast_unconfirmed"):
                result["broadcast_unconfirmed"] = True
        print(json.dumps(result, indent=2))
    except (WithdrawalError, PayoutChainError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
