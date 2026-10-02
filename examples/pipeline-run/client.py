#!/usr/bin/env python3
"""Preflight, sign locally, invoke pipeline.run once; resume the same run on HTTP 202.

Dependencies: pip install 'aimarket-hub[escrow]'. No private key is sent to the hub.
Without --execute this prints a quote only and never signs or sends a payment.
"""
import argparse
import json
import os
from pathlib import Path

import httpx

from aimarket_hub.pipeline_payments import payment_transaction


def rpc(client, url, method, params):
    response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"RPC refused {method}: {body['error']}")
    return body["result"]


def checked(response):
    response.raise_for_status()
    return response.json()


def persist(path, state):
    # The scoped run token and signed payments authorize spending. Keep this file
    # private, and save BEFORE invoke so a lost response cannot create another run.
    with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        os.chmod(path, 0o600)
        json.dump(state, out, indent=2)
        out.flush()
        os.fsync(out.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blueprint", nargs="?", help="JSON object containing Studio nodes")
    parser.add_argument("--hub", help="target hub origin")
    parser.add_argument("--rpc", help="RPC for the quoted chain")
    parser.add_argument("--budget", type=float, help="maximum token budget in USD, excluding gas")
    parser.add_argument("--execute", action="store_true", help="authorize real wallet payments for the quoted graph")
    parser.add_argument("--state", default="pipeline-run.private.json", help="private recovery file")
    parser.add_argument("--resume", action="store_true", help="resume saved run; never create a new quote")
    args = parser.parse_args()
    with httpx.Client(timeout=180) as client:
        if args.resume:
            saved = json.loads(Path(args.state).read_text())
            hub, request = saved["hub"], saved["request"]
        else:
            if not args.hub or not args.blueprint or args.budget is None:
                parser.error("provide blueprint, --hub and --budget, or use --resume")
            if args.execute and Path(args.state).exists():
                parser.error("recovery file already exists; use --resume or a new --state path")
            hub = args.hub.rstrip("/")
            graph = json.loads(Path(args.blueprint).read_text())
            quote = checked(client.post(hub + "/studio/preflight", json={
                "nodes": graph["nodes"], "max_budget_usd": args.budget}))
            print(json.dumps({k: v for k, v in quote.items() if k != "access_token"}, indent=2))
            if not quote["ready"] or not args.execute:
                return
            wallet, account = "0x" + "00" * 20, None
            if int(quote["total_units"]):
                from eth_account import Account
                secret = os.environ.get("PIPELINE_WALLET_KEY")
                if not secret or not args.rpc:
                    parser.error("paid execution needs PIPELINE_WALLET_KEY and --rpc")
                account = Account.from_key(secret)
                wallet = account.address
            offers = checked(client.post(hub + f"/studio/paid-runs/{quote['run_id']}/prepare-pipeline",
                headers={"X-Studio-Run-Token": quote["access_token"]}, json={"wallet": wallet}))["offers"]
            transactions = {}
            if offers:
                from eth_account.messages import encode_typed_data
                from eth_utils import to_checksum_address
                terms = offers[0]["terms"]
                if int(rpc(client, args.rpc, "eth_chainId", []), 16) != terms["chain_id"]:
                    raise RuntimeError("RPC chain differs from the quote")
                balance = int(rpc(client, args.rpc, "eth_call", [{"to": terms["token_contract"],
                    "data": "0x70a08231" + wallet[2:].lower().zfill(64)}, "latest"]), 16)
                if balance < int(quote["total_units"]):
                    raise RuntimeError("fund the wallet with the full quoted token budget first")
                nonce = int(rpc(client, args.rpc, "eth_getTransactionCount", [wallet, "pending"]), 16)
                gas_price = int(rpc(client, args.rpc, "eth_gasPrice", []), 16)
                max_fee = max(gas_price * 2, 1)
                gas_limit = 300_000
                native = int(rpc(client, args.rpc, "eth_getBalance", [wallet, "latest"]), 16)
                if native < len(offers) * gas_limit * max_fee:
                    raise RuntimeError("insufficient native balance for the signed maximum gas cost")
                for i, step in enumerate(offers):
                    signature = account.sign_message(encode_typed_data(full_message=step["authorization"])).signature
                    tx = payment_transaction(step, wallet, "0x" + signature.hex().removeprefix("0x"))
                    tx.pop("from")
                    tx.update(to=to_checksum_address(tx["to"]), type=2, chainId=terms["chain_id"],
                        nonce=nonce + i, gas=gas_limit, maxFeePerGas=max_fee,
                        maxPriorityFeePerGas=min(gas_price, max_fee))
                    transactions[step["id"]] = "0x" + account.sign_transaction(tx).raw_transaction.hex().removeprefix("0x")
            request = {"product_id": "hephaestus", "capability_id": "pipeline.run@v1", "source_hub": "local",
                       "input": {"run_id": quote["run_id"], "access_token": quote["access_token"],
                                 "transactions": transactions}}
            persist(args.state, {"hub": hub, "request": request})
        response = client.post(hub + "/ai-market/v2/invoke", json=request)
        answer = checked(response)
        print(json.dumps(answer, indent=2))
        if response.status_code == 202:
            print("Run is pending. Use --resume with the same --state file; do not create new payments.")


if __name__ == "__main__":
    main()
