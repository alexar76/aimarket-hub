"""Trial quota, verification records and appeals, receipt checks and the lottery work-seat
entitlement.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from aimarket_hub import receipt_binding, verified_settlement
from aimarket_hub.api_models import AppealRequest, LotteryEntitlementRequest, ReceiptVerifyRequest
from aimarket_hub.config import HubConfig
from aimarket_hub.sandbox_trials import sandbox_quota
from aimarket_hub.signing import pqc_required, receipt_signature_version, same_key, Signer
from aimarket_hub.verified_settlement import VerifiedSettlementService


def register(
    *,
    _ADMIN_TOKEN: str,
    _AGENT_TOKENS: dict[str, str],
    _NS_AGENT: tuple[str, str, dict[str, str]],
    _TRUSTED_PROXIES: frozenset[str],
    _check_invoke_rate: Callable[..., Any],
    _require_subject_credential: Callable[..., Any],
    config: HubConfig,
    router: APIRouter,
    signer: Signer,
    verify_svc: VerifiedSettlementService,
    db: Any = None,
) -> None:
    """Attach the sandbox, verification, receipt and lottery routes to ``router``."""
    # api.py's module-level helpers; imported here because api.py imports this module.
    from aimarket_hub.api import (
        _client_address,
    )

    # ── Invoke (plugin-gated) ───────────────────────────────────

    @router.get("/sandbox/quota")
    async def sandbox_quota_endpoint(visitor_id: str = ""):
        """Remaining free widget trials for a visitor id (embed localStorage)."""
        return {**sandbox_quota(visitor_id), "protocol_version": "v2"}

    @router.get("/verification/{nonce}")
    async def verification_lookup(nonce: str):
        """Pay-on-Verified verdict lookup: the async settlement's polling surface.

        Returns the (possibly still pending) verification envelope, the receipt
        with the envelope folded in, and — for refunded verdicts — the signed
        verification_rejection receipt.
        """
        rec = verify_svc.lookup(nonce)
        if rec is None:
            return JSONResponse(status_code=404, content={
                "success": False,
                "error": "verification_not_found",
                "protocol_version": "v2",
            })
        return {"success": True, **rec, "protocol_version": "v2"}

    @router.get("/verification/appeals/stats")
    async def verification_appeal_stats():
        """How often the appeal court agreed with the first instance — aggregate only.

        The research signal for calibrating the first-instance verifier. Two path segments,
        so it never collides with GET /verification/{nonce}.
        """
        return {"success": True, **verify_svc.appeal_stats(), "protocol_version": "v2"}

    @router.post("/verification/{nonce}/appeal")
    async def verification_appeal(
        nonce: str,
        request: Request,
        body: AppealRequest | None = None,
        x_payment_channel_secret: str | None = Header(default=None, alias="X-Payment-Channel-Secret"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        """Appeal a provisional Pay-on-Verified verdict to the second verifier.

        Only the party the verdict went against, only inside the window, and only with a
        bond: the buyer appeals a pass with the channel secret (bond held on that channel),
        the seller appeals a fail with its publisher account's X-API-Key (bond held on its
        hub credits). 202 with the filed appeal; the outcome lands on
        GET /verification/{nonce}. See docs/pay-on-verified.md "Appeals".
        """
        if not _check_invoke_rate(_client_address(request, _TRUSTED_PROXIES) or "anonymous"):
            return JSONResponse(status_code=429, content={
                "success": False, "error": "rate_limited", "protocol_version": "v2",
            })
        try:
            filed = verify_svc.file_appeal(
                nonce,
                channel_secret=(x_payment_channel_secret or "").strip(),
                api_key=(x_api_key or "").strip(),
                statement=(body.statement if body is not None else ""),
            )
        except verified_settlement.AppealRefused as exc:
            return JSONResponse(status_code=exc.status, content={
                "success": False, "error": exc.code, "detail": exc.detail,
                "protocol_version": "v2",
            })
        return JSONResponse(status_code=202, content={
            "success": True, **filed, "protocol_version": "v2",
        })

    def _pinned_signer(public_key: str) -> tuple[str, str, str] | None:
        """(key, pq_key, who) for a key this Hub trusts: its own, or one pinned for an active peer.

        The key named inside a receipt is only a lookup handle. It is accepted when it
        equals a key this Hub already holds for itself or for an active federation peer,
        so a self-signed object never verifies just because it names its own key. `pq_key` is
        the ML-DSA-65 key pinned with it ("" when none is pinned yet)."""
        if not public_key or same_key(public_key, signer.public_key_b64):
            return signer.public_key_b64, str(getattr(signer, "pq_public_key_b64", "") or ""), \
                config.hub_url or "self"
        if db is None:
            return None
        try:
            peers = db.list_peers()
        except Exception:  # noqa: BLE001
            return None
        for peer in peers:
            if peer.status == "active" and peer.public_key and same_key(peer.public_key, public_key):
                return peer.public_key, str(getattr(peer, "pq_public_key", "") or ""), peer.url
        return None

    @router.post("/receipts/verify")
    async def receipt_verify(body: ReceiptVerifyRequest):
        """Verify one receipt, and optionally its binding, against keys this Hub pinned.

        The public key is never accepted from the request, so a caller cannot make an
        arbitrary self-signed object appear trusted: a receipt verifies against this Hub's
        own Ed25519 + ML-DSA-65 identity or against the key pinned for an active federation
        peer (a routed answer carries the providing hub's receipt). Browser clients still
        verify Ed25519 locally; this endpoint evaluates ML-DSA-65 until WebCrypto exposes a
        post-quantum primitive.
        """
        receipt = body.receipt
        # Nginx caps the production request body, but direct ASGI deployments and test
        # clients may not. Keep verification CPU and response size predictably bounded.
        def _size(value: Any, over: int) -> int:
            try:
                return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode())
            except (TypeError, ValueError, RecursionError):
                return over
        if _size(receipt, 131_073) > 131_072:
            raise HTTPException(status_code=413, detail="receipt exceeds 128 KiB")
        # The answer a binding is checked against may be larger than the receipt.
        if _size(body.model_dump(), 262_145) > 262_144:
            raise HTTPException(status_code=413, detail="request exceeds 256 KiB")

        signature = receipt.get("signature")
        version = receipt_signature_version(receipt)
        named_key = str(signature.get("public_key") or "") if isinstance(signature, dict) else ""
        binding = body.receipt_binding or (body.response or {}).get("receipt_binding")
        # A hub does not put its key in its own receipts. Such a receipt is tried against this
        # Hub's key and then against the key a binding names as its signer, when that key is
        # pinned for an active peer: a routed answer carries the provider hub's receipt.
        if named_key:
            candidates = [c for c in (_pinned_signer(named_key),) if c is not None]
        else:
            candidates = [c for c in (_pinned_signer(""),) if c is not None]
            bound_signer = str(binding.get("receipt_signer") or "") if isinstance(binding, dict) else ""
            if bound_signer and not same_key(bound_signer, signer.public_key_b64):
                candidates += [c for c in (_pinned_signer(bound_signer),) if c is not None]
        pinned = candidates[0] if candidates else None
        classical_valid = False
        hybrid_valid = False
        pq_present = isinstance(signature, dict) and bool(signature.get("pq_value"))
        pq_pinned = False
        if version and isinstance(signature, dict):
            for candidate in candidates:
                key, pq_key, _ = candidate
                if not any(Signer.verify(key, str(signature.get("value") or ""), canonical)
                           for canonical in signer.receipt_canonical_candidates(receipt, version)):
                    continue
                pinned, classical_valid = candidate, True
                # Both identities are pinned: this Hub's own, or the ML-DSA key pinned for the
                # peer. A peer whose PQ key is not pinned yet has its ML-DSA layer checked as
                # presented, and the answer says so (`pq_pinned`).
                pq_pinned = bool(pq_key)
                hybrid_valid = (signer.verify_receipt_signature(receipt) if same_key(key, signer.public_key_b64)
                                else signer.verify_receipt_signature(receipt, key, pq_key))
                break

        answer: dict[str, Any] = {
            "valid": hybrid_valid,
            "classical_valid": classical_valid,
            "pq_present": pq_present,
            # `None` means there was no PQ claim to evaluate. When present, `valid`
            # requires both layers; for this Hub's own receipts ML-DSA is pinned too.
            "pq_valid": hybrid_valid if pq_present else None,
            "pq_pinned": pq_pinned if pq_present else None,
            "algorithm": "ed25519+ml-dsa-65" if pq_present else "ed25519",
            "signature_version": version or None,
            "signer": pinned[2] if pinned else None,
            "signer_matches_live_key": classical_valid and pinned is not None
            and same_key(pinned[0], signer.public_key_b64),
            "policy_requires_pq": pqc_required(),
            "protocol_version": "v2",
        }
        if isinstance(binding, dict):
            bsig = binding.get("signature")
            bpinned = _pinned_signer(str(bsig.get("public_key") or "") if isinstance(bsig, dict) else "")
            if bpinned is None:
                answer["binding"] = {"valid": False, "signature_valid": False,
                                     "detail": "binding signed by a key this Hub does not hold"}
            else:
                check = receipt_binding.verify(
                    binding, public_key_b64=bpinned[0], response=body.response,
                    input_payload=body.input, client_nonce=body.client_nonce,
                    pq_public_key_b64=bpinned[1] or None)
                check["signer"] = bpinned[2]
                # The key must belong to the hub the binding names, or a peer could sign a
                # binding in another hub's name.
                own = same_key(bpinned[0], signer.public_key_b64)
                named_hub = str(binding.get("hub") or "").rstrip("/")
                check["hub_matches"] = (
                    (not config.hub_url or named_hub == config.hub_url.rstrip("/")) if own
                    else named_hub == str(bpinned[2]).rstrip("/"))
                # It names THIS receipt: the same nonce, and as signer the key the receipt
                # really verified under — whether or not the receipt names a key itself.
                check["names_this_receipt"] = bool(
                    str(receipt.get("nonce") or "") == binding.get("receipt_nonce")
                    and classical_valid and pinned is not None
                    and same_key(pinned[0], str(binding.get("receipt_signer") or "")))
                check["valid"] = bool(check["valid"] and check["names_this_receipt"]
                                      and check["hub_matches"] and hybrid_valid)
                answer["binding"] = check
        return answer

    @router.post("/lottery/work-seat-entitlement")
    async def lottery_work_seat_entitlement(
        body: LotteryEntitlementRequest,
        authorization: str = Header(default=""),
    ):
        """Issue a short-lived, Hub-signed lottery entitlement for this agent.

        Every federated Hub uses its own agent credential registry and signing key.
        The lottery relayer trusts only this Hub or an active peer key pinned by its
        federation registry; a self-selected key is never accepted.
        """
        # NOT via the shared-publish-token relaxation. The whole value of this
        # entitlement is "this Hub can tell its agents apart": the relayer turns
        # (hub_url, agent_id) into the on-chain participant nullifier that rations one
        # unpaid work seat per agent. A credential that speaks for ANY agent_id mints
        # unlimited distinct participants from one token — a sybil generator for the
        # lottery — so this route requires a real per-agent credential (or the operator's
        # admin token) on every hub, production or not.
        if not _AGENT_TOKENS and not _ADMIN_TOKEN:
            raise HTTPException(
                status_code=503,
                detail=(
                    "disabled: lottery entitlements need a per-agent credential. Set "
                    "AIMARKET_AGENT_TOKENS='<agent_id>:<secret>,...' (the shared publish "
                    "token cannot prove which agent is asking)."
                ),
            )
        _require_subject_credential(authorization, body.agent_id, (_NS_AGENT,))
        from aimarket_hub.lottery_entitlement import issue_entitlement

        try:
            entitlement = issue_entitlement(
                signer=signer,
                hub_url=config.hub_url,
                agent_id=body.agent_id,
                wallet=body.wallet,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"entitlement": entitlement, "protocol_version": "v2"}
