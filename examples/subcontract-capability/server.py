#!/usr/bin/env python3
"""weather.witness@v1 — a provider that hires two other providers through the hub that called it.

Subcontracting (aimarket-protocol/mandates.md §6) needs a provider that BUYS while it
serves. Until this one, none did: every capability the apex hub lists is federated, and the
hub hands its job token only to providers it executes itself. This is the smallest real one.

For a place (a city GAIA relays, or latitude + longitude) it buys two readings through the
SAME hub, inside the caller's job:

* ``gaia.weather.read@v1`` and ``gaia.air.read@v1`` (product ``gaia.gateway``, routed to
  ``https://iot.modelmarket.dev`` — a federated child MUST name its ``source_hub``, or the
  hub looks for a local capability and refuses);

and answers with both device-attested readings, whether they describe the same place and
moment, and each child's job node and work-receipt digest.

Who pays for the readings is the buyer's choice (§6.2-6.3):

* **cost-plus** — the buyer set aside an allowance, so the hub sent ``X-AIMarket-Job-Grant``.
  The purchases carry the job token and the grant and NOTHING else: the hub refuses a grant
  sent together with another payment, and the allowance pays.
* **fixed price** — no grant. The purchases carry the job token and this provider's own
  ``X-API-Key``, under a daily cap (``WITNESS_DAILY_CAP_USD``). Without a key configured the
  provider refuses rather than spend.

Before it buys anything it verifies the job token: the hub's Ed25519 signature (the key the
hub publishes as ``signer_public_key`` in ``/.well-known/ai-market.json``), ``iss`` equal to
the one hub it serves, ``exp``, that the token was issued for a call to THIS capability, and
that its node has not been served before. The invoke URL is public, so without that anyone
could make it spend — its own money in fixed-price mode, and a stranger's leaked grant in
cost-plus mode.

The reply is signed with this provider's own Ed25519 key over the hub's request-bound
canonical (capability id + product id + a hash of the input + the result), the one
``supply_security._bound_response_canonical`` verifies, so it cannot be replayed against
another call.

Deliberately stdlib + ``cryptography`` only: it runs from a bare systemd unit on the hub host.

    python3 server.py                 # serve (see README.md for the environment)
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

CAPABILITY_ID = "weather.witness@v1"
PRODUCT_ID = "weather-witness"
JOB_HEADER = "X-AIMarket-Job"
TOKEN_DOMAIN = b"aimarket-job-token/1\n"
GRANT_HEADER = "X-AIMarket-Job-Grant"
USER_AGENT = "weather-witness/1 (+https://modelmarket.dev/providers/weather-witness/healthz)"

WEATHER_CAP = "gaia.weather.read@v1"
AIR_CAP = "gaia.air.read@v1"

MICRO_PER_USD = 1_000_000
# The credits ledger's unit ($0.00001). A fee is rounded UP to it before it is counted
# against the daily cap, so the cap never under-counts what the ledger actually took.
LEDGER_UNIT_MICRO = 10
MAX_BODY_BYTES = 64 * 1024

# Two readings "agree" when they describe the same place and the same moment.
#  * 75 km is GAIA's own radius: it refuses to answer a place with a weather relay farther
#    away than that, because a farther relay is not that place's weather.
#  * 2 h because air quality is an hourly model product while the weather relays update every
#    15 minutes; two readings further apart than that do not describe one moment.
AGREE_MAX_KM = 75.0
AGREE_MAX_SECONDS = 2 * 3600

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
    child_source_hub: str = "https://iot.modelmarket.dev"
    child_product_id: str = "gaia.gateway"
    # Sent as each child's max_price_usd, and reserved against the daily cap while the
    # purchase is in flight. The hub compares a ROUTED child's price + routing fee rounded UP
    # TO WHOLE CENTS against it, so for a $0.001 reading anything below $0.01 refuses every
    # purchase with 409 price_limit_exceeded.
    child_max_price_usd: float = 0.01
    # Both children are bought in parallel and must be back well inside the hub's 30 s
    # provider timeout; the token and grant die 60 s after they were issued.
    child_timeout_s: float = 12.0

    def __post_init__(self) -> None:
        self.hub_url = self.hub_url.strip().rstrip("/")
        self.hub_api_url = (self.hub_api_url or self.hub_url).strip().rstrip("/")
        if not self.hub_url.startswith(("https://", "http://")):
            raise SystemExit("WITNESS_HUB_URL must be the hub's origin, e.g. https://modelmarket.dev")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        here = Path(__file__).resolve().parent
        key_path = Path(env.get("WITNESS_KEY_PATH") or here / "provider_key")
        return cls(
            hub_url=env.get("WITNESS_HUB_URL", "https://modelmarket.dev"),
            hub_api_url=env.get("WITNESS_HUB_API_URL", ""),
            hub_signer_pubkey=env.get("WITNESS_HUB_SIGNER_PUBKEY", "").strip(),
            key_path=key_path,
            state_path=Path(env.get("WITNESS_STATE_PATH") or key_path.parent / "daily_spend.json"),
            api_key=env.get("WITNESS_API_KEY", "").strip(),
            daily_cap_usd=_usd(env, "WITNESS_DAILY_CAP_USD", 0.05),
            child_source_hub=env.get("WITNESS_CHILD_SOURCE_HUB", "https://iot.modelmarket.dev").rstrip("/"),
            child_product_id=env.get("WITNESS_CHILD_PRODUCT_ID", "gaia.gateway"),
            child_max_price_usd=_usd(env, "WITNESS_CHILD_MAX_PRICE_USD", 0.01),
            child_timeout_s=_usd(env, "WITNESS_CHILD_TIMEOUT_S", 12.0) or 12.0,
        )


class Refusal(Exception):
    """An answer that is not a witness: status, a stable error code and why."""

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

    Pinned (WITNESS_HUB_SIGNER_PUBKEY) or read once from the hub's well-known over TLS. A
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

    def verify(self, token: str) -> dict[str, Any]:
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
        # and have this provider spend inside a job that never asked for a witness.
        if path[-1] != CAPABILITY_ID:
            raise Refusal(403, "job_invalid", f"the job token was issued for {path[-1]}, not {CAPABILITY_ID}")
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
                # One hub call, one witness. A second request under the same node is a
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


# ── the capability ───────────────────────────────────────────────────────────


def parse_place(raw: Any) -> dict[str, Any]:
    """The part of the input the children receive: a city, or latitude + longitude."""
    if not isinstance(raw, dict):
        raise Refusal(400, "input_invalid", "input must be an object")
    lat, lon = raw.get("latitude"), raw.get("longitude")
    if lat is not None or lon is not None:
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError) as exc:
            raise Refusal(400, "input_invalid", "latitude and longitude must be numbers, together") from exc
        if not (math.isfinite(lat_f) and math.isfinite(lon_f) and -90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            raise Refusal(400, "input_invalid", "latitude must be in [-90, 90] and longitude in [-180, 180]")
        return {"latitude": lat_f, "longitude": lon_f}
    city = raw.get("city")
    if isinstance(city, str) and city.strip() and len(city) <= 80:
        return {"city": city.strip()}
    raise Refusal(400, "input_invalid", 'pass a place: {"city": "Berlin"} or {"latitude": …, "longitude": …}')


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _point(resolved: dict[str, Any], lat_key: str, lon_key: str) -> tuple[float, float] | None:
    try:
        point = (float(resolved[lat_key]), float(resolved[lon_key]))
    except (KeyError, TypeError, ValueError):
        return None
    return point if all(math.isfinite(v) for v in point) else None


def _when(reading: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(reading.get("ts") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _output(child: dict[str, Any]) -> dict[str, Any]:
    out = child.get("output", child.get("result"))
    return out if isinstance(out, dict) else {}


def agreement(weather: dict[str, Any], air: dict[str, Any]) -> dict[str, Any]:
    """Do the two readings describe the same place and the same moment?"""
    w_out, a_out = _output(weather), _output(air)
    w_res, a_res = w_out.get("resolved") or {}, a_out.get("resolved") or {}
    w_at = _point(w_res, "relay_latitude", "relay_longitude")
    a_at = _point(a_res, "latitude", "longitude")
    w_ts, a_ts = _when(w_out.get("reading") or {}), _when(a_out.get("reading") or {})
    location_km = round(_km(w_at, a_at), 1) if w_at and a_at else None
    seconds = int(abs((w_ts - a_ts).total_seconds())) if w_ts and a_ts and w_ts.tzinfo and a_ts.tzinfo else None
    reasons = []
    if location_km is None:
        reasons.append("a reading does not say where it was taken")
    elif location_km > AGREE_MAX_KM:
        reasons.append(f"the readings are {location_km} km apart (more than {AGREE_MAX_KM:.0f})")
    if seconds is None:
        reasons.append("a reading does not say when it was taken")
    elif seconds > AGREE_MAX_SECONDS:
        reasons.append(f"the readings are {seconds} s apart (more than {AGREE_MAX_SECONDS})")
    return {"agree": not reasons, "location_km": location_km, "time_apart_s": seconds,
            "max_km": AGREE_MAX_KM, "max_time_apart_s": AGREE_MAX_SECONDS, "reasons": reasons}


def _reading(capability_id: str, child: dict[str, Any]) -> dict[str, Any]:
    out = _output(child)
    reading = out.get("reading") or {}
    # The device attestation travels verbatim: it is GAIA's signature over the raw reading,
    # checkable by the buyer without trusting either this provider or the hub.
    return {"capability_id": capability_id, "reading": reading,
            "attestation": out.get("attestation"), "resolved": out.get("resolved")}


def _child_ref(capability_id: str, child: dict[str, Any]) -> dict[str, Any]:
    job = child.get("job") if isinstance(child.get("job"), dict) else {}
    receipt = child.get("provenance_receipt") if isinstance(child.get("provenance_receipt"), dict) else {}
    return {"capability_id": capability_id, "node": job.get("node"), "funded_by": job.get("funded_by"),
            "price_usd": child.get("price_usd"), "receipt_digest": receipt.get("digest_sri")}


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


class Witness:
    """The whole provider: verify, buy both readings, compare, sign."""

    def __init__(self, cfg: Config, *, transport: Transport = urllib_transport,
                 key: Ed25519PrivateKey | None = None, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.transport = transport
        self.key = key or load_or_create_key(cfg.key_path)
        self.pubkey_b64 = public_key_b64(self.key)
        self.hub_key = HubKey(cfg, transport, now)
        self.tokens = TokenVerifier(cfg, self.hub_key, now)
        self.cap = DailyCap(cfg.state_path, cfg.daily_cap_usd, now)
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="witness-child")

    # -- one purchase ---------------------------------------------------------

    def _buy(self, capability_id: str, place: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        body = {
            "product_id": self.cfg.child_product_id,
            "capability_id": capability_id,
            # Required for a federated child: without it the hub looks for a LOCAL capability,
            # finds only the peer's listing and refuses with 400.
            "source_hub": self.cfg.child_source_hub,
            "input": place,
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

    # -- one call -------------------------------------------------------------

    def handle(self, headers: Mapping[str, str], raw_body: bytes) -> tuple[int, dict[str, str], bytes]:
        try:
            status, extra_headers, body = self._handle(headers, raw_body)
        except Refusal as refusal:
            status, extra_headers, body = refusal.status, {}, refusal.body()
        except Exception:  # noqa: BLE001 - a bug here is this provider's fault, and says so
            import traceback

            traceback.print_exc(file=sys.stderr)
            status, extra_headers, body = 500, {}, {"success": False, "error": "internal_error",
                                                    "detail": "the witness failed; see the provider's log"}
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
        if envelope.get("capability_id") not in (None, CAPABILITY_ID):
            raise Refusal(404, "unknown_capability", f"this provider serves {CAPABILITY_ID} only")

        # Nothing is bought, and no input is even looked at, before the token checks out.
        token = (lower.get(JOB_HEADER.lower()) or "").strip()
        if not token:
            raise Refusal(403, "job_required",
                          f"{CAPABILITY_ID} is sold through {self.cfg.hub_url}: buy it there, not here")
        claims = self.tokens.verify(token)
        place = parse_place(envelope.get("input"))

        grant = (lower.get(GRANT_HEADER.lower()) or "").strip()
        ceiling = int(round(self.cfg.child_max_price_usd * MICRO_PER_USD))
        reserved = 0
        reservation = None
        if grant:
            mode = "cost-plus"
            # The token and the grant, exactly as received, and NOTHING else: the allowance
            # pays, and the hub refuses a grant that arrives with any other payment.
            child_headers = {JOB_HEADER: token, GRANT_HEADER: grant}
        else:
            mode = "fixed-price"
            if not self.cfg.api_key:
                raise Refusal(402, "allowance_required",
                              "this provider pays for its readings only from the buyer's allowance here: "
                              'invoke with "subcontract": {"allowance_usd": 0.01}')
            reserved = 2 * ceiling
            reservation = self.cap.reserve(reserved)
            if reservation is None:
                raise Refusal(429, "daily_cap_reached",
                              "this provider's own daily budget for readings is spent: invoke with "
                              '"subcontract": {"allowance_usd": 0.01} to pay them from your allowance')
            child_headers = {JOB_HEADER: token, "X-API-Key": self.cfg.api_key}

        # Until every outcome is known, the whole reservation counts as spent: an exception
        # after one child delivered must not hand its cost back to the cap.
        spent = reserved
        try:
            futures = {cap: self._pool.submit(self._buy, cap, place, child_headers) for cap in (WEATHER_CAP, AIR_CAP)}
            outcomes = {cap: f.result() for cap, f in futures.items()}
            delivered = {cap: status == 200 and body.get("success") is True
                         for cap, (status, body) in outcomes.items()}
            if mode == "fixed-price":
                spent = sum(cost_bound(status, body, ceiling) for status, body in outcomes.values())
        finally:
            if reservation is not None:
                self.cap.settle(reservation, spent)

        if not all(delivered.values()):
            # Not a witness with one reading. A child that delivered is still paid (§6.3:
            # materials consumed are paid for) and shows in the buyer's bill as captured.
            raise Refusal(502, "child_failed", "a reading could not be bought", children=[
                {"capability_id": cap, "status": status, "error": body.get("error"),
                 "detail": str(body.get("detail") or body.get("refuse_reason") or "")[:200]}
                for cap, (status, body) in outcomes.items() if not delivered[cap]
            ])

        weather, air = outcomes[WEATHER_CAP][1], outcomes[AIR_CAP][1]
        result = {
            "witness": "weather-witness/1",
            "place": place,
            "weather": _reading(WEATHER_CAP, weather),
            "air": _reading(AIR_CAP, air),
            "agreement": agreement(weather, air),
            "funding": mode,
            "job": {"job_id": claims["job"], "node": claims["node"], "depth": claims["depth"]},
            "children": [_child_ref(WEATHER_CAP, weather), _child_ref(AIR_CAP, air)],
        }
        signature = base64.b64encode(self.key.sign(bound_canonical(
            str(envelope.get("capability_id") or ""), str(envelope.get("product_id") or ""),
            envelope.get("input"), result,
        ).encode("utf-8"))).decode()
        return 200, {"X-Provider-Signature": signature}, {"success": True, "result": result}

    def health(self) -> dict[str, Any]:
        return {"ok": True, "capability_id": CAPABILITY_ID, "product_id": PRODUCT_ID,
                "hub": self.cfg.hub_url, "provider_pubkey": self.pubkey_b64,
                "fixed_price": bool(self.cfg.api_key)}


# ── HTTP ─────────────────────────────────────────────────────────────────────


def make_handler(witness: Witness) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "weather-witness/1"

        def _send(self, status: int, headers: Mapping[str, str], body: bytes) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") in ("/healthz", "/health"):
                body = json.dumps(witness.health()).encode()
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
            status, headers, body = witness.handle(dict(self.headers.items()), self.rfile.read(length))
            self._send(status, headers, body)

        def log_message(self, fmt: str, *args: Any) -> None:
            # The request line only: headers carry the job token and the grant, which are
            # bearer secrets for the length of one call and have no business in a log.
            sys.stderr.write(f"[weather-witness] {fmt % args}\n")

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
    bind = os.environ.get("WITNESS_BIND", "127.0.0.1")
    port = int(os.environ.get("WITNESS_PORT", "9475"))
    witness = Witness(cfg)
    print(f"weather-witness serving {CAPABILITY_ID} on http://{bind}:{port}/invoke for {cfg.hub_url}", flush=True)
    print(f"provider_pubkey: {witness.pubkey_b64}", flush=True)
    print(f"fixed-price mode: {'on, cap $%.4f/day' % cfg.daily_cap_usd if cfg.api_key else 'off (allowance only)'}",
          flush=True)
    ThreadingHTTPServer((bind, port), make_handler(witness)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
