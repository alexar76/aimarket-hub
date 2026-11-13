"""Authenticated publisher withdrawal quotes, reservations, and status."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from aimarket_hub import credits, deposit_watch
from aimarket_hub import publisher_withdrawals as payouts
from aimarket_hub.paging import page_limit


def register(*, router: APIRouter, ledger: credits.CreditsLedger | None, hub_url: str) -> None:
    if ledger is None:
        return
    store = payouts.PublisherWithdrawals(ledger)

    def account_for(key: str | None) -> str:
        if not credits.enabled():
            raise HTTPException(status_code=503, detail="credit accounts are off on this hub")
        account = ledger.resolve(key or "")
        if not account:
            raise HTTPException(status_code=401, detail="unknown or disabled X-API-Key")
        return account

    def parse(body: dict[str, Any]) -> tuple[str, str, int, str]:
        sender = payouts.configured_sender()
        if not sender:
            raise HTTPException(status_code=503, detail="publisher USDC withdrawals are unavailable on this hub")
        try:
            request_id, amount_mc, recipient = payouts.terms(body, sender)
        except payouts.WithdrawalError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        return sender, request_id, amount_mc, recipient

    @router.get("/account/withdrawals")
    async def list_withdrawals(limit: int = 50, x_api_key: str | None = Header(None, alias="X-API-Key")):
        account = account_for(x_api_key)
        return {"success": True, **store.summary(account, page_limit(limit, maximum=100)),
                "enabled": bool(payouts.configured_sender()), "protocol_version": "v2"}

    @router.post("/account/withdrawals/quote")
    async def quote_withdrawal(body: dict, x_api_key: str | None = Header(None, alias="X-API-Key")):
        account = account_for(x_api_key)
        sender, request_id, amount_mc, recipient = parse(body)
        issued_at = int(time.time())
        message = payouts.wallet_message(hub_url=hub_url, account=account, sender=sender, request_id=request_id,
                                         amount_mc=amount_mc, recipient=recipient, issued_at=issued_at)
        return {"success": True, "request_id": request_id, "issued_at": issued_at, "message": message,
                "amount_atomic": str(amount_mc * payouts.UNITS_PER_MC), "protocol_version": "v2"}

    @router.post("/account/withdrawals", status_code=202)
    async def request_withdrawal(body: dict, x_api_key: str | None = Header(None, alias="X-API-Key")):
        account = account_for(x_api_key)
        sender, request_id, amount_mc, recipient = parse(body)
        try:
            existing = store.by_request(account, request_id)
            if existing:
                row = store.replay(existing, amount_mc, recipient)
                return {"success": True, **payouts.public(row), "protocol_version": "v2"}
            issued_at = body.get("issued_at")
            signature = body.get("signature")
            if type(issued_at) is not int or abs(int(time.time()) - issued_at) > payouts.WALLET_PROOF_TTL:
                raise payouts.WithdrawalError(400, "withdrawal quote expired or missing; request a new quote")
            if not deposit_watch.signature_links_available():
                raise payouts.WithdrawalError(503, "wallet signature verification is unavailable")
            message = payouts.wallet_message(hub_url=hub_url, account=account, sender=sender, request_id=request_id,
                                             amount_mc=amount_mc, recipient=recipient, issued_at=issued_at)
            if not isinstance(signature, str) or len(signature) not in (130, 132) \
                    or deposit_watch.signer_of(message, signature) != recipient:
                raise payouts.WithdrawalError(403, "recipient must sign this exact withdrawal quote")
            row = store.reserve(account=account, request_id=request_id, amount_mc=amount_mc,
                                recipient=recipient, sender=sender)
        except payouts.WithdrawalError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        return {"success": True, **payouts.public(row), "protocol_version": "v2"}

    @router.get("/account/withdrawals/{withdrawal_id}")
    async def read_withdrawal(withdrawal_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")):
        row = store.get(withdrawal_id, account=account_for(x_api_key))
        if row is None:
            raise HTTPException(status_code=404, detail="unknown withdrawal")
        return {"success": True, **payouts.public(row), "protocol_version": "v2"}

    @router.delete("/account/withdrawals/{withdrawal_id}")
    async def cancel_withdrawal(withdrawal_id: str, x_api_key: str | None = Header(None, alias="X-API-Key")):
        try:
            row = store.cancel(withdrawal_id, account_for(x_api_key))
        except payouts.WithdrawalError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        return {"success": True, **payouts.public(row), "protocol_version": "v2"}
