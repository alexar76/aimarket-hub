"""x402 bridge: this hub pays an x402 seller from its own small wallet for a buyer who pays it in credits.

Why (owner, 2026-11-08). A buyer on this hub pays with a prepaid credit key. Outside sellers — x402 hubs such
as agent-exec, observed x402 sellers such as tienda.textokit.store — take payment only at their own x402
door, from the buyer's own wallet. A credit buyer has no wallet, met the seller's 402 and bought nothing, so
an outside node gained nothing from joining the federation. The bridge closes that gap with no work on the
seller's side: the hub reserves the price on the buyer's credits, signs an EIP-3009 ``transferWithAuthorization``
from its bridge wallet for exactly the seller's quoted amount, sends the call again with the payment, and
charges the buyer what it paid once the seller delivered.

What keeps it safe:

- Off unless ``AIMARKET_X402_BRIDGE=1`` and a key file readable only by its owner is configured.
- Only the ``exact`` scheme in USDC on Base (eip155:8453), and only the address the listing or the seller's own
  well-known already named (``expected_pay_to``): a 402 cannot redirect the payment.
- Never more than the price this hub quoted the buyer (``limit_usd``), never more than
  ``AIMARKET_X402_BRIDGE_MAX_CALL_USD`` (default 0.05) per call and ``AIMARKET_X402_BRIDGE_SELLER_DAY_USD``
  (default 0.50) per seller per UTC day — counted for the seller (the peer or observed origin) AND for each
  payee address, because a seller may publish as many payees as it likes — and never more than the wallet
  holds minus payments still in flight.
  The wallet balance is the operator's main limit: topping the wallet up raises it at once, with no setting to
  change. An optional ``AIMARKET_X402_BRIDGE_DAY_USD`` caps a whole day.
- Every payment is a ledger row (``x402_bridge_payments``) written BEFORE it is signed, under a lock row that
  serialises concurrent purchases on SQLite and PostgreSQL alike, so the caps hold under load.
- One signed authorization per purchase, a fresh random nonce, a short validity window; no automatic retry
  of a payment. A transport error after sending is recorded as ``unknown`` and counts against the caps.
- Only money the buyer paid in (card or USDC top-ups) reaches an outside seller. Signup and operator grants
  keep buying this hub's own resources only, except on the operator's own accounts (ecosystem.json) and those
  named in ``AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS``. "Paid in" is counted paid-first: top-ups minus
  everything the account spent, holds or staked on this hub (the current call's own hold excepted), minus
  the bridge payments it was not charged for. Granted credit spent elsewhere cannot free paid credit for the
  bridge, and the bridge never pays out more for an account than the account paid in.
- The buyer is charged only for a delivery. A seller that refuses the payment, or takes it and fails, costs the
  buyer no credit, but the payment still counts against that buyer's paid-in allowance until a
  reconciliation reads ``authorizationState`` and finds the authorization unused (it then becomes ``void``).
  Otherwise a buyer and a seller working together could settle-then-fail in a loop and empty the wallet on
  a single top-up (money-flow audit 2026-11-11, x402-bridge-1). The loss stays the bridge's, recorded with
  the settlement transaction, and is bounded per buyer by what that buyer paid in.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import stat
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

logger = logging.getLogger("aimarket_hub.x402_bridge")

NETWORK = "eip155:8453"
_NETWORK_ALIASES = {"eip155:8453": NETWORK, "base": NETWORK}
CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
USDC_DOMAIN = {"name": "USD Coin", "version": "2"}
_CMC_FIXED_GET = "https://pro-api.coinmarketcap.com/x402/v3/cryptocurrency/quotes/latest?id=1,1027"
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_KEY = re.compile(r"(0x)?[0-9a-fA-F]{64}")
# Statuses after which money may have left the wallet; all of them count against the caps.
# "refused" too: a seller that answered 402 still holds a signed authorization it could settle
# until validBefore, so refusing and settling in a loop must not get past the caps.
SPENDING = ("reserved", "signed", "delivered", "undelivered", "unknown", "refused")
# Possibly not yet visible in the on-chain balance: subtracted from it before a new payment.
# The window outlives every authorization (AUTH_TTL_MAX_S + validAfter skew), after which an
# unsettled one can no longer move money and a settled one is in the balance.
IN_FLIGHT = ("reserved", "signed", "unknown", "refused")
# Paid out (or possibly paid out) for a buyer who was NOT charged for it: they count against that
# buyer's paid-in allowance, not only against the wallet's caps. "signed" is here for a row a dead
# process left behind; a live purchase's own row is "signed" while its hold is still held, and the
# hold — not this row — is what counts for it (see buyer_bridge_left_mc).
UNCHARGED = ("undelivered", "unknown", "refused", "signed")
# A row reconcile() proved moved no money: never signed, or its authorization expired unused.
VOID = "void"
# Past validBefore by this much, an unused authorization can no longer be settled by anyone.
RECONCILE_GRACE_S = 120
RECONCILE_LIMIT = 3
AUTH_TTL_MAX_S = 600
IN_FLIGHT_WINDOW_S = 900
SETTLED_LAG_S = 120
BALANCE_CACHE_S = 10.0


class BridgeRefused(Exception):
    """The bridge will not pay this offer. ``reason`` is safe to show the buyer."""

    def __init__(self, reason: str, *, code: str = "bridge_refused"):
        super().__init__(reason)
        self.reason, self.code = reason, code


# ── configuration ────────────────────────────────────────────────────────────


def _flag(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _usd(name: str, default: float) -> float:
    raw = _flag(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number; using %s", name, default)
        return default
    return value if value >= 0 else default


def switched_on() -> bool:
    return _flag("AIMARKET_X402_BRIDGE").lower() in ("1", "true", "yes", "on")


def key_file() -> str:
    return _flag("AIMARKET_X402_BRIDGE_KEY_FILE")


def max_call_usd() -> float:
    return _usd("AIMARKET_X402_BRIDGE_MAX_CALL_USD", 0.05)


def seller_day_usd() -> float:
    return _usd("AIMARKET_X402_BRIDGE_SELLER_DAY_USD", 0.50)


def day_usd() -> float | None:
    raw = _flag("AIMARKET_X402_BRIDGE_DAY_USD")
    return _usd("AIMARKET_X402_BRIDGE_DAY_USD", 0.0) if raw else None


def granted_accounts() -> frozenset[str]:
    """Credit accounts allowed to spend GRANTED credit through the bridge: the operator's own (ecosystem.json
    ``self.accounts`` and AIMARKET_OPERATOR_ACCOUNTS, the operator's money either way) and any named in
    AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS (invoiced customers). Everyone else spends only what they paid in:
    see ``BridgeLedger.reserve``."""
    named = {a.strip() for a in _flag("AIMARKET_X402_BRIDGE_GRANTED_ACCOUNTS").split(",") if a.strip()}
    with contextlib.suppress(Exception):
        from aimarket_hub import ecosystem

        named |= set(ecosystem.current().self_accounts)
    return frozenset(named)


def auth_ttl_s() -> int:
    try:
        value = int(_flag("AIMARKET_X402_BRIDGE_AUTH_TTL_S") or 300)
    except ValueError:
        value = 300
    return max(30, min(value, AUTH_TTL_MAX_S))


_key_cache: dict[str, Any] = {}


def _load_key() -> str:
    """The bridge key from its file. Refuses a file anyone but its owner can read: a key that leaked to
    another account on the host is a wallet that is no longer ours alone."""
    path = key_file()
    if not path:
        raise BridgeRefused("the bridge has no key file configured", code="bridge_off")
    try:
        info = os.stat(path)
    except OSError:
        raise BridgeRefused("the bridge key file is missing", code="bridge_off") from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BridgeRefused("the bridge key file is readable by others; chmod 600 it", code="bridge_off")
    cached = _key_cache.get(path)
    if cached and cached[0] == info.st_mtime_ns:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        # Unreadable by the hub's user (owned by another account): the bridge is off, the hub is not.
        raise BridgeRefused("the bridge key file is not readable by the hub", code="bridge_off") from None
    if not _KEY.fullmatch(raw):
        raise BridgeRefused("the bridge key file does not hold a 32-byte hex key", code="bridge_off")
    key = raw if raw.startswith("0x") else "0x" + raw
    from eth_account import Account

    _key_cache[path] = (info.st_mtime_ns, key, Account.from_key(key).address)
    return key


def address() -> str:
    """The bridge wallet's address (public). Raises BridgeRefused when the key is unusable."""
    _load_key()
    return _key_cache[key_file()][2]


