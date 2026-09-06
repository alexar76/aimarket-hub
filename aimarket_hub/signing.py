"""Ed25519 signing for hub manifests and receipts.

Fail-fast: refuses to initialize if cryptography is not installed
or if an existing key file is corrupted.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Post-quantum verification is OPTIONAL at install time (`aimarket-hub[pqc]`) and additive at
# runtime. A hub without the library still verifies every classical signature exactly as before;
# it simply fail-closes on a PQ-signed document it cannot evaluate, which is why the extra must
# reach every hub BEFORE any peer starts signing hybrid.
try:
    from dilithium_py.ml_dsa import ML_DSA_65 as _MLDSA

    _PQ_LIB = True
except Exception:  # pragma: no cover - optional extra
    _MLDSA = None
    _PQ_LIB = False


def pqc_available() -> bool:
    """Whether this hub can evaluate an ML-DSA-65 signature."""
    return _PQ_LIB


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _pqc_required() -> bool:
    return _truthy(os.environ.get("AIMARKET_PQC_REQUIRE"))


def pqc_required() -> bool:
    """Whether this hub refuses a document carrying no PQ signature. Off by default."""
    return _pqc_required()


class PQCMisconfigured(RuntimeError):
    """PQ signatures are required but this hub cannot check one. Loud, not a silent reject."""

logger = logging.getLogger(__name__)

# ── Canonical-format versions ────────────────────────────────────────────────
#
# v1 is the original field set. v2 additionally binds the fields a Pay-on-Verified
# money outcome is *justified* by — the delivery verdict, the audit score, the bar
# applied, and the settlement's own resolution — which v1 left outside the
# signature: a stored or forwarded envelope's stated REASONS were unauthenticated
# while its verdict was, so a relay could rewrite "why" without breaking the
# signature and a dispute would be argued from tampered evidence.
#
# The version travels in the signature block AND inside the v2 canonical itself
# (`|v:2|`), so it is bound by the signature rather than merely asserted next to it, and
# it is ABSENT for v1 — every byte a previously-signed receipt/envelope carries is
# unchanged and still verifies.
#
# Receipts cannot simply be flipped to v2 everywhere: `receipt_canonical` v1 is a
# cross-package interop shape (oracle_core, platon and the protocol test vectors all
# mirror the 7-field v1 string), so a plain invoke receipt MUST stay v1 or external
# verifiers reject it. But leaving the choice to each call site is what left the hub
# split-brained: `verified_settlement` signed its rejection receipt at v2 while
# `safety_gate.build_rejection_receipt` and the api's plugin-block receipt signed
# byte-identical evidence at v1, where every v1 field on a rejection is a constant and
# the signature therefore authenticated nothing about WHY the money came back.
#
# The version is therefore no longer a per-call-site default: `sign_receipt` DERIVES it
# from the receipt's own content (`resolve_receipt_version`). A receipt that carries any
# field only v2 binds is signed at v2; an interop invoke receipt, which carries none of
# them, keeps the byte-stable v1 canonical. A call site can still pin a version
# explicitly, but it can no longer under-sign its evidence by omission.
VERIFICATION_SIG_VERSION = 2
RECEIPT_SIG_VERSION = 2
# The version an unversioned signature block means. Absent == 1 for back-compat.
LEGACY_SIG_VERSION = 1

# Fields each v2 canonical binds through a digest, ON TOP of its v1 string. A digest
# rather than more `|`-joined text: several of these are verifier- or peer-authored
# strings, and a raw join would let a `|` inside one forge the boundary of the next.
_VERIFICATION_V2_FIELDS = (
    "status", "performed", "verified", "settled", "audit_score", "threshold",
    "delivery_fulfils", "delivery_reasons", "reason", "verifier", "mode",
)
_RECEIPT_V2_FIELDS = (
    "type", "channel_id", "category", "plugin", "reason", "verify_score",
    "delivery_reasons", "trace_id", "refunded",
)


def _fields_digest(obj: dict[str, Any], names: tuple[str, ...]) -> str:
    """Deterministic digest over a named subset of `obj`.

    Missing keys are bound as `null` on purpose — dropping a field must change the
    digest, or removing `delivery_reasons` from a stored envelope would go unnoticed.
    """
    import hashlib
    import json

    payload = {name: obj.get(name) for name in names}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), default=str).encode()
    ).hexdigest()


def resolve_receipt_version(receipt: dict[str, Any]) -> int:
    """The canonical version a receipt MUST be signed at, derived from its content.

    v2 iff the receipt carries at least one field that only v2 binds. This is what makes
    the receipt/verification split readable instead of accidental: the interop invoke
    receipt (nonce/product/capability/price/timestamp/success/latency) carries none of
    them and stays on the byte-stable v1 string that oracle_core, platon and the protocol
    vectors mirror, while any receipt that states a REASON, a score, a channel or a refund
    — i.e. every rejection receipt — is signed over that evidence.
    """
    return 2 if any(name in receipt for name in _RECEIPT_V2_FIELDS) else 1


def _signed_version(artefact: dict[str, Any]) -> int:
    """The canonical version an artefact's OWN signature block names.

    Returns 0 when there is no usable signature or the version is unreadable, so callers
    fail closed instead of silently assuming v1 for a malformed block.
    """
    sig = artefact.get("signature")
    if not isinstance(sig, dict) or not sig.get("value"):
        return 0
    raw = sig.get("version", LEGACY_SIG_VERSION)
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return 0
    return version if version >= 1 else 0


def receipt_signature_version(receipt: dict[str, Any]) -> int:
    """Version of the canonical this receipt was signed under; 0 if not determinable.

    Discoverable from the artefact, not inferred by the reader: for v2 the version is
    inside the signed bytes, and for v1 the absence of the field IS the v1 marker.
    """
    return _signed_version(receipt)


def unsigned_receipt_fields(receipt: dict[str, Any]) -> tuple[str, ...]:
    """v2-only fields this receipt carries that its OWN signature does not cover.

    Empty for anything signed at the version its content requires. Non-empty means the
    artefact is legacy (signed before v2 existed, or by a peer still on v1): the values
    are present but unauthenticated, which a dispute has to be able to see rather than
    guess. Also non-empty for an unsigned/unreadable block — everything is uncovered then.
    """
    present = tuple(name for name in _RECEIPT_V2_FIELDS if name in receipt)
    if not present:
        return ()
    return () if _signed_version(receipt) >= 2 else present


def _ensure_keypair(path: Path) -> tuple[bytes, bytes]:
    """Load or create an Ed25519 keypair.

    Raises:
        ImportError: cryptography not installed.
        RuntimeError: existing key file corrupted (fail-fast, no silent regen).
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Verify cryptography is available BEFORE touching any files
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise ImportError(
            "cryptography>=44 is required for Ed25519 signing. "
            "Install it: pip install cryptography>=44"
        ) from exc

    if path.exists():
        raw = path.read_bytes()
        if len(raw) == 64:
            return raw[:32], raw[32:]
        # Corrupted key file — fail fast, don't silently regenerate
        raise RuntimeError(
            f"Ed25519 key file {path} exists but is corrupted "
            f"(size={len(raw)}, expected 64 bytes). "
            "Remove it manually if you want to regenerate, "
            "or restore from backup."
        )

    # No key file — generate a new one
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    seed = priv.private_bytes_raw()
    pub_bytes = pub.public_bytes_raw()
    path.write_bytes(seed + pub_bytes)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)

    is_prod = os.environ.get("AIFACTORY_PROD", "").strip() == "1"
    if is_prod:
        logger.warning(
            "PRODUCTION: New Ed25519 keypair generated at %s. "
            "This key signs all hub manifests and receipts. "
            "Back it up immediately — if lost, federation trust is broken.",
            path,
        )

    return seed, pub_bytes


