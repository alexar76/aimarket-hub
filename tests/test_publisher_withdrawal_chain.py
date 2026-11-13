from dataclasses import replace

import pytest

from aimarket_hub import settle
from aimarket_hub.publisher_withdrawal_chain import (
    CHAIN_ID,
    USDC_ADDRESS,
    PayoutChainError,
    PublisherWithdrawalChain,
)

SENDER = "0x" + "12" * 20
RECIPIENT = "0x" + "34" * 20
OTHER = "0x" + "56" * 20
TX_HASH = "0x" + "aa" * 32
BLOCK_HASH = "0x" + "bb" * 32
UNITS = 70_010


def _transfer_log():
    return {
        "address": USDC_ADDRESS,
        "topics": [settle.TRANSFER_TOPIC, "0x" + SENDER[2:].zfill(64), "0x" + RECIPIENT[2:].zfill(64)],
        "data": f"0x{UNITS:064x}",
    }


def _receipt(**changes):
    return {
        "transactionHash": TX_HASH,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0x20",
        "status": "0x1",
        "from": SENDER,
        "to": USDC_ADDRESS,
        "logs": [_transfer_log()],
        **changes,
    }


def _read_rpc(receipt, *, finalized="0x22", canonical=BLOCK_HASH, chain_id=CHAIN_ID):
    def rpc(method, params):
        if method == "eth_chainId":
            return hex(chain_id)
        if method == "eth_getTransactionReceipt":
            assert params == [TX_HASH]
            return receipt
        if method == "eth_getBlockByNumber":
            assert params[1] is False
            if params[0] == "finalized":
                return {"number": finalized}
            assert params[0] == "0x20"
            return {"number": "0x20", "hash": canonical}
        raise AssertionError(method)

    return rpc


@pytest.mark.parametrize(("receipt", "expected"), [
    (_receipt(), "paid"),
    (_receipt(status="0x0", logs=[]), "reverted"),
    (_receipt(status="0x2"), "pending"),
    (_receipt(transactionHash="0x" + "cd" * 32), "pending"),
    (_receipt(logs=[{**_transfer_log(), "removed": True}]), "pending"),
    (_receipt(**{"from": OTHER}), "mismatch"),
    (_receipt(to=OTHER), "mismatch"),
    (_receipt(logs=[]), "mismatch"),
    (_receipt(logs=[{**_transfer_log(), "address": OTHER}]), "mismatch"),
    (_receipt(logs=[{**_transfer_log(), "data": f"0x{UNITS + 1:064x}"}]), "mismatch"),
    (_receipt(logs=[_transfer_log(), _transfer_log()]), "mismatch"),
    (None, "pending"),
])
def test_reconcile_exact_transfer(receipt, expected):
    chain = PublisherWithdrawalChain(SENDER, rpc=_read_rpc(receipt))
    assert chain.reconcile(tx_hash=TX_HASH, recipient=RECIPIENT, amount_units=UNITS) == expected


@pytest.mark.parametrize("rpc", [
    _read_rpc(_receipt(), finalized="0x1f"),
    _read_rpc(_receipt(), canonical="0x" + "cc" * 32),
    _read_rpc(_receipt(), chain_id=1),
    _read_rpc(_receipt(blockNumber="0xnot-a-block")),
])
def test_reconcile_waits_for_readable_canonical_finality(rpc):
    chain = PublisherWithdrawalChain(SENDER, rpc=rpc)
    assert chain.reconcile(tx_hash=TX_HASH, recipient=RECIPIENT, amount_units=UNITS) == "pending"


def test_broadcast_retries_exact_signed_bytes_after_timeout():
    eth_account = pytest.importorskip("eth_account")
    from eth_account.typed_transactions import TypedTransaction
    from eth_utils import keccak
    from hexbytes import HexBytes

    account = eth_account.Account.create()
    calls = []

    def rpc(method, params):
        if method == "eth_chainId":
            return hex(CHAIN_ID)
        if method == "eth_getTransactionCount":
            assert params == [account.address.lower(), "pending"]
            return "0x9"
        if method == "eth_estimateGas":
            assert params[0]["to"].lower() == USDC_ADDRESS.lower()
            assert params[0]["from"] == account.address.lower()
            return hex(60_000)
        if method == "eth_gasPrice":
            return hex(1_500_000)
        if method == "eth_sendRawTransaction":
            calls.append(params[0])
            if len(calls) == 1:
                raise TimeoutError("RPC accepted the bytes and closed the connection")
            return "0x" + keccak(HexBytes(params[0])).hex()
        raise AssertionError(method)

    chain = PublisherWithdrawalChain(account.address, rpc=rpc)
    payout = chain.prepare(recipient=RECIPIENT, amount_units=UNITS,
                           nonce=chain.pending_nonce(), private_key=account.key.hex())
    assert calls == []
    raw = HexBytes(payout.raw_tx)
    tx = TypedTransaction.from_bytes(raw).as_dict()
    assert eth_account.Account.recover_transaction(raw) == account.address
    assert tx["chainId"] == CHAIN_ID
    assert tx["nonce"] == 9
    assert tx["value"] == 0
    assert "0x" + bytes(tx["to"]).hex() == USDC_ADDRESS.lower()
    assert tx["data"].hex() == f"a9059cbb{RECIPIENT[2:]:0>64}{UNITS:064x}"
    with pytest.raises(ValueError, match="invalid signed Base USDC payout"):
        chain.broadcast(replace(payout, tx_hash=TX_HASH))
    for recipient, units, nonce in [(OTHER, UNITS, 9), (RECIPIENT, UNITS + 1, 9), (RECIPIENT, UNITS, 10)]:
        with pytest.raises(ValueError, match="invalid signed Base USDC payout"):
            chain.broadcast(payout, recipient=recipient, amount_units=units, nonce=nonce)
    with pytest.raises(PayoutChainError, match="unconfirmed"):
        chain.broadcast(payout, recipient=RECIPIENT, amount_units=UNITS, nonce=9)
    assert chain.broadcast(payout, recipient=RECIPIENT, amount_units=UNITS, nonce=9) == payout.tx_hash
    assert calls == [payout.raw_tx, payout.raw_tx]


def test_prepare_refuses_unbound_sender_and_invalid_amount_before_rpc():
    eth_account = pytest.importorskip("eth_account")
    account = eth_account.Account.create()

    def rpc(method, params):
        pytest.fail(f"unexpected RPC: {method}")

    chain = PublisherWithdrawalChain(SENDER, rpc=rpc)
    with pytest.raises(ValueError, match="does not match"):
        chain.prepare(recipient=RECIPIENT, amount_units=UNITS, nonce=0, private_key=account.key.hex())
    for invalid in (True, 0, -1, 0.1, 2**256):
        with pytest.raises(ValueError, match="atomic units"):
            chain.prepare(recipient=RECIPIENT, amount_units=invalid, nonce=0, private_key=account.key.hex())


def test_rpc_failure_during_reconciliation_keeps_the_withdrawal_pending():
    def rpc(method, params):
        raise TimeoutError

    chain = PublisherWithdrawalChain(SENDER, rpc=rpc)
    assert chain.reconcile(tx_hash=TX_HASH, recipient=RECIPIENT, amount_units=UNITS) == "pending"
