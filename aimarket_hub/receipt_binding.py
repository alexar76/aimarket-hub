"""Receipt binding: what was asked, what was answered, and the buyer's own nonce, signed.

The invoke receipt (spec §7.3.3) signs who sold what, the price, the time, success and
latency. It never signed the request or the answer, and its nonce is chosen by the hub, so a
hub could hand back a receipt it had signed for an earlier identical call. TensorFeed's AFTA
receipts bind both hashes and a caller nonce; checking ours against theirs on 2026-10-30 is
how the gap was found (docs/evidence/tensorfeed-receipts-2026-10-30).

The binding is a separate signed object next to the receipt, the way the Pay-on-Verified
verdict envelope is: the receipt's own canonical and signature are untouched, so every
verifier that reads receipts today keeps working, and one that knows the binding can check
it. It is signed by the hub that answered the buyer, which on a routed call is not the hub
that signed the receipt — so it names the receipt by nonce AND signer key.

Bytes (spec §7.3.9):

    receipt_binding:1|{sha256 hex of the binding's fields as compact sorted JSON}

where the fields are exactly BINDING_FIELDS, an absent one bound as null. `input_sha256`
and `output_sha256` are SHA-256 hex of the request's `input` and of the answer's
`output_member` value, each as JSON with sorted keys, compact separators and non-ASCII
characters as themselves — the same rule as the receipt v2 field digest.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

BINDING_VERSION = 1
# The buyer's nonce. `X-Agent-Nonce` is the header TensorFeed's AFTA receipts read, accepted
# so a client written for those works here unchanged.
CLIENT_NONCE_HEADERS = ("X-Client-Nonce", "X-Agent-Nonce")
CLIENT_NONCE_ECHO_HEADER = "X-Client-Nonce-Echo"
_CLIENT_NONCE = re.compile(r"[A-Za-z0-9._-]{8,128}")
# Where an invoke answer carries the work: `result` for a local capability, `output` for a
# routed one. Checked in this order.
OUTPUT_MEMBERS = ("result", "output")
BINDING_FIELDS = (
    "v", "hub", "receipt_nonce", "receipt_signer", "input_sha256",
    "output_member", "output_sha256", "client_nonce", "timestamp",
)


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def binding_canonical(binding: dict[str, Any]) -> str:
    return "receipt_binding:%d|%s" % (
        BINDING_VERSION, payload_sha256({name: binding.get(name) for name in BINDING_FIELDS}))


def read_client_nonce(headers: Any) -> tuple[str | None, str | None]:
    """(nonce, error). Both None when the buyer sent none; a malformed one is an error, not
    silently dropped, because a buyer who sent a nonce is relying on getting it back signed."""
    getlist = getattr(headers, "getlist", None)
    for name in CLIENT_NONCE_HEADERS:
        # Sent twice, layers that read the first and the last occurrence would see two
        # different nonces (the idempotency middleware fingerprints one, the route signs the
        # other); one value or a refusal (review of 2026-11-11).
        if callable(getlist) and len(getlist(name)) > 1:
            return None, "send %s once" % name
        raw = headers.get(name)
        if raw is None:
            continue
        value = str(raw).strip()
        if not _CLIENT_NONCE.fullmatch(value):
            return None, "%s must be 8-128 characters of A-Z a-z 0-9 . _ -" % name
        return value, None
    return None, None


def output_member(content: dict[str, Any]) -> str | None:
    """The member of an invoke answer that carries the work, or None. The provenance credential
    names the same member (`outputDigestOf`), so both digests describe one value."""
    return next((m for m in OUTPUT_MEMBERS if m in content), None)


_output_member = output_member  # the name used before 2026-11-11


def build(content: dict[str, Any], *, input_payload: Any, client_nonce: str | None,
          hub_url: str, signer: Any, now: float | None = None,
          receipt_signer: str | None = None) -> dict[str, Any] | None:
    """The signed binding for one delivered answer, or None when there is nothing to bind
    (no receipt, or no output member).

    `receipt_signer` is the key the caller knows signed the receipt when the receipt does not
    name one: this hub's own key for a local answer, the pinned key of the peer that was called
    for a routed one. Hubs do not put their key in their own receipts, so falling back to this
    hub's key made a routed binding name the routing hub as the signer of the provider's receipt
    (review of 2026-11-11); with neither, it is bound as null rather than guessed."""
    receipt = content.get("receipt")
    member = output_member(content)
    if not isinstance(receipt, dict) or not receipt.get("nonce") or member is None:
        return None
    signature = receipt.get("signature")
    binding: dict[str, Any] = {
        "v": BINDING_VERSION,
        "hub": hub_url,
        "receipt_nonce": str(receipt.get("nonce")),
        "receipt_signer": (signature.get("public_key") if isinstance(signature, dict) else None)
        or receipt_signer,
        "input_sha256": payload_sha256(input_payload),
        "output_member": member,
        "output_sha256": payload_sha256(content.get(member)),
        "client_nonce": client_nonce,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    canonical = binding_canonical(binding)
    binding["signature"] = {
        "algorithm": "ed25519",
        "public_key": signer.public_key_b64,
        "value": signer.sign_canonical(canonical),
        **signer._pq_fields(canonical),
    }
    return binding


def verify(binding: dict[str, Any], *, public_key_b64: str, response: dict[str, Any] | None = None,
           input_payload: Any = None, client_nonce: str | None = None,
           pq_public_key_b64: str | None = None) -> dict[str, Any]:
    """Check a binding's signature against a key the CALLER trusts, and, when given, that it
    describes this answer, this input and this nonce. Every check is reported separately.

    `pq_public_key_b64` pins the ML-DSA-65 half too; without it the post-quantum layer is
    checked against the key the binding carries, which proves nothing about who signed it."""
    from aimarket_hub.signing import Signer

    sig = binding.get("signature") if isinstance(binding, dict) else None
    report: dict[str, Any] = {
        "signature_valid": bool(isinstance(sig, dict) and Signer.verify_hybrid(
            public_key_b64, sig, binding_canonical(binding),
            pq_public_key_b64=pq_public_key_b64 or None)),
    }
    if response is not None:
        receipt = response.get("receipt") if isinstance(response.get("receipt"), dict) else {}
        member = binding.get("output_member")
        report["receipt_matches"] = str(receipt.get("nonce") or "") == binding.get("receipt_nonce")
        report["output_matches"] = (
            member in OUTPUT_MEMBERS and member in response
            and payload_sha256(response.get(member)) == binding.get("output_sha256"))
    if input_payload is not None:
        report["input_matches"] = payload_sha256(input_payload) == binding.get("input_sha256")
    if client_nonce is not None:
        report["client_nonce_matches"] = client_nonce == binding.get("client_nonce")
    report["valid"] = all(v for v in report.values())
    return report
