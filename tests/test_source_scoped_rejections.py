"""A spoofed relay listing can be removed without rejecting its genuine origin."""
import json

from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.peer_rejections import capability_is_rejected


def test_same_tool_from_genuine_origin_remains_allowed(monkeypatch, tmp_path):
    path = tmp_path / 'peer-rejections.json'
    path.write_text(json.dumps({'version': 1, 'peer_urls': [], 'invoke_urls': [],
        'capability_keys': [], 'capability_sources': [{'product_id': 'hestia',
        'capability_id': 'hestia.hearth.list@v1', 'source_hub': 'https://hub.modelmarket.dev'}]}))
    monkeypatch.setenv('AIMARKET_PEER_REJECTIONS_FILE', str(path))
    key = ('hestia', 'hestia.hearth.list@v1')
    assert capability_is_rejected(*key, source_hub='https://HUB.MODELMARKET.DEV:443/')
    assert not capability_is_rejected(*key, source_hub='https://hestia.modelmarket.dev')
    assert not capability_is_rejected(*key, source_hub='local')
    db = HubDatabase(tmp_path / 'hub.db')
    try:
        for origin in ('https://hub.modelmarket.dev', 'https://hestia.modelmarket.dev'):
            db.upsert_capability(Capability(product_id=key[0], capability_id=key[1],
                                           name='hearth', source_hub=origin))
        assert db.get_capability(*key, source_hub='https://hub.modelmarket.dev') is None
        assert db.get_capability(*key, source_hub='https://hestia.modelmarket.dev') is not None
    finally:
        db.close()
