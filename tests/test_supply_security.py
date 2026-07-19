"""Supply-side security — stake, rate limits, LUMEN trust, response signatures."""

from __future__ import annotations

import json
import base64
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.publish import validate_manifest
from aimarket_hub.signing import Signer
from aimarket_hub.supply_security import SupplySecurity, SupplySecurityPolicy

PUBLISH_TOKEN = "sec-test-token"
PUBLISH_HEADERS = {"Authorization": f"Bearer {PUBLISH_TOKEN}"}

MANIFEST = {
    "product_id": "sec-demo",
    "capability_id": "secure.greet@v1",
    "name": "secure greet",
    "description": "Signed provider demo",
    "invoke_url": "http://127.0.0.1:3457/invoke",
    "price_per_call_usd": 0.02,
    "publisher_id": "pub-wallet-1",
    "provider_pubkey": "",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {"greeting": {"type": "string"}}},
}


class _ProviderResp:
    status_code = 200
    text = ""

    def __init__(self, payload: dict, headers: dict | None = None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


@pytest.fixture
def keys_and_signer(tmp_path):
    key_path = tmp_path / "provider_key"
    signer = Signer(key_path)
    return signer.public_key_b64, signer


@pytest.fixture
def hub_client(monkeypatch, keys_and_signer):
    pubkey, provider_signer = keys_and_signer
    monkeypatch.setenv("AIMARKET_PUBLISH_TOKEN", PUBLISH_TOKEN)
    monkeypatch.setenv("AIMARKET_ALLOW_LOCAL_PUBLISH", "1")
    monkeypatch.setenv("AIMARKET_SANDBOX_STUB_INVOKE", "1")
    monkeypatch.setenv("AIFACTORY_PUBLIC_URL", "http://factory.test")
    monkeypatch.setenv("AIMARKET_SUPPLY_SECURITY_RELAXED", "1")
    monkeypatch.setenv("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "0")
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "0")

    manifest = {**MANIFEST, "provider_pubkey": pubkey}

    class _RoutingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **k):
            if "127.0.0.1:3457" in url:
                inp = (k.get("json") or {}).get("input", {})
                name = inp.get("name", "world")
                result = {"greeting": f"Hello, {name}!"}
                canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                sig = provider_signer.sign_canonical(canonical)
                return _ProviderResp({"success": True, "result": result}, {"X-Provider-Signature": sig})
            return _ProviderResp({"output": {}})

    import aimarket_hub.api as api_mod

    monkeypatch.setattr(api_mod.httpx, "AsyncClient", _RoutingAsyncClient)

    with tempfile.TemporaryDirectory() as tmp:
        config = HubConfig()
        config.db_path = str(Path(tmp) / "test.db")
        config.signing_key_path = str(Path(tmp) / "hub_key")
        db = HubDatabase(config.db_path)
        signer = Signer(config.signing_key_path)
        app = create_app(config=config, db=db, signer=signer)
        with TestClient(app) as client:
            yield client, manifest, db


