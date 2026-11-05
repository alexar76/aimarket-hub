"""The public manifest never shows an operator gateway provider's private address (supplier review, 2026-11-04)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.hub_routes.catalog import published_invoke_url
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer

HUB = "https://hub.example"
VIA_HUB = HUB + "/ai-market/v2/invoke"


def cap(url, source="local"):
    return SimpleNamespace(invoke_url=url, source_hub=source)


@pytest.mark.parametrize("url, shown", [
    ("http://aicom-diagnostics:8124/diagnose", VIA_HUB),        # gateway host and a dotless container name
    ("http://memory-market:8810/capabilities/x/invoke", VIA_HUB),
    ("http://localhost:9000/invoke", VIA_HUB),
    ("http://127.0.0.1:9000/invoke", VIA_HUB),
    ("http://172.17.0.1:8000/invoke", VIA_HUB),
    ("http://10.1.2.3/invoke", VIA_HUB),
    ("http://[::1]:8000/invoke", VIA_HUB),
    ("http://gw.internal.example/invoke", VIA_HUB),            # named in AIMARKET_INVOKE_HOST_GATEWAY
    ("https://seller.example.com/invoke", "https://seller.example.com/invoke"),
    ("https://8.8.8.8/invoke", "https://8.8.8.8/invoke"),
    ("", None),
])
def test_local_rows(monkeypatch, url, shown):
    monkeypatch.setenv("AIMARKET_INVOKE_HOST_GATEWAY", "aicom-diagnostics, GW.internal.example")
    assert published_invoke_url(cap(url), HUB + "/") == shown


def test_federated_rows_keep_the_hub_address(monkeypatch):
    monkeypatch.delenv("AIMARKET_INVOKE_HOST_GATEWAY", raising=False)
    assert published_invoke_url(cap(None, "https://peer.example"), HUB) == VIA_HUB
    assert published_invoke_url(cap("https://peer.example/x", "https://peer.example"), HUB) == "https://peer.example/x"


def test_manifest_shows_the_hub_for_a_gateway_provider_and_still_stores_its_address(monkeypatch, tmp_path):
    for name in ("AIMARKET_AUTO_CRAWL", "AIMARKET_FEDERATION_ASSAY"):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("AIMARKET_INVOKE_HOST_GATEWAY", "aicom-diagnostics")
    config = HubConfig()
    config.hub_url = HUB
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    db = HubDatabase(config.db_path)
    try:
        internal = "http://aicom-diagnostics:8124/diagnose"
        db.upsert_capability(Capability(capability_id="aicom.diagnose@v1", product_id="aicom-diagnostics",
                                        name="AICOM code diagnostics", price_per_call_usd=0.1, trust_score=0.5,
                                        invoke_url=internal))
        db.upsert_capability(Capability(capability_id="public@v1", product_id="public", name="Public",
                                        price_per_call_usd=0.1, trust_score=0.5,
                                        invoke_url="https://seller.example.com/invoke"))
        with TestClient(create_app(config=config, db=db, signer=Signer(config.signing_key_path))) as client:
            body = client.get("/ai-market/v2/manifest").text
        assert "aicom-diagnostics:8124" not in body
        import json
        tools = {t["capability_id"]: t for t in json.loads(body)["tools"]}
        assert tools["aicom.diagnose@v1"]["invoke_url"] == VIA_HUB
        assert tools["public@v1"]["invoke_url"] == "https://seller.example.com/invoke"
        assert db.get_capability("aicom-diagnostics", "aicom.diagnose@v1").invoke_url == internal, "routing unchanged"
    finally:
        db.close()
