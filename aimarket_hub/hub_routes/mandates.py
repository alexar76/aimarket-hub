"""Mandates (aimarket-protocol/mandates.md): registration, owners, revocation, and subcontract
job trees.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from aimarket_hub import credits, mandates
from aimarket_hub.credits import CreditsLedger
from aimarket_hub.mandates import MandateStore
from aimarket_hub.subcontract import JobStore


def register(
    *,
    _TRUSTED_PROXIES: frozenset[str],
    _check_invoke_rate: Callable[..., Any],
    _is_admin_bearer: Callable[..., Any],
    hub_credits: CreditsLedger,
    hub_jobs: JobStore,
    hub_mandates: MandateStore,
    router: APIRouter,
) -> SimpleNamespace:
    """Attach the /mandates and /jobs routes to ``router``."""
    # api.py's module-level helpers; imported here because api.py imports this module.
    from aimarket_hub.api import (
        _client_address,
    )

    # ── Mandates (aimarket-protocol/mandates.md §3-5) ────────────────────────
    def _mandate_refusal(exc: mandates.MandateError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body())

    async def _json_body(request: Request, limit: int = mandates.MAX_DOC_BYTES + 4096) -> Any:
        raw = await request.body()
        if len(raw) > limit:
            raise mandates.MandateError(413, "mandate_malformed", f"body exceeds {limit} bytes")  # §7 lists 413
        try:
            return json.loads(raw or b"null")
        except ValueError as exc:
            raise mandates.MandateError(400, "mandate_malformed", "body is not JSON") from exc

    def _api_key_account(x_api_key: str | None) -> str:
        if not credits.enabled() or not x_api_key:
            return ""
        return hub_credits.resolve(x_api_key)

    @router.post("/mandates")
    async def register_mandate(request: Request):
        """Register a mandate (§3.5). The document authenticates itself; the hub only
        refuses one whose root nobody on this hub has agreed to fund."""
        if not _check_invoke_rate(_client_address(request, _TRUSTED_PROXIES) or "anonymous"):
            return JSONResponse(status_code=429, content={"success": False, "error": "rate_limited"})
        try:
            document = await _json_body(request)
            return {**hub_mandates.register(document), "protocol_version": "v2"}
        except mandates.MandateError as exc:
            return _mandate_refusal(exc)

    @router.get("/mandates/owners")
    async def list_mandate_owners(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        account = _api_key_account(x_api_key)
        if not account:
            return JSONResponse(status_code=401, content={"success": False, "error": "invalid_api_key"})
        return {"account_id": account, "owners": hub_mandates.owners_of(account), "protocol_version": "v2"}

    @router.get("/mandates/{digest:path}")
    async def mandate_status(
        digest: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        x_proof: str | None = Header(default=None, alias=mandates.PROOF_HEADER),
    ):
        """Validity and chain for anyone holding the digest; usage only for the chain's keys
        or the funding account (§3.5)."""
        row = hub_mandates.row(digest)
        if row is None:
            return JSONResponse(status_code=404, content={"success": False, "error": "mandate_unknown"})
        # Status of the mandate AS A CHAIN: one whose parent was revoked, or that has expired,
        # cannot be used however clean its own row looks.
        status, reason = hub_mandates.status_of(digest)
        chain_digests: list[str] = []
        cursor = digest
        while cursor and len(chain_digests) <= mandates.MAX_CHAIN:
            chain_digests.append(cursor)
            cursor = (hub_mandates.row(cursor) or {}).get("parent_digest") or ""
        out: dict[str, Any] = {
            "digest": digest,
            "status": status,
            "issuer": row["issuer"],
            "subject": row["subject"],
            "parent": row["parent_digest"] or None,
            "root": row["root_digest"],
            "chain": list(reversed(chain_digests)),
            "depth": row["depth"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "revoked_at": row["revoked_at"] or None,
            "protocol_version": "v2",
        }
        if reason:
            out["status_reason"] = reason
        try:
            entitled = False
            account = _api_key_account(x_api_key)
            if account:
                owner = hub_mandates.owner_account(row["root_issuer"])
                entitled = owner is not None and owner[0] == account
            elif x_proof:
                leaf = hub_mandates.get(digest)
                if leaf is not None:
                    # Any key in the chain may read its usage (§3.5): the owner and every
                    # re-delegating agent, not only the key the mandate was issued to.
                    hub_mandates.verify_request(leaf=leaf, proof_header=x_proof, method="GET",
                                                path=mandates.signed_path(request), body=b"",
                                                signers=hub_mandates.chain_keys(digest))
                    entitled = True
            if entitled:
                out["usage"] = hub_mandates.usage(digest)
        except mandates.MandateError as exc:
            return _mandate_refusal(exc)
        return out

    @router.post("/mandates/owners")
    async def link_mandate_owner(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        authorization: str = Header(default=""),
    ):
        """Link an owner DID to the credit account that pays for its mandates (§4).

        The first owner needs the account's API key and the DID's own signature. Every later
        change also needs an existing owner's signature (`authorization` in the body) — or the
        operator's admin token, the recovery path for an owner who has lost every key."""
        account = _api_key_account(x_api_key)
        if not account:
            return JSONResponse(status_code=401, content={
                "success": False, "error": "invalid_api_key",
                "detail": "linking an owner needs the funding account's X-API-Key",
            })
        try:
            payload = await _json_body(request)
            if not isinstance(payload, dict):
                raise mandates.MandateError(400, "mandate_malformed", "body must be an object")
            linked = hub_mandates.link_owner(
                account_id=account, did=str(payload.get("did") or ""), proof=payload.get("proof"),
                require_mandate=bool(payload.get("require_mandate", False)),
                authorization=payload.get("authorization"), admin=_is_admin_bearer(authorization),
            )
            return {**linked, "protocol_version": "v2"}
        except mandates.MandateError as exc:
            return _mandate_refusal(exc)

    @router.post("/mandates/owners/unlink")
    async def unlink_mandate_owner(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        authorization: str = Header(default=""),
    ):
        """Unlink an owner (§4): the account's API key AND an owner's signature (or the admin
        token). The key alone would let whoever holds a leaked key delete the row that carries
        require_mandate and pay with the bare key again."""
        account = _api_key_account(x_api_key)
        if not account:
            return JSONResponse(status_code=401, content={"success": False, "error": "invalid_api_key"})
        try:
            payload = await _json_body(request)
            if not isinstance(payload, dict):
                raise mandates.MandateError(400, "mandate_malformed", "body must be an object")
            return {**hub_mandates.unlink_owner(
                account_id=account, did=str(payload.get("did") or ""),
                authorization=payload.get("authorization"), admin=_is_admin_bearer(authorization),
            ), "protocol_version": "v2"}
        except mandates.MandateError as exc:
            return _mandate_refusal(exc)

    @router.post("/mandates/revoke")
    async def revoke_mandate(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        """Revoke a mandate and everything below it (§5.5): signed by an issuer in its chain,
        or by the funding account's API key as the owner's kill switch."""
        try:
            payload = await _json_body(request)
            if not isinstance(payload, dict) or not isinstance(payload.get("digest"), str):
                raise mandates.MandateError(400, "mandate_malformed", "body must be {digest, ...}")
            account = _api_key_account(x_api_key)
            if account and not payload.get("s"):
                result = hub_mandates.revoke(digest=payload["digest"], account_id=account)
            else:
                result = hub_mandates.revoke(
                    digest=payload["digest"], by=str(payload.get("by") or ""),
                    proof={"t": payload.get("t"), "n": payload.get("n"), "s": payload.get("s")},
                )
            return {**result, "protocol_version": "v2"}
        except mandates.MandateError as exc:
            return _mandate_refusal(exc)

    @router.get("/jobs/{job_id}")
    async def job_tree(job_id: str):
        """A subcontracting job's tree (§6.4). The id is an unguessable capability."""
        if not re.fullmatch(r"job_[0-9a-f]{24}", job_id or ""):
            return JSONResponse(status_code=404, content={"success": False, "error": "job_unknown"})
        tree = hub_jobs.tree(job_id)
        if tree is None:
            return JSONResponse(status_code=404, content={"success": False, "error": "job_unknown"})
        return {**tree, "protocol_version": "v2"}

    return SimpleNamespace(
        _api_key_account=_api_key_account,
    )
