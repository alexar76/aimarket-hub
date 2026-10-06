#!/usr/bin/env python3
"""claim.audit@v1 — Independent AI's auditor that hires Attested Memory through the hub that called it.

The first CROSS-COMPANY subcontract. Two independent businesses, two hubs:

* the buyer calls ``claim.audit@v1`` on **independentai.network/hub** inside a job, with an allowance;
* this provider (run by Independent AI) buys two checks from **Attested Memory**, whose hub is
  ``hub.attestedmemory.net``, as children of that same job:

  - ``claim.check@v1`` — Attested's verdict on the claim from the evidence supplied
    (supported / contested / rejected / unverified);
  - ``contradiction.scan@v1`` — lexical-negation conflicts among the statements supplied;

* Independent's hub pays Attested's hub out of its own credit account there
  (``AIMARKET_PEER_API_KEYS``) and carves price + routing fee out of the buyer's allowance.

The answer is Independent's audit: Attested's two signed findings, a combined verdict
(``inconsistent`` when the statements contradict each other, otherwise Attested's status), each
child's job node and receipt digest, and this provider's own Ed25519 signature over the hub's
request-bound canonical.

Everything that is not the business logic — the job-token verification against the hub's key,
the one-time node check, the grant/fixed-price funding, the daily cap, the transport — is
``examples/subcontract-capability/server.py`` (``weather.witness@v1``) verbatim.

    python3 server.py                 # serve (environment: AUDIT_*, see README.md)
    python3 server.py --print-pubkey  # the provider_pubkey for capability.json
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

CAPABILITY_ID = "claim.audit@v1"
PRODUCT_ID = "independent-claim-audit"
JOB_HEADER = "X-AIMarket-Job"
TOKEN_DOMAIN = b"aimarket-job-token/1\n"
GRANT_HEADER = "X-AIMarket-Job-Grant"
USER_AGENT = "independent-claim-audit/1 (+https://independentai.network/hub/providers/claim-audit/healthz)"

# Attested Memory's two checks, on its own hub (product_id, capability_id).
CHECK = ("claim-check", "claim.check@v1")
SCAN = ("contradiction-scan", "contradiction.scan@v1")

# The same audit, paid differently: its two checks are bought on Attested's OWN hub and paid per
# call in USDC on Base from this provider's wallet — no account there, nothing prepaid. Priced to
# cover them (claim.audit@v1's price assumes the buyer's allowance pays the checks).
DIRECT_CAPABILITY_ID = "claim.audit.direct@v1"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_CHAIN_ID = 8453
DEFAULT_BASE_RPCS = "https://mainnet.base.org,https://base-rpc.publicnode.com,https://base.drpc.org"
#: keccak256("transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)")[:4]
TRANSFER_WITH_AUTHORIZATION_SELECTOR = "0xe3ee160e"
EIP712_TYPES: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"},
    ],
}

MICRO_PER_USD = 1_000_000
# The credits ledger's unit ($0.00001). A fee is rounded UP to it before it is counted
# against the daily cap, so the cap never under-counts what the ledger actually took.
LEDGER_UNIT_MICRO = 10
MAX_BODY_BYTES = 64 * 1024
MAX_STATEMENTS = 50


# Transport: (method, url, headers, body, timeout) -> (status, headers, body). Injectable so
# the hub's test suite can run this exact code against the real hub app in-process.
Transport = Callable[[str, str, Mapping[str, str], "bytes | None", float], "tuple[int, dict[str, str], bytes]"]


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None,
                     timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, **dict(headers)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read(4 * MAX_BODY_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read(4 * MAX_BODY_BYTES)
    except (urllib.error.URLError, OSError) as exc:
        return 0, {}, json.dumps({"error": "unreachable", "detail": str(exc)[:200]}).encode()


# ── configuration ────────────────────────────────────────────────────────────


def _usd(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "")
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value < 0:
        # A NaN cap compares false against everything, i.e. it would never refuse.
        raise SystemExit(f"{name}={raw!r} is not a non-negative number")
    return value


@dataclass
class Config:
    hub_url: str                       # the ONE hub whose tokens this provider accepts
    key_path: Path                     # this provider's Ed25519 key (PEM, created on first run)
    state_path: Path                   # the daily-cap ledger
    hub_api_url: str = ""              # where purchases are sent; defaults to hub_url
    hub_signer_pubkey: str = ""        # pin the hub's key instead of fetching it
    api_key: str = ""                  # this provider's own credits key (fixed price only)
    daily_cap_usd: float = 0.05
    child_source_hub: str = "https://hub.attestedmemory.net"
    # Sent as each child's max_price_usd, and reserved against the daily cap while the
    # purchase is in flight. The hub compares a ROUTED child's price + routing fee rounded UP
    # TO WHOLE CENTS against it, so for Attested's $0.022 check anything below $0.03 refuses it with
    # 409 price_limit_exceeded.
    child_max_price_usd: float = 0.03
    # Both children are bought in parallel and must be back well inside the hub's 30 s
    # provider timeout; the token and grant die 60 s after they were issued.
    child_timeout_s: float = 12.0
    # claim.audit.direct@v1: this provider's own Base wallet ({"private_key": "0x…"}, 0600), the
    # nodes it reads and sends through, and the ONLY addresses a 402 may name as the payee — a hub
    # that answered with any other payTo would be paid nothing.
    wallet_key_path: Path | None = None
    rpc_urls: tuple[str, ...] = ()
    child_payees: tuple[str, ...] = ()
    # One paid child: the 402, signing, sending, the confirmations, the delivery.
    direct_timeout_s: float = 25.0
    confirmations: int = 2

    def __post_init__(self) -> None:
        self.hub_url = self.hub_url.strip().rstrip("/")
        self.hub_api_url = (self.hub_api_url or self.hub_url).strip().rstrip("/")
        if not self.hub_url.startswith(("https://", "http://")):
            raise SystemExit("AUDIT_HUB_URL must be the hub's origin, e.g. https://independentai.network/hub")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        here = Path(__file__).resolve().parent
        key_path = Path(env.get("AUDIT_KEY_PATH") or here / "provider_key")
        return cls(
            hub_url=env.get("AUDIT_HUB_URL", "https://independentai.network/hub"),
            hub_api_url=env.get("AUDIT_HUB_API_URL", ""),
            hub_signer_pubkey=env.get("AUDIT_HUB_SIGNER_PUBKEY", "").strip(),
            key_path=key_path,
            state_path=Path(env.get("AUDIT_STATE_PATH") or key_path.parent / "daily_spend.json"),
            api_key=env.get("AUDIT_API_KEY", "").strip(),
            daily_cap_usd=_usd(env, "AUDIT_DAILY_CAP_USD", 0.05),
            child_source_hub=env.get("AUDIT_CHILD_SOURCE_HUB", "https://hub.attestedmemory.net").rstrip("/"),
            child_max_price_usd=_usd(env, "AUDIT_CHILD_MAX_PRICE_USD", 0.03),
            child_timeout_s=_usd(env, "AUDIT_CHILD_TIMEOUT_S", 12.0) or 12.0,
            wallet_key_path=Path(env["AUDIT_WALLET_KEY_PATH"]) if env.get("AUDIT_WALLET_KEY_PATH") else None,
            rpc_urls=tuple(u.strip() for u in env.get("AUDIT_BASE_RPC_URLS", DEFAULT_BASE_RPCS).split(",")
                           if u.strip()),
            child_payees=tuple(a.strip().lower() for a in env.get("AUDIT_CHILD_PAYEES", "").split(",")
                               if a.strip()),
            direct_timeout_s=_usd(env, "AUDIT_DIRECT_TIMEOUT_S", 25.0) or 25.0,
            confirmations=max(1, int(_usd(env, "AUDIT_CONFIRMATIONS", 2))),
        )



class Refusal(Exception):
    """An answer that is not an audit: status, a stable error code and why."""

    def __init__(self, status: int, error: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status, self.error, self.detail, self.extra = status, error, detail, extra

    def body(self) -> dict[str, Any]:
        return {"success": False, "error": self.error, "detail": self.detail, **self.extra}


# ── this provider's key and its signature ───────────────────────────────────


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise SystemExit(f"{path} is not an Ed25519 private key")
        return key
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    # Created 0600 before the secret is written, not chmod-ed after it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    return key


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def bound_canonical(capability_id: str, product_id: str, input_payload: Any, result: Any) -> str:
    """Byte for byte what the hub verifies (supply_security._bound_response_canonical).

    Bound to the ENVELOPE the hub posted — its capability_id, product_id and input — not to
    anything this provider recomputes. Signing a value of its own choosing produces a valid
    signature over the wrong bytes, which the hub reports only as "invalid provider response
    signature".
    """
    input_json = json.dumps(input_payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(
        {
            "capability_id": capability_id or "",
            "product_id": product_id or "",
            "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            "result": result,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


# ── the hub's key and its job tokens ────────────────────────────────────────


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class HubKey:
    """The public key the hub signs job tokens with.

    Pinned (AUDIT_HUB_SIGNER_PUBKEY) or read once from the hub's well-known over TLS. A
    failed fetch is remembered for a while: otherwise every forged call a stranger sends would
    make this provider fetch the well-known again, one outbound request per inbound one.
    """

    RETRY_AFTER_S = 30.0

    def __init__(self, cfg: Config, transport: Transport, now: Callable[[], float] = time.time):
        self._cfg, self._transport, self._now = cfg, transport, now
        self._lock = threading.Lock()
        self._key: Ed25519PublicKey | None = None
        self._b64 = ""
        self._failed_at = -math.inf

    @property
    def b64(self) -> str:
        return self._b64

    def get(self) -> Ed25519PublicKey:
        with self._lock:
            if self._key is not None:
                return self._key
            raw = self._cfg.hub_signer_pubkey
            if not raw:
                if self._now() - self._failed_at < self.RETRY_AFTER_S:
                    raise Refusal(503, "hub_key_unavailable", "the hub's signing key could not be read; retry shortly")
                status, _, body = self._transport(
                    "GET", f"{self._cfg.hub_url}/.well-known/ai-market.json", {}, None, 10.0)
                try:
                    raw = str(json.loads(body or b"{}").get("signer_public_key") or "") if status == 200 else ""
                except ValueError:
                    raw = ""
                if not raw:
                    self._failed_at = self._now()
                    raise Refusal(503, "hub_key_unavailable",
                                  f"{self._cfg.hub_url}/.well-known/ai-market.json names no signer_public_key")
            try:
                self._key = Ed25519PublicKey.from_public_bytes(base64.b64decode(raw, validate=True))
            except Exception as exc:  # noqa: BLE001 - any malformed key is the same refusal
                self._failed_at = self._now()
                raise Refusal(503, "hub_key_unavailable", "the hub's signer_public_key is not an Ed25519 key") from exc
            self._b64 = raw
            return self._key


_JOB_ID = re.compile(r"^job_[0-9a-f]{24}$")


class TokenVerifier:
    """Accepts only a live job token this hub issued for a call to THIS capability, once."""

    def __init__(self, cfg: Config, hub_key: HubKey, now: Callable[[], float] = time.time):
        self._cfg, self._hub_key, self._now = cfg, hub_key, now
        self._seen: dict[str, float] = {}      # node -> exp
        self._lock = threading.Lock()

    def verify(self, token: str, capability_id: str = CAPABILITY_ID) -> dict[str, Any]:
        try:
            payload_b64, sig_b64 = (token or "").strip().split(".", 1)
            payload, signature = _b64url_decode(payload_b64), _b64url_decode(sig_b64)
            claims = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            raise Refusal(403, "job_invalid", f"{JOB_HEADER} is not a job token") from exc
        try:
            # The hub signs "aimarket-job-token/1\n" + the claims (mandates.md §6.1): the
            # prefix keeps a token from verifying as anything else the hub's key signs.
            self._hub_key.get().verify(signature, TOKEN_DOMAIN + payload)
        except InvalidSignature as exc:
            raise Refusal(403, "job_invalid", "the job token was not signed by the hub this provider serves") from exc
        if not isinstance(claims, dict) or claims.get("v") != 1:
            raise Refusal(403, "job_invalid", "unsupported job token version")
        if claims.get("iss") != self._cfg.hub_url:
            raise Refusal(403, "job_invalid", f"the job token was issued by another hub, not {self._cfg.hub_url}")
        exp = claims.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool) or exp < self._now():
            raise Refusal(403, "job_invalid", "the job token has expired")
        path = claims.get("path")
        if not (isinstance(path, list) and path and all(isinstance(p, str) for p in path)):
            raise Refusal(403, "job_invalid", "the job token has no path")
        # The hub gives a token to EVERY provider it executes. Without this check another
        # provider on the same hub could replay the token it was handed at this public URL
        # and have this provider spend inside a job that never asked for an audit.
        if path[-1] != capability_id:
            raise Refusal(403, "job_invalid", f"the job token was issued for {path[-1]}, not {capability_id}")
        if claims.get("product") != PRODUCT_ID:
            raise Refusal(403, "job_invalid", "the job token was issued for another product")
        node = claims.get("node")
        if not (isinstance(node, str) and node) or not _JOB_ID.match(str(claims.get("job") or "")):
            raise Refusal(403, "job_invalid", "the job token names no job or node")
        depth, max_depth = claims.get("depth"), claims.get("maxDepth")
        if not (isinstance(depth, int) and isinstance(max_depth, int)):
            raise Refusal(403, "job_invalid", "the job token has no depth")
        if depth + 1 > max_depth:
            # The hub would refuse both purchases with job_limit anyway; refusing here
            # keeps the attempt from ever reaching it.
            raise Refusal(403, "job_limit", "this job allows no deeper subcontracting than this call")
        with self._lock:
            now = self._now()
            for seen_node, seen_exp in list(self._seen.items()):
                if seen_exp < now:
                    del self._seen[seen_node]
            if node in self._seen:
                # One hub call, one audit. A second request under the same node is a
                # replay of a token somebody saw, not the hub asking again.
                raise Refusal(403, "job_replayed", "this job node has already been served")
            self._seen[node] = float(exp)
        return claims


# ── the daily cap on this provider's own money ──────────────────────────────


@dataclass(frozen=True)
class Reservation:
    day: str
    micro: int


class DailyCap:
    """What this provider may spend of its OWN money per UTC day (fixed-price mode).

    Reserve-before-spend: the worst case of a call is reserved before anything is bought,
    under one lock, and settled down to what the purchases actually cost afterwards. The file
    holds the reserved total, so a crash mid-call counts the in-flight reservation as spent —
    the error runs toward refusing, never toward overspending. One process only (the unit runs
    one); the lock is not shared across processes.
    """

    def __init__(self, path: Path, cap_usd: float, now: Callable[[], float] = time.time):
        self._path, self._cap = path, int(round(cap_usd * MICRO_PER_USD))
        self._now = now
        self._lock = threading.Lock()

    def _day(self) -> str:
        return datetime.fromtimestamp(self._now(), tz=timezone.utc).strftime("%Y-%m-%d")

    def _read(self, day: str) -> int:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            raise Refusal(503, "daily_cap_unavailable", "cannot read the spending ledger") from exc
        if not isinstance(state, dict):
            raise Refusal(503, "daily_cap_unavailable", "invalid spending ledger")
        if state.get("day") != day:
            return 0
        used = state.get("used_micro")
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            raise Refusal(503, "daily_cap_unavailable", "invalid spending ledger")
        return used

    def _write(self, day: str, used: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day, "used_micro": used}), encoding="utf-8")
        os.replace(tmp, self._path)   # atomic: a crash never leaves half a ledger

    def used_micro(self) -> int:
        with self._lock:
            return self._read(self._day())

    def reserve(self, micro: int) -> Reservation | None:
        with self._lock:
            day = self._day()
            used = self._read(day)
            if used + micro > self._cap:
                return None
            self._write(day, used + micro)
            return Reservation(day, micro)

    def settle(self, reservation: Reservation, spent_micro: int) -> None:
        give_back = max(0, reservation.micro - max(0, spent_micro))
        if give_back:
            with self._lock:
                day = self._day()
                if reservation.day == day:
                    self._write(day, max(0, self._read(day) - give_back))


# ── paying a child per call in USDC (claim.audit.direct@v1) ─────────────────


class PaymentRefused(Exception):
    """Nothing was sent: the 402 was not one this provider pays, or the chain refused it up front."""


class PaymentUnknown(Exception):
    """A transaction was sent and its outcome is not known in time: it may still be mined."""


def _hex(value: Any, digits: int) -> bool:
    text = str(value or "")
    return (len(text) == digits + 2 and text[:2] in ("0x", "0X")
            and all(c in "0123456789abcdefABCDEF" for c in text[2:]))


def _whole(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _word(value: int | str) -> str:
    if isinstance(value, str):
        return (value[2:] if value.startswith(("0x", "0X")) else value).rjust(64, "0").lower()
    return format(value, "064x")


class ChainWallet:
    """This provider's own hot wallet on Base. It pays exactly what a hub's 402 asks of it, to an
    address it was told to expect, with an EIP-3009 transferWithAuthorization it sends itself (the
    hub holds no key and submits nothing) — and it keeps the float small: a stolen server loses
    the float, not the company's treasury."""

    def __init__(self, cfg: Config, transport: Transport, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        from eth_account import Account

        data = json.loads(cfg.wallet_key_path.read_text(encoding="utf-8"))
        self.account = Account.from_key(data["private_key"] if isinstance(data, dict) else str(data))
        self.address = self.account.address
        self.cfg, self.transport, self.now, self.sleep = cfg, transport, now, sleep
        self._lock = threading.Lock()
        self._next_nonce: int | None = None

    def rpc(self, method: str, params: list[Any]) -> Any:
        last = "no RPC configured"
        for url in self.cfg.rpc_urls:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            status, _, raw = self.transport("POST", url, {"Content-Type": "application/json"}, body, 10.0)
            try:
                answer = json.loads(raw or b"{}")
            except ValueError:
                answer = {}
            if status == 200 and isinstance(answer, dict) and "result" in answer:
                return answer["result"]
            last = str((answer.get("error") if isinstance(answer, dict) else None) or f"HTTP {status}")[:200]
        raise PaymentUnknown(f"{method}: {last}")

    def _terms(self, offer: dict[str, Any], max_units: int) -> tuple[str, str, int, dict[str, Any]]:
        accepts = offer.get("accepts") if isinstance(offer.get("accepts"), list) else []
        accept = accepts[0] if accepts and isinstance(accepts[0], dict) else {}
        extra = accept.get("extra") if isinstance(accept.get("extra"), dict) else {}
        nonce = str(offer.get("nonce") or extra.get("nonce") or "")
        pay_to = str(accept.get("payTo") or "")
        units = _whole(accept.get("maxAmountRequired") or accept.get("amount"))
        if accept.get("scheme") != "exact" or str(accept.get("network")) not in ("base", f"eip155:{BASE_CHAIN_ID}"):
            raise PaymentRefused("the 402 does not ask for an exact USDC payment on Base")
        if str(accept.get("asset") or "").lower() != USDC_BASE:
            raise PaymentRefused("the 402 asks for a token that is not USDC on Base")
        if not _hex(nonce, 64) or not offer.get("payment_secret"):
            raise PaymentRefused("the 402 carries no payment nonce and secret")
        if not _hex(pay_to, 40) or pay_to.lower() not in self.cfg.child_payees:
            raise PaymentRefused(f"the 402 names a payee this provider does not pay: {pay_to[:12]}")
        if units is None or units > max_units:
            raise PaymentRefused(f"the 402 asks for {units} units, more than this provider's ceiling {max_units}")
        if not extra.get("name") or not extra.get("version"):
            raise PaymentRefused("the 402 names no EIP-712 domain")
        return nonce, pay_to, units, extra

    def pay(self, offer: dict[str, Any], *, max_units: int, deadline: float) -> dict[str, Any]:
        """Pay one seller-direct 402 and return {tx_hash, nonce, units, pay_to} once mined."""
        from eth_account import Account
        from eth_utils import to_checksum_address

        nonce, pay_to, units, extra = self._terms(offer, max_units)
        typed = {
            "types": EIP712_TYPES, "primaryType": "TransferWithAuthorization",
            "domain": {"name": str(extra["name"]), "version": str(extra["version"]), "chainId": BASE_CHAIN_ID,
                       "verifyingContract": to_checksum_address(USDC_BASE)},
            "message": {"from": self.address, "to": to_checksum_address(pay_to), "value": units,
                        "validAfter": 0, "validBefore": int(self.now()) + 600, "nonce": nonce},
        }
        sig = Account.sign_typed_data(self.account.key, full_message=typed).signature.hex()
        sig = sig[2:] if sig.startswith("0x") else sig
        v, r, s = int(sig[128:130], 16), sig[0:64], sig[64:128]
        v = v + 27 if v in (0, 1) else v
        data = TRANSFER_WITH_AUTHORIZATION_SELECTOR + "".join(_word(w) for w in (
            self.address, pay_to, units, 0, typed["message"]["validBefore"], nonce, v, r, s))
        with self._lock:   # two children in parallel must not take the same account nonce
            if int(str(self.rpc("eth_chainId", [])), 16) != BASE_CHAIN_ID:
                raise PaymentRefused("the node is not on Base")
            pending = int(str(self.rpc("eth_getTransactionCount", [self.address, "pending"])), 16)
            tx_nonce = max(pending, self._next_nonce or 0)
            base_fee = int(str((self.rpc("eth_getBlockByNumber", ["latest", False]) or {}).get("baseFeePerGas") or "0x0"), 16)
            try:
                gas = int(str(self.rpc("eth_estimateGas", [{"from": self.address, "to": to_checksum_address(USDC_BASE),
                                                             "data": data}])), 16)
            except PaymentUnknown as exc:
                # An authorization the token would revert (no USDC, a spent nonce) fails here, before
                # anything is sent.
                raise PaymentRefused(f"the token would refuse this payment: {exc}") from None
            tip = 1_000_000
            tx = {"chainId": BASE_CHAIN_ID, "nonce": tx_nonce, "to": to_checksum_address(USDC_BASE), "value": 0,
                  "data": data, "gas": int(gas * 1.3) + 5_000, "maxPriorityFeePerGas": tip,
                  "maxFeePerGas": base_fee * 2 + tip, "type": 2}
            signed = self.account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            raw_hex = raw.hex()
            tx_hash = str(self.rpc("eth_sendRawTransaction", ["0x" + (raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)]))
            self._next_nonce = tx_nonce + 1
        paid = {"tx_hash": tx_hash, "nonce": nonce, "units": units, "pay_to": pay_to.lower()}
        while self.now() < deadline:
            try:
                receipt = self.rpc("eth_getTransactionReceipt", [tx_hash])
                if receipt:
                    if str(receipt.get("status")) not in ("0x1", "1"):
                        raise PaymentRefused(f"the payment {tx_hash} reverted (gas spent, no USDC moved)")
                    head = int(str(self.rpc("eth_blockNumber", [])), 16)
                    if head - int(str(receipt.get("blockNumber") or "0x0"), 16) + 1 >= self.cfg.confirmations:
                        return paid
            except PaymentUnknown:
                pass
            self.sleep(1.0)
        raise PaymentUnknown(f"the payment {tx_hash} was not confirmed in time; it may still be mined")


# ── the capability ───────────────────────────────────────────────────────────


# ── the capability ───────────────────────────────────────────────────────────


def parse_input(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """(input for claim.check, input for contradiction.scan), or a Refusal the hub scores as incomplete."""
    if not isinstance(raw, dict):
        raise Refusal(400, "input_invalid", "claim required: send {claim, evidence, statements}")
    claim = raw.get("claim")
    if not isinstance(claim, str) or not 3 <= len(claim) <= 2000:
        raise Refusal(400, "input_invalid", "claim required: a string of 3-2000 characters")
    evidence = raw.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 100:
        raise Refusal(400, "input_invalid", "evidence required: a list of at most 100 items")
    statements = raw.get("statements")
    if (not isinstance(statements, list) or not 2 <= len(statements) <= MAX_STATEMENTS
            or not all(isinstance(s, str) and 0 < len(s) <= 2000 for s in statements)):
        raise Refusal(400, "input_invalid",
                      f"statements required: 2-{MAX_STATEMENTS} non-empty strings to scan for conflicts")
    return {"claim": claim, "evidence": evidence}, {"statements": statements}


def _output(child: dict[str, Any]) -> dict[str, Any]:
    out = child.get("output", child.get("result"))
    return out if isinstance(out, dict) else {}



def _child_ref(capability_id: str, child: dict[str, Any]) -> dict[str, Any]:
    job = child.get("job") if isinstance(child.get("job"), dict) else {}
    receipt = child.get("provenance_receipt") if isinstance(child.get("provenance_receipt"), dict) else {}
    signed = child.get("receipt") if isinstance(child.get("receipt"), dict) else {}
    ref = {"capability_id": capability_id, "node": job.get("node"), "funded_by": job.get("funded_by"),
           "price_usd": child.get("price_usd"), "receipt_digest": receipt.get("digest_sri"),
           # The seller hub's own signed receipt: present whether or not it issues provenance receipts.
           "receipt_nonce": signed.get("nonce")}
    paid = child.get("_paid") if isinstance(child.get("_paid"), dict) else {}
    if paid.get("tx_hash"):
        # Paid per call on chain: the transaction anyone can look up, and to whom it went.
        ref["payment"] = {"rail": "usdc-per-call", "chain": "base", "tx_hash": paid["tx_hash"],
                          "amount_usd": round(int(paid.get("units") or 0) / MICRO_PER_USD, 6),
                          "pay_to": paid.get("pay_to")}
    return ref


def _json_object(raw: bytes | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def charged_micro(child: dict[str, Any], ceiling_micro: int) -> int:
    """An upper bound on what one delivered child cost this provider's own key.

    The hub reports the price it charged and its routing-fee rate; the fee is added and
    rounded up to the ledger's unit. Where the hub is the seller of record it charges no fee
    at all, so this over-counts by the fee — toward refusing, never toward overspending.
    """
    try:
        price = max(0.0, float(child.get("price_usd") or 0.0))
        bps = max(0, int(child.get("routing_fee_bps") or 0))
    except (TypeError, ValueError, OverflowError):
        return ceiling_micro
    if not math.isfinite(price):
        return ceiling_micro
    micro = price * MICRO_PER_USD * (1 + bps / 10_000)
    micro = int(math.ceil(micro / LEDGER_UNIT_MICRO - 1e-9) * LEDGER_UNIT_MICRO)
    return min(ceiling_micro, micro)


def cost_bound(status: int, child: dict[str, Any], ceiling_micro: int) -> int:
    """What one purchase may have cost, for the daily cap: never less than the truth."""
    if status == 200 and child.get("success") is True:
        return charged_micro(child, ceiling_micro)
    if status == 0 or status >= 500:
        # Unknown. A timeout here does not stop the hub, which may still deliver and
        # settle; a 5xx can come after the price was captured (a failed fee capture).
        return ceiling_micro
    # A refusal (4xx) or a peer's honest refusal (200, success false): the hub releases
    # every hold on those exits, so nothing was taken.
    return 0



def combined_verdict(check: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
    """Independent's reading of Attested's two findings."""
    status = str(check.get("status") or "unverified")
    conflicts = scan.get("contradictions") if isinstance(scan.get("contradictions"), list) else []
    if conflicts:
        return {"verdict": "inconsistent", "because": f"{len(conflicts)} contradiction(s) among the statements",
                "attested_status": status}
    return {"verdict": status, "because": "no contradiction among the statements; Attested's status stands",
            "attested_status": status}


class Auditor:
    """The whole provider: verify, buy Attested's two checks, combine, sign."""

    def __init__(self, cfg: Config, *, transport: Transport = urllib_transport,
                 key: Ed25519PrivateKey | None = None, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.transport = transport
        self.key = key or load_or_create_key(cfg.key_path)
        self.pubkey_b64 = public_key_b64(self.key)
        self.hub_key = HubKey(cfg, transport, now)
        self.tokens = TokenVerifier(cfg, self.hub_key, now)
        self.cap = DailyCap(cfg.state_path, cfg.daily_cap_usd, now)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="audit-child")
        self.wallet = ChainWallet(cfg, transport) if cfg.wallet_key_path else None

    def _buy(self, child: tuple[str, str], child_input: dict[str, Any],
             headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        product_id, capability_id = child
        body = {
            "product_id": product_id,
            "capability_id": capability_id,
            # Required for a routed child: without it the hub looks for a LOCAL capability,
            # finds only the peer's listing and refuses with 400.
            "source_hub": self.cfg.child_source_hub,
            "input": child_input,
            "max_price_usd": self.cfg.child_max_price_usd,
        }
        status, _, raw = self.transport(
            "POST", f"{self.cfg.hub_api_url}/ai-market/v2/invoke",
            {"Content-Type": "application/json", **headers},
            json.dumps(body, separators=(",", ":")).encode("utf-8"), self.cfg.child_timeout_s,
        )
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def _buy_direct(self, child: tuple[str, str], child_input: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Buy one check on the subcontractor's OWN hub and pay it per call in USDC: the hub answers
        402 with the seller's terms, this provider pays them on chain, then presents the payment."""
        product_id, capability_id = child
        url = f"{self.cfg.child_source_hub}/ai-market/v2/invoke"
        body = json.dumps({"product_id": product_id, "capability_id": capability_id, "input": child_input},
                          separators=(",", ":")).encode("utf-8")
        deadline = time.time() + self.cfg.direct_timeout_s
        status, _, raw = self.transport("POST", url, {"Content-Type": "application/json"}, body,
                                        self.cfg.child_timeout_s)
        offer = _json_object(raw)
        if status != 402:
            return status, offer        # served unpaid, or refused before any money moved
        ceiling = int(round(self.cfg.child_max_price_usd * MICRO_PER_USD))
        try:
            paid = self.wallet.pay(offer, max_units=ceiling, deadline=deadline)
        except PaymentRefused as exc:
            return 402, {"success": False, "error": "payment_refused", "detail": str(exc)[:200]}
        except PaymentUnknown as exc:
            return 0, {"success": False, "error": "payment_unconfirmed", "detail": str(exc)[:200],
                       "_paid": {"units": ceiling, "unknown": True}}
        headers = {"Content-Type": "application/json", "X-Payment": paid["tx_hash"],
                   "X-Payment-Nonce": paid["nonce"], "X-Payment-Secret": str(offer.get("payment_secret"))}
        while True:
            status, _, raw = self.transport("POST", url, headers, body, self.cfg.child_timeout_s)
            answer = _json_object(raw)
            # The hub's node can be a block behind this one: "not on chain yet" / "1 confirmation(s)".
            if status == 402 and time.time() < deadline:
                time.sleep(1.5)
                continue
            break
        answer["_paid"] = paid
        return status, answer

    def handle(self, headers: Mapping[str, str], raw_body: bytes) -> tuple[int, dict[str, str], bytes]:
        try:
            status, extra_headers, body = self._handle(headers, raw_body)
        except Refusal as refusal:
            status, extra_headers, body = refusal.status, {}, refusal.body()
        except Exception:  # noqa: BLE001 - a bug here is this provider's fault, and says so
            import traceback

            traceback.print_exc(file=sys.stderr)
            status, extra_headers, body = 500, {}, {"success": False, "error": "internal_error",
                                                    "detail": "the auditor failed; see the provider's log"}
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return status, {"Content-Type": "application/json", **extra_headers}, payload

    def _handle(self, headers: Mapping[str, str], raw_body: bytes) -> tuple[int, dict[str, str], dict[str, Any]]:
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            envelope = json.loads(raw_body or b"{}")
        except ValueError as exc:
            raise Refusal(400, "input_invalid", "the body is not JSON") from exc
        if not isinstance(envelope, dict):
            raise Refusal(400, "input_invalid", "the body must be the hub's invoke envelope")
        capability_id = envelope.get("capability_id") or CAPABILITY_ID
        if capability_id not in (CAPABILITY_ID, DIRECT_CAPABILITY_ID):
            raise Refusal(404, "unknown_capability",
                          f"this provider serves {CAPABILITY_ID} and {DIRECT_CAPABILITY_ID} only")
        direct = capability_id == DIRECT_CAPABILITY_ID
        if direct and (self.wallet is None or not self.cfg.child_payees):
            raise Refusal(503, "wallet_not_configured",
                          f"{DIRECT_CAPABILITY_ID} pays its subcontractors from this provider's wallet, "
                          "and none is configured here")

        # Nothing is bought, and no input is even looked at, before the token checks out.
        token = (lower.get(JOB_HEADER.lower()) or "").strip()
        if not token:
            raise Refusal(403, "job_required",
                          f"{CAPABILITY_ID} is sold through {self.cfg.hub_url}: buy it there, not here")
        claims = self.tokens.verify(token, capability_id)
        check_input, scan_input = parse_input(envelope.get("input"))

        grant = (lower.get(GRANT_HEADER.lower()) or "").strip()
        ceiling = int(round(self.cfg.child_max_price_usd * MICRO_PER_USD))
        reserved = 0
        reservation = None
        child_headers: dict[str, str] = {}
        if direct:
            mode = "usdc-per-call"
            # The same daily budget bounds what the wallet may spend; an allowance the buyer sent
            # is left alone — this audit is priced to pay its checks itself.
            reserved = 2 * ceiling
            reservation = self.cap.reserve(reserved)
            if reservation is None:
                raise Refusal(429, "daily_cap_reached",
                              "this provider's own daily budget for paying Attested is spent; try tomorrow")
        elif grant:
            mode = "cost-plus"
            # The token and the grant, exactly as received, and NOTHING else: the allowance
            # pays, and the hub refuses a grant that arrives with any other payment.
            child_headers = {JOB_HEADER: token, GRANT_HEADER: grant}
        else:
            mode = "fixed-price"
            if not self.cfg.api_key:
                raise Refusal(402, "allowance_required",
                              "this provider pays Attested only from the buyer's allowance here: "
                              'invoke with "subcontract": {"allowance_usd": 0.05}')
            reserved = 2 * ceiling
            reservation = self.cap.reserve(reserved)
            if reservation is None:
                raise Refusal(429, "daily_cap_reached",
                              "this provider's own daily budget is spent: invoke with "
                              '"subcontract": {"allowance_usd": 0.05} to pay Attested from your allowance')
            child_headers = {JOB_HEADER: token, "X-API-Key": self.cfg.api_key}

        spent = reserved
        try:
            if direct:
                futures = {CHECK: self._pool.submit(self._buy_direct, CHECK, check_input),
                           SCAN: self._pool.submit(self._buy_direct, SCAN, scan_input)}
            else:
                futures = {CHECK: self._pool.submit(self._buy, CHECK, check_input, child_headers),
                           SCAN: self._pool.submit(self._buy, SCAN, scan_input, child_headers)}
            outcomes = {child: f.result() for child, f in futures.items()}
            delivered = {child: status == 200 and body.get("success") is True
                         for child, (status, body) in outcomes.items()}
            if mode == "fixed-price":
                spent = sum(cost_bound(status, body, ceiling) for status, body in outcomes.values())
            elif direct:
                # What left the wallet, delivered or not: a payment is spent the moment it is mined.
                spent = sum(int((body.get("_paid") or {}).get("units") or 0) for _, body in outcomes.values())
        finally:
            if reservation is not None:
                self.cap.settle(reservation, spent)

        if not all(delivered.values()):
            # A child that delivered is still paid (§6.3: materials consumed are paid for) and
            # shows in the buyer's bill as captured.
            raise Refusal(502, "child_failed", "an Attested check could not be bought", children=[
                {"capability_id": child[1], "status": status, "error": body.get("error"),
                 "detail": str(body.get("detail") or body.get("refuse_reason") or "")[:200]}
                for child, (status, body) in outcomes.items() if not delivered[child]
            ])

        check, scan = outcomes[CHECK][1], outcomes[SCAN][1]
        result = {
            "audit": "independent-claim-audit/1",
            "auditor": "Independent AI",
            "subcontractor": {"name": "Attested Memory", "hub": self.cfg.child_source_hub},
            "claim": check_input["claim"],
            **combined_verdict(_output(check), _output(scan)),
            "attested": {"claim_check": _output(check), "contradiction_scan": _output(scan)},
            "funding": mode,
            "job": {"job_id": claims["job"], "node": claims["node"], "depth": claims["depth"]},
            "children": [_child_ref(CHECK[1], check), _child_ref(SCAN[1], scan)],
        }
        signature = base64.b64encode(self.key.sign(bound_canonical(
            str(envelope.get("capability_id") or ""), str(envelope.get("product_id") or ""),
            envelope.get("input"), result,
        ).encode("utf-8"))).decode()
        return 200, {"X-Provider-Signature": signature}, {"success": True, "result": result}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "capability_id": CAPABILITY_ID, "product_id": PRODUCT_ID,
                "hub": self.cfg.hub_url, "subcontractor_hub": self.cfg.child_source_hub,
                "provider_pubkey": self.pubkey_b64, "fixed_price": bool(self.cfg.api_key),
                "direct": {"capability_id": DIRECT_CAPABILITY_ID,
                           "wallet": self.wallet.address if self.wallet else None,
                           "payees": list(self.cfg.child_payees)}}


# ── HTTP ─────────────────────────────────────────────────────────────────────


def make_handler(auditor: Auditor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "independent-claim-audit/1"

        def _send(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("/healthz", "/health"):
                body = json.dumps(auditor.health()).encode()
                self._send(200, {"Content-Type": "application/json"}, body)
            else:
                self._send(404, {"Content-Type": "application/json"}, b'{"error":"not_found"}')

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/invoke":
                self._send(404, {"Content-Type": "application/json"}, b'{"error":"not_found"}')
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                self._send(413, {"Content-Type": "application/json"}, b'{"error":"body_too_large"}')
                return
            status, headers, body = auditor.handle(dict(self.headers.items()), self.rfile.read(length))
            self._send(status, headers, body)

        def log_message(self, fmt: str, *args: Any) -> None:
            # The request line only: headers carry the job token and the grant, which are
            # bearer secrets for the length of one call and have no business in a log.
            sys.stderr.write(f"[claim-audit] {fmt % args}\n")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print-pubkey", action="store_true",
                        help="print this provider's public key (creating the key if needed) and exit")
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    if args.print_pubkey:
        print(public_key_b64(load_or_create_key(cfg.key_path)))
        return 0
    bind = os.environ.get("AUDIT_BIND", "127.0.0.1")
    port = int(os.environ.get("AUDIT_PORT", "9476"))
    auditor = Auditor(cfg)
    print(f"claim-audit serving {CAPABILITY_ID} on http://{bind}:{port}/invoke for {cfg.hub_url}", flush=True)
    print(f"provider_pubkey: {auditor.pubkey_b64}", flush=True)
    print(f"fixed-price mode: {'on, cap $%.4f/day' % cfg.daily_cap_usd if cfg.api_key else 'off (allowance only)'}",
          flush=True)
    print(f"{DIRECT_CAPABILITY_ID}: " + (f"pays from {auditor.wallet.address} to {', '.join(cfg.child_payees) or 'NOBODY'}"
                                          if auditor.wallet else "off (no AUDIT_WALLET_KEY_PATH)"), flush=True)
    ThreadingHTTPServer((bind, port), make_handler(auditor)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
