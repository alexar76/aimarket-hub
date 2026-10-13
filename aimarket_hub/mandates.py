"""Agent mandates — an owner's signed spending limits, enforced by the hub.

Normative text: ``aimarket-protocol/mandates.md``. This module is the reference
implementation of its sections 3–5.

Why a hub needs this. Before mandates the only answer to "who authorised this spend?" was
"whoever holds the API key". An agent that should spend a few cents a day on weather reads
held the same key as its owner, so a bug, a prompt injection or a leaked log line could
spend the whole balance on anything in the catalogue. A mandate splits the two: the owner
keeps the key, the agent holds its own Ed25519 key and a credential that says how much it
may spend, on what, where, until when. The hub verifies the credential and enforces the
limits itself — the agent cannot opt out of them, because without the mandate it has no way
to pay at all.

The document is an AWR/2-style W3C Verifiable Credential (``eddsa-jcs-2022`` over RFC 8785),
so this module adds no cryptography of its own: canonicalization, the proof and ``did:key``
all come from the ``awr`` reference package that the provenance plugin already depends on.

Three rules carry the design:

* **Every limit is a counter moved by one conditional statement** (``spent + amount <=
  limit``). A read-then-write check would let N concurrent calls all see room for one more.
  This is the same invariant the credits ledger holds for balances.
* **The reservation follows the money.** The mandate is reserved when the credit hold is
  taken and settled when the hold is captured or released, through :class:`MandatedCredits`,
  a per-request proxy over the credits ledger. The invoke path therefore needs no edits at
  its several hold/capture/release sites: whichever branch runs (local, federated, routing
  fee) goes through the proxy.
* **Spend is recorded against every mandate in the chain**, so a re-delegation can never
  let children spend more, together, than their parent allows.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from aimarket_hub.db_backend import returning, returning_one

logger = logging.getLogger(__name__)

MANDATE_HEADER = "X-AIMarket-Mandate"
PROOF_HEADER = "X-AIMarket-Mandate-Proof"
AGENT_HEADER = "X-AIMarket-Agent"
PRINCIPAL_HEADER = "X-AIMarket-Principal"

MANDATE_TYPE = "AIMarketMandate"
VC_CONTEXT = "https://www.w3.org/ns/credentials/v2"
MAX_CHAIN = 4
MAX_SAFE_INTEGER = 2**53 - 1
# The keys an eddsa-jcs-2022 proof carries. Every one but `@context` and `proofValue` is
# inside the signed proof configuration; `@context` is REPLACED by the document's before
# hashing (awr.proof.proof_config), so whatever a proof carries there is unsigned.
_PROOF_KEYS = frozenset({"@context", "type", "cryptosuite", "created", "verificationMethod",
                         "proofPurpose", "proofValue"})
MAX_SPAN_S = 366 * 86400
MAX_DOC_BYTES = 16 * 1024
MAX_AUDIENCE = 8
MAX_SCOPE = 64
MAX_SUBCONTRACT_DEPTH = 3
PROOF_SKEW_S = 300
# A nonce must be remembered for longer than the window in which its proof is accepted
# (±PROOF_SKEW_S), or a proof could be replayed the moment its nonce is forgotten.
NONCE_TTL_S = 2 * PROOF_SKEW_S + 60
MICRO_PER_USD = 1_000_000

REQUEST_DOMAIN = "aimarket-mandate-request/1"
OWNER_LINK_DOMAIN = "aimarket-owner-link/1"
OWNER_CHANGE_DOMAIN = "aimarket-owner-change/1"
REVOKE_DOMAIN = "aimarket-mandate-revoke/1"

LIMIT_FIELDS = ("perCall", "perDay", "total", "perProductPerDay")
REQUIRED_LIMITS = ("perCall", "perDay")

_RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_URN_UUID = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
# A hub's public base URL: scheme, host, optional port and optional path (a hub mounted under
# a path, like independentai.network/hub, is named by that path), never a trailing slash.
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~-]+)*$")
_PATTERN = re.compile(r"^(\*|[A-Za-z0-9_.@-]+(\.\*)?)$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SRI = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")


class MandateError(Exception):
    """A refusal with the status and machine code mandates.md §7 assigns to it."""

    def __init__(self, status: int, error: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": self.error,
            "detail": self.detail,
            **self.extra,
            "protocol_version": "v2",
        }


# ── the awr dependency ────────────────────────────────────────────────────

def _awr() -> Any:
    """The awr reference package, or a 503 that says why mandates are off.

    Imported lazily so a hub built without it still starts; it simply refuses mandates.
    The hub image installs awr for the provenance plugin (aimarket-hub/Dockerfile).
    """
    try:
        from awr import didkey, digest, jcs, proof  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only on a stripped install
        raise MandateError(
            503, "mandates_unavailable",
            f"this hub cannot verify mandates: the awr package is not installed ({exc})",
        ) from exc
    import awr

    return awr


def available() -> bool:
    try:
        _awr()
    except MandateError:
        return False
    return True


# ── small helpers ─────────────────────────────────────────────────────────

def b64url_decode(text: str) -> bytes:
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def usd_to_micro(usd: float) -> int:
    """USD → µUSD. The hub prices in millicents (10 µUSD), so this is exact for any
    price the credits ledger can hold."""
    return int(round(float(usd) * MICRO_PER_USD))


def micro_to_usd(micro: int) -> float:
    return round(int(micro) / MICRO_PER_USD, 6)


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_Z.match(value):
        raise MandateError(403, "mandate_invalid", f"{name} must be RFC 3339 UTC with whole seconds and Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        # The regex checks the shape only; 2026-02-30 or hour 25 fit it. An unauthenticated
        # POST /mandates must get a refusal, not an uncaught 500.
        raise MandateError(403, "mandate_invalid", f"{name} is not a real date and time: {exc}") from exc


def utc_day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def normalize_origin(url: str) -> str:
    return (url or "").strip().rstrip("/")


def pattern_matches(pattern: str, capability_id: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return capability_id.startswith(pattern[:-1])
    return capability_id == pattern


def pattern_covers(parent: str, child: str) -> bool:
    """Does the parent pattern allow everything the child pattern allows? (§3.4 rule 4)"""
    if parent == "*":
        return True
    if child == "*":
        return False
    if parent.endswith(".*"):
        return child.startswith(parent[:-1])
    return child == parent


def scope_allows(scope: Iterable[str], capability_id: str) -> bool:
    return any(pattern_matches(p, capability_id) for p in scope)


# ── the document ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Mandate:
    digest: str
    issuer: str
    subject: str
    valid_from: datetime
    valid_until: datetime
    audience: tuple[str, ...]
    scope: tuple[str, ...]
    limits: dict[str, int]
    subcontract: dict[str, int] | None
    parent: str
    document: dict[str, Any] = field(repr=False)

    def live_at(self, now: float) -> bool:
        return self.valid_from.timestamp() <= now < self.valid_until.timestamp()


def _int_field(value: Any, name: str, *, low: int = 1, high: int | None = None) -> int:
    # bool is an int subclass in Python; `true` is not a limit.
    if not isinstance(value, int) or isinstance(value, bool) or value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f">= {low}"
        raise MandateError(403, "mandate_invalid", f"{name} must be an integer {bound}")
    return value


def _reject_non_integers(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise MandateError(403, "mandate_invalid", f"{path}: numbers in a mandate must be integers")
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_SAFE_INTEGER:
        # I-JSON (RFC 7493): past ±(2^53 - 1) a double-based parser reads a different
        # number, so the limit the owner signed and the one a verifier enforces could differ.
        raise MandateError(403, "mandate_invalid", f"{path}: integers in a mandate must be within ±(2^53 - 1)")
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_non_integers(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_non_integers(v, f"{path}[{i}]")


def parse_mandate(document: Any) -> Mandate:
    """Validate a secured mandate against §3.2 and verify its proof. Raises MandateError.

    Structural rules first, then the signature: a document that is not a mandate at all is
    refused with a message about its shape, not a cryptographic failure.
    """
    awr = _awr()
    if not isinstance(document, dict):
        raise MandateError(400, "mandate_malformed", "a mandate is a JSON object")
    _reject_non_integers(document)
    try:
        canonical = awr.jcs.canonicalize(document)
    except Exception as exc:
        raise MandateError(400, "mandate_malformed", f"mandate is not canonicalizable: {exc}") from exc
    if len(canonical) > MAX_DOC_BYTES:
        raise MandateError(400, "mandate_malformed", f"mandate exceeds {MAX_DOC_BYTES} bytes")

    if document.get("@context") != [VC_CONTEXT]:
        raise MandateError(403, "mandate_invalid", f"@context must be exactly [{VC_CONTEXT!r}]")
    if document.get("type") != ["VerifiableCredential", MANDATE_TYPE]:
        raise MandateError(403, "mandate_invalid", f"type must be ['VerifiableCredential', {MANDATE_TYPE!r}]")
    if not isinstance(document.get("id"), str) or not _URN_UUID.match(document["id"]):
        raise MandateError(403, "mandate_invalid", "id must be a urn:uuid: URI")

    issuer = document.get("issuer")
    subject_block = document.get("credentialSubject")
    if not isinstance(subject_block, dict):
        raise MandateError(403, "mandate_invalid", "credentialSubject must be an object")
    subject = subject_block.get("id")
    for name, did in (("issuer", issuer), ("credentialSubject.id", subject)):
        try:
            awr.didkey.parse_did_key(did)
        except Exception as exc:
            raise MandateError(403, "mandate_invalid", f"{name} must be an Ed25519 did:key ({exc})") from exc
    if issuer == subject:
        raise MandateError(403, "mandate_invalid", "a mandate cannot be issued to its own issuer")

    valid_from = _parse_time(document.get("validFrom"), "validFrom")
    valid_until = _parse_time(document.get("validUntil"), "validUntil")
    if valid_until <= valid_from:
        raise MandateError(403, "mandate_invalid", "validUntil must be after validFrom")
    if (valid_until - valid_from).total_seconds() > MAX_SPAN_S:
        raise MandateError(403, "mandate_invalid", "a mandate may span at most 366 days")

    body = subject_block.get("aimarketMandate")
    if not isinstance(body, dict):
        raise MandateError(403, "mandate_invalid", "credentialSubject.aimarketMandate is required")
    if body.get("version") != 1:
        raise MandateError(403, "mandate_invalid", "aimarketMandate.version must be 1")

    audience = body.get("audience")
    if (not isinstance(audience, list) or not 1 <= len(audience) <= MAX_AUDIENCE
            or not all(isinstance(a, str) and _ORIGIN.match(a) for a in audience)):
        raise MandateError(403, "mandate_invalid", "audience must be 1-8 hub base URLs (scheme://host[:port][/path])")
    scope = body.get("scope")
    if (not isinstance(scope, list) or not 1 <= len(scope) <= MAX_SCOPE
            or not all(isinstance(p, str) and _PATTERN.match(p) for p in scope)):
        raise MandateError(403, "mandate_invalid", "scope must be 1-64 capability ids, 'prefix.*' or '*'")

    raw_limits = body.get("limits")
    if not isinstance(raw_limits, dict):
        raise MandateError(403, "mandate_invalid", "limits is required")
    unknown = set(raw_limits) - set(LIMIT_FIELDS)
    if unknown:
        raise MandateError(403, "mandate_invalid", f"unknown limits: {sorted(unknown)}")
    for required in REQUIRED_LIMITS:
        if required not in raw_limits:
            raise MandateError(403, "mandate_invalid", f"limits.{required} is required")
    limits = {k: _int_field(v, f"limits.{k}") for k, v in raw_limits.items()}

    subcontract = None
    if "subcontract" in body:
        raw = body["subcontract"]
        if not isinstance(raw, dict) or set(raw) != {"perCallAllowance", "maxDepth"}:
            raise MandateError(403, "mandate_invalid", "subcontract must hold exactly perCallAllowance and maxDepth")
        subcontract = {
            "perCallAllowance": _int_field(raw["perCallAllowance"], "subcontract.perCallAllowance"),
            "maxDepth": _int_field(raw["maxDepth"], "subcontract.maxDepth", high=MAX_SUBCONTRACT_DEPTH),
        }

    parent = body.get("parent", "")
    if parent and (not isinstance(parent, str) or not _SRI.match(parent)):
        raise MandateError(403, "mandate_invalid", "parent must be a sha256- SRI digest")

    proof = document.get("proof")
    if not isinstance(proof, dict):
        raise MandateError(403, "mandate_invalid", "the mandate has no proof")
    # The digest names a mandate and is hashed over the proof too, so anything in the proof
    # the signature does not cover is a free parameter of the name. `proof["@context"]` is
    # one: left free, the agent a mandate constrains could register copy after copy of it —
    # each a new digest with new counters, none touched by revoking the original.
    if set(proof) != _PROOF_KEYS:
        raise MandateError(403, "mandate_invalid",
                           f"proof must carry exactly {sorted(_PROOF_KEYS)}")
    if proof["@context"] != document["@context"]:
        raise MandateError(403, "mandate_invalid", "proof.@context must equal the document's @context")
    if proof.get("type") != awr.proof.PROOF_TYPE or proof.get("cryptosuite") != awr.proof.CRYPTOSUITE:
        raise MandateError(403, "mandate_invalid", "proof must be a DataIntegrityProof using eddsa-jcs-2022")
    if proof.get("proofPurpose") != awr.proof.PROOF_PURPOSE:
        raise MandateError(403, "mandate_invalid", "proofPurpose must be assertionMethod")
    if proof.get("verificationMethod") != awr.didkey.verification_method_for(issuer):
        raise MandateError(403, "mandate_invalid", "proof.verificationMethod must be the issuer's did:key method")
    try:
        ok = awr.proof.verify_document_signature(document, proof, awr.didkey.parse_did_key(issuer))
    except Exception as exc:
        raise MandateError(403, "mandate_invalid", f"the proof does not verify: {exc}") from exc
    if not ok:
        raise MandateError(403, "mandate_invalid", "the proof does not verify against the issuer key")

    return Mandate(
        digest=awr.digest.canonical_sri(document),
        issuer=issuer,
        subject=subject,
        valid_from=valid_from,
        valid_until=valid_until,
        audience=tuple(normalize_origin(a) for a in audience),
        scope=tuple(scope),
        limits=limits,
        subcontract=subcontract,
        parent=parent or "",
        document=document,
    )


def check_link(child: Mandate, parent: Mandate) -> None:
    """§3.4 rules 1-6. Raises MandateError naming the first rule the child breaks."""
    def bad(why: str) -> MandateError:
        return MandateError(403, "mandate_invalid", f"re-delegation {child.digest} {why}")

    if child.parent != parent.digest:
        raise bad("does not name this parent")
    if child.issuer != parent.subject:
        raise bad("is not issued by the parent's subject")
    if child.valid_from < parent.valid_from or child.valid_until > parent.valid_until:
        raise bad("outlives its parent")
    if not set(child.audience) <= set(parent.audience):
        raise bad("names a hub its parent does not")
    for pattern in child.scope:
        if not any(pattern_covers(p, pattern) for p in parent.scope):
            raise bad(f"widens scope with {pattern!r}")
    for name, value in parent.limits.items():
        if name not in child.limits:
            raise bad(f"drops the parent's limit {name}")
        if child.limits[name] > value:
            raise bad(f"raises limit {name}")
    if child.subcontract is not None:
        if parent.subcontract is None:
            raise bad("allows subcontracting its parent does not")
        for name in ("perCallAllowance", "maxDepth"):
            if child.subcontract[name] > parent.subcontract[name]:
                raise bad(f"raises subcontract.{name}")


# ── proofs over requests, links and revocations ──────────────────────────

def request_message(*, hub_origin: str, method: str, path: str, digest: str,
                    t: int, nonce: str, body: bytes) -> bytes:
    return "\n".join([
        REQUEST_DOMAIN, normalize_origin(hub_origin), f"{method.upper()} {path}",
        digest, str(int(t)), nonce, hashlib.sha256(body or b"").hexdigest(),
    ]).encode("utf-8")


def owner_link_message(*, hub_origin: str, account_id: str, did: str, t: int, nonce: str) -> bytes:
    return "\n".join([OWNER_LINK_DOMAIN, normalize_origin(hub_origin), account_id, did,
                      str(int(t)), nonce]).encode("utf-8")


def owner_change_message(*, hub_origin: str, account_id: str, action: str, did: str,
                         require_mandate: bool, t: int, nonce: str) -> bytes:
    """What an existing owner signs to authorize a change of owners (§4): link, unlink, policy."""
    return "\n".join([OWNER_CHANGE_DOMAIN, normalize_origin(hub_origin), account_id, action, did,
                      "1" if require_mandate else "0", str(int(t)), nonce]).encode("utf-8")


def revoke_message(*, hub_origin: str, digest: str, t: int, nonce: str) -> bytes:
    return "\n".join([REVOKE_DOMAIN, normalize_origin(hub_origin), digest,
                      str(int(t)), nonce]).encode("utf-8")


def signed_path(request: Any) -> str:
    """The request path a proof signs (§5.1): relative to the hub's base URL — a mount prefix
    the ASGI server reports as root_path is excluded — percent-decoded, no query string."""
    path = request.url.path
    root = str(request.scope.get("root_path") or "")
    if root and path.startswith(root):
        path = path[len(root):] or "/"
    return path


def parse_proof_header(value: str) -> tuple[int, str, bytes]:
    """``t=<unix>;n=<nonce>;s=<b64url>`` → (t, nonce, signature)."""
    parts: dict[str, str] = {}
    for item in (value or "").split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip()
    try:
        t = int(parts["t"])
        nonce = parts["n"]
        sig = b64url_decode(parts["s"])
    except (KeyError, ValueError, TypeError) as exc:
        raise MandateError(400, "mandate_malformed", f"{PROOF_HEADER} must be t=<unix>;n=<nonce>;s=<b64url>") from exc
    return t, nonce, sig


def verify_did_signature(did: str, message: bytes, signature: bytes) -> bool:
    awr = _awr()
    try:
        return bool(awr.didkey.verify_signature(awr.didkey.parse_did_key(did), signature, message))
    except Exception:
        return False


# ── storage ───────────────────────────────────────────────────────────────

class MandateStore:
    """Mandates, owner links, the replay guard and the spend counters.

    Everything lives in the hub database next to the credits ledger, so a mandate hold and
    the credit hold it shadows are always on the same backend.
    """

    def __init__(self, conn: Any, hub_origin: str):
        self._conn = conn
        self.hub_origin = normalize_origin(hub_origin)
        # digest → parsed Mandate. Safe to keep: the digest IS the content, so a cached
        # entry can never go stale. Revocation and expiry live in the row, which chain()
        # reads every time.
        self._parsed: dict[str, Mandate] = {}

    # registration and lookup ------------------------------------------------

    def get(self, digest: str) -> Mandate | None:
        cached = self._parsed.get(digest)
        if cached is not None:
            return cached
        row = self._conn.execute("SELECT document FROM mandates WHERE digest = ?", (digest,)).fetchone()
        if not row:
            return None
        mandate = parse_mandate(json.loads(row["document"]))
        if mandate.digest != digest:
            raise MandateError(500, "mandate_store_corrupt", f"stored mandate {digest} hashes to {mandate.digest}")
        if len(self._parsed) > 4096:
            self._parsed.clear()
        self._parsed[digest] = mandate
        return mandate

    def row(self, digest: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT digest, issuer, subject, parent_digest, root_digest, root_issuer, depth, "
            "valid_from, valid_until, registered_at, revoked_at, revoked_by "
            "FROM mandates WHERE digest = ?", (digest,),
        ).fetchone()
        return dict(row) if row else None

    def register(self, document: Any, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        mandate = parse_mandate(document)
        if mandate.valid_until.timestamp() <= now:
            raise MandateError(403, "mandate_invalid", "the mandate has already expired")
        if self.hub_origin not in mandate.audience:
            # §3.2: a hub MUST refuse a mandate that does not name it — storing one it will
            # never serve is storage given away.
            raise MandateError(403, "mandate_invalid", f"this mandate is not addressed to {self.hub_origin}")
        existing = self.row(mandate.digest)
        if existing:
            return self._registration_answer(existing)
        if mandate.parent:
            parent_row = self.row(mandate.parent)
            if parent_row is None:
                raise MandateError(403, "mandate_invalid", "register the parent mandate first")
            chain = self.chain(mandate.parent, now=now)
            check_link(mandate, chain[-1])
            depth = int(parent_row["depth"]) + 1
            if depth >= MAX_CHAIN:
                raise MandateError(403, "mandate_invalid", f"a chain holds at most {MAX_CHAIN} mandates")
            root_digest, root_issuer = parent_row["root_digest"], parent_row["root_issuer"]
        else:
            depth, root_digest, root_issuer = 0, mandate.digest, mandate.issuer
        if self.owner_account(root_issuer) is None:
            raise MandateError(
                402, "mandate_unfunded",
                "the root issuer has no funding account on this hub — link it first "
                "(POST /ai-market/v2/mandates/owners)",
            )
        # A credential id names ONE mandate per issuer (unique index, migration 038). The
        # proof rules above already leave the subject no way to re-spell a document; this
        # is the second wall — should any encoding freedom remain, a re-spelled copy still
        # carries the original's id and is refused here, atomically.
        self._conn.execute(
            "INSERT INTO mandates (digest, issuer, subject, parent_digest, root_digest, root_issuer, "
            "depth, valid_from, valid_until, document, credential_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (
                mandate.digest, mandate.issuer, mandate.subject, mandate.parent, root_digest,
                root_issuer, depth, mandate.document["validFrom"], mandate.document["validUntil"],
                json.dumps(mandate.document, separators=(",", ":"), sort_keys=True),
                mandate.document["id"],
            ),
        )
        self._conn.commit()
        row = self.row(mandate.digest)
        if row is None:
            raise MandateError(403, "mandate_invalid",
                               f"{mandate.issuer} already registered a different mandate with id {mandate.document['id']}")
        return self._registration_answer(row)

    @staticmethod
    def _registration_answer(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "digest": row.get("digest"),
            "status": "revoked" if row.get("revoked_at") else "active",
            "root": row.get("root_digest"),
            "root_issuer": row.get("root_issuer"),
            "depth": row.get("depth"),
        }

    def chain(self, digest: str, *, now: float | None = None) -> list[Mandate]:
        """Root → leaf, every link re-checked, none expired or revoked. Raises MandateError."""
        now = time.time() if now is None else now
        rows: list[dict[str, Any]] = []
        cursor = digest
        while cursor:
            row = self.row(cursor)
            if row is None:
                raise MandateError(403, "mandate_invalid", f"unknown mandate {cursor} — register it first")
            rows.append(row)
            if len(rows) > MAX_CHAIN:
                raise MandateError(403, "mandate_invalid", "mandate chain is too long")
            cursor = row["parent_digest"]
        rows.reverse()
        chain = [self.get(r["digest"]) for r in rows]
        mandates: list[Mandate] = [m for m in chain if m is not None]
        for row, mandate in zip(rows, mandates):
            if row["revoked_at"]:
                raise MandateError(403, "mandate_invalid", f"mandate {row['digest']} was revoked")
            if not mandate.live_at(now):
                raise MandateError(403, "mandate_invalid", f"mandate {row['digest']} is not valid now")
        for parent, child in zip(mandates, mandates[1:]):
            check_link(child, parent)
        return mandates

    # owners -----------------------------------------------------------------

    def owner_account(self, did: str) -> tuple[str, bool] | None:
        row = self._conn.execute(
            "SELECT account_id, require_mandate FROM mandate_owners WHERE did = ?", (did,),
        ).fetchone()
        if not row:
            return None
        return str(row["account_id"]), bool(row["require_mandate"])

    def account_requires_mandate(self, account_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM mandate_owners WHERE account_id = ? AND require_mandate = 1 LIMIT 1",
            (account_id,),
        ).fetchone()
        return row is not None

    def owners_of(self, account_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT did, require_mandate, linked_at FROM mandate_owners WHERE account_id = ? ORDER BY linked_at",
            (account_id,),
        ).fetchall()
        return [{"did": r["did"], "require_mandate": bool(r["require_mandate"]), "linked_at": r["linked_at"]} for r in rows]

    def _authorize_owner_change(self, *, account_id: str, action: str, did: str, require_mandate: bool,
                                authorization: Any, now: float, admin: bool) -> None:
        """An account that already has an owner changes only with an owner's signature (§4).

        The API key proves the ACCOUNT, and that is exactly what a leaked key gives an attacker.
        If the key alone could link a second DID, the attacker would link their own and issue
        themselves an unlimited mandate; if it alone could unlink, it would switch
        `require_mandate` off by deleting the row that carries it. So once one owner exists,
        every later change — another owner, an unlink, a policy change — must also be signed
        by a key that is already an owner. The operator's admin token is the recovery path
        for an owner who has lost every key.
        """
        owners = {o["did"] for o in self.owners_of(account_id)}
        if admin or not owners:
            return
        if not isinstance(authorization, dict):
            raise MandateError(
                403, "owner_authorization_required",
                "this account already has an owner: the change must also be signed by one "
                "(authorization: {by, t, n, s})",
            )
        by = str(authorization.get("by") or "")
        if by not in owners:
            raise MandateError(403, "owner_authorization_required", "authorization.by is not an owner of this account")
        t, nonce, sig = self._signed_fields(authorization)
        self._check_freshness(t, now)
        message = owner_change_message(hub_origin=self.hub_origin, account_id=account_id, action=action,
                                       did=did, require_mandate=require_mandate, t=t, nonce=nonce)
        if not verify_did_signature(by, message, sig):
            raise MandateError(401, "mandate_proof_invalid", "the owner authorization does not verify")
        self._consume_nonce(by, nonce, now)

    def link_owner(self, *, account_id: str, did: str, proof: Any, require_mandate: bool,
                   authorization: Any = None, admin: bool = False,
                   now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        _awr()
        if not isinstance(did, str) or not did.startswith("did:key:"):
            raise MandateError(400, "mandate_malformed", "did must be an Ed25519 did:key")
        t, nonce, sig = self._signed_fields(proof)
        self._check_freshness(t, now)
        message = owner_link_message(hub_origin=self.hub_origin, account_id=account_id, did=did, t=t, nonce=nonce)
        if not verify_did_signature(did, message, sig):
            raise MandateError(401, "mandate_proof_invalid", "the owner signature does not verify for this account and hub")
        current = self.owner_account(did)
        if current is not None and current[0] != account_id:
            raise MandateError(409, "owner_linked_elsewhere", "this did is already linked to another account")
        if current is not None and current[1] == bool(require_mandate):
            self._consume_nonce(did, nonce, now)
            return {"did": did, "account_id": account_id, "require_mandate": current[1], "unchanged": True}
        self._authorize_owner_change(
            account_id=account_id, action="link" if current is None else "policy", did=did,
            require_mandate=bool(require_mandate), authorization=authorization, now=now, admin=admin,
        )
        self._consume_nonce(did, nonce, now)
        if current is None:
            self._conn.execute(
                "INSERT INTO mandate_owners (did, account_id, require_mandate) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (did, account_id, 1 if require_mandate else 0),
            )
        else:
            self._conn.execute(
                "UPDATE mandate_owners SET require_mandate = ? WHERE did = ? AND account_id = ?",
                (1 if require_mandate else 0, did, account_id),
            )
        self._conn.commit()
        linked = self.owner_account(did)
        if linked is None or linked[0] != account_id:
            raise MandateError(409, "owner_linked_elsewhere", "this did is already linked to another account")
        return {"did": did, "account_id": account_id, "require_mandate": linked[1]}

    def unlink_owner(self, *, account_id: str, did: str, authorization: Any = None, admin: bool = False,
                     now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        current = self.owner_account(did)
        if current is None or current[0] != account_id:
            raise MandateError(404, "owner_not_linked", "this did is not linked to this account")
        self._authorize_owner_change(account_id=account_id, action="unlink", did=did, require_mandate=False,
                                     authorization=authorization, now=now, admin=admin)
        gone = returning_one(
            self._conn,
            "DELETE FROM mandate_owners WHERE did = ? AND account_id = ? RETURNING did", (did, account_id),
            commit=True,
        )
        if gone is None:
            raise MandateError(404, "owner_not_linked", "this did is not linked to this account")
        return {"did": did, "unlinked": True}

    # revocation -------------------------------------------------------------

    def revoke(self, *, digest: str, by: str = "", proof: Any = None, account_id: str = "",
               now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        row = self.row(digest)
        if row is None:
            raise MandateError(404, "mandate_unknown", "no such mandate")
        allowed = False
        if account_id:
            owner = self.owner_account(row["root_issuer"])
            allowed = owner is not None and owner[0] == account_id
            by = by or f"account:{account_id}"
        else:
            t, nonce, sig = self._signed_fields(proof)
            self._check_freshness(t, now)
            issuers = self._issuers_up_from(digest)
            if by not in issuers:
                raise MandateError(403, "mandate_invalid", "only an issuer in this mandate's chain may revoke it")
            message = revoke_message(hub_origin=self.hub_origin, digest=digest, t=t, nonce=nonce)
            if not verify_did_signature(by, message, sig):
                raise MandateError(401, "mandate_proof_invalid", "the revocation signature does not verify")
            self._consume_nonce(by, nonce, now)
            allowed = True
        if not allowed:
            raise MandateError(403, "mandate_invalid", "this account does not fund the mandate's chain")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        self._conn.execute(
            "UPDATE mandates SET revoked_at = ?, revoked_by = ? WHERE digest = ? AND revoked_at = ''",
            (stamp, by[:200], digest),
        )
        self._conn.commit()
        return {"digest": digest, "status": "revoked", "revoked_at": (self.row(digest) or {}).get("revoked_at")}

    def _issuers_up_from(self, digest: str) -> set[str]:
        issuers: set[str] = set()
        cursor = digest
        for _ in range(MAX_CHAIN + 1):
            row = self.row(cursor)
            if row is None:
                break
            issuers.add(row["issuer"])
            cursor = row["parent_digest"]
            if not cursor:
                break
        return issuers

    # request proofs ---------------------------------------------------------

    @staticmethod
    def _signed_fields(proof: Any) -> tuple[int, str, bytes]:
        if isinstance(proof, str):
            return parse_proof_header(proof)
        if not isinstance(proof, dict):
            raise MandateError(400, "mandate_malformed", "proof must be {t, n, s}")
        try:
            return int(proof["t"]), str(proof["n"]), b64url_decode(str(proof["s"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise MandateError(400, "mandate_malformed", "proof must be {t, n, s}") from exc

    @staticmethod
    def _check_freshness(t: int, now: float) -> None:
        if abs(now - t) > PROOF_SKEW_S:
            raise MandateError(401, "mandate_proof_invalid", f"proof timestamp is more than {PROOF_SKEW_S}s from the hub clock")

    def _consume_nonce(self, did: str, nonce: str, now: float) -> None:
        if not _NONCE.match(nonce or ""):
            raise MandateError(400, "mandate_malformed", "nonce must be 16-64 base64url characters")
        with contextlib.suppress(Exception):
            self._conn.execute("DELETE FROM mandate_nonces WHERE seen_at < ?", (now - NONCE_TTL_S,))
        won = returning_one(
            self._conn,
            "INSERT INTO mandate_nonces (agent, nonce, seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING nonce",
            (did, nonce, now),
            commit=True,
        )
        if won is None:
            raise MandateError(401, "mandate_proof_invalid", "this proof nonce was already used")

    def verify_request(self, *, leaf: Mandate, proof_header: str, method: str, path: str,
                       body: bytes, now: float | None = None, signers: Iterable[str] | None = None) -> str:
        """Verify a request proof (§5.1) and return the DID that signed it.

        ``signers`` defaults to the leaf's subject — the only key that may SPEND with the
        mandate. Reading its usage (§3.5) passes every key in the chain instead.
        """
        now = time.time() if now is None else now
        t, nonce, sig = parse_proof_header(proof_header)
        self._check_freshness(t, now)
        message = request_message(hub_origin=self.hub_origin, method=method, path=path,
                                  digest=leaf.digest, t=t, nonce=nonce, body=body)
        for did in (list(signers) if signers is not None else [leaf.subject]):
            if verify_did_signature(did, message, sig):
                self._consume_nonce(did, nonce, now)
                return did
        raise MandateError(401, "mandate_proof_invalid", "the request proof does not verify for an allowed key")

    def chain_keys(self, digest: str) -> list[str]:
        """Every issuer and subject in a mandate's chain (§3.5 usage readers)."""
        keys: list[str] = []
        cursor = digest
        for _ in range(MAX_CHAIN + 1):
            row = self.row(cursor)
            if row is None:
                break
            for did in (row["subject"], row["issuer"]):
                if did not in keys:
                    keys.append(did)
            cursor = row["parent_digest"]
            if not cursor:
                break
        return keys

    def status_of(self, digest: str, *, now: float | None = None) -> tuple[str, str]:
        """(status, reason) of a mandate AS A CHAIN: a mandate whose ancestor is revoked or
        expired cannot be used, so reporting its own row as "active" would mislead."""
        now = time.time() if now is None else now
        try:
            chain = self.chain(digest, now=now)
        except MandateError as exc:
            reason = exc.detail
            if "revoked" in reason:
                return "revoked", reason
            if "not valid now" in reason:
                cursor, pending = digest, False
                while cursor:
                    m = self.get(cursor)
                    if m is None:
                        break
                    pending = pending or m.valid_from.timestamp() > now
                    cursor = m.parent or ""
                return ("pending" if pending else "expired"), reason
            return "invalid", reason
        return ("active" if chain else "invalid"), ""

    # usage ------------------------------------------------------------------

    def usage(self, digest: str, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        mandate = self.get(digest)
        if mandate is None:
            raise MandateError(404, "mandate_unknown", "no such mandate")
        day = utc_day(now)
        spent_today = self._counter(digest, f"d:{day}")
        spent_total = self._counter(digest, "total") if "total" in mandate.limits else self._sum_all(digest)
        out: dict[str, Any] = {
            "day": day,
            "spent_today_usd": micro_to_usd(spent_today),
            "per_day_usd": micro_to_usd(mandate.limits["perDay"]),
            "remaining_today_usd": micro_to_usd(max(0, mandate.limits["perDay"] - spent_today)),
            "spent_total_usd": micro_to_usd(spent_total),
        }
        if "total" in mandate.limits:
            out["total_usd"] = micro_to_usd(mandate.limits["total"])
            out["remaining_total_usd"] = micro_to_usd(max(0, mandate.limits["total"] - spent_total))
        return out

    def _counter(self, digest: str, period: str) -> int:
        row = self._conn.execute(
            "SELECT spent_micro FROM mandate_usage WHERE digest = ? AND period = ?", (digest, period),
        ).fetchone()
        return int(row["spent_micro"]) if row else 0

    def _sum_all(self, digest: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_micro), 0) AS s FROM mandate_holds WHERE digest = ? AND status != 'released'",
            (digest,),
        ).fetchone()
        return int(row["s"]) if row else 0

    # the spend counters -----------------------------------------------------

    @staticmethod
    def periods_for(mandate: Mandate, product_id: str, day: str) -> list[tuple[str, int, str]]:
        """(counter key, limit, limit name) for every counter this spend moves."""
        periods = [(f"d:{day}", mandate.limits["perDay"], "perDay")]
        if "total" in mandate.limits:
            periods.append(("total", mandate.limits["total"], "total"))
        if "perProductPerDay" in mandate.limits:
            periods.append((f"p:{product_id}:{day}", mandate.limits["perProductPerDay"], "perProductPerDay"))
        return periods

    def reserve(self, chain: list[Mandate], *, receipt_id: str, amount_micro: int,
                product_id: str, now: float | None = None, force: bool = False,
                per_call_before: int = 0, check_per_call: bool = True) -> None:
        """Reserve ``amount_micro`` against every limit of every mandate in ``chain``.

        Each counter moves by one conditional UPDATE, so it can never pass its limit. When
        one refuses, the counters already moved for this reservation are moved back and the
        call is refused naming the limit (§5.3). ``force`` records a spend that has already
        happened (the routing fee debited after a peer answered): it moves the counters
        unconditionally, because refusing to record money that is gone would only make the
        counters lie.

        ``perCall`` is about the whole CALL, which can take more than one hold (a federated
        call reserves its price and then the routing fee): ``per_call_before`` is what this
        call has already reserved. A subcontracting allowance is bounded by its own
        ``subcontract.perCallAllowance`` instead, so it passes ``check_per_call=False``.
        """
        now = time.time() if now is None else now
        if amount_micro <= 0:
            return
        day = utc_day(now)
        if self._conn.execute(
            "SELECT 1 FROM mandate_holds WHERE receipt_id = ? LIMIT 1", (receipt_id,),
        ).fetchone():
            raise MandateError(409, "mandate_hold_exists", f"receipt {receipt_id} already holds a mandate reservation")
        moved: list[tuple[str, str]] = []
        try:
            for mandate in chain:
                if not force and check_per_call and per_call_before + amount_micro > mandate.limits["perCall"]:
                    raise MandateError(
                        402, "mandate_limit",
                        f"this call costs {micro_to_usd(per_call_before + amount_micro)} USD, over the per-call limit",
                        limit="perCall", mandate=mandate.digest,
                    )
                for period, limit, name in self.periods_for(mandate, product_id, day):
                    self._conn.execute(
                        "INSERT INTO mandate_usage (digest, period, spent_micro) VALUES (?, ?, 0) "
                        "ON CONFLICT DO NOTHING",
                        (mandate.digest, period),
                    )
                    if force:
                        hit = returning_one(
                            self._conn,
                            "UPDATE mandate_usage SET spent_micro = spent_micro + ? "
                            "WHERE digest = ? AND period = ? RETURNING spent_micro",
                            (amount_micro, mandate.digest, period),
                            commit=True,
                        )
                    else:
                        hit = returning_one(
                            self._conn,
                            "UPDATE mandate_usage SET spent_micro = spent_micro + ? "
                            "WHERE digest = ? AND period = ? AND spent_micro + ? <= ? RETURNING spent_micro",
                            (amount_micro, mandate.digest, period, amount_micro, limit),
                            commit=True,
                        )
                    # Committed at once, inside the statement's own critical section: on the
                    # shared SQLite connection a rollback issued by another thread's failed
                    # statement would otherwise undo this increment while the hold row below
                    # still got written — a limit that under-counts.
                    if hit is None:
                        raise MandateError(
                            402, "mandate_limit",
                            f"the {name} limit of mandate {mandate.digest} would be exceeded",
                            limit=name, mandate=mandate.digest,
                        )
                    moved.append((mandate.digest, period))
            by_digest: dict[str, list[str]] = {}
            for digest, period in moved:
                by_digest.setdefault(digest, []).append(period)
            for digest, periods in by_digest.items():
                self._conn.execute(
                    "INSERT INTO mandate_holds (receipt_id, digest, amount_micro, periods, status) "
                    "VALUES (?, ?, ?, ?, 'held')",
                    (receipt_id, digest, amount_micro, json.dumps(periods)),
                )
            self._conn.commit()
        except Exception:
            self._unmove(moved, amount_micro)
            raise

    def _unmove(self, moved: list[tuple[str, str]], amount_micro: int) -> None:
        for digest, period in moved:
            with contextlib.suppress(Exception):
                self._conn.execute(
                    "UPDATE mandate_usage SET spent_micro = spent_micro - ? WHERE digest = ? AND period = ?",
                    (amount_micro, digest, period),
                )
        with contextlib.suppress(Exception):
            self._conn.commit()

    def capture(self, receipt_id: str) -> None:
        self._conn.execute(
            "UPDATE mandate_holds SET status = 'captured', resolved_at = datetime('now') "
            "WHERE receipt_id = ? AND status = 'held'",
            (receipt_id,),
        )
        self._conn.commit()

    def release(self, receipt_id: str) -> None:
        self.settle(receipt_id, 0)

    def settle(self, receipt_id: str, keep_micro: int) -> None:
        """Resolve a hold keeping ``keep_micro`` of it as spent and giving the rest back.

        ``keep_micro == 0`` is a release; ``keep_micro`` equal to the held amount is a
        capture. Anything between is how a subcontracting allowance settles: what the
        children actually drew stays spent, the rest returns to every counter it came from.
        The status change is the claim, so a hold settles exactly once.
        """
        rows = returning(
            self._conn,
            "UPDATE mandate_holds SET status = ?, resolved_at = datetime('now') "
            "WHERE receipt_id = ? AND status = 'held' RETURNING digest, amount_micro, periods, pending_back_micro",
            ("captured" if keep_micro > 0 else "released", receipt_id),
            # The claim is committed before the counters move, for the reason reserve() gives:
            # a rollback another thread triggers on the shared connection must not be able to
            # undo the claim while leaving the counter changes that depend on it.
            commit=True,
        )
        for row in rows:
            held = int(row["amount_micro"])
            # What give_back() recorded while the hold was still open: a child refunded to
            # the buyer's balance between the ledger release and this settle, which the
            # caller's keep_micro (computed at the release) still counts as spent.
            keep = max(0, min(int(keep_micro), held) - int(row["pending_back_micro"] or 0))
            give_back = held - keep
            if keep != held:
                self._conn.execute(
                    "UPDATE mandate_holds SET amount_micro = ? WHERE receipt_id = ? AND digest = ?",
                    (keep, receipt_id, row["digest"]),
                )
            if give_back:
                for period in json.loads(row["periods"] or "[]"):
                    self._conn.execute(
                        "UPDATE mandate_usage SET spent_micro = spent_micro - ? WHERE digest = ? AND period = ?",
                        (give_back, row["digest"], period),
                    )
        self._conn.commit()

    def give_back(self, receipt_id: str, amount_micro: int) -> None:
        """Return part of an already SETTLED hold to its counters.

        A subcontractor whose price was carved out of an allowance and who then failed after
        the allowance had closed is refunded to the buyer's balance, not to the allowance —
        so the allowance's settled amount over-states what the job spent by exactly that
        price. This takes it back off every counter the allowance moved.
        """
        if amount_micro <= 0:
            return
        rows: list[Any] = []
        for _ in range(2):
            rows = returning(
                self._conn,
                "UPDATE mandate_holds SET amount_micro = amount_micro - ? "
                "WHERE receipt_id = ? AND status = 'captured' AND amount_micro >= ? RETURNING digest, periods",
                (int(amount_micro), receipt_id, int(amount_micro)),
                commit=True,
            )
            if rows:
                break
            # Not settled yet: the root's ledger release has happened (that is why the child
            # was refunded to the balance) but its settle() has not. Leave the amount on the
            # open hold for settle() to subtract, in the same claim that closes it. If the
            # settle claimed in between, this matches nothing and the loop takes the
            # settled path again.
            pending = returning(
                self._conn,
                "UPDATE mandate_holds SET pending_back_micro = pending_back_micro + ? "
                "WHERE receipt_id = ? AND status = 'held' RETURNING digest",
                (int(amount_micro), receipt_id),
                commit=True,
            )
            if pending:
                return
        for row in rows:
            for period in json.loads(row["periods"] or "[]"):
                self._conn.execute(
                    "UPDATE mandate_usage SET spent_micro = spent_micro - ? WHERE digest = ? AND period = ?",
                    (int(amount_micro), row["digest"], period),
                )
        self._conn.commit()


