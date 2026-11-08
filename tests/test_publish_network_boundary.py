"""Community registration must not borrow the hub's private provider authority."""
from contextlib import contextmanager
import asyncio
import socket
import threading

from fastapi.testclient import TestClient
import httpx
import pytest

from aimarket_hub.api import create_app
from aimarket_hub import provider_recovery, factory_bridge, factory_products_loader
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.publish import private_invoke_authorized
from aimarket_hub.signing import Signer

ADMIN = {"Authorization": "Bearer local-admin-test"}
PRIVATE = "http://operator-provider:9123/arbitrary-private-path"
PUBLIC = "https://public-provider.example/invoke"


def manifest(url=PRIVATE, publisher_id=""):
    return {"product_id": "network-test", "capability_id": "network.test@v1",
            "invoke_url": url, "price_per_call_usd": 0.0, "publisher_id": publisher_id}


def invoke(client, headers):
    return client.post("/ai-market/v2/invoke", headers=headers, json={
        "product_id": "network-test", "capability_id": "network.test@v1",
        "source_hub": "local", "input": {"canary": "offline"},
    })


@pytest.fixture
def hub(monkeypatch, tmp_path):
    for name, value in {
        "AIMARKET_ADMIN_TOKEN": "local-admin-test", "AIMARKET_CREDITS_ENABLED": "1",
        "AIMARKET_CREDITS_FREE_GRANT_USD": "0", "AIMARKET_ORACLE_FAMILY_URL": "off",
        "AIMARKET_SUPPLY_MIN_STAKE_USD": "0", "AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG": "0",
        "AIMARKET_SUPPLY_OPERATOR_PUBLISHERS": "operator-seller",
        "AIMARKET_PUBLISHER_TOKENS": "operator-seller:operator-key,community-seller:community-key",
        "AIMARKET_SUPPLY_PRODUCT_ALLOWLIST": "", "AIMARKET_SUPPLY_CHAIN_ADMISSION_MODE": "off",
        "AIMARKET_INVOKE_HOST_GATEWAY": "operator-provider", "AIMARKET_ALLOW_LOCAL_PUBLISH": "1",
        "AIMARKET_CAPABILITY_TOKEN": "fake-internal-token", "AIFACTORY_CRYPTO_ENABLED": "0",
    }.items():
        monkeypatch.setenv(name, value)

    def dns(host, port=None, *args, **kwargs):
        address = "8.8.8.8" if host == "public-provider.example" else "10.9.0.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "result": {"canary": "mock-only"}})

    original_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(provider))
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)

    @contextmanager
    def build():
        config = HubConfig()
        config.db_path = str(tmp_path / "hub.db")
        config.signing_key_path = str(tmp_path / "key")
        db = HubDatabase(config.db_path)
        app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
        with TestClient(app) as client:
            signup = client.post("/ai-market/v2/accounts", json={"label": "community"}).json()
            yield client, db, requests, {"X-API-Key": signup["api_key"]}, app

    return build


@pytest.mark.parametrize("url", [PRIVATE, "http://127.0.0.1:9123/private", "http://[::1]:9123/private"])
def test_credit_publisher_cannot_select_private_provider(hub, url):
    with hub() as (client, db, requests, headers, _):
        registered = client.post("/ai-market/v2/supply/register", headers=headers, json=manifest(url))
        assert registered.status_code == 400, registered.text
        assert db.get_capability("network-test", "network.test@v1", "local") is None
        assert invoke(client, headers).status_code == 404
        assert requests == []


def test_scoped_community_publisher_cannot_select_gateway(hub):
    with hub() as (client, _, requests, _, _):
        response = client.post("/ai-market/v2/supply/register",
                               headers={"Authorization": "Bearer community-key"},
                               json=manifest(publisher_id="community-seller"))
        assert response.status_code == 400, response.text
        assert requests == []


@pytest.mark.parametrize("admin", [True, False])
def test_explicit_operator_registration_retains_private_delivery(hub, admin):
    with hub() as (client, db, requests, buyer, _):
        headers = ADMIN if admin else {"Authorization": "Bearer operator-key"}
        response = client.post("/ai-market/v2/supply/register", headers=headers,
                               json=manifest(publisher_id="operator-seller"))
        assert response.status_code == 200, response.text
        cap = db.get_capability("network-test", "network.test@v1", "local")
        assert cap.catalogue_evidence["operator_invoke_url"] == PRIVATE
        assert invoke(client, buyer).status_code == 200
        assert len(requests) == 1
        assert str(requests[0].url) == PRIVATE
        assert requests[0].headers["X-AIMarket-Internal-Token"] == "fake-internal-token"