class TestSupplySecurityPolicy:
    def test_relaxed_zero_stake(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_SUPPLY_SECURITY_RELAXED", "1")
        pol = SupplySecurityPolicy.from_config(HubConfig())
        assert pol.min_stake_usd == 0.0
        assert pol.relaxed is True


class TestSupplySecurityUnit:
    def test_stake_and_publish_gate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIMARKET_SUPPLY_SECURITY_RELAXED", "0")
        monkeypatch.setenv("AIMARKET_SUPPLY_MIN_STAKE_USD", "10")
        monkeypatch.setenv("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "0")
        db_path = tmp_path / "unit.db"
        db = HubDatabase(db_path)
        sec = SupplySecurity(db, HubConfig(), signer=Signer(tmp_path / "k"))
        with patch.object(sec.lumen, "score_entity", return_value={"score": 0.6, "degraded": False}):
            body = {**MANIFEST, "provider_pubkey": "dGVzdA=="}
            with pytest.raises(ValueError, match="minimum stake"):
                sec.validate_publish(body)
            sec.stake("pub-wallet-1", 15.0, "tx-demo")
            pub_id, _ = sec.validate_publish(body)
            assert pub_id == "pub-wallet-1"

    def test_sanitize_blocks_secrets(self, tmp_path):
        db = HubDatabase(tmp_path / "san.db")
        sec = SupplySecurity(db, HubConfig(), signer=Signer(tmp_path / "k"))
        with pytest.raises(ValueError, match="sensitive"):
            sec.sanitize_input({"api_key": "leak"})

    def test_verify_response_signature(self, tmp_path, keys_and_signer, monkeypatch):
        monkeypatch.setenv("AIMARKET_ALLOW_LOCAL_PUBLISH", "1")
        pubkey, provider_signer = keys_and_signer
        db = HubDatabase(tmp_path / "sig.db")
        sec = SupplySecurity(db, HubConfig(), signer=Signer(tmp_path / "k"))
        sec.policy.require_response_signature = True
        cap = validate_manifest({**MANIFEST, "provider_pubkey": pubkey, "publisher_id": "p1"})
        cap.provider_pubkey = pubkey
        result = {"greeting": "hi"}
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        sig = provider_signer.sign_canonical(canonical)
        sec.verify_provider_response(cap, result, sig)
        with pytest.raises(ValueError, match="invalid provider"):
            sec.verify_provider_response(cap, result, base64.b64encode(b"\x00" * 64).decode())


class TestSupplyApi:
    def test_stake_register_invoke(self, hub_client):
        client, manifest, db = hub_client
        stake = client.post(
            "/ai-market/v2/supply/stake",
            json={"publisher_id": "pub-wallet-1", "amount_usd": 20.0, "tx_hash": "0xabc"},
            headers=PUBLISH_HEADERS,
        )
        assert stake.status_code == 200
        assert stake.json()["stake_usd"] == 20.0

        reg = client.post("/ai-market/v2/supply/register", json=manifest, headers=PUBLISH_HEADERS)
        assert reg.status_code == 200
        body = reg.json()
        assert body["published"] is True
        assert body["trust_score"] >= 0

        inv = client.post("/ai-market/v2/invoke", json={
            "product_id": "sec-demo",
            "capability_id": "secure.greet@v1",
            "source_hub": "local",
            "input": {"name": "argus"},
        })
        assert inv.status_code == 200
        assert inv.json()["result"]["greeting"] == "Hello, argus!"

    def test_publish_rate_limit(self, hub_client, monkeypatch):
        client, manifest, _ = hub_client
        monkeypatch.delenv("AIMARKET_SUPPLY_SECURITY_RELAXED", raising=False)
        monkeypatch.setenv("AIMARKET_SUPPLY_MIN_STAKE_USD", "0")
        monkeypatch.setenv("AIMARKET_SUPPLY_PUBLISH_PER_HOUR", "1")
        monkeypatch.setenv("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "0")

        with tempfile.TemporaryDirectory() as tmp:
            config = HubConfig()
            config.db_path = str(Path(tmp) / "rate.db")
            config.signing_key_path = str(Path(tmp) / "key")
            db = HubDatabase(config.db_path)
            signer = Signer(config.signing_key_path)
            import aimarket_hub.api as api_mod
            app = create_app(config=config, db=db, signer=signer)
            with TestClient(app) as c:
                c.post("/ai-market/v2/supply/register", json=manifest, headers=PUBLISH_HEADERS)
                second = c.post(
                    "/ai-market/v2/supply/register",
                    json={**manifest, "capability_id": "secure.greet2@v1"},
                    headers=PUBLISH_HEADERS,
                )
                assert second.status_code == 400
                assert "rate limit" in second.json()["detail"]

    def test_search_hides_low_trust(self, hub_client):
        client, manifest, db = hub_client
        client.post("/ai-market/v2/supply/stake", json={"publisher_id": "pub-wallet-1", "amount_usd": 5}, headers=PUBLISH_HEADERS)
        client.post("/ai-market/v2/supply/register", json=manifest, headers=PUBLISH_HEADERS)
        db.supply_set_publisher_trust("pub-wallet-1", 0.1)
        search = client.get("/ai-market/v2/search", params={"intent": "secure", "min_trust": 0.25})
        ids = [m["capability_id"] for m in search.json()["matches"]]
        assert "secure.greet@v1" not in ids

# ── Verify-first wiring + DB-backed failure streak (production entry points) ──────


def test_create_app_wires_verify_first_escalation(tmp_path):
    """A cheap guard that dies if `verify_svc.attach_supply_security(...)` is dropped
    or reordered in create_app — otherwise the whole verified-failure slash ladder is
    silently dead in production while every unit test still passes."""
    config = HubConfig()
    config.db_path = str(tmp_path / "wire.db")
    config.signing_key_path = str(tmp_path / "wire_key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    with TestClient(app):
        assert app.state.verify_svc._supply_security is app.state.supply_security


class _FailingProviderClient:
    """Outbound client that answers the provider invoke_url with a 5xx."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *a, **k):
        if "127.0.0.1:3457" in url:
            r = _ProviderResp({"error": "provider exploded"})
            r.status_code = 500
            return r
        return _ProviderResp({"output": {}})


def test_invoke_5xx_streak_accumulates_and_slashes_over_http(tmp_path, monkeypatch):
    """End-to-end coverage of the PRODUCTION slash entry point: N provider 5xx invokes
    over HTTP must write supply_fault_events rows and fire one calibrated slash. Guards
    the api.py handler (>=500 branch, cap.publisher_id, record_invoke call) — a unit test
    on record_invoke alone cannot catch a regression there."""
    monkeypatch.setenv("AIMARKET_PUBLISH_TOKEN", PUBLISH_TOKEN)
    monkeypatch.setenv("AIMARKET_ALLOW_LOCAL_PUBLISH", "1")
    monkeypatch.setenv("AIMARKET_SUPPLY_SECURITY_RELAXED", "0")
    monkeypatch.setenv("AIMARKET_SUPPLY_MIN_STAKE_USD", "0")
    monkeypatch.setenv("AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG", "0")
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "0")
    monkeypatch.setenv("AIMARKET_SUPPLY_SLASH_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("AIMARKET_SUPPLY_SLASH_COOLDOWN_S", "0")
    monkeypatch.delenv("AIMARKET_SANDBOX_STUB_INVOKE", raising=False)

    import aimarket_hub.api as api_mod
    monkeypatch.setattr(api_mod.httpx, "AsyncClient", _FailingProviderClient)

    config = HubConfig()
    config.db_path = str(tmp_path / "streak.db")
    config.signing_key_path = str(tmp_path / "streak_key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    with TestClient(app) as client:
        client.post("/ai-market/v2/supply/stake",
                    json={"publisher_id": "pub-wallet-1", "amount_usd": 20.0, "tx_hash": "0xabc"},
                    headers=PUBLISH_HEADERS)
        client.post("/ai-market/v2/supply/register", json=MANIFEST, headers=PUBLISH_HEADERS)

        for _ in range(2):
            r = client.post("/ai-market/v2/invoke", json={
                "product_id": "sec-demo", "capability_id": "secure.greet@v1",
                "source_hub": "local", "input": {"name": "x"},
            })
            assert r.status_code == 502  # provider 5xx surfaces to the consumer

        assert db.supply_slash_events_recent("pub-wallet-1"), "2nd 5xx should have fired a slash"
        assert db.supply_stake_get("pub-wallet-1") < 20.0  # stake actually reduced

    # A 4xx must NOT be counted as a provider fault (boundary guard, both directions).
    assert db.supply_fault_count_recent("pub-wallet-1", "invoke_failure", 600) == 0  # cleared after slash