# ── admission ─────────────────────────────────────────────────────────────

@dataclass
class Admission:
    """A verified mandated call: who pays, who asked, and the chain whose limits apply."""

    chain: list[Mandate]
    account_id: str
    product_id: str
    store: MandateStore
    # The last refusal the ledger proxy produced. The invoke path turns any failed hold into
    # a generic 402; the route puts the precise mandates.md §7 refusal back (see api.py).
    last_refusal: MandateError | None = None

    @property
    def leaf(self) -> Mandate:
        return self.chain[-1]

    @property
    def agent(self) -> str:
        return self.leaf.subject

    @property
    def principal(self) -> str:
        return self.chain[0].issuer

    def provider_headers(self) -> dict[str, str]:
        return {AGENT_HEADER: self.agent, PRINCIPAL_HEADER: self.principal}

    def summary(self) -> dict[str, Any]:
        return {"digest": self.leaf.digest, "agent": self.agent, "principal": self.principal,
                "depth": len(self.chain) - 1}


def admit(store: MandateStore, *, digest: str, proof_header: str, method: str, path: str,
          body: bytes, capability_id: str, product_id: str, api_key_account: str = "",
          other_rail: bool = False, now: float | None = None) -> Admission:
    """§5.2: everything a mandated invoke must pass before any money is reserved."""
    now = time.time() if now is None else now
    digest = (digest or "").strip()
    if not _SRI.match(digest):
        raise MandateError(400, "mandate_malformed", f"{MANDATE_HEADER} must be a sha256- SRI digest")
    if not proof_header:
        raise MandateError(401, "mandate_proof_invalid", f"{PROOF_HEADER} is required with a mandate")
    if other_rail:
        raise MandateError(
            400, "mandate_rail_unsupported",
            "a mandate is enforced on the credits ledger; do not send a payment channel or an "
            "x402 payment with it",
        )
    chain = store.chain(digest, now=now)
    leaf = chain[-1]
    # The proof first (§5.2 order): without it, anyone holding a digest could probe which
    # capabilities are in the mandate's scope from the refusal codes.
    store.verify_request(leaf=leaf, proof_header=proof_header, method=method, path=path, body=body, now=now)
    if store.hub_origin not in leaf.audience:
        raise MandateError(403, "mandate_invalid", f"this mandate is not addressed to {store.hub_origin}")
    if not scope_allows(leaf.scope, capability_id):
        raise MandateError(403, "mandate_scope", f"{capability_id} is outside the mandate's scope")
    owner = store.owner_account(chain[0].issuer)
    if owner is None:
        raise MandateError(402, "mandate_unfunded", "the root issuer has no funding account on this hub")
    account_id = owner[0]
    if api_key_account and api_key_account != account_id:
        raise MandateError(
            403, "mandate_invalid",
            "X-API-Key names a different account from the one this mandate spends from",
        )
    return Admission(chain=chain, account_id=account_id, product_id=product_id, store=store)