def unavailable_reason() -> str:
    """'' when the bridge can pay, else why not (for the status page and logs)."""
    if not switched_on():
        return "AIMARKET_X402_BRIDGE is off"
    try:
        import eth_account  # noqa: F401
    except Exception:  # pragma: no cover - environment-dependent
        return "eth-account is not installed"
    try:
        address()
    except BridgeRefused as exc:
        return exc.reason
    return ""


def enabled() -> bool:
    return unavailable_reason() == ""


# ── offers ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Offer:
    x402_version: int
    accept: dict[str, Any]
    resource: Any
    pay_to: str
    amount_atomic: int
    max_timeout_s: int
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def amount_usd(self) -> float:
        return self.amount_atomic / 10 ** USDC_DECIMALS


def _accepts_of(body: Any, headers: dict[str, Any] | None) -> tuple[int, list[Any], Any]:
    from aimarket_hub.federation_assay import x402_header_offer

    doc = body if isinstance(body, dict) else {}
    accepts, version, resource = doc.get("accepts"), doc.get("x402Version"), doc.get("resource")
    if not (isinstance(accepts, list) and accepts):
        header = x402_header_offer(headers) or {}
        accepts, version = header.get("accepts"), header.get("x402Version", version)
        resource = header.get("resource", resource)
    if not isinstance(accepts, list):
        accepts = []
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1
    return version, accepts, resource


def seller_key(seller: str) -> str:
    """The seller a daily cap is counted for: the origin (scheme://host[:port], lower-case) of a URL, or the
    trimmed lower-case string. Paths and trailing slashes do not make a second seller."""
    from urllib.parse import urlsplit

    text = str(seller or "").strip()
    with contextlib.suppress(ValueError):
        parts = urlsplit(text)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    return text.rstrip("/").lower()


def _payees(expected: str | Iterable[str] | None) -> frozenset[str]:
    if expected is None:
        return frozenset()
    items = [expected] if isinstance(expected, str) else list(expected)
    return frozenset(str(a).strip().lower() for a in items if _ADDRESS.fullmatch(str(a).strip()))


