"""Hub Prometheus metrics tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import metrics as m
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMARKET_SKIP_SEED", "1")
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_SANDBOX_ENABLED", "1")
    root = tmp_path / "hub"
    root.mkdir()
    db = HubDatabase(root / "hub.db")
    db.upsert_capability(
        Capability(
            capability_id="meter.test@v1",
            product_id="prod-meter",
            name="meter.test",
            version="v1",
            description="priced stub for metrics tests",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            price_per_call_usd=0.02,
            source_hub="local",
            source_hub_name="test",
        )
    )
    cfg = HubConfig()
    cfg.db_path = str(root / "hub.db")
    cfg.signing_key_path = str(root / "key")
    app = create_app(config=cfg, db=db, signer=Signer(root / "key"))
    with TestClient(app) as c:
        yield c


def test_metrics_endpoint_exposes_hub_up(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "aimarket_hub_up" in body
    assert "aimarket_hub_invokes_total" in body


def test_record_invoke_and_payment_required():
    m.record_invoke("platon.oracle@v1", "payment_required", duration_s=0.01)
    after = m.metrics_payload()[0].decode()
    assert "aimarket_hub_payment_required_total" in after
    assert 'capability="platon.oracle@v1"' in after
    assert "payment_required" in after


def test_invoke_402_increments_metrics(client):
    r = client.post(
        "/ai-market/v2/invoke",
        json={
            "capability_id": "meter.test@v1",
            "product_id": "prod-meter",
            "source_hub": "local",
            "input": {"q": "ping"},
        },
    )
    assert r.status_code == 402, r.text
    after = client.get("/metrics").text
    assert "payment_required" in after
    assert "meter.test@v1" in after


def test_track_invoke_context_manager():
    with m.track_invoke("demo.cap@v1") as slot:
        slot["result"] = "ok"
    body = m.metrics_payload()[0].decode()
    assert "demo.cap@v1" in body
