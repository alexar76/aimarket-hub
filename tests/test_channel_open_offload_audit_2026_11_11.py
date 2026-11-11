"""Audit 2026-11-11, channels-6: /channel/open ran blocking chain RPC on the event loop."""
from __future__ import annotations

import asyncio

from tests._mandate_kit import hub


def _off_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return False
    except RuntimeError:
        return True


def test_channel_open_and_close_run_off_the_event_loop(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path, AIFACTORY_CRYPTO_ENABLED="1") as (client, db):
        from aimarket_hub.hub_routes import channels as routes

        seen: list[bool] = []
        monkeypatch.setattr(routes, "open_channel",
                            lambda **kw: seen.append(_off_loop()) or {"channel": {"channel_id": "ch_0123456789abcdef"}})
        monkeypatch.setattr(routes, "close_channel",
                            lambda **kw: seen.append(_off_loop()) or {"settlement": {"status": "settled"}})
        r = client.post("/ai-market/v2/channel/open", json={"deposit_usd": 1.0})
        assert r.status_code == 200, r.text
        r = client.post("/ai-market/v2/channel/close", json={"channel_id": "ch_0123456789abcdef"})
        assert r.status_code == 200, r.text
        assert seen == [True, True]