def choose_offer(body: Any, headers: dict[str, Any] | None, *, limit_usd: float,
                 expected_pay_to: str | Iterable[str] | None,
                 explicit_get_url: str | None = None) -> Offer:
    """The cheapest offer of a 402 the bridge may pay, or BridgeRefused saying why none qualifies.

    ``expected_pay_to`` is required: the addresses the seller published before this 402 (its
    well-known, its signed listing, the operator's pin). A 402 alone never chooses where money goes.
    """
    from aimarket_hub.observed_sellers import offer_terms

    allowed = _payees(expected_pay_to)
    if not allowed:
        raise BridgeRefused("the seller has published no Base payee the bridge could check this 402 against")
    version, accepts, resource = _accepts_of(body, headers)
    if not accepts:
        raise BridgeRefused("the seller's 402 carries no payment offer")
    get_resources = set()
    if explicit_get_url:
        # Some sellers (including CMC) bind their challenge to the endpoint, without
        # its query. The actual URL, including the query, stays fixed by operator consent.
        endpoint = urlsplit(explicit_get_url)._replace(query="", fragment="").geturl()
        get_resources = {explicit_get_url, endpoint}
    reasons: list[str] = []
    best: Offer | None = None
    for accept in accepts:
        if not isinstance(accept, dict):
            continue
        terms = offer_terms(accept)
        if terms is None:
            reasons.append("an offer has no valid amount or recipient")
            continue
        if str(terms.get("scheme") or "").lower() != "exact":
            reasons.append(f"scheme {terms.get('scheme')!r} (the bridge pays 'exact')")
            continue
        if _NETWORK_ALIASES.get(str(accept.get("network") or "")) != NETWORK:
            reasons.append(f"network {accept.get('network')!r} (the bridge pays on Base)")
            continue
        if str(terms.get("asset") or "").lower() != USDC.lower():
            reasons.append("an asset other than Base USDC")
            continue
        pay_to = str(terms.get("pay_to") or "")
        if not _ADDRESS.fullmatch(pay_to):
            reasons.append("a recipient that is not an address")
            continue
        if pay_to.lower() not in allowed:
            reasons.append("a recipient other than the ones this seller published")
            continue
        extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
        if explicit_get_url:
            route = accept.get("resource") if version == 1 else (resource or {})
            if isinstance(route, dict):
                route = route.get("url")
            if (version not in (1, 2) or not isinstance(route, str) or route not in get_resources
                    or extra.get("name") != USDC_DOMAIN["name"] or str(extra.get("version")) != USDC_DOMAIN["version"]
                    or extra.get("assetTransferMethod", "eip3009") != "eip3009"):
                reasons.append("the fixed GET route needs a resource-bound Base EIP-3009 challenge")
                continue
        amount = int(terms["amount_atomic"])
        try:
            timeout = int(accept.get("maxTimeoutSeconds") or 300)
        except (TypeError, ValueError):
            timeout = 300
        offer = Offer(x402_version=version, accept=dict(accept), resource=resource, pay_to=pay_to,
                      amount_atomic=amount, max_timeout_s=max(1, timeout),
                      extra=extra)
        if offer.amount_usd > limit_usd + 1e-9:
            reasons.append(f"a price of ${offer.amount_usd:.6f}, above the ${limit_usd:.6f} quoted to the buyer")
            continue
        if best is None or offer.amount_atomic < best.amount_atomic:
            best = offer
    if best is None:
        raise BridgeRefused("no offer the bridge may pay: " + "; ".join(dict.fromkeys(reasons))[:400])
    return best


# ── signing ──────────────────────────────────────────────────────────────────


def _sign_digest(digest: bytes, key: str) -> str:
    from eth_account import Account

    signer = getattr(Account, "unsafe_sign_hash", None)
    signed = signer(digest, key) if signer else Account.signHash(digest, private_key=key)  # pragma: no cover
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def sign_payment(offer: Offer, *, now: int | None = None) -> tuple[str, str, dict[str, Any]]:
    """(header name, header value, authorization) for one payment of ``offer`` from the bridge wallet.

    The signature is recovered again before it leaves: a mismatch is a bug, never a payment.
    """
    from aimarket_hub.escrow_bridge import eip712

    key = _load_key()
    payer = address()
    stamp = int(time.time()) if now is None else int(now)
    ttl = min(auth_ttl_s(), offer.max_timeout_s if offer.max_timeout_s >= 30 else 30)
    auth = {
        "from": payer,
        "to": offer.pay_to,
        "value": str(offer.amount_atomic),
        "validAfter": str(stamp - 60),
        "validBefore": str(stamp + ttl),
        "nonce": "0x" + secrets.token_hex(32),
    }
    # The domain is USDC's own, never the offer's `extra`: what this hub signs is decided here, and a
    # seller naming another domain gets a signature it cannot settle, not one meant for something else.
    digest = eip712.transfer_authorization_digest(
        sender=payer, recipient=offer.pay_to, value=offer.amount_atomic,
        valid_after=int(auth["validAfter"]), valid_before=int(auth["validBefore"]), nonce=auth["nonce"],
        token_name=USDC_DOMAIN["name"], token_version=USDC_DOMAIN["version"], chain_id=CHAIN_ID,
        token_address=USDC,
    )
    signature = _sign_digest(digest, key)
    if not eip712.addresses_equal(eip712.recover_transfer_signer(signature, digest) or "", payer):
        raise BridgeRefused("the bridge's own signature did not verify", code="bridge_error")
    inner = {"signature": signature, "authorization": auth}
    if offer.x402_version >= 2:
        payload: dict[str, Any] = {"x402Version": 2, "accepted": offer.accept, "payload": inner}
        if offer.resource is not None:
            payload["resource"] = offer.resource
        header = "PAYMENT-SIGNATURE"   # x402 v2; X-PAYMENT carries the same payload (see below)
    else:
        payload = {"x402Version": 1, "scheme": "exact", "network": str(offer.accept.get("network") or "base"),
                   "payload": inner}
        header = "X-PAYMENT"
    value = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return header, value, auth


