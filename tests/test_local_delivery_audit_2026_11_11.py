"""Audit 2026-11-11, invoke-local-1: a call was captured before its answer was built.

A provider result holding NaN/Infinity made JSONResponse raise after the capture (the buyer paid,
the publisher was paid, the client got a 500 and paid again on retry); a non-object result did
the same in the provenance fallback on hubs without the provenance plugin.
"""
from __future__ import annotations

import json

import pytest

from tests._mandate_kit import balance, funded_account, hub, list_provider
from tests.test_subcontract import BRIEF, Providers


class _NaNResp:
    status_code = 200
    headers = {}
    text = '{"result": {"temp_c": NaN}}'

    def json(self):
        return json.loads(self.text)   # httpx parses NaN the same way


def test_a_nan_result_is_refused_before_anything_is_charged(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path) as (client, db):
        import aimarket_hub.outbound_http as outbound

        async def _post(url, **kw):
            return _NaNResp()

        monkeypatch.setattr(outbound, "safe_post", _post)
        list_provider(db, "brief.make@v1", "brief", 0.010, url="https://brief.test/invoke")
        account, api_key = funded_account(client, db, 1.0)
        for _ in range(2):
            r = client.post("/ai-market/v2/invoke", headers={"X-API-Key": api_key}, json=BRIEF)
            assert r.status_code == 502 and "NaN" in r.text
        assert balance(db, account) == pytest.approx(1.0)
        assert all(row["status"] == "released"
                   for row in db._conn.execute("SELECT status FROM credit_holds").fetchall())


@pytest.mark.parametrize("result", [["a", "b"], "plain text answer", None, 42])
def test_a_non_object_result_is_delivered_and_charged_once(monkeypatch, tmp_path, result):
    with hub(monkeypatch, tmp_path) as (client, db):
        providers = Providers(client.app_ref)
        import aimarket_hub.outbound_http as outbound

        monkeypatch.setattr(outbound, "safe_post", providers.post)
        list_provider(db, "brief.make@v1", "brief", 0.010, url="https://brief.test/invoke")

        async def brief(body, headers, p):
            return 200, {"result": result}

        providers.add("https://brief.test/invoke", brief)
        account, api_key = funded_account(client, db, 1.0)
        r = client.post("/ai-market/v2/invoke", headers={"X-API-Key": api_key}, json=BRIEF)
        assert r.status_code == 200, r.text
        assert balance(db, account) == pytest.approx(0.99)


def test_acex_revenue_goes_to_the_product_that_ran(monkeypatch, tmp_path):
    """invoke-local-5: the listing is resolved by capability id, so a caller naming another floated
    product_id credited THAT product's CapShares pool and audit rewards with this call's revenue."""
    with hub(monkeypatch, tmp_path) as (client, db):
        providers = Providers(client.app_ref)
        import aimarket_hub.outbound_http as outbound
        from aimarket_hub import acex_audit, acex_ipo

        monkeypatch.setattr(outbound, "safe_post", providers.post)
        list_provider(db, "brief.make@v1", "brief", 0.010, url="https://brief.test/invoke")

        async def brief(body, headers, p):
            return 200, {"result": {"text": "ok"}}

        providers.add("https://brief.test/invoke", brief)
        credited: list[tuple[str, str]] = []
        monkeypatch.setattr(acex_ipo, "accrue_revenue",
                            lambda product, usd: credited.append(("pool", product)) or {"ok": False})
        monkeypatch.setattr(acex_audit, "accrue_audit_rewards",
                            lambda product, usd: credited.append(("audit", product)) or {"ok": False})
        account, api_key = funded_account(client, db, 1.0)
        r = client.post("/ai-market/v2/invoke", headers={"X-API-Key": api_key},
                        json={**BRIEF, "product_id": "someone-elses-floated-product"})
        assert r.status_code == 200, r.text
        assert credited == [("pool", "brief"), ("audit", "brief")]
        stat = db._conn.execute("SELECT product_id FROM invocation_stats").fetchall()
        assert [row["product_id"] for row in stat] == ["brief"]
