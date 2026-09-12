"""x402 payments the hub actually honours.

The module shipped able to *advertise* payment and not to take it, which is half a rail:
every x402 client in the ecosystem could read this hub's 402 and none of them could pay it.
Acceptance is verification plus a receivable, and the split matters —

* verification is complete and local: scheme, network, asset, recipient, amount, validity
  window and the EIP-3009 signature, all checked before any work happens;
* settlement is not: submitting `transferWithAuthorization` needs an RPC and gas, so until
  the sweep runs the authorization is money promised, published as `x402_unsettled_usd`.

The signature tests need a crypto stack the hub's own test venv deliberately lacks (see
`escrow_bridge.eip712.CryptoUnavailable`), so they skip there and the rest still runs — the
terms, replay and ceiling checks are pure logic and must never be the reason a payment is
wrongly accepted.
"""
from __future__ import annotations

import base64
import json
import secrets
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import x402
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.escrow_bridge import eip712
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer

PAY_TO = "0x1218Ff3600000000000000000000000000000a0a"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PRICE = 0.02

crypto = pytest.mark.skipif(
    not eip712.crypto_available(), reason="eth-account not installed in this interpreter",
)


@contextmanager
def _hub(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("AIMARKET_X402_ACCEPT", "1")
    monkeypatch.setenv("AIMARKET_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("AIMARKET_X402_CHAIN", "base")
    monkeypatch.setenv("AIMARKET_ORACLE_FAMILY_URL", "off")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / f"hub-{len(list(tmp_path.iterdir()))}"
    root.mkdir(parents=True, exist_ok=True)
    config = HubConfig()
    config.db_path = str(root / "hub.db")
    config.signing_key_path = str(root / "key")
    db = HubDatabase(root / "hub.db")
    db.upsert_capability(Capability(
        capability_id="x.pay@v1", product_id="x-pay", name="Paid pack",
        description="static", price_per_call_usd=PRICE, source_hub="local",
        invoke_url="", prompt_template=json.dumps({"answer": "paid"}),
    ))
    app = create_app(config=config, db=db, signer=Signer(root / "key"))
    with TestClient(app) as client:
        yield client


def _authorization(sender: str, *, value: int = 20_000, to: str = PAY_TO,
                   valid_before: int = 4_000_000_000, nonce: str | None = None) -> dict:
    return {
        "from": sender,
        "to": to,
        "value": str(value),
        "validAfter": "0",
        "validBefore": str(valid_before),
        "nonce": nonce or ("0x" + secrets.token_hex(32)),
    }


def _envelope(auth: dict, signature: str) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": USDC_BASE,
        "payload": {"authorization": auth, "signature": signature},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _sign(auth: dict, private_key: str) -> str:
    from eth_account import Account

    digest = eip712.transfer_authorization_digest(
        sender=auth["from"], recipient=auth["to"], value=int(auth["value"]),
        valid_after=int(auth["validAfter"]), valid_before=int(auth["validBefore"]),
        nonce=auth["nonce"], token_name="USD Coin", token_version="2",
        chain_id=8453, token_address=USDC_BASE,
    )
    return Account._sign_hash(digest, private_key).signature.hex()


def _invoke(client, header: str | None = None):
    headers = {"X-PAYMENT": header} if header else {}
    return client.post("/ai-market/v2/invoke", headers=headers, json={
        "product_id": "x-pay", "capability_id": "x.pay@v1",
        "input": {}, "source_hub": "local",
    })


class TestTermsAreEnforcedBeforeAnyWork:
    def test_a_payment_to_somebody_else_is_refused(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path) as client:
            auth = _authorization("0x" + "11" * 20, to="0x" + "22" * 20)
            r = _invoke(client, _envelope(auth, "0x" + "00" * 65))
            assert r.status_code == 402
            assert "addressed to somebody else" in r.json()["detail"]

    def test_an_underpayment_is_refused_with_the_real_numbers(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path) as client:
            auth = _authorization("0x" + "11" * 20, value=1)
            r = _invoke(client, _envelope(auth, "0x" + "00" * 65))
            assert r.status_code == 402
            assert "the call costs 20000" in r.json()["detail"]

    def test_an_expired_authorization_is_refused(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path) as client:
            auth = _authorization("0x" + "11" * 20, valid_before=1)
            r = _invoke(client, _envelope(auth, "0x" + "00" * 65))
            assert r.status_code == 402
            assert "expired" in r.json()["detail"]

    def test_a_payment_on_another_chain_is_refused(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path) as client:
            payload = {
                "scheme": "exact", "network": "eip155:1", "asset": USDC_BASE,
                "payload": {"authorization": _authorization("0x" + "11" * 20),
                            "signature": "0x" + "00" * 65},
            }
            r = _invoke(client, base64.b64encode(json.dumps(payload).encode()).decode())
            assert r.status_code == 402
            assert "network" in r.json()["detail"]

    def test_a_malformed_header_is_a_400_not_a_free_call(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path) as client:
            r = _invoke(client, "!!!not base64 and not json!!!")
            assert r.status_code == 400
            assert r.json()["error"] == "payment_malformed"

    def test_an_unsigned_payload_never_buys_anything(self, monkeypatch, tmp_path):
        with _hub(monkeypatch, tmp_path) as client:
            payload = {"scheme": "exact", "payload": {
                "authorization": _authorization("0x" + "11" * 20)}}
            r = _invoke(client, base64.b64encode(json.dumps(payload).encode()).decode())
            assert r.status_code == 402
            assert "no signed authorization" in r.json()["detail"]


class TestWithARealSignature:
    @crypto
    def test_a_valid_payment_buys_the_call_and_becomes_a_receivable(self, monkeypatch, tmp_path):
        from eth_account import Account

        acct = Account.create()
        with _hub(monkeypatch, tmp_path) as client:
            auth = _authorization(acct.address)
            r = _invoke(client, _envelope(auth, _sign(auth, acct.key)))
            assert r.status_code == 200, r.text
            assert r.json()["result"]["answer"] == "paid"

            stats = client.get("/ai-market/v2/stats/live").json()["summary"]["x402"]
            assert stats["payments"] == 1
            # Promised, not collected — the distinction the module exists to keep.
            assert stats["x402_unsettled_usd"] == pytest.approx(PRICE)

    @crypto
    def test_the_same_authorization_cannot_buy_twice(self, monkeypatch, tmp_path):
        from eth_account import Account

        acct = Account.create()
        with _hub(monkeypatch, tmp_path) as client:
            auth = _authorization(acct.address)
            header = _envelope(auth, _sign(auth, acct.key))
            assert _invoke(client, header).status_code == 200
            second = _invoke(client, header)
            assert second.status_code == 402
            assert "already been used" in second.json()["detail"]

    @crypto
    def test_somebody_elses_signature_does_not_pay_for_you(self, monkeypatch, tmp_path):
        from eth_account import Account

        payer, stranger = Account.create(), Account.create()
        with _hub(monkeypatch, tmp_path) as client:
            auth = _authorization(payer.address)
            r = _invoke(client, _envelope(auth, _sign(auth, stranger.key)))
            assert r.status_code == 402
            assert "does not match the stated payer" in r.json()["detail"]

    @crypto
    def test_the_receivable_ceiling_stops_accepting(self, monkeypatch, tmp_path):
        from eth_account import Account

        acct = Account.create()
        with _hub(monkeypatch, tmp_path, AIMARKET_X402_MAX_UNSETTLED_USD="0.03") as client:
            first = _authorization(acct.address)
            assert _invoke(client, _envelope(first, _sign(first, acct.key))).status_code == 200
            second = _authorization(acct.address)
            r = _invoke(client, _envelope(second, _sign(second, acct.key)))
            assert r.status_code == 402
            assert "not accepting more unsettled" in r.json()["detail"]


class TestTheSwitchIsOff():
    def test_a_payment_header_is_ignored_when_acceptance_is_off(self, monkeypatch, tmp_path):
        """Discovery-only stays the default: taking money is the operator's decision."""
        with _hub(monkeypatch, tmp_path, AIMARKET_X402_ACCEPT="0") as client:
            auth = _authorization("0x" + "11" * 20)
            r = _invoke(client, _envelope(auth, "0x" + "00" * 65))
            # No rail is on at all in this fixture, so the call is simply free — what must
            # not happen is the hub acting on an unverified payment.
            assert r.status_code == 200
            assert "x402" not in client.get("/ai-market/v2/stats/live").json()["summary"]


def test_verification_refuses_when_the_hub_cannot_check_signatures(monkeypatch):
    """A hub without a crypto stack must refuse, not assume. Nothing may read
    'we could not verify' as 'the payer signed'."""
    monkeypatch.setenv("AIMARKET_X402_PAY_TO", PAY_TO)
    monkeypatch.setenv("AIMARKET_X402_CHAIN", "base")

    def _boom(*_a, **_k):
        raise eip712.CryptoUnavailable("eth-account unavailable: test")

    monkeypatch.setattr(eip712, "recover_transfer_signer", _boom)
    result = x402.verify_payment(
        {"scheme": "exact", "payload": {
            "authorization": _authorization("0x" + "11" * 20),
            "signature": "0x" + "00" * 65,
        }},
        price_usd=PRICE,
    )
    assert result["ok"] is False
    assert "cannot verify signatures" in result["error"]