def settlement(headers: dict[str, Any] | None) -> dict[str, Any]:
    """The seller's settlement answer (PAYMENT-RESPONSE / X-PAYMENT-RESPONSE), or {}."""
    if not headers:
        return {}
    raw = next((str(v) for k, v in headers.items()
                if str(k).lower() in ("payment-response", "x-payment-response")), "").strip()
    if not raw or len(raw) > 8192:
        return {}
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        with contextlib.suppress(Exception):
            data = json.loads(decode(raw + "=" * (-len(raw) % 4)))
            if isinstance(data, dict):
                return data
    return {}


def _tx_of(settled: dict[str, Any]) -> str:
    tx = str(settled.get("transaction") or settled.get("txHash") or settled.get("tx_hash") or "")
    return tx if _TX_HASH.fullmatch(tx) else ""


def _cmc_network_id_matches(value: Any) -> bool:
    """Reject a conflicting chain ID when CMC supplies one in a recognizable form."""
    if type(value) is int:
        return value == CHAIN_ID
    if not isinstance(value, str) or not value.strip():
        return False
    tag = value.strip().lower()
    if re.fullmatch(r"[0-9]+", tag):
        return tag.lstrip("0") == str(CHAIN_ID)
    if re.fullmatch(r"0x[0-9a-f]+", tag):
        return tag[2:].lstrip("0") == format(CHAIN_ID, "x")
    if tag.startswith("eip155:"):
        return tag == NETWORK
    return tag not in {"base-sepolia", "ethereum", "optimism", "arbitrum", "polygon", "solana"}


def _cmc_settlement_tx(settled: dict[str, Any], *, wallet: str, offer: Offer) -> str:
    tx = settled.get("txHash")
    if (settled.get("success") is not True
            or not isinstance(tx, str) or not _TX_HASH.fullmatch(tx)
            or ("network" in settled and str(settled["network"]) not in _NETWORK_ALIASES)
            or ("networkId" in settled and not _cmc_network_id_matches(settled["networkId"]))
            or ("payer" in settled and str(settled["payer"]).lower() != wallet.lower())
            or ("amount" in settled and str(settled["amount"]) != str(offer.amount_atomic))
            or any(alias in settled and str(settled[alias]).lower() != tx.lower()
                   for alias in ("transaction", "tx_hash"))):
        return ""
    return tx.lower()


def _cmc_paid_on_base(tx: str, *, wallet: str, nonce: str, offer: Offer, rpc: Any = None) -> bool:
    from aimarket_hub import chain_net, settle

    payer_topic = "0x" + wallet[2:].lower().zfill(64)
    payee_topic = "0x" + offer.pay_to[2:].lower().zfill(64)
    authorization = [settle.AUTHORIZATION_USED_TOPIC, payer_topic, nonce.lower()]
    transfer = [settle.TRANSFER_TOPIC, payer_topic, payee_topic]
    amount = f"0x{offer.amount_atomic:064x}"

    def verify(call: Any) -> bool:
        if int(call("eth_chainId", []), 16) != CHAIN_ID:
            raise ValueError("settlement RPC differs from Base mainnet")
        receipt = call("eth_getTransactionReceipt", [tx])
        if not receipt or receipt["transactionHash"].lower() != tx or int(receipt["status"], 16) != 1:
            return False
        height = int(receipt["blockNumber"], 16)
        block_hash = receipt["blockHash"].lower()
        if height <= 0 or not _TX_HASH.fullmatch(block_hash):
            return False
        block = call("eth_getBlockByNumber", [hex(height), False])
        if not block or int(block["number"], 16) != height or block["hash"].lower() != block_hash:
            return False

        def matches(log: dict[str, Any], topics: list[str], data: str) -> bool:
            return (log["address"].lower() == USDC.lower()
                    and log.get("removed", False) is False
                    and log["transactionHash"].lower() == tx
                    and log["blockHash"].lower() == block_hash
                    and int(log["blockNumber"], 16) == height
                    and [topic.lower() for topic in log["topics"]] == topics
                    and log["data"].lower() == data)

        logs = receipt["logs"]
        bound = [index for index, log in enumerate(logs) if matches(log, authorization, "0x")]
        if len(bound) != 1 or bound[0] == len(logs) - 1:
            return False
        used, moved = logs[bound[0]:bound[0] + 2]
        return (matches(moved, transfer, amount)
                and int(moved["logIndex"], 16) == int(used["logIndex"], 16) + 1)

    if rpc is not None:
        return verify(rpc)
    # Read the chain ID, receipt and canonical block from the same node.
    timeout = 3.0
    pool = chain_net.pool_for_urls(settle.rpc_urls(CHAIN_ID), chain_id=CHAIN_ID, timeout=timeout)
    return pool.run(lambda url: verify(lambda method, params: settle._rpc(url, method, params, timeout)))


# ── balance ──────────────────────────────────────────────────────────────────

_balance_cache: dict[str, tuple[float, float]] = {}


def wallet_balance_usd(wallet: str, *, rpc: Any = None, fresh: bool = False) -> float:
    """USDC the bridge wallet holds on Base, read from chain (cached ``BALANCE_CACHE_S``)."""
    now = time.time()
    cached = _balance_cache.get(wallet)
    if cached and not fresh and now - cached[1] < BALANCE_CACHE_S:
        return cached[0]
    if rpc is None:
        from aimarket_hub import settle

        def rpc(method: str, params: list[Any]) -> Any:
            return settle.rpc_call(settle.rpc_urls(CHAIN_ID), method, params, chain_id=CHAIN_ID, timeout=8.0)
    raw = rpc("eth_call", [{"to": USDC, "data": "0x70a08231" + wallet[2:].lower().zfill(64)}, "latest"])
    value = int(str(raw), 16) / 10 ** USDC_DECIMALS
    _balance_cache[wallet] = (value, now)
    return value


