"""Operator-only endpoint authority must not leak through public catalogues."""
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer


def test_private_authority_is_hidden_from_search_and_manifest(monkeypatch, tmp_path):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('AIMARKET_ORACLE_FAMILY_URL', 'off')
    monkeypatch.setenv('AIMARKET_AUTO_CRAWL', '0')
    monkeypatch.setenv('AIMARKET_UPDATE_CHECK', '0')
    monkeypatch.setenv('AIMARKET_SKIP_SEED', '1')
    monkeypatch.setenv('AIMARKET_INVOKE_HOST_GATEWAY', 'operator-private')
    config = HubConfig()
    config.db_path = str(tmp_path / 'hub.db')
    config.signing_key_path = str(tmp_path / 'key')
    config.hub_url = 'https://public-hub.example'
    db = HubDatabase(config.db_path)
    private = 'http://operator-private:9123/private/invoke'
    db.upsert_capability(Capability(product_id='grant-fixture', capability_id='grant.test@v1',
        name='grant fixture', invoke_url=private, trust_score=1, price_per_call_usd=0,
        catalogue_evidence={'operator_invoke_url': private}))
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    try:
        with TestClient(app) as client:
            for route in ('/ai-market/v2/search', '/ai-market/v2/manifest'):
                response = client.get(route)
                assert response.status_code == 200
                assert 'grant-fixture' in response.text
                assert private not in response.text
                assert 'operator_invoke_url' not in response.text
    finally:
        db.close()
