"""Ed25519 signing for hub manifests and receipts.

Fail-fast: refuses to initialize if cryptography is not installed
or if an existing key file is corrupted.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

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


class Signer:
    """Ed25519 signer for a hub instance."""

    def __init__(self, key_path: str | Path = "data/hub_signing_key"):
        self.key_path = Path(key_path)
        self._seed, self._pub_bytes = _ensure_keypair(self.key_path)
        self._public_key_b64 = base64.b64encode(self._pub_bytes).decode()

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

    def verify(self, public_key_b64: str, signature_b64: str, canonical: str) -> bool:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError as exc:
            raise ImportError(
                "cryptography>=44 is required for Ed25519 verification. "
                "Install it: pip install cryptography>=44"
            ) from exc

        try:
            pk_bytes = base64.b64decode(public_key_b64)
            sig_bytes = base64.b64decode(signature_b64)
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
        return {
            "algorithm": "ed25519",
            "public_key": self.public_key_b64,
            "value": self.sign_canonical(self.manifest_canonical(manifest)),
        }

    def object_canonical(self, obj: dict[str, Any]) -> str:
        """Deterministic canonical over an ENTIRE object (minus any existing `signature`).

        For documents where every field must be tamper-evident — e.g. `/.well-known/ai-market.json`
        and `/ai-market/v2/prices`, whose contents (mcp_servers, prices, federation, peers) are not
        covered by the structural manifest canonical.
        """
        import json

        body = {k: v for k, v in obj.items() if k != "signature"}
        return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def sign_object(self, obj: dict[str, Any]) -> dict[str, str]:
        """Sign the full object canonical (see object_canonical)."""
        return {
            "algorithm": "ed25519",
            "public_key": self.public_key_b64,
            "value": self.sign_canonical(self.object_canonical(obj)),
        }

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
        self, manifest: dict[str, Any], known_public_key: str = ""
    ) -> bool:
        """Verify manifest signature against a KNOWN public key (pinned).

        Args:
            manifest: The manifest dict containing a 'signature' block.
            known_public_key: Peer's public key from a trusted source (DB pinning).
                              MUST be non-empty — empty key returns False.

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
        return self.verify(known_public_key, sig.get("value", ""), canonical)


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