def authorization_used(wallet: str, nonce: str, *, rpc: Any = None) -> bool:
    """Whether USDC has consumed this authorization (EIP-3009 ``authorizationState``): settles an ``unknown``."""
    if rpc is None:
        from aimarket_hub import settle

        def rpc(method: str, params: list[Any]) -> Any:
            return settle.rpc_call(settle.rpc_urls(CHAIN_ID), method, params, chain_id=CHAIN_ID, timeout=8.0)
    data = "0xe94a0102" + wallet[2:].lower().zfill(64) + nonce[2:].lower().zfill(64)
    return int(str(rpc("eth_call", [{"to": USDC, "data": data}, "latest"])), 16) != 0


# ── ledger ───────────────────────────────────────────────────────────────────


def _day_start(now: float) -> float:
    return now - (now % 86400)


class BridgeLedger:
    """``x402_bridge_payments`` (migration 067). Every statement goes through the hub's backend."""

    def __init__(self, conn: Any):
        self._conn = conn

    def _sum(self, where: str, params: tuple[Any, ...]) -> float:
        placeholders = ",".join("?" for _ in SPENDING)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS total FROM x402_bridge_payments "
            f"WHERE status IN ({placeholders}) AND {where}", (*SPENDING, *params),
        ).fetchone()
        return float(row["total"] or 0) if row else 0.0

    def spent_today(self, now: float, pay_to: str | None = None, *, seller: str | None = None) -> float:
        if pay_to:
            return self._sum("created_at >= ? AND lower(pay_to) = ?", (_day_start(now), pay_to.lower()))
        if seller:
            return self._sum("created_at >= ? AND seller = ?", (_day_start(now), seller_key(seller)))
        return self._sum("created_at >= ?", (_day_start(now),))

    def in_flight(self, now: float) -> float:
        """Money that may have left, or may yet leave, the wallet without showing in a balance read.

        Unsettled authorizations until they expire, and settled ones for SETTLED_LAG_S: a balance read
        cached by another worker (or a node a block behind) can predate their settlement.
        """
        placeholders = ",".join("?" for _ in IN_FLIGHT)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS total FROM x402_bridge_payments "
            f"WHERE (status IN ({placeholders}) AND created_at >= ?) "
            f"OR (status IN ('delivered', 'undelivered') AND updated_at >= ?)",
            (*IN_FLIGHT, now - IN_FLIGHT_WINDOW_S, now - SETTLED_LAG_S),
        ).fetchone()
        return float(row["total"] or 0) if row else 0.0

    def buyer_paid_usd(self, account_id: str) -> float:
        from aimarket_hub.credits import mc_to_usd

        row = self._conn.execute(
            "SELECT topped_up_mc FROM credit_accounts WHERE account_id = ?", (account_id,),
        ).fetchone()
        return mc_to_usd(row["topped_up_mc"] or 0) if row else 0.0

    def buyer_spent_usd(self, account_id: str) -> float:
        """Bridge purchases charged (or about to be charged) to this buyer."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM x402_bridge_payments "
            "WHERE buyer_account = ? AND status IN ('reserved', 'signed', 'delivered')", (account_id,),
        ).fetchone()
        return float(row["total"] or 0) if row else 0.0

    def buyer_uncharged_mc(self, account_id: str) -> int:
        """Millicents the bridge paid out (or may have) for this buyer without charging the buyer.

        A ``signed`` row counts only once its purchase's credit hold is no longer held — a row a dead
        process left behind. While the hold is held, the hold itself is what counts (it is in
        ``held_mc``), and counting the row as well would refuse a buyer's second concurrent purchase.
        """
        placeholders = ",".join("?" for _ in UNCHARGED)
        row = self._conn.execute(
            "SELECT COALESCE(SUM((CAST(p.amount_atomic AS BIGINT) + 9) / 10), 0) AS mc "
            "FROM x402_bridge_payments p "
            f"WHERE p.buyer_account = ? AND p.status IN ({placeholders}) "
            "AND NOT (p.status = 'signed' AND EXISTS (SELECT 1 FROM credit_holds h "
            "WHERE h.receipt_id = p.receipt_id AND h.status = 'held'))",
            (account_id, *UNCHARGED),
        ).fetchone()
        return int(row["mc"] or 0) if row else 0

    def buyer_bridge_left_mc(self, account_id: str, *, own_receipt: str = "") -> int:
        """What this buyer may still send to outside sellers, in millicents. Paid-first and conservative:

            top-ups − spent − staked − held (except this purchase's own hold) − bridge payments not charged

        Signup and operator grants never become USDC for a stranger: credit spent anywhere on this hub
        is assumed to have been the paid part, so a grant cannot free paid-in credit for the bridge
        (audit 2026-11-11, x402-bridge-4). Payments that left the wallet without a charge stay counted
        until reconcile() proves the authorization was never used (x402-bridge-1).
        """
        row = self._conn.execute(
            "SELECT topped_up_mc, spent_mc, held_mc, collateral_mc FROM credit_accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            return 0
        own = 0
        if own_receipt:
            hold = self._conn.execute(
                "SELECT amount_mc FROM credit_holds WHERE receipt_id = ? AND account_id = ? AND status = 'held'",
                (own_receipt, account_id),
            ).fetchone()
            own = int(hold["amount_mc"] or 0) if hold else 0
        committed = int(row["spent_mc"] or 0) + int(row["collateral_mc"] or 0) + max(0, int(row["held_mc"] or 0) - own)
        return int(row["topped_up_mc"] or 0) - committed - self.buyer_uncharged_mc(account_id)

    def reconcile(self, *, now: float, wallet: str, rpc: Any = None, buyer_account: str = "",
                  limit: int = RECONCILE_LIMIT) -> int:
        """Settle rows that can no longer change; returns how many it resolved. Never raises for the chain.

        - ``reserved`` past the in-flight window: the process died before signing, nothing can move → ``void``.
        - ``signed`` / ``unknown`` / ``refused`` past validBefore + grace: USDC's ``authorizationState``
          decides — used → ``undelivered`` (the seller took it; the bridge's loss, counted against the
          buyer), unused → ``void`` (nothing moved; the buyer's allowance comes back).
        A chain read that fails leaves the row as it is for a later pass.
        """
        from aimarket_hub.db_backend import atomic

        scope, args = ("AND buyer_account = ? ", (buyer_account,)) if buyer_account else ("", ())
        resolved = 0
        with atomic(self._conn):
            cur = self._conn.execute(
                "UPDATE x402_bridge_payments SET status = ?, updated_at = ?, "
                "detail = 'never signed: the purchase stopped before the payment existed' "
                f"WHERE status = 'reserved' AND created_at < ? {scope}",
                (VOID, now, now - IN_FLIGHT_WINDOW_S, *args),
            )
            resolved += max(0, int(getattr(cur, "rowcount", 0) or 0))
        rows = self._conn.execute(
            "SELECT id, status, nonce FROM x402_bridge_payments "
            "WHERE status IN ('signed', 'unknown', 'refused') AND valid_before > 0 AND valid_before < ? "
            f"AND nonce <> '' {scope}ORDER BY created_at LIMIT ?",
            (now - RECONCILE_GRACE_S, *args, int(limit)),
        ).fetchall()
        for row in rows:
            try:
                used = authorization_used(wallet, str(row["nonce"]), rpc=rpc)
            except Exception as exc:  # noqa: BLE001 - a later pass retries
                logger.info("x402 bridge: cannot read authorizationState for %s yet: %s", row["id"], exc)
                continue
            new = "undelivered" if used else VOID
            detail = ("the authorization was used on chain: the seller took the payment and the buyer was "
                      "not charged" if used else "the authorization expired unused: nothing left the wallet")
            with atomic(self._conn):
                cur = self._conn.execute(
                    "UPDATE x402_bridge_payments SET status = ?, updated_at = ?, detail = ? "
                    "WHERE id = ? AND status = ?",
                    (new, now, detail, row["id"], row["status"]),
                )
                resolved += max(0, int(getattr(cur, "rowcount", 0) or 0))
        return resolved

    def reserve(self, *, offer: Offer, buyer_account: str, receipt_id: str, seller: str, route: str,
                balance_usd: float, now: float, prevent_unresolved: bool = False) -> str:
        """Check every cap and write the ``reserved`` row in one transaction; returns its id."""
        from aimarket_hub.db_backend import atomic

        amount = offer.amount_usd
        with atomic(self._conn):
            # The lock row: every reservation updates it first, so concurrent reservations queue here on
            # PostgreSQL (row lock) as they already do on SQLite (the backend's lock), and the sums below
            # include every reservation that committed before this one.
            self._conn.execute("UPDATE x402_bridge_state SET version = version + 1 WHERE id = 1")
            if prevent_unresolved and self._conn.execute(
                "SELECT 1 FROM x402_bridge_payments WHERE buyer_account = ? AND route = ? "
                "AND status IN ('reserved', 'signed', 'unknown', 'refused') LIMIT 1",
                (buyer_account, route[:500]),
            ).fetchone():
                raise BridgeRefused("a previous payment for this fixed GET is still unresolved",
                                    code="bridge_pending")
            if amount > max_call_usd() + 1e-9:
                raise BridgeRefused(f"${amount:.6f} is above the bridge's per-call cap of ${max_call_usd():.2f}",
                                    code="bridge_cap")
            # Per seller AND per payee. The payee alone was the key until 2026-11-11, and a seller
            # publishes as many payees as it likes: rotating payTo across 100 of them multiplied the
            # "per seller" cap a hundredfold (audit x402-bridge-2). The payee check stays because two
            # sellers can share one.
            if (self.spent_today(now, seller=seller) + amount > seller_day_usd() + 1e-9
                    or self.spent_today(now, offer.pay_to) + amount > seller_day_usd() + 1e-9):
                raise BridgeRefused("this seller reached the bridge's daily cap; try again tomorrow (UTC)",
                                    code="bridge_cap")
            cap = day_usd()
            if cap is not None and self.spent_today(now) + amount > cap + 1e-9:
                raise BridgeRefused("the bridge reached its daily cap; try again tomorrow (UTC)", code="bridge_cap")
            if self.in_flight(now) + amount > balance_usd + 1e-9:
                raise BridgeRefused("the bridge wallet does not hold enough USDC right now", code="bridge_empty")
            if buyer_account not in granted_accounts():
                # Free credit never becomes USDC in a stranger's wallet (test_federated_selling pins the
                # same rule for the resale path): a buyer's bridge purchases are bounded by the money
                # that buyer actually paid in — card or USDC top-ups — counted paid-first, and the
                # payments the bridge made for this buyer without charging them count too. See
                # buyer_bridge_left_mc for why both matter.
                needed_mc = -(-int(offer.amount_atomic) // 10)
                if self.buyer_bridge_left_mc(buyer_account, own_receipt=receipt_id) < needed_mc:
                    raise BridgeRefused("bridge purchases are paid from topped-up credit that is still unspent; "
                                        "signup and granted credit buy this hub's own capabilities only, and "
                                        "payments a seller took without delivering count against it",
                                        code="bridge_paid_only")
            payment_id = "xb_" + uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO x402_bridge_payments (id, created_at, updated_at, buyer_account, receipt_id, seller, "
                "route, pay_to, network, asset, amount_atomic, amount_usd, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')",
                (payment_id, now, now, buyer_account, receipt_id, seller_key(seller)[:200], route[:500],
                 offer.pay_to, NETWORK, USDC, str(offer.amount_atomic), amount),
            )
        return payment_id

    def mark(self, payment_id: str, status: str, *, nonce: str = "", valid_before: int = 0, tx_hash: str = "",
             http_status: int = 0, detail: str = "") -> None:
        from aimarket_hub.db_backend import atomic

        with atomic(self._conn):
            self._conn.execute(
                "UPDATE x402_bridge_payments SET status = ?, updated_at = ?, "
                "nonce = CASE WHEN ? <> '' THEN ? ELSE nonce END, "
                "valid_before = CASE WHEN ? > 0 THEN ? ELSE valid_before END, "
                "tx_hash = CASE WHEN ? <> '' THEN ? ELSE tx_hash END, "
                "http_status = CASE WHEN ? > 0 THEN ? ELSE http_status END, detail = ? WHERE id = ?",
                (status, time.time(), nonce, nonce, valid_before, valid_before, tx_hash, tx_hash,
                 http_status, http_status, detail[:500], payment_id),
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, created_at, seller, route, pay_to, amount_usd, status, tx_hash, http_status "
            "FROM x402_bridge_payments ORDER BY created_at DESC LIMIT ?", (int(limit),),
        ).fetchall()
        keys = ("id", "created_at", "seller", "route", "pay_to", "amount_usd", "status", "tx_hash", "http_status")
        return [{key: row[key] for key in keys} for row in rows]

    def count_today(self, now: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM x402_bridge_payments WHERE created_at >= ? AND status = 'delivered'",
            (_day_start(now),),
        ).fetchone()
        return int(row["n"] or 0) if row else 0


# ── one purchase ─────────────────────────────────────────────────────────────


@dataclass
class Payment:
    payment_id: str
    amount_atomic: int
    pay_to: str
    nonce: str
    status: str = "signed"
    tx_hash: str = ""

    @property
    def amount_usd(self) -> float:
        return self.amount_atomic / 10 ** USDC_DECIMALS

    @property
    def amount_mc(self) -> int:
        """The ledger's millicents for this amount, rounded UP (USDC has one more decimal)."""
        return -(-self.amount_atomic // 10)

    def public(self, hub_url: str) -> dict[str, Any]:
        return {"via": "x402-bridge", "hub": hub_url, "payment_id": self.payment_id, "amount_usd": self.amount_usd,
                "pay_to": self.pay_to, "network": NETWORK, "asset": USDC, "tx_hash": self.tx_hash or None}


async def pay_and_resend(*, ledger: BridgeLedger, first: Any, resend: Any, limit_usd: float,
                         expected_pay_to: str | Iterable[str] | None, buyer_account: str, receipt_id: str, seller: str,
                         route: str, rpc: Any = None, now: float | None = None,
                         explicit_get: bool = False) -> tuple[Any, Payment]:
    """Pay the 402 in ``first`` and send the call again.

    ``resend(extra_headers) -> response`` repeats the original request with the payment header added.
    Returns (response, payment); the caller charges the buyer only if the response is a delivery, and must then
    call ``finish``. Raises BridgeRefused before anything is signed when the offer or a cap says no.
    """
    try:
        body = first.json()
    except ValueError:
        body = {}
    headers = dict(getattr(first, "headers", {}) or {})
    offer = choose_offer(body, headers, limit_usd=limit_usd, expected_pay_to=expected_pay_to,
                         explicit_get_url=route if explicit_get else None)
    stamp = time.time() if now is None else now
    wallet = address()
    try:
        balance = wallet_balance_usd(wallet, rpc=rpc)
    except BridgeRefused:
        raise
    except Exception as exc:
        raise BridgeRefused("the bridge could not read its wallet balance right now", code="bridge_error") from exc
    # This buyer's stale rows first: a payment proved unused gives their allowance back, one proved
    # used stays counted. Bounded (RECONCILE_LIMIT chain reads) and never in the way of the purchase.
    try:
        ledger.reconcile(now=stamp, wallet=wallet, rpc=rpc, buyer_account=buyer_account)
    except Exception as exc:  # noqa: BLE001
        logger.warning("x402 bridge: reconciliation before a purchase failed: %s", exc)
    payment_id = ledger.reserve(offer=offer, buyer_account=buyer_account, receipt_id=receipt_id, seller=seller,
                                route=route, balance_usd=balance, now=stamp, prevent_unresolved=explicit_get)
    try:
        header, value, auth = sign_payment(offer, now=int(stamp))
    except Exception as exc:
        ledger.mark(payment_id, "failed", detail=f"signing failed: {type(exc).__name__}")
        raise BridgeRefused("the bridge could not sign the payment", code="bridge_error") from exc
    ledger.mark(payment_id, "signed", nonce=auth["nonce"], valid_before=int(auth["validBefore"]))
    payment = Payment(payment_id=payment_id, amount_atomic=offer.amount_atomic, pay_to=offer.pay_to,
                      nonce=auth["nonce"])
    # One authorization under every header name an x402 seller reads: v2 servers read
    # PAYMENT-SIGNATURE, v1 and hybrid ones X-PAYMENT (agent-exec's v2 402 asks for "X-PAYMENT").
    # USDC accepts a nonce once, so naming it twice can never pay twice.
    extra = {header: value, "X-PAYMENT": value}
    try:
        resp = await resend(extra)
    except Exception as exc:
        # The seller may or may not have settled. Never retried: the caps keep counting it until the
        # authorization expires or a reconciliation reads authorizationState.
        ledger.mark(payment_id, "unknown", detail=f"no answer after the payment was sent: {type(exc).__name__}")
        payment.status = "unknown"
        _balance_cache.pop(wallet, None)
        raise
    settled = settlement(dict(getattr(resp, "headers", {}) or {}))
    if explicit_get and route == _CMC_FIXED_GET and any(
        alias in settled for alias in ("txHash", "networkId", "x402FlowId")
    ):
        tx = _cmc_settlement_tx(settled, wallet=wallet, offer=offer)
        if resp.status_code == 200 and tx:
            try:
                if await asyncio.wait_for(asyncio.to_thread(
                    _cmc_paid_on_base, tx, wallet=wallet, nonce=payment.nonce, offer=offer, rpc=rpc,
                ), timeout=10):
                    payment.tx_hash = tx
            except Exception as exc:
                logger.warning("x402 bridge: CMC receipt check unavailable: %s", type(exc).__name__)
    elif not explicit_get or (
        settled.get("success") is True
        and str(settled.get("network")) in _NETWORK_ALIASES
        and ("payer" not in settled or str(settled["payer"]).lower() == wallet.lower())
        and ("amount" not in settled or str(settled["amount"]) == str(offer.amount_atomic))
    ):
        payment.tx_hash = _tx_of(settled)
    _balance_cache.pop(wallet, None)
    return resp, payment


def finish(ledger: BridgeLedger, payment: Payment, *, delivered: bool, http_status: int, detail: str = "") -> None:
    """Record how the purchase ended. ``delivered`` = the buyer got the work and is charged."""
    if delivered:
        status = "delivered"
    elif http_status == 402:
        status = "refused"          # the seller declined the payment: nothing moved
    elif payment.tx_hash:
        status = "undelivered"      # settled on chain, no delivery: the bridge's loss
    else:
        status = "unknown"          # no settlement header: may or may not have settled
    payment.status = status
    ledger.mark(payment.payment_id, status, tx_hash=payment.tx_hash, http_status=http_status, detail=detail)
    if status in ("undelivered", "unknown"):
        logger.warning("x402 bridge: payment %s to %s ended %s (http %s)", payment.payment_id, payment.pay_to,
                       status, http_status)


def status(ledger: BridgeLedger | None, *, hub_url: str = "", rpc: Any = None) -> dict[str, Any]:
    """The public status: whether the bridge pays, from which wallet, its limits and today's spending."""
    now = time.time()
    reason = unavailable_reason()
    doc: dict[str, Any] = {
        "bridge": "x402", "enabled": reason == "", "reason": reason or None, "hub": hub_url,
        "network": NETWORK, "asset": USDC, "scheme": "exact",
        "limits": {"per_call_usd": max_call_usd(), "per_seller_day_usd": seller_day_usd(),
                   "day_usd": day_usd(), "wallet": "the wallet balance minus payments in flight"},
        "how_it_works": ("A buyer paying this hub with credits buys from an x402 seller: the hub pays the seller's "
                         "quoted price from this wallet and charges the buyer the same amount once the seller "
                         "delivered. Nothing is charged for a refused or failed call, but such a payment "
                         "counts against the buyer's topped-up credit until it is shown unused on chain. "
                         "The per-seller limit counts the seller and each payee address."),
    }
    if reason:
        return doc
    try:
        wallet = address()
        doc["wallet"] = wallet
        with contextlib.suppress(Exception):
            doc["balance_usd"] = round(wallet_balance_usd(wallet, rpc=rpc), 6)
    except BridgeRefused as exc:  # pragma: no cover - raced with a key change
        doc.update(enabled=False, reason=exc.reason)
        return doc
    if ledger is not None:
        with contextlib.suppress(Exception):
            doc["today"] = {"spent_usd": round(ledger.spent_today(now), 6),
                            "in_flight_usd": round(ledger.in_flight(now), 6),
                            "delivered": ledger.count_today(now)}
            doc["recent"] = [{k: r[k] for k in ("created_at", "seller", "amount_usd", "status", "tx_hash")}
                             for r in ledger.recent(10)]
    return doc


# ── operator CLI ─────────────────────────────────────────────────────────────


def _keygen(path: str) -> int:
    """Create a new bridge key (0600). Prints the address only; refuses to overwrite."""
    from eth_account import Account

    if os.path.exists(path):
        print(f"refused: {path} exists")
        return 2
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    acct = Account.create(secrets.token_hex(32))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex())
        fh.write("\n")
    print(acct.address)
    return 0


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "keygen":
        return _keygen(args[1])
    if len(args) == 1 and args[0] == "address":
        print(address())
        return 0
    print("usage: python -m aimarket_hub.x402_bridge keygen PATH | address")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