def test_admin_can_authorize_exact_url_without_operator_publisher_membership(hub):
    with hub() as (client, db, requests, buyer, _):
        response = client.post("/ai-market/v2/supply/register", headers=ADMIN,
                               json=manifest(publisher_id="admin-managed"))
        assert response.status_code == 200, response.text
        cap = db.get_capability("network-test", "network.test@v1", "local")
        assert cap.catalogue_evidence == {"operator_invoke_url": PRIVATE}
        assert invoke(client, buyer).status_code == 200
        assert requests[0].headers["X-AIMarket-Internal-Token"] == "fake-internal-token"


@pytest.mark.parametrize("evidence", [{}, {"operator_invoke_url": "http://operator-provider/other"}])
@pytest.mark.parametrize("publisher", ["old-community", "operator-seller"])
def test_legacy_community_private_row_is_blocked_before_http(hub, evidence, publisher):
    with hub() as (client, db, requests, headers, _):
        db.upsert_capability(Capability(product_id="network-test", capability_id="network.test@v1",
                                       name="legacy", price_per_call_usd=0, publisher_id=publisher,
                                       invoke_url=PRIVATE, trust_score=1, catalogue_evidence=evidence))
        response = invoke(client, headers)
        assert response.status_code == 502, response.text
        assert requests == []


def test_untrusted_manifest_cannot_forge_operator_grant_and_public_still_works(hub):
    with hub() as (client, db, requests, headers, _):
        forged = {**manifest(PUBLIC), "catalogue_evidence": {"operator_invoke_url": PRIVATE},
                  "operator_invoke_url": PRIVATE, "operator": True}
        response = client.post("/ai-market/v2/supply/register", headers=headers, json=forged)
        assert response.status_code == 200, response.text
        cap = db.get_capability("network-test", "network.test@v1", "local")
        assert "operator_invoke_url" not in cap.catalogue_evidence
        assert invoke(client, headers).status_code == 200
        assert requests[0].url.host == "8.8.8.8"
        assert requests[0].headers["Host"] == "public-provider.example"
        assert "X-AIMarket-Internal-Token" not in requests[0].headers
        # Even an old row or a later URL edit cannot inherit a submitted grant.
        cap.invoke_url = PRIVATE
        db.upsert_capability(cap)
        assert invoke(client, headers).status_code == 502
        assert len(requests) == 1


def test_peer_metadata_never_authorizes_local_transport():
    cap = Capability(product_id="p", capability_id="c@v1", name="n", source_hub="https://peer.example",
                     invoke_url=PRIVATE, publisher_id="operator-seller",
                     catalogue_evidence={"operator_invoke_url": PRIVATE})
    assert not private_invoke_authorized(cap)


def test_gateway_hostname_is_not_enough_to_receive_private_token(hub, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port=None, **kw:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 80))])
    with hub() as (client, _, requests, headers, _):
        response = client.post("/ai-market/v2/supply/register", headers=headers, json=manifest())
        assert response.status_code == 200, response.text
        assert invoke(client, headers).status_code == 200
        assert requests[0].url.host == "8.8.8.8"
        assert "X-AIMarket-Internal-Token" not in requests[0].headers


def test_credit_account_cannot_claim_explicit_operator_identity(hub):
    with hub() as (client, _, requests, headers, _):
        response = client.post("/ai-market/v2/supply/register", headers=headers,
                               json=manifest(publisher_id="operator-seller"))
        assert response.status_code == 403
        assert requests == []


def test_shared_dev_token_claiming_operator_name_never_gains_private_authority(hub, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "0")
    monkeypatch.setenv("AIMARKET_PUBLISH_TOKEN", "shared-dev-key")
    monkeypatch.setenv("AIMARKET_PUBLISHER_TOKENS", "")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port=None, **kw:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port or 80))])
    with hub() as (client, db, requests, buyer, _):
        response = client.post("/ai-market/v2/supply/register",
                               headers={"Authorization": "Bearer shared-dev-key"},
                               json=manifest(publisher_id="operator-seller"))
        assert response.status_code == 200, response.text
        cap = db.get_capability("network-test", "network.test@v1", "local")
        assert cap.publisher_id == "operator-seller"
        assert not private_invoke_authorized(cap)
        assert invoke(client, buyer).status_code == 200
        assert requests[0].url.host == "8.8.8.8"
        assert "X-AIMarket-Internal-Token" not in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, False, "true"])
