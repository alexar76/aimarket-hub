"""Search by default; --invoke runs one weather call capped at $0.01.

Requires Python 3.11+ and aimarket-agent>=2.4.0. See docs/agent-first-call.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re

from aimarket_agent import A2AClient
from aimarket_agent.a2a import task_result, task_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--invoke", action="store_true", help="Run one call, up to $0.01")
    mode.add_argument("--task-id", help="Read an existing Task; do not create another order")
    parser.add_argument("--payment-tx", help="Submit an already mined x402 transfer for --task-id")
    args = parser.parse_args()
    if args.payment_tx and (not args.task_id or not re.fullmatch(r"0x[0-9a-fA-F]{64}", args.payment_tx)):
        parser.error("--payment-tx requires --task-id and a 32-byte transaction hash")
    client = A2AClient(
        os.getenv("AIMARKET_HUB_URL", "https://modelmarket.dev"),
        api_key=os.getenv("AIMARKET_KEY", ""),
    )
    try:
        card = client.card()
        print("Agent:", card.get("name", "AIMarket"))
        if args.task_id:
            task = client.get_task(args.task_id)
            if args.payment_tx and task_state(task) == "TASK_STATE_INPUT_REQUIRED":
                task = client.pay_x402(task, args.payment_tx)
            return show_result(task)
        offers = client.search("gaia weather open-meteo", budget=0.01, limit=10)["matches"]
        offer = next((m for m in offers
                      if m.get("product_id") == "gaia.gateway"
                      and m.get("capability_id") == "gaia.weather.read@v1"), None)
        if offer is None:
            print("Weather offer unavailable. Retry later or inspect the catalogue.")
            return 1
        print("Offer:", json.dumps(offer, ensure_ascii=False, indent=2))
        if not args.invoke:
            print("Search only. Use --invoke to run one call capped at $0.01.")
            return 0
        task = client.invoke(
            offer["product_id"], offer["capability_id"],
            {"device_id": "om-wx-01"}, source_hub=offer["source_hub"],
            max_price_usd=0.01,
        )
        return show_result(task)
    finally:
        client.close()


def show_result(task: dict) -> int:
    state = task_state(task)
    print("Task:", task.get("id"), state)
    if state != "TASK_STATE_COMPLETED":
        print("Status:", json.dumps(task.get("status"), ensure_ascii=False, indent=2))
        print("Call incomplete. See docs/a2a.md before resuming or retrying.")
        return 2
    if not task.get("receipt_verified"):
        print("Receipt verification failed:", task.get("receipt_verify_reason"))
        return 3
    result = task_result(task)
    if result is None:
        print("Receipt verified, but weather result is missing. See docs/a2a.md.")
        return 4
    print("Receipt verified against the signer's key.")
    print("Result:", json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
