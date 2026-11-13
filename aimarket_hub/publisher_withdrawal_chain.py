"""Sign and reconcile publisher transfers of canonical USDC on Base.

The caller persists the signed bytes and hash before broadcasting. Retrying the
same bytes reuses the transaction hash and nonce. A broadcast response is never
proof of payment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from aimarket_hub import chain_net, settle, x402

CHAIN_ID = 8453
USDC_ADDRESS = str(x402.ASSET_PROFILES["base", "USDC"]["address"])
USDC_DECIMALS = int(x402.ASSET_PROFILES["base", "USDC"]["decimals"])
TRANSFER_SELECTOR = "a9059cbb"
ZERO_ADDRESS = "0x" + "0" * 40
MAX_TRANSFER_GAS = 250_000
_RPC_TIMEOUT = 8.0
_HEX_QUANTITY = re.compile(r"0x[0-9a-fA-F]{1,64}")

Rpc = Callable[[str, list[Any]], Any]
PayoutStatus = Literal["pending", "paid", "reverted", "mismatch"]


class PayoutChainError(RuntimeError):
    """A chain operation could not be completed; persisted signed bytes stay reserved."""


@dataclass(frozen=True)
class SignedPayout:
    raw_tx: str
    tx_hash: str


def _quantity(value: Any) -> int:
    if isinstance(value, str) and _HEX_QUANTITY.fullmatch(value):
        return int(value, 16)
    raise ValueError("invalid JSON-RPC quantity")


def _address(value: str) -> str:
    if not isinstance(value, str) or not settle.is_address(value) or value.strip().lower() == ZERO_ADDRESS:
        raise ValueError("payout requires a nonzero EVM address")
    return value.strip().lower()


def _transfer_data(recipient: str, amount_units: int) -> str:
    if type(amount_units) is not int or not 0 < amount_units < 2**256:
        raise ValueError("payout amount must be positive USDC atomic units")
    return f"0x{TRANSFER_SELECTOR}{recipient[2:].zfill(64)}{amount_units:064x}"


class PublisherWithdrawalChain:
    def __init__(self, sender: str, *, rpc: Rpc | None = None):
        self.sender = _address(sender)
        self._rpc = rpc

    def _on_base(self, operation: Callable[[Rpc], Any]) -> Any:
        def run(rpc: Rpc) -> Any:
            if _quantity(rpc("eth_chainId", [])) != CHAIN_ID:
                raise PayoutChainError("payout RPC is not Base mainnet")
            return operation(rpc)

        if self._rpc is not None:
            return run(self._rpc)

        pool = chain_net.pool_for_urls(settle.rpc_urls(CHAIN_ID), chain_id=CHAIN_ID, timeout=_RPC_TIMEOUT)
        # Verify the chain and run the whole operation at the same node. In particular a
        # receipt from one node cannot borrow a finalized head from another.
        return pool.run(lambda url: run(lambda method, params: settle._rpc(url, method, params, _RPC_TIMEOUT)))

    def pending_nonce(self) -> int:
        """Read the dedicated sender's nonce before the caller allocates it under a lock."""
        return self._on_base(lambda rpc: _quantity(rpc("eth_getTransactionCount", [self.sender, "pending"])))

    def prepare(self, *, recipient: str, amount_units: int, nonce: int, private_key: str) -> SignedPayout:
        from eth_account import Account
        from eth_utils import keccak, to_checksum_address

        recipient = _address(recipient)
        if recipient in (self.sender, USDC_ADDRESS.lower()):
            raise ValueError("invalid payout recipient")
        if type(nonce) is not int or not 0 <= nonce < 2**64 - 1:
            raise ValueError("invalid payout transaction nonce")
        data = _transfer_data(recipient, amount_units)
        try:
            account = Account.from_key(private_key)
        except (TypeError, ValueError):
            raise ValueError("invalid payout signing key") from None
        if account.address.lower() != self.sender:
            raise ValueError("payout signing key does not match the configured sender")

        def sign(rpc: Rpc) -> SignedPayout:
            to = to_checksum_address(USDC_ADDRESS)
            tx = {"to": to, "data": data, "value": 0}
            estimate = _quantity(rpc("eth_estimateGas", [{**tx, "from": self.sender, "value": "0x0"}]))
            gas = (estimate * 6 + 4) // 5
            price = _quantity(rpc("eth_gasPrice", []))
            if not 0 < gas <= MAX_TRANSFER_GAS or not 0 < price < 2**255:
                raise PayoutChainError("invalid payout gas estimate")
            signed = account.sign_transaction({
                **tx, "type": 2, "chainId": CHAIN_ID, "nonce": nonce, "gas": gas,
                "maxFeePerGas": 2 * price, "maxPriorityFeePerGas": min(price, 1_000_000),
            })
            raw = bytes(signed.raw_transaction)
            return SignedPayout(raw_tx="0x" + raw.hex(), tx_hash="0x" + keccak(raw).hex())

        return self._on_base(sign)

    def broadcast(self, payout: SignedPayout, *, recipient: str | None = None,
                  amount_units: int | None = None, nonce: int | None = None) -> str:
        """Send persisted bytes. Any ambiguous result can be retried with this same payout."""
        if recipient is not None or amount_units is not None or nonce is not None:
            if recipient is None or amount_units is None or type(nonce) is not int:
                raise ValueError("payout recipient, amount and nonce are required together")
            expected_data = _transfer_data(_address(recipient), amount_units)[2:]
        else:
            expected_data = None
        self._check_signed_payout(payout, expected_data=expected_data, nonce=nonce)
        try:
            returned = self._on_base(lambda rpc: rpc("eth_sendRawTransaction", [payout.raw_tx]))
        except Exception:
            raise PayoutChainError("payout broadcast is unconfirmed. Reconcile the same transaction.") from None
        if not isinstance(returned, str) or returned.lower() != payout.tx_hash.lower():
            raise PayoutChainError("payout RPC returned a different transaction hash")
        return payout.tx_hash.lower()

    def _check_signed_payout(self, payout: SignedPayout, *, expected_data: str | None, nonce: int | None) -> None:
        from eth_account import Account
        from eth_account.typed_transactions import TypedTransaction
        from eth_utils import keccak
        from hexbytes import HexBytes

        try:
            raw = HexBytes(payout.raw_tx)
            if not raw or raw[0] != 2 or len(raw) > 512 or "0x" + keccak(raw).hex() != payout.tx_hash.lower():
                raise ValueError
            tx = TypedTransaction.from_bytes(raw).as_dict()
            data = bytes(tx["data"]).hex()
            if (tx["chainId"] != CHAIN_ID or "0x" + bytes(tx["to"]).hex() != USDC_ADDRESS.lower()
                    or tx["value"] != 0 or tx.get("accessList") or not 0 < tx["gas"] <= MAX_TRANSFER_GAS
                    or len(data) != 136 or data[:32] != TRANSFER_SELECTOR + "0" * 24
                    or int(data[32:72], 16) == 0 or int(data[72:], 16) == 0
                    or (expected_data is not None and (data != expected_data or tx["nonce"] != nonce))
                    or Account.recover_transaction(raw).lower() != self.sender):
                raise ValueError
        except Exception:
            raise ValueError("invalid signed Base USDC payout") from None

    def reconcile(self, *, tx_hash: str, recipient: str, amount_units: int) -> PayoutStatus:
        """Check a known signed hash against its canonical, finalized transaction receipt."""
        if not isinstance(tx_hash, str) or not settle.is_tx_hash(tx_hash):
            raise ValueError("invalid payout transaction hash")
        tx_hash = tx_hash.strip().lower()
        recipient = _address(recipient)
        amount_word = _transfer_data(recipient, amount_units)[-64:]

        def verify(rpc: Rpc) -> PayoutStatus:
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            if not receipt or receipt["transactionHash"].lower() != tx_hash:
                return "pending"
            block_number = _quantity(receipt["blockNumber"])
            finalized = rpc("eth_getBlockByNumber", ["finalized", False])
            if not finalized or not 0 < block_number <= _quantity(finalized["number"]):
                return "pending"
            canonical = rpc("eth_getBlockByNumber", [hex(block_number), False])
            if (not canonical or _quantity(canonical["number"]) != block_number
                    or not settle.is_tx_hash(receipt["blockHash"])
                    or canonical["hash"].lower() != receipt["blockHash"].lower()):
                return "pending"
            if receipt["from"].lower() != self.sender or receipt["to"].lower() != USDC_ADDRESS.lower():
                return "mismatch"
            status = _quantity(receipt["status"])
            if status == 0:
                return "reverted"
            if status != 1 or not isinstance(receipt["logs"], list):
                return "pending"

            sender_topic = "0x" + self.sender[2:].zfill(64)
            recipient_topic = "0x" + recipient[2:].zfill(64)
            transfers = []
            for log in receipt["logs"]:
                if log["address"].lower() != USDC_ADDRESS.lower():
                    continue
                topics = [topic.lower() for topic in log["topics"]]
                if len(topics) == 3 and topics[:2] == [settle.TRANSFER_TOPIC, sender_topic]:
                    if log.get("removed") is True:
                        return "pending"
                    transfers.append((topics[2], log["data"].lower()))
            return "paid" if transfers == [(recipient_topic, "0x" + amount_word)] else "mismatch"

        try:
            return self._on_base(verify)
        except Exception:
            # Missing, malformed, or unavailable chain data must never resolve a withdrawal.
            return "pending"
