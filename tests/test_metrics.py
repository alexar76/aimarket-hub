"""Hub Prometheus metrics tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import metrics as m
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
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
            # A provider endpoint, so the row is *executable* and the paywall is the
            # first thing an unpaid call meets. Without it the invoke handler now
            # answers 404 before the 402 — a hub must not demand payment for something
            # it has no way to run — and this test would be measuring that instead of
            # the metric it is about. Nothing is contacted: 402 comes first either way.
            invoke_url="https://provider.example.com/invoke",
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


# ── Federation peer health ──────────────────────────────────────────────────
#
# A rejected key pin freezes a peer's catalogue while the hub keeps serving
# normally, so nothing else goes red. These gauges exist so an alert can see it
# within minutes instead of the five days it actually took.


@pytest.fixture()
def client_and_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMARKET_SKIP_SEED", "1")
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    root = tmp_path / "fedhub"
    root.mkdir()
    db = HubDatabase(root / "hub.db")
    cfg = HubConfig()
    cfg.db_path = str(root / "hub.db")
    cfg.signing_key_path = str(root / "key")
    app = create_app(config=cfg, db=db, signer=Signer(root / "key"))
    with TestClient(app) as c:
        yield c, db


def _gauge(body: str, name: str) -> float:
    for line in body.splitlines():
        if line.startswith(name + " "):
            return float(line.split(" ", 1)[1])
    raise AssertionError(f"gauge {name} not found in /metrics")


def test_no_peers_reports_no_rejections_and_no_crawl_age(client_and_db):
    client, _db = client_and_db
    body = client.get("/metrics").text
    assert _gauge(body, "aimarket_hub_federation_peers_rejected") == 0
    # Sentinel, not zero: "never crawled" must not read as "crawled just now".
    assert _gauge(body, "aimarket_hub_federation_peer_stalest_crawl_seconds") == -1


def test_a_rejected_pin_is_counted(client_and_db):
    client, db = client_and_db
    db.upsert_peer(Peer(
        url="https://atlas.example.com", name="ATLAS",
        public_key="b2xkX2tleV9vbGRfb2xkX29sZA==", trusted=True,
        last_crawl="2026-09-01T00:00:00Z",
    ))
    db.upsert_peer(Peer(
        url="https://fine.example.com", name="Fine",
        public_key="ZmluZV9rZXlfZmluZV9maW5lXw==", trusted=True,
        last_crawl="2026-09-01T00:00:00Z",
    ))
    assert _gauge(
        client.get("/metrics").text, "aimarket_hub_federation_peers_rejected"
    ) == 0

    db.record_peer_key_mismatch(
        "https://atlas.example.com",
        pinned_key="b2xkX2tleV9vbGRfb2xkX29sZA==",
        advertised_key="bmV3X2tleV9uZXdfbmV3X25ldw==",
    )
    assert _gauge(
        client.get("/metrics").text, "aimarket_hub_federation_peers_rejected"
    ) == 1


def test_stalest_crawl_age_tracks_the_oldest_peer(client_and_db):
    client, db = client_and_db
    db.upsert_peer(Peer(
        url="https://fresh.example.com", name="Fresh",
        public_key="ZnJlc2hfa2V5X2ZyZXNoX2ZyZXNo", trusted=True,
        last_crawl=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ))
    db.upsert_peer(Peer(
        url="https://frozen.example.com", name="Frozen",
        public_key="ZnJvemVuX2tleV9mcm96ZW5fXw==", trusted=True,
        last_crawl=(
            datetime.now(timezone.utc) - timedelta(days=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ))

    age = _gauge(
        client.get("/metrics").text,
        "aimarket_hub_federation_peer_stalest_crawl_seconds",
    )
    assert 4.5 * 86400 < age < 5.5 * 86400


def test_an_unparseable_timestamp_does_not_break_the_scrape(client_and_db):
    client, db = client_and_db
    db.upsert_peer(Peer(
        url="https://odd.example.com", name="Odd",
        public_key="b2RkX2tleV9vZGRfb2RkX29kZA==", trusted=True,
        last_crawl="not-a-timestamp",
    ))
    r = client.get("/metrics")
    assert r.status_code == 200
    assert _gauge(r.text, "aimarket_hub_federation_peer_stalest_crawl_seconds") == -1
