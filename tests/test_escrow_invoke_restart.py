"""A signed escrow invoke through the API with both ledgers reopened on retry."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import time

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import channels
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.escrow_bridge import chain, eip712, store
from aimarket_hub.hub_routes.invoke_parts import local_delivery
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer


@pytest.mark.parametrize(("result", "interruption"), [
    ({"answer": "escrow"}, None),
    ({"ok": False, "error": "provider refused"}, None),
    (None, None),
    ({"answer": "escrow"}, "before_capture"),
    ({"answer": "escrow"}, "after_capture"),
    ({"answer": "escrow"}, "expired_before_capture"),
    ({"answer": "escrow"}, "expired_then_reaped"),
])
def test_signed_escrow_invoke_replays_after_restart(monkeypatch, tmp_path, result, interruption):
    provider_refuses = result is None or result.get("ok") is False
    from eth_account import Account

    hub_wallet = "0x" + "cc" * 20
    escrow_contract = "0x" + "ee" * 20
    escrow_channel = "0x" + "11" * 32
    receipt = "0x" + "22" * 32
    token = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    depositor = Account.from_key("0x" + "42" * 32)
    for key, value in {
        "AIFACTORY_CRYPTO_ENABLED": "1",
        "AIMARKET_ESCROW_BRIDGE_ENABLED": "1",
        "AIMARKET_ESCROW_BRIDGE_DB_PATH": str(tmp_path / "bridge.db"),
        "AIMARKET_VERIFY_SETTLEMENTS_DB_PATH": str(tmp_path / "hub.db"),
        "AIMARKET_ESCROW_CONTRACT": escrow_contract,
        "AIMARKET_ESCROW_HUB_ADDRESS": hub_wallet,
        "AIMARKET_ESCROW_COLLECT_INTERVAL_S": "0",
        "AIMARKET_ORACLE_FAMILY_URL": "off",
    }.items():
        monkeypatch.setenv(key, value)
    onchain = chain.EscrowChannel(
        channel_id=escrow_channel, depositor=depositor.address, hub=hub_wallet, token=token,
        deposit_amount=5_000_000, balance=5_000_000, used_amount=0, expires_at=int(time.time()) + 3600,
        nonce=0, status=0,
    )
    monkeypatch.setattr(chain, "chain_id", lambda: 8453)
    monkeypatch.setattr(chain, "escrow_address", lambda: escrow_contract)
    monkeypatch.setattr(chain, "read_channel", lambda *args, **kwargs: onchain)

    @contextmanager
    def running_hub():
        config = HubConfig()
        config.db_path = str(tmp_path / "hub.db")
        config.signing_key_path = str(tmp_path / "key")
        db = HubDatabase(config.db_path)
        monkeypatch.setattr(channels, "_ledger", channels.ChannelLedger(str(tmp_path / "channels.db")))
        try:
            with TestClient(create_app(config=config, db=db, signer=Signer(config.signing_key_path))) as client:
                yield client, db
        finally:
            db.close()

    executions = []
    channel_id = ""

    def ledger_totals():
        with channels._ledger._get_conn() as conn:
            row = conn.execute(
                "SELECT balance_cents, used_cents FROM channels WHERE channel_id = ?", (channel_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM debited_receipts WHERE receipt_id = ?", (receipt,),
            ).fetchone()[0]
            return row["balance_cents"], row["used_cents"], count

    async def execute(d, call, local):
        executions.append(local.nonce)
        assert channels.hold_state(receipt) == "held"
        assert ledger_totals() == (498, 0, 0)
        return result

    monkeypatch.setattr(local_delivery, "_local_execute", execute)
    auth = eip712.DebitAuthorization(
        channel_id=escrow_channel, hub=hub_wallet, token=token,
        amount=20_000, receipt_id=receipt, nonce=0, deadline=int(time.time()) + 2400,
    )
    signature = Account._sign_hash(
        eip712.debit_digest(auth, chain_id=8453, verifying_contract=escrow_contract), depositor.key,
    ).signature
    payment = auth.as_message()
    payment.update(channelId=escrow_channel, receiptId=receipt, signature="0x" + signature.hex())
    body = {
        "product_id": "escrow-restart", "capability_id": "escrow.restart@v1",
        "source_hub": "local", "input": {"query": "original"},
        "payment_authorization": payment,
    }

    with running_hub() as (client, db):
        db.upsert_capability(Capability(
            capability_id=body["capability_id"], product_id=body["product_id"],
            name="Escrow restart", price_per_call_usd=0.02, source_hub="local",
            prompt_template='{"answer": "escrow"}',
        ))
        opened = client.post("/ai-market/v2/channel/open", json={
            "deposit_usd": 5, "chain": "base", "wallet": depositor.address,
            "escrow_channel_id": escrow_channel,
        })
        assert opened.status_code == 200, opened.text
        opened = opened.json()["channel"]
        channel_id = opened["channel_id"]
        headers = {"X-Payment-Channel": channel_id, "X-Payment-Channel-Secret": opened["channel_secret"]}
        expected = (500, 0, 0) if provider_refuses else (498, 2, 1)
        if interruption:
            capture = channels.capture_hold
            saved_response = None

            def interrupted_capture(receipt_id):
                nonlocal saved_response
                row = db._conn.execute("SELECT status, response_body FROM invoke_idempotency").fetchone()
                assert row["status"] == "prepared" and row["response_body"]
                saved_response = base64.b64decode(row["response_body"])
                if interruption == "after_capture":
                    assert capture(receipt_id)["ok"]
                raise RuntimeError("simulated interruption")

            with monkeypatch.context() as injected:
                injected.setattr(channels, "capture_hold", interrupted_capture)
                with pytest.raises(RuntimeError, match="simulated interruption"):
                    client.post("/ai-market/v2/invoke", headers=headers, json=body)
            assert ledger_totals() == (expected if interruption == "after_capture" else (498, 0, 0))
            if interruption == "before_capture":
                with channels._ledger._get_conn() as conn:
                    conn.execute("UPDATE channel_holds SET created_at = '2000-01-01T00:00:00Z'")
                    conn.commit()
                reap = channels._ledger.reap_stale_holds(max_age_secs=1)
                assert not reap.get("error") and reap["reaped"] == 0 and reap["skipped"] == 1
            if interruption.startswith("expired"):
                row = db._conn.execute("SELECT response_headers FROM invoke_idempotency").fetchone()
                metadata = json.loads(row["response_headers"])
                metadata["deadline"] = 1
                db._conn.execute("UPDATE invoke_idempotency SET response_headers = ?", (json.dumps(metadata),))
                db._conn.commit()
            if interruption == "expired_then_reaped":
                # Nothing can capture the hold now, so the checkpoint no longer keeps it
                # from the reaper; held for ever, it kept the channel from closing.
                with channels._ledger._get_conn() as conn:
                    conn.execute("UPDATE channel_holds SET created_at = '2000-01-01T00:00:00Z'")
                    conn.commit()
                reap = channels._ledger.reap_stale_holds(max_age_secs=1)
                assert not reap.get("error") and reap["reaped"] == 1
                assert ledger_totals() == (500, 0, 0)
        else:
            first = client.post("/ai-market/v2/invoke", headers=headers, json=body)
            assert first.status_code == (502 if provider_refuses else 200), first.text
            assert ledger_totals() == expected
            assert channels.hold_state(receipt) == ("released" if provider_refuses else "captured")
        if provider_refuses:
            authorizations = store.AuthorizationStore()
            try:
                assert authorizations.get(receipt).status == store.ABANDONED
            finally:
                authorizations.close()

    with running_hub() as (client, _):
        replay_headers = {**headers, "Idempotency-Key": "unrelated-explicit-key"}
        replay = client.post("/ai-market/v2/invoke", headers=replay_headers, json=body)
        if interruption and interruption.startswith("expired"):
            # The authorization can no longer be collected: the hold goes back to the
            # channel, the authorization leaves its nonce window, and every retry gets
            # the same definite answer instead of waiting for a capture that cannot come.
            assert replay.status_code == 409, replay.text
            assert replay.json()["error"] == "escrow_authorization_expired"
            assert replay.headers["X-AIMarket-Idempotency"] == "replayed"
            assert executions == [receipt]
            assert ledger_totals() == (500, 0, 0)
            assert channels.hold_state(receipt) in ("released", "reaped")
            authorizations = store.AuthorizationStore()
            try:
                assert authorizations.get(receipt).status == store.ABANDONED
            finally:
                authorizations.close()
            again = client.post("/ai-market/v2/invoke", headers=headers, json=body)
            assert again.status_code == 409 and again.content == replay.content
            assert executions == [receipt]
            return
        if interruption:
            assert replay.status_code == 200, replay.text
            assert replay.content == saved_response
            assert replay.json()["result"] == result
            assert replay.json()["receipt"]["nonce"] == receipt
            assert channels.hold_state(receipt) == "captured"
        else:
            assert replay.status_code == first.status_code, replay.text
            assert replay.json() == first.json()
        assert replay.headers["X-AIMarket-Idempotency"] == "replayed"

        conflicting = client.post(
            "/ai-market/v2/invoke", headers=headers,
            json={**body, "input": {"query": "changed"}},
        )
        assert conflicting.status_code == 422, conflicting.text
        assert executions == [receipt]
        assert ledger_totals() == expected