async def test_legacy_recovery_spec_cannot_access_gateway(hub, flag):
    with hub() as (_, _, requests, _, _):
        spec = {"invoke_url": PRIVATE}
        if flag is not None:
            spec["allow_private"] = flag
        with pytest.raises(ValueError, match="unsafe outbound URL"):
            await provider_recovery.request_status(spec, "op_test")
        assert requests == []


@pytest.mark.asyncio
async def test_trusted_recovery_descriptor_retains_gateway_and_internal_token(hub, monkeypatch):
    monkeypatch.setenv("AIMARKET_PROVIDER_OPERATION_PRODUCTS", "network-test")
    with hub() as (_, _, requests, _, _):
        cap = Capability(product_id="network-test", capability_id="network.test@v1", name="operator",
                         invoke_url=PRIVATE, provider_pubkey="public-key", publisher_id="operator-seller",
                         catalogue_evidence={"operator_invoke_url": PRIVATE})
        descriptor = provider_recovery.descriptor(cap)
        assert descriptor["allow_private"] is True
        await provider_recovery.request_status(descriptor, "op_test")
        assert str(requests[0].url) == PRIVATE + "/operations/op_test"
        assert requests[0].headers["X-AIMarket-Internal-Token"] == "fake-internal-token"


def test_recovery_authority_does_not_come_from_public_request(hub, monkeypatch):
    monkeypatch.setenv("AIMARKET_PROVIDER_OPERATION_PRODUCTS", "network-test")
    with hub() as (client, _, requests, _, _):
        response = client.post("/ai-market/v2/operations/prepare", json={
            "product_id": "network-test", "capability_id": "network.test@v1", "max_price_usd": 1,
            "allow_private": True, "provider_recovery": {"invoke_url": PRIVATE, "allow_private": True},
        })
        assert response.status_code == 422
        cap = Capability(product_id="network-test", capability_id="network.test@v1", name="community",
                         invoke_url=PRIVATE, provider_pubkey="public-key", publisher_id="community")
        assert provider_recovery.descriptor(cap)["allow_private"] is False
        assert requests == []


@pytest.mark.parametrize("previous_url,grant,expected", [
    (PRIVATE, PRIVATE, True), (PRIVATE + "/old", PRIVATE + "/old", False),
    (PRIVATE, "http://operator-provider/elsewhere", False), (None, None, False),
])
def test_factory_import_preserves_only_existing_exact_operator_grant(tmp_path, monkeypatch,
                                                                    previous_url, grant, expected):
    db = HubDatabase(tmp_path / "factory.db")
    if previous_url:
        db.upsert_capability(Capability(product_id="network-test", capability_id="network.test@v1", name="old",
                                       invoke_url=previous_url,
                                       catalogue_evidence={"operator_invoke_url": grant}))
    row = {"capability_id": "network.test@v1", "product_id": "network-test", "name": "imported",
           "version": "v1", "description": "d", "input_schema": {}, "output_schema": {},
           "price_per_call_usd": 0, "p50_latency_ms": 1, "success_rate_30d": 1,
           "invoke_url": PRIVATE, "catalogue_evidence": {"operator_invoke_url": PRIVATE}}
    monkeypatch.setattr(factory_products_loader, "iter_shipped_factory_products",
                        lambda path: {"network-test": {"catalogue_evidence": {"operator_invoke_url": PRIVATE}}})
    monkeypatch.setattr(factory_bridge, "_capability_rows_for_product", lambda pid, data: [row])
    assert factory_bridge.import_factory_products(db) == 1
    cap = db.get_capability("network-test", "network.test@v1", "local")
    assert private_invoke_authorized(cap) is expected
    assert cap.catalogue_evidence == ({"operator_invoke_url": PRIVATE} if expected else {})


@pytest.mark.asyncio
async def test_slow_publish_dns_does_not_block_other_requests(hub, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked_dns(*args, **kwargs):
        entered.set()
        assert release.wait(2), "test must release its fake resolver"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    with hub() as (_, _, requests, headers, app):
        monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:
            task = asyncio.create_task(client.post("/ai-market/v2/supply/register", headers=headers,
                                                   json=manifest(PUBLIC)))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.001)
                assert entered.is_set()
                response = await asyncio.wait_for(client.get("/ai-market/v2/supply/policy"), 0.2)
                assert response.status_code == 200
                assert not task.done()
            finally:
                release.set()
                response = await task
            assert response.status_code == 200, response.text
        assert requests == []
