"""The market rail: the buyer pays the seller on-chain; the Hub only verifies.

INVARIANT
    USDC moves from the buyer to the listing's ``payout_address``. The Hub
    reads the chain (and, when present, the x402 ``PAYMENT-SIGNATURE`` that
    bound that transfer to this call) and never holds, credits, or reroutes
    the money. The host has no key and takes no cut.

This is the same economic joint as HESTIA, lifted to the catalogue:

    announce (hearth publishes ``payout_address``)
      → discover (Hub index / Bazaar ``payTo`` is that address)
      → pay seller (USDC on Base, EIP-3009 or a plain transfer)
      → consume (Hub or hearth verifies the receipt, then serves)

Wallet-address publishers are payable because they *are* the payee.
Operator-owned listings pay the operator's own wallet — the operator is
the seller, not a custodian. A priced listing with nobody to pay is
refused, not billed to the platform.

Credits and payment channels remain prepaid conveniences. They are not
how a seller is paid for a catalogue sale.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# keccak256("Transfer(address,indexed from,indexed to,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# keccak256("AuthorizationUsed(address,bytes32)") — EIP-3009. The token
# contract emits this when it accepts transferWithAuthorization; the nonce
# is the one the payer SIGNED. Matching it is what binds a payment to one
# call without the Hub holding a key.
AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
)

_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NONCE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class PaymentError(RuntimeError):
    """The call has not been paid for. Carries a reason the buyer can act on."""


def is_address(value: str) -> bool:
    return bool(_ADDRESS.match((value or "").strip()))


def is_tx_hash(value: str) -> bool:
    return bool(_TX_HASH.match((value or "").strip()))


def is_nonce(value: str) -> bool:
    return bool(_NONCE.match((value or "").strip()))


def to_units(amount_usd: float, decimals: int) -> int:
    """Dollars → token base units, rounded up so the seller never eats dust."""
    scaled = float(amount_usd) * (10 ** decimals)
    units = int(scaled)
    return units + 1 if scaled - units > 1e-9 else units


def seller_pay_to(cap: Any, *, operator_pay_to: str = "") -> str:
    """Who receives USDC for this listing. Empty means it cannot be sold.

    Order is the market joint, not a fallback stack that ends at the platform:

    1. The listing's own ``payout_address`` (what a hearth announces).
    2. A publisher identified by a 0x wallet — they *are* the payee.
    3. An operator-owned listing (no separate publisher) uses the operator's
       own seller wallet. That is the operator selling their work, not
       custody of someone else's sale.
    """
    explicit = str(getattr(cap, "payout_address", "") or "").strip()
    if is_address(explicit):
        return explicit
    publisher = str(getattr(cap, "publisher_id", "") or "").strip()
    if is_address(publisher):
        return publisher
    if not publisher and is_address(operator_pay_to):
        return operator_pay_to.strip()
    return ""


@dataclass(frozen=True)
class PaymentTerms:
    """What a buyer must do, in the units the chain uses."""

    chain: str
    token: str
    token_contract: str
    decimals: int
    pay_to: str
    amount_units: int
    amount_usd: float
    min_confirmations: int
    chain_id: int = 8453
    eip712_name: str = "USD Coin"
    eip712_version: str = "2"
    #: What the SELLER must receive. Equal to `amount_units` when there is no fee.
    seller_units: int = 0
    #: What the operator must receive, and where. Both zero/empty means no fee.
    fee_units: int = 0
    fee_to: str = ""
    #: Who the 402 names as `payTo` — the splitter when one is configured, so an
    #: off-the-shelf x402 client can produce both legs with one signature. The
    #: VERIFICATION still names the seller and the operator, never this address:
    #: what the hub requires is that both were paid, not that a particular contract
    #: was used to pay them.
    offer_to: str = ""

    def __post_init__(self) -> None:
        if not self.seller_units:
            object.__setattr__(self, "seller_units", self.amount_units)
        if not self.offer_to:
            object.__setattr__(self, "offer_to", self.pay_to)

    def as_x402_accept(self, resource: str = "") -> dict[str, Any]:
        return {
            "scheme": "exact",
            "network": self.chain,
            "maxAmountRequired": str(self.amount_units),
            "asset": self.token_contract,
            "payTo": self.offer_to,
            "resource": resource,
            "description": f"{self.amount_usd} {self.token}",
            "mimeType": "application/json",
            "maxTimeoutSeconds": 300,
            "extra": {
                "name": self.eip712_name,
                "version": self.eip712_version,
                "decimals": self.decimals,
                "symbol": self.token,
                "chainId": self.chain_id,
                "verifyingContract": self.token_contract,
            },
        }


def terms_for(cap: Any, *, operator_pay_to: str = "") -> PaymentTerms | None:
    """Priced listing with a seller, or None — never terms that pay nobody."""
    from aimarket_hub import x402

    price = float(getattr(cap, "price_per_call_usd", 0) or 0)
    pay_to = seller_pay_to(cap, operator_pay_to=operator_pay_to)
    if price <= 0 or not pay_to:
        return None
    profile = x402.asset_profile()
    if not profile:
        return None
    decimals = int(profile.get("decimals") or 6)
    extra = profile.get("extra") or {}
    gross = to_units(price, decimals)
    # A fee needs somewhere to send it. Configured without a recipient it is not
    # applied at all rather than quietly charged to the seller.
    bps = fee_bps()
    fee_to = fee_recipient() if bps else ""
    if not is_address(fee_to) or fee_to.lower() == pay_to.lower():
        bps, fee_to = 0, ""
    seller_units, fee_units = fee_split(gross, bps)
    offer_to = splitter_address() if bps else ""
    return PaymentTerms(
        chain=x402.chain(),
        token=x402.asset_symbol(),
        token_contract=str(profile["address"]),
        decimals=decimals,
        pay_to=pay_to,
        amount_units=gross,
        amount_usd=price,
        min_confirmations=_min_confirmations(),
        chain_id=x402.chain_id() or 8453,
        eip712_name=str(extra.get("name") or "USD Coin"),
        eip712_version=str(extra.get("version") or "2"),
        seller_units=seller_units,
        fee_units=fee_units,
        fee_to=fee_to,
        offer_to=offer_to or pay_to,
    )


def fee_bps() -> int:
    """The operator's share of a catalogue sale, in basis points. 0 = off.

    Capped at the same 10% the contract enforces, because the only thing worse than
    a hub that takes nothing is one whose advertised terms and whose contract
    disagree about how much.
    """
    try:
        return max(0, min(1000, int(os.getenv("AIMARKET_MARKET_FEE_BPS", "0"))))
    except ValueError:
        return 0


def fee_recipient() -> str:
    """Where the operator's share goes. Defaults to the hub's own x402 wallet."""
    from aimarket_hub import x402

    explicit = (os.getenv("AIMARKET_MARKET_FEE_TO") or "").strip()
    return explicit if is_address(explicit) else x402.recipient()


def splitter_address() -> str:
    """The `MarketSplitter` a 402 names as `payTo` so the buyer pays once.

    Without it a fee still settles — the hub requires both legs either way — but the
    buyer has to produce them itself, which off-the-shelf x402 clients cannot do.
    """
    value = (os.getenv("AIMARKET_MARKET_SPLITTER") or "").strip()
    return value if is_address(value) else ""


def fee_split(gross_units: int, bps: int) -> tuple[int, int]:
    """(seller_units, fee_units) — the same arithmetic the contract does.

    The OPERATOR's share is the computed one and the seller takes the remainder, so
    every rounding remainder goes to the seller and the fee is never more than `bps`
    of the gross. Mirrors `MarketSplitter._settle`; a divergence here would advertise
    terms the chain then refuses to satisfy.
    """
    fee = (int(gross_units) * int(bps)) // 10_000
    return int(gross_units) - fee, fee


def require_binding() -> bool:
    """EIP-3009 nonce binding. On by default, same as HESTIA."""
    raw = os.getenv("AIMARKET_SETTLE_REQUIRE_BINDING", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def invoice_ttl_s() -> int:
    try:
        return max(30, int(os.getenv("AIMARKET_SETTLE_INVOICE_TTL_S", "300")))
    except ValueError:
        return 300


def max_age_s() -> int:
    try:
        return max(0, int(os.getenv("AIMARKET_SETTLE_MAX_AGE_S", "0")))
    except ValueError:
        return 0


def _min_confirmations() -> int:
    try:
        return max(1, int(os.getenv("AIMARKET_SETTLE_MIN_CONFIRMATIONS", "1")))
    except ValueError:
        return 1


def _topic_address(topic: str) -> str:
    return "0x" + (topic or "")[-40:]


def _rpc(url: str, method: str, params: list[Any], timeout: float) -> Any:
    response = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise PaymentError(f"chain rpc refused {method}: {body['error']}")
    return body.get("result")


def rpc_urls() -> list[str]:
    """Where to ask whether the seller was paid. Same exclusive-override rule
    as deposit verification: a bubble RPC must not fall through to mainnet."""
    from aimarket_hub import x402
    from aimarket_hub.deposit_verify import rpc_urls_for

    explicit = (os.getenv("AIMARKET_SETTLE_RPC_URL") or "").strip()
    if explicit:
        return [part.strip() for part in explicit.split(",") if part.strip()]
    return rpc_urls_for(x402.chain())


def verify_transfer(
    *,
    tx_hash: str,
    terms: PaymentTerms,
    timeout: float = 10.0,
    max_age_s: int = 0,
    require_nonce: str = "",
    rpc_url: str = "",
) -> dict[str, Any]:
    """Confirm ``tx_hash`` paid ``terms``. Raises PaymentError with the reason.

    Returns the settled facts — never the caller's claim about them.
    Ported from ``hestia.payments.verify_transfer`` so Hub and hearth share
    one definition of "the seller was paid".
    """
    if not is_tx_hash(tx_hash):
        raise PaymentError("payment reference must be a 0x transaction hash")
    tx_hash = tx_hash.strip().lower()

    urls = [part.strip() for part in (rpc_url or "").split(",") if part.strip()] or rpc_urls()
    if not urls:
        raise PaymentError("no chain endpoint is configured")

    receipt = None
    used = ""
    transport_error: Exception | None = None
    for url in urls:
        try:
            found = _rpc(url, "eth_getTransactionReceipt", [tx_hash], timeout)
        except (httpx.HTTPError, PaymentError) as exc:
            transport_error = exc
            continue
        if found:
            receipt, used = found, url
            break
    if receipt is None:
        if transport_error is not None and len(urls) == 1:
            raise transport_error
        raise PaymentError("transaction is not on chain yet")
    if str(receipt.get("status", "")).lower() not in ("0x1", "1"):
        raise PaymentError("transaction reverted")

    head = int(_rpc(used, "eth_blockNumber", [], timeout), 16)
    mined = int(receipt.get("blockNumber", "0x0"), 16)
    confirmations = max(0, head - mined + 1)
    if confirmations < terms.min_confirmations:
        raise PaymentError(
            f"payment has {confirmations} confirmation(s); "
            f"{terms.min_confirmations} required"
        )

    if max_age_s > 0:
        block = _rpc(used, "eth_getBlockByNumber", [hex(mined), False], timeout)
        raw_ts = (block or {}).get("timestamp")
        if raw_ts is None:
            raise PaymentError("cannot establish payment age; refusing")
        try:
            mined_at = int(str(raw_ts), 16)
        except (TypeError, ValueError) as exc:
            raise PaymentError("cannot establish payment age; refusing") from exc
        age = time.time() - mined_at
        if age > max_age_s:
            raise PaymentError(
                f"payment is {int(age)}s old; must be within {max_age_s}s of the "
                "call (an old transfer cannot be replayed as a fresh payment)"
            )

    want_to = terms.pay_to.lower()
    want_fee_to = (terms.fee_to or "").lower()
    want_token = terms.token_contract.lower()
    paid = 0
    paid_fee = 0
    authorizer = ""
    nonce_seen = False
    want_nonce = (require_nonce or "").strip().lower()
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if not topics:
            continue
        if str(log.get("address", "")).lower() != want_token:
            continue
        topic0 = (topics[0] or "").lower()
        if want_nonce and topic0 == AUTHORIZATION_USED_TOPIC and len(topics) >= 3:
            if (topics[2] or "").lower() == want_nonce:
                nonce_seen = True
                authorizer = _topic_address(topics[1]).lower()
            continue
        if topic0 != TRANSFER_TOPIC or len(topics) < 3:
            continue
        destination = _topic_address(topics[2]).lower()
        amount = int(log.get("data") or "0x0", 16)
        if destination == want_to:
            paid += amount
        elif want_fee_to and destination == want_fee_to:
            paid_fee += amount

    if want_nonce and not nonce_seen:
        raise PaymentError(
            "payment is not bound to this call: the transaction carries no "
            "EIP-3009 authorization for the nonce this hub issued. Pay with "
            "transferWithAuthorization using the nonce from the 402."
        )
    if paid == 0:
        raise PaymentError(
            f"no {terms.token} transfer to {terms.pay_to} found in that transaction"
        )
    if paid < terms.seller_units:
        raise PaymentError(
            f"paid {paid} base units, the seller's share is {terms.seller_units}"
        )
    # The fee is enforced by the chain, not by trusting a contract. Both legs must be
    # in THIS transaction: a buyer who paid the seller alone at the net price has not
    # paid for the call, and one who used the splitter produced both legs anyway. That
    # is why the hub never checks which address the payment was routed through.
    if terms.fee_units and paid_fee < terms.fee_units:
        raise PaymentError(
            f"the operator's share was not paid: {paid_fee} of {terms.fee_units} base "
            f"units reached {terms.fee_to}. Pay through the splitter named in the 402, "
            "or send both transfers in one transaction."
        )
    return {
        "tx_hash": tx_hash,
        "paid_units": paid,
        "fee_units": paid_fee,
        "confirmations": confirmations,
        "block_number": mined,
        "chain": terms.chain,
        "token": terms.token,
        "pay_to": terms.pay_to,
        "nonce": want_nonce,
        "authorizer": authorizer,
        "amount_usd": terms.amount_usd,
        "verified_at": time.time(),
    }


def extract_payment_ref(raw: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
    """Tx hash + optional nonce from a header that may be a hash or x402 JSON."""
    text = (raw or "").strip()
    if is_tx_hash(text):
        return {"tx_hash": text.lower(), "nonce": ""}
    body = payload or {}
    extra = body.get("extra") if isinstance(body.get("extra"), dict) else {}
    inner = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    auth = inner.get("authorization") if isinstance(inner.get("authorization"), dict) else (
        body.get("authorization") if isinstance(body.get("authorization"), dict) else {}
    )
    candidates = (
        body.get("txHash"), body.get("tx_hash"), body.get("transaction"),
        body.get("settleTx"), extra.get("txHash"), extra.get("tx_hash"),
        inner.get("txHash"), inner.get("tx_hash"),
    )
    tx_hash = ""
    for item in candidates:
        if is_tx_hash(str(item or "")):
            tx_hash = str(item).strip().lower()
            break
    nonce = str(
        body.get("nonce") or extra.get("nonce") or auth.get("nonce") or ""
    ).strip()
    return {"tx_hash": tx_hash, "nonce": nonce if is_nonce(nonce) else ""}


class InvoiceStore:
    """What a 402 promised, so settlement checks the terms the Hub advertised."""

    def __init__(self, conn: Any):
        self._conn = conn

    def mint(
        self, *, nonce: str, capability_id: str, pay_to: str, amount_units: int, ttl_s: int,
    ) -> dict[str, Any]:
        now = time.time()
        expires = now + max(30, int(ttl_s))
        self._conn.execute(
            "INSERT OR REPLACE INTO settle_invoices "
            "(nonce, capability_id, pay_to, amount_units, expires_at, consumed_at, tx_hash) "
            "VALUES (?, ?, ?, ?, ?, 0, '')",
            (nonce.lower(), capability_id, pay_to.lower(), str(amount_units), expires),
        )
        self._conn.commit()
        return {
            "nonce": nonce.lower(),
            "capability_id": capability_id,
            "pay_to": pay_to.lower(),
            "amount_units": amount_units,
            "expires_at": expires,
        }

    def get(self, nonce: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM settle_invoices WHERE nonce = ?",
            (nonce.lower(),),
        ).fetchone()
        return dict(row) if row else None

    def consume(self, nonce: str, tx_hash: str) -> bool:
        cur = self._conn.execute(
            "UPDATE settle_invoices SET consumed_at = ?, tx_hash = ? "
            "WHERE nonce = ? AND consumed_at = 0",
            (time.time(), tx_hash.lower(), nonce.lower()),
        )
        self._conn.commit()
        return (cur.rowcount or 0) > 0

    def release(self, nonce: str) -> None:
        if not nonce:
            return
        self._conn.execute(
            "UPDATE settle_invoices SET consumed_at = 0, tx_hash = '' WHERE nonce = ?",
            (nonce.lower(),),
        )
        self._conn.commit()


class PaymentStore:
    """Spent on-chain payments: one transfer buys one Hub call."""

    def __init__(self, conn: Any):
        self._conn = conn

    def seen_tx(self, tx_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT settle_tx_hash FROM x402_payments WHERE settle_tx_hash = ?",
            (tx_hash.lower(),),
        ).fetchone()
        return row is not None

    def seen_nonce(self, nonce: str) -> bool:
        row = self._conn.execute(
            "SELECT nonce FROM x402_payments WHERE nonce = ?",
            (str(nonce),),
        ).fetchone()
        return row is not None

    def claim(
        self,
        *,
        tx_hash: str,
        pay_to: str,
        payer: str,
        amount_usd: float,
        amount_atomic: int | str,
        asset: str,
        network: str,
        capability_id: str = "",
        nonce: str = "",
        receipt_id: str = "",
    ) -> bool:
        """Record a settled payment. False if this transfer was already spent."""
        key = (nonce or tx_hash).strip()
        if not key:
            return False
        if self.seen_tx(tx_hash) or (nonce and self.seen_nonce(nonce)):
            return False
        try:
            self._conn.execute(
                "INSERT INTO x402_payments "
                "(nonce, payer, amount_atomic, amount_usd, asset, network, "
                "receipt_id, capability_id, status, settle_tx_hash, settled_at, pay_to) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'settled', ?, datetime('now'), ?)",
                (
                    key, payer or "",
                    str(amount_atomic), float(amount_usd),
                    asset, network, receipt_id, capability_id,
                    tx_hash.lower(), pay_to.lower(),
                ),
            )
            self._conn.commit()
        except Exception:
            return False
        return True

    def release(self, tx_hash: str) -> None:
        if not tx_hash:
            return
        self._conn.execute(
            "DELETE FROM x402_payments WHERE settle_tx_hash = ?",
            (tx_hash.lower(),),
        )
        self._conn.commit()