def _load_or_make_pq(path: Path) -> tuple[bytes, bytes]:
    """Load or create this hub's ML-DSA-65 keypair, beside the classical one.

    The file is `{key_path}_mldsa`, so a hub whose signing key lives on a persistent volume keeps
    its post-quantum identity across restarts — and one whose key path is NOT on a volume gets a
    fresh PQ identity on every recreate, which peers that pinned the old one will see as a key
    change. Check the volume before enabling `AIMARKET_PQC`.
    """
    if path.exists():
        pk_hex, sk_hex = path.read_text().split("\n")[:2]
        return bytes.fromhex(pk_hex), bytes.fromhex(sk_hex)
    pk, sk = _MLDSA.keygen()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pk.hex()}\n{sk.hex()}\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return pk, sk


class Signer:
    """Ed25519 signer for a hub instance."""

    def __init__(self, key_path: str | Path = "data/hub_signing_key",
                 pqc: bool | None = None):
        self.key_path = Path(key_path)
        self._seed, self._pub_bytes = _ensure_keypair(self.key_path)
        self._public_key_b64 = base64.b64encode(self._pub_bytes).decode()

        # Phase 2. The hub could VERIFY a post-quantum signature and not produce one, which meant
        # the busiest authenticated channel in the ecosystem — a hub's signed `.well-known` and
        # manifest — could never carry the PQ layer, and no peer could ever pin a PQ key for it.
        # Off by default: a signer that gets ahead of the verifiers de-federates itself, because
        # verification fails CLOSED on a `pq_value` it cannot evaluate.
        if pqc is None:
            pqc = _truthy(os.environ.get("AIMARKET_PQC"))
        self._pq: tuple[bytes, bytes] | None = None
        if pqc and _PQ_LIB:
            self._pq = _load_or_make_pq(Path(f"{self.key_path}_mldsa"))

    @property
    def pq_public_key_b64(self) -> str:
        """This hub's ML-DSA-65 public key, or "" when PQ signing is off."""
        return base64.b64encode(self._pq[0]).decode() if self._pq else ""

    def _pq_fields(self, canonical: str) -> dict[str, str]:
        """The additive `pq_*` block for a signature object. Empty when PQ signing is off."""
        if self._pq is None:
            return {}
        pk, sk = self._pq
        return {
            "pq_algorithm": "ml-dsa-65",
            "pq_public_key": base64.b64encode(pk).decode(),
            "pq_value": base64.b64encode(_MLDSA.sign(sk, canonical.encode())).decode(),
        }

    @property
    def public_key_b64(self) -> str:
        return self._public_key_b64

    def sign(self, payload: bytes) -> bytes:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            return Ed25519PrivateKey.from_private_bytes(self._seed).sign(payload)
        except ImportError as exc:
            raise ImportError(
                "cryptography>=44 is required for Ed25519 signing. "
                "Install it: pip install cryptography>=44"
            ) from exc

    def sign_canonical(self, canonical: str) -> str:
        sig = self.sign(canonical.encode())
        return base64.b64encode(sig).decode()

    @staticmethod
    def verify_hybrid(public_key_b64: str, sig: dict[str, Any], canonical: str,
                      *, require_pq: bool | None = None,
                      pq_public_key_b64: str | None = None) -> bool:
        """Verify a signature OBJECT, checking the post-quantum layer when it is present.

        The hub previously read only `signature.value` and ignored `pq_*` entirely, which meant a
        peer could publish an ML-DSA-65 signature and the hub would never look at it — the PQ
        fields were decoration on the busiest verification path in the ecosystem.

        Policy, matching `oracle_core.signing`:
          1. Ed25519 MUST verify against the PINNED key. Always, and first.
          2. If PQ is required and there is no `pq_value` -> reject. Without this rule an
             adversary who breaks Ed25519 just deletes the pq_* keys and is accepted.
          3. If `pq_value` is present, ML-DSA-65 MUST verify too.

        `require_pq` defaults to `AIMARKET_PQC_REQUIRE`. It is OFF by default and must stay off
        until every FEDERATED PEER signs hybrid — peers are third parties this hub does not
        control, so requiring PQ globally would de-federate everyone who has not migrated.

        `pq_public_key_b64` PINS the peer's PQ identity. Omitted, the PQ key is taken from the
        signature object, which makes the PQ layer useless against the adversary it exists for:
        one who can forge Ed25519 forges it against the pinned classical key and attaches an
        ML-DSA keypair of their own. The hub does not yet carry a per-peer PQ key — `PeerRecord`
        stores `public_key` only — so requiring PQ here without adding that field first would buy
        presence, not attribution. Recording each peer's PQ key on first sight, while classical
        signatures still authenticate it, is the prerequisite for phase 3.
        """
        must_pq = _pqc_required() if require_pq is None else bool(require_pq)
        if must_pq and not _PQ_LIB:
            raise PQCMisconfigured(
                "AIMARKET_PQC_REQUIRE is on but dilithium-py is missing — "
                "install aimarket-hub[pqc] on this hub")
        if not Signer.verify(public_key_b64, sig.get("value", ""), canonical):
            return False
        pq_value = sig.get("pq_value")
        if not pq_value:
            return not must_pq
        if not _PQ_LIB:
            return False        # fail-closed on a PQ signature this hub cannot evaluate
        presented = sig.get("pq_public_key", "")
        if pq_public_key_b64 and presented != pq_public_key_b64:
            return False        # pinned PQ identity: a substituted key is a forgery attempt
        try:
            pk = decode_b64(presented)
            raw = decode_b64(pq_value)
            if pk is None or raw is None:
                return False
            return bool(_MLDSA.verify(pk, canonical.encode(), raw))
        except Exception:
            return False

    @staticmethod
    def verify(public_key_b64: str, signature_b64: str, canonical: str) -> bool:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as exc:
            raise ImportError(
                "cryptography>=44 is required for Ed25519 verification. "
                "Install it: pip install cryptography>=44"
            ) from exc

        try:
            pk_bytes = decode_b64(public_key_b64)
            sig_bytes = decode_b64(signature_b64)
            if pk_bytes is None or sig_bytes is None:
                return False
            Ed25519PublicKey.from_public_bytes(pk_bytes).verify(sig_bytes, canonical.encode())
            return True
        except (InvalidSignature, ValueError):
            return False

    def manifest_canonical(self, manifest: dict[str, Any]) -> str:
        """Canonical string signed for a manifest. Carries `generated_at` (freshness)."""
        import hashlib
        import json
        tools = manifest.get("tools", [])
        tools_hash = hashlib.sha256(
            json.dumps(tools, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        # by_hub carries per-peer trust_score + routing metadata; it MUST be covered by the
        # signature or a relay could tamper with peer trust/routing under a valid signature.
        by_hub_hash = hashlib.sha256(
            json.dumps(manifest.get("by_hub", {}), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return (
            f"capabilities_count:{manifest.get('capabilities_count', 0)}"
            f"|generated_at:{manifest.get('generated_at', '')}"
            f"|protocol_version:{manifest.get('protocol_version', 'v1')}"
            f"|tools_hash:{tools_hash}"
            f"|by_hub_hash:{by_hub_hash}"
        )

    def sign_manifest(self, manifest: dict[str, Any]) -> dict[str, str]:
        # Sign over structural integrity fields + content digest.
        # The manifest signature now covers the full tools[] array via a hash
        # so that any price/trust/agent modification invalidates the signature.
        canonical = self.manifest_canonical(manifest)
        signed = {
            "algorithm": "ed25519",
            "public_key": self.public_key_b64,
            "value": self.sign_canonical(canonical),
        }
        signed.update(self._pq_fields(canonical))
        return signed

    @staticmethod
    def object_canonical(obj: dict[str, Any]) -> str:
        """Deterministic canonical over an ENTIRE object (minus any existing `signature`).

        Static on purpose: canonicalizing is not a privileged act, and the update advisor that
        verifies a hub's `.well-known` holds no signing key at all. Instance calls
        (`signer.object_canonical(...)`) keep working unchanged.

        For documents where every field must be tamper-evident — e.g. `/.well-known/ai-market.json`
        and `/ai-market/v2/prices`, whose contents (mcp_servers, prices, federation, peers) are not
        covered by the structural manifest canonical.
        """
        import json

        body = {k: v for k, v in obj.items() if k != "signature"}
        return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def verify_object_signature(obj: dict[str, Any], public_key_b64: str, *,
                                require_pq: bool | None = None,
                                pq_public_key_b64: str | None = None) -> bool:
        """Verify a WHOLE-document signature (`sign_object`) against a pinned public key.

        `/.well-known/ai-market.json` and `/ai-market/v2/prices` are signed this way rather than
        with `manifest_canonical`, which covers structural fields only (see `api.py`, where the
        well-known switched to `sign_object` precisely so `mcp_servers`, `peers` and the price
        rows became tamper-evident). A verifier that reaches for `verify_manifest_signature` on
        those documents gets a confident `False`, which reads exactly like a bad signature.

        Keyless and static: the caller needs the PEER's public key, never one of its own. Pass the
        key from a PIN — the document advertises `signer_public_key` equal to
        `signature.public_key`, so a key read out of the response verifies it against itself.
        """
        if not public_key_b64:
            return False        # fail closed: no pin means no trust chain, as on manifests
        sig = obj.get("signature") or {}
        if not sig:
            return False
        return Signer.verify_hybrid(public_key_b64, sig, Signer.object_canonical(obj),
                                    require_pq=require_pq,
                                    pq_public_key_b64=pq_public_key_b64)

    def sign_object(self, obj: dict[str, Any]) -> dict[str, str]:
        """Sign the full object canonical (see object_canonical), hybrid when PQ signing is on."""
        canonical = self.object_canonical(obj)
        signed = {
            "algorithm": "ed25519",
            "public_key": self.public_key_b64,
            "value": self.sign_canonical(canonical),
        }
        # Additive: both signatures cover the SAME canonical string, so a verifier that has never
        # heard of ML-DSA reads `algorithm` and `value` and ignores the rest.
        signed.update(self._pq_fields(canonical))
        return signed

    def receipt_canonical(self, receipt: dict[str, Any], version: int = 1) -> str:
        """Canonical string signed for a receipt. Carries `nonce` + `timestamp` (replay).

        v1 (the default HERE, because this method is also the interop vector generator)
        is the cross-package interop shape mirrored by oracle_core, platon and the
        protocol test vectors — do not change it. Signing goes through `sign_receipt`,
        which picks the version from the receipt's content. v2 additionally binds
        the fields a REJECTION receipt is argued from (`reason`, `verify_score`,
        `delivery_reasons`, `trace_id`, `refunded`, `channel_id`): on a rejection the
        v1 fields are all constant (price 0, success 0, latency 0), so v1 signed
        essentially nothing about why the buyer's money came back.
        """
        base = (
            f"nonce:{receipt.get('nonce','')}"
            f"|product_id:{receipt.get('product_id','')}"
            f"|capability_id:{receipt.get('capability_id','')}"
            f"|price_usd:{receipt.get('price_usd',0)}"
            f"|timestamp:{receipt.get('timestamp','')}"
            f"|success:{1 if receipt.get('success') else 0}"
            f"|latency_ms:{receipt.get('latency_ms',0)}"
        )
        if version < 2:
            return base
        return f"{base}|v:2|fields:{_fields_digest(receipt, _RECEIPT_V2_FIELDS)}"

    def sign_receipt(
        self, receipt: dict[str, Any], version: int | None = None
    ) -> dict[str, Any]:
        """Sign a receipt at the canonical version its CONTENT requires.

        `version=None` (the default, and what every call site should use) resolves via
        `resolve_receipt_version`: a receipt whose evidence fields only v2 binds is signed
        at v2, an interop invoke receipt at the byte-stable v1. Pass a version only to pin
        one deliberately — pinning 1 on a receipt that carries v2 evidence signs that
        evidence out of the signature, which is exactly the bug this default removes.
        """
        resolved = resolve_receipt_version(receipt) if version is None else int(version)
        sig: dict[str, Any] = {
            "algorithm": "ed25519",
            "value": self.sign_canonical(self.receipt_canonical(receipt, resolved)),
        }
        if resolved > 1:
            # Absent == 1, so a v1 block is byte-identical to what it always was.
            sig["version"] = resolved
        return sig

    def verify_receipt_signature(
        self, receipt: dict[str, Any], known_public_key: str = ""
    ) -> bool:
        """Verify a receipt against the canonical version its OWN signature names.

        Back-compat is the point: a receipt signed before v2 existed carries no
        `version` and must keep verifying against the v1 canonical forever.
        Defaults to this hub's key because these receipts are hub-self-signed; a peer's
        receipt must pass the peer's pinned key explicitly.
        """
        version = _signed_version(receipt)
        if version == 0:
            # No usable signature, or an unreadable version — fail closed.
            return False
        canonical = self.receipt_canonical(receipt, version)
        return self.verify(
            known_public_key or self.public_key_b64,
            receipt["signature"]["value"],
            canonical,
        )

    def verification_canonical(
        self, verification: dict[str, Any], version: int = VERIFICATION_SIG_VERSION
    ) -> str:
        """Canonical string signed for a Pay-on-Verified verdict envelope.

        Separate from receipt_canonical on purpose: the invoke receipt is signed
        (and possibly already delivered to the buyer) BEFORE the verdict exists,
        so the verdict gets its own signature instead of mutating the receipt
        canonical. Carries `nonce` (the receipt nonce) + `timestamp` (replay).

        v2 (the default for new signatures) also binds the rest of the money
        outcome: `delivery_fulfils`/`delivery_reasons` (what the verdict actually
        said about the delivery), `audit_score` and `threshold` (the numbers it was
        decided from), and `status`/`verified`/`settled`/`reason` (what the hub then
        did with the money). Under v1 all of those travelled unauthenticated next to
        an authenticated `verdict`, so a stored envelope could be re-described
        without invalidating its signature. v1 remains available for verifying
        envelopes signed before the change.
        """
        base = (
            f"nonce:{verification.get('nonce','')}"
            f"|capability_id:{verification.get('capability_id','')}"
            f"|verdict:{verification.get('verdict','')}"
            f"|verify_score:{verification.get('verify_score',0)}"
            f"|trace_id:{verification.get('trace_id','')}"
            f"|timestamp:{verification.get('timestamp','')}"
        )
        if version < 2:
            return base
        return f"{base}|v:2|fields:{_fields_digest(verification, _VERIFICATION_V2_FIELDS)}"

    def sign_verification(
        self, verification: dict[str, Any], version: int = VERIFICATION_SIG_VERSION
    ) -> dict[str, Any]:
        sig: dict[str, Any] = {
            "algorithm": "ed25519",
            "value": self.sign_canonical(self.verification_canonical(verification, version)),
        }
        if version > 1:
            sig["version"] = version
        return sig

    def verify_verification_signature(
        self, verification: dict[str, Any], known_public_key: str = ""
    ) -> bool:
        """Verify a verdict envelope against the canonical version it names.

        The envelope's `signature` block is excluded from the canonical, so passing
        the envelope as stored (signature included) is correct.
        """
        version = _signed_version(verification)
        if version == 0:
            return False
        canonical = self.verification_canonical(verification, version)
        return self.verify(
            known_public_key or self.public_key_b64,
            verification["signature"]["value"],
            canonical,
        )

    def verify_manifest_signature(
        self, manifest: dict[str, Any], known_public_key: str = "",
        pinned_pq_public_key: str = ""
    ) -> bool:
        """Verify manifest signature against a KNOWN public key (pinned).

        Args:
            manifest: The manifest dict containing a 'signature' block.
            known_public_key: Peer's public key from a trusted source (DB pinning).
                              MUST be non-empty — empty key returns False.
            pinned_pq_public_key: The peer's ML-DSA key, likewise from a trusted source. Empty
                              means "not pinned yet", and then a present PQ signature is checked
                              against the key the document itself carries — which authenticates
                              the document but NOT the peer. See `verify_hybrid`.

        Security: The public key MUST come from a trusted source (peer DB record),
        NOT from the signature block itself — otherwise signatures are self-verifying.
        First-contact callers should reject the peer until an operator approves it.
        """
        if not known_public_key:
            # Fail-closed: no pinned key = cannot verify trust chain.
            # Previously fell back to sig.public_key which is self-verifying.
            return False
        sig = manifest.get("signature") or {}
        if not sig:
            return False
        # Use the single canonical (covers tools_hash AND by_hub_hash) — do NOT duplicate it
        # here, or sign/verify can silently diverge and miss tampering.
        canonical = self.manifest_canonical(manifest)
        # Hybrid: a peer that signs with ML-DSA-65 now has that signature CHECKED, not ignored.
        return self.verify_hybrid(known_public_key, sig, canonical,
                                  pq_public_key_b64=pinned_pq_public_key or None)



_URLSAFE_TO_STANDARD = str.maketrans("-_", "+/")


def decode_b64(text: str) -> bytes | None:
    """Base64 in either alphabet, padded or not. ``None`` when it is not base64 at all.

    The spec and the test vectors use standard base64, and so does this hub — but two of
    our own implementations (the factory's AI-Market signer and the UNI bubble) publish
    keys and signatures as unpadded base64url, and a stranger's SDK may do either. Plain
    ``base64.b64decode`` does not fail on a `-`/`_`: with ``validate=False`` it *discards*
    the characters it does not recognise and returns different bytes, so a perfectly good
    signature verified as garbage and the peer was told its signature did not match its key.
    Strict decoding, tried in both alphabets, is the honest reading — it accepts an
    encoding, never a wrong signature.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    padded = raw + "=" * (-len(raw) % 4)
    # `urlsafe_b64decode` takes no `validate`, so the url alphabet is folded into the
    # standard one and decoded strictly: same tolerance, no silent character dropping.
    for candidate in (padded, padded.translate(_URLSAFE_TO_STANDARD)):
        try:
            return base64.b64decode(candidate, validate=True)
        except (ValueError, binascii.Error):
            continue
    return None


def same_key(a: str, b: str) -> bool:
    """Do these two strings name the same key? Encoding is not identity.

    A peer that re-publishes its key in the other base64 alphabet has not rotated it, and
    treating that as a rotation quarantines a peer that did nothing.
    """
    if not a or not b:
        return False
    left, right = decode_b64(a), decode_b64(b)
    if left is None or right is None:
        return a == b
    return left == right


def verify_crypto_ready(key_path: str | Path = "data/hub_signing_key") -> Signer:
    """Fail-fast crypto initialization for hub startup.

    Returns a Signer or exits the process.
    Call this at the top of main() / create_app().
    """
    try:
        return Signer(key_path=key_path)
    except ImportError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