# ── the per-request ledger proxy ──────────────────────────────────────────

class MandatedCredits:
    """The credits ledger as one mandated request sees it.

    Every reservation the invoke path takes on the credits rail is first reserved against
    the mandate chain, and settles with it. Anything the invoke path asks that is not about
    moving money (balance, resolve, …) passes straight through.
    """

    def __init__(self, inner: Any, admission: Admission):
        self._inner = inner
        self._admission = admission
        # What this CALL has reserved so far, for perCall (a price hold, then a fee hold).
        self._call_micro = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @staticmethod
    def _micro(amount_usd: float) -> int:
        """µUSD of what the credits ledger will ACTUALLY hold: it counts millicents
        (10 µUSD), so a limit is compared with the money that moves, not a finer figure."""
        from aimarket_hub.credits import usd_to_mc

        return usd_to_mc(amount_usd) * 10

    def _reserved_hold(self, account_id: str, amount_usd: float, receipt_id: str, *,
                       per_call: bool) -> dict[str, Any]:
        micro = self._micro(amount_usd)
        try:
            self._admission.store.reserve(
                self._admission.chain, receipt_id=receipt_id, amount_micro=micro,
                product_id=self._admission.product_id,
                per_call_before=self._call_micro, check_per_call=per_call,
            )
        except MandateError as exc:
            self._admission.last_refusal = exc
            return {"error": exc.detail}
        try:
            result = self._inner.hold(account_id, amount_usd, receipt_id)
        except Exception:
            # The limit must not stay reserved for money that was never held.
            self._admission.store.release(receipt_id)
            raise
        if result.get("error"):
            self._admission.store.release(receipt_id)
        elif per_call:
            self._call_micro += micro
        return result

    def hold(self, account_id: str, amount_usd: float, receipt_id: str) -> dict[str, Any]:
        return self._reserved_hold(account_id, amount_usd, receipt_id, per_call=True)

    def hold_allowance(self, account_id: str, amount_usd: float, receipt_id: str) -> dict[str, Any]:
        """A subcontracting allowance: counted against perDay/total/perProductPerDay, bounded
        by subcontract.perCallAllowance (checked by the caller) rather than perCall."""
        return self._reserved_hold(account_id, amount_usd, receipt_id, per_call=False)

    def capture_hold(self, receipt_id: str) -> dict[str, Any]:
        result = self._inner.capture_hold(receipt_id)
        if not result.get("error"):
            self._admission.store.capture(receipt_id)
        return result

    def release_hold(self, receipt_id: str) -> dict[str, Any]:
        result = self._inner.release_hold(receipt_id)
        if result.get("already") == "captured":
            # The money was spent (the capture committed, then something later failed): the
            # limit keeps it. Releasing the reservation here would hand the agent back a limit
            # for money that is gone.
            self._admission.store.capture(receipt_id)
        else:
            self._admission.store.release(receipt_id)
        return result

    def debit(self, account_id: str, amount_usd: float, receipt_id: str = "", note: str = "",
              **kwargs: Any) -> dict[str, Any]:
        result = self._inner.debit(account_id, amount_usd, receipt_id=receipt_id, note=note, **kwargs)
        if not result.get("error") and receipt_id:
            with contextlib.suppress(MandateError):
                self._admission.store.reserve(
                    self._admission.chain, receipt_id=receipt_id,
                    amount_micro=self._micro(amount_usd), product_id=self._admission.product_id,
                    force=True,
                )
                self._admission.store.capture(receipt_id)
        return result
