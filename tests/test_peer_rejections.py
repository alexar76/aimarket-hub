"""A deleted probe cannot return through announce, gossip, or provider registration."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import realm
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.crawler import Crawler
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
from aimarket_hub.peer_rejections import invoke_is_rejected, peer_is_rejected
from aimarket_hub.signing import Signer

BAD = "http://147.124.215.108:18097"
FAKE = "http://147.124.215.108:19093/redir"
GOOD = "https://oracles.modelmarket.dev/family"


def test_gossip_url_storage_and_frontier_have_a_finite_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("AIMARKET_PEER_REJECTIONS_FILE", raising=False)
    monkeypatch.setenv("AIMARKET_DB_PATH", str(tmp_path / "hub.db"))
    huge = "https://public.example/" + "a" * 4096
    assert peer_is_rejected(huge)
    assert not realm.peer_inside(huge)
    db = HubDatabase(tmp_path / "hub.db")
    try:
        assert db.announce_peer(Peer(url=huge, name="oversized gossip"), max_pending=2000) == "rejected_policy"
        assert db.count_pending_peers() == 0
    finally:
        db.close()
ALIASES = (
    BAD,
    "http://ignored-user@147.124.215.108:18097",
    "http://ignored-user:ignored-pass@147.124.215.108:18097",
    BAD + "?alias=1",
    "http://2474432364:18097",
    "http://0x937cd76c:18097",
    "http://0223.0174.0327.0154:18097",
    "http://147.8181612:18097",
    "http://[::ffff:147.124.215.108]:18097",
    "http://[0:0:0:0:0:ffff:937c:d76c]:18097",
    "http://１４７．１２４．２１５．１０８:18097",
    "http://147.124.215.108.:018097",
)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / "peer-rejections.json"
    path.write_text(json.dumps({"version": 1, "peer_urls": [BAD], "invoke_urls": [FAKE],
                               "capability_keys": [{"product_id": "prodplatonx", "capability_id": "platon.random@v1"}]}))
    monkeypatch.setenv("AIMARKET_PEER_REJECTIONS_FILE", str(path))
    monkeypatch.setenv("AIMARKET_CHAIN_REALM", "live")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    return path


def test_missing_default_allows_but_broken_selected_policy_blocks(tmp_path, monkeypatch):
    monkeypatch.delenv("AIMARKET_PEER_REJECTIONS_FILE", raising=False)
    monkeypatch.setenv("AIMARKET_DB_PATH", str(tmp_path / "hub.db"))
    assert not peer_is_rejected(GOOD)
    path = tmp_path / "selected.json"
    monkeypatch.setenv("AIMARKET_PEER_REJECTIONS_FILE", str(path))
    assert peer_is_rejected(GOOD)
    path.write_text("{")
    assert peer_is_rejected(GOOD)
    path.unlink()
    path.mkdir()
    assert peer_is_rejected(GOOD)


def test_exact_url_and_discovery_descendants_preserve_other_apps(policy):
    for alias in ALIASES:
        assert peer_is_rejected(alias), alias
        assert peer_is_rejected(alias + "/.well-known/ai-market.json"), alias
    assert peer_is_rejected(BAD + "/")
    assert peer_is_rejected(BAD + "/./")
    assert peer_is_rejected(BAD + "/%2e/")
    assert peer_is_rejected(BAD + "/genuine/../")
    assert peer_is_rejected(BAD + "/.well-known/ai-market.json")
    assert peer_is_rejected(BAD + "/.well-known/aimarket/hub.json")
    assert peer_is_rejected(BAD + "/.well-known/agent-marketplace")
    assert peer_is_rejected(BAD + "/ai-market/manifest")
    assert peer_is_rejected(BAD + "/ai-market/./manifest")
    assert peer_is_rejected(BAD + "/ai-market/v2/%2e/manifest")
    assert peer_is_rejected(BAD + "/ai-market/manifest?alias=1")
    assert peer_is_rejected(BAD + "/.well-known/agent-marketplace?alias=1")
    assert peer_is_rejected(BAD + "//ai-market///manifest")
    assert not peer_is_rejected("http://147.124.215.108:18098")
    assert not peer_is_rejected(BAD + "/genuine-app")
    assert not peer_is_rejected(GOOD)
    assert not realm.peer_inside(BAD)
    assert realm.peer_inside(GOOD)
    assert invoke_is_rejected(FAKE)
    assert invoke_is_rejected("http://147.124.215.108:19093/./redir")
    assert invoke_is_rejected("http://147.124.215.108:19093/%72edir")
    assert invoke_is_rejected("http://147.124.215.108:19093/app/../redir")
    assert invoke_is_rejected("http://ignored-user@147.124.215.108:19093/redir")
    assert invoke_is_rejected("http://2474432364:19093/redir")
    assert invoke_is_rejected("http://[::ffff:147.124.215.108]:19093/redir?alias=1")
    assert invoke_is_rejected("http://147.124.215.108:19093//redir")
    assert not invoke_is_rejected(FAKE + "-genuine")
    assert not invoke_is_rejected("http://147.124.215.108:19093/app%2Fredir")


def test_updated_policy_is_applied_without_restarting(policy):
    assert peer_is_rejected(BAD)
    policy.write_text(json.dumps({"version": 1, "peer_urls": [], "invoke_urls": []}))
    assert not peer_is_rejected(BAD)


def test_query_specific_redirect_tombstone_stays_scoped(policy):
    redirect = "https://redirect.example.test/to?target=http://127.0.0.1:9001/"
    policy.write_text(json.dumps({"version": 1, "peer_urls": [redirect], "invoke_urls": [redirect]}))
    assert peer_is_rejected(redirect)
    assert invoke_is_rejected(redirect)
    other = "https://redirect.example.test/to?target=https://genuine.example.test/"
    assert not peer_is_rejected(other)
    assert not invoke_is_rejected(other)


def test_literal_midpath_slash_aliases_preserve_other_apps_and_reserved_delimiters(policy):
    policy.write_text(json.dumps({"version": 1, "peer_urls": [],
                                 "invoke_urls": ["https://provider.example.test/app/redir"]}))
    assert invoke_is_rejected("https://provider.example.test/app//redir")
    assert invoke_is_rejected("https://provider.example.test//app///redir?alias=1")
    assert not invoke_is_rejected("https://provider.example.test/app%2Fredir")
    assert not invoke_is_rejected("https://provider.example.test/genuine-app/redir")
    assert not invoke_is_rejected("https://provider.example.test:9443/app/redir")


def test_database_rejects_peer_and_fake_registration_but_preserves_real_oracle(policy, tmp_path):
    db = HubDatabase(tmp_path / "hub.db")
    bad = Peer(url=BAD, name="fake")
    assert db.announce_peer(bad, max_pending=2000) == "rejected_policy"
    db.upsert_peer(bad)
    assert db.get_peer(BAD) is None
    for alias in ALIASES:
        assert db.announce_peer(Peer(url=alias, name="blocked alias"), max_pending=2000) == "rejected_policy"
        db.upsert_peer(Peer(url=alias, name="blocked alias"))
        assert db.get_peer(alias) is None
    # An innocent URL may not provide a rejected discovery address as a second door.
    db.upsert_peer(Peer(url=GOOD, name="alias", well_known_url=BAD + "/.well-known/ai-market.json"))
    assert db.get_peer(GOOD) is None
    db.upsert_capability(Capability(product_id="other", capability_id="probe@v1",
                                    name="fake", source_hub=BAD))
    db.upsert_capability(Capability(product_id="other", capability_id="probe@v1",
                                    name="fake local", source_hub="local", invoke_url=FAKE))
    db.upsert_capability(Capability(product_id="other", capability_id="probe@v1",
                                    name="fake endpoint", source_hub=GOOD, peer_endpoint=FAKE))
    db.upsert_capability(Capability(product_id="prodplatonx", capability_id="platon.random@v1",
                                    name="fake relay", source_hub=GOOD, peer_endpoint=FAKE))
    db.upsert_capability(Capability(product_id="prod-platon", capability_id="platon.random@v1",
                                    name="real oracle", source_hub=GOOD))
    assert db.list_capabilities(source_hub=BAD) == []
    assert db.list_capabilities(source_hub="local") == []
    assert db.get_capability("prodplatonx", "platon.random@v1", GOOD) is None
    assert db.get_capability("other", "probe@v1", GOOD) is None
    assert db.get_capability("prod-platon", "platon.random@v1", GOOD) is not None
    db.close()


def test_open_and_admin_reannouncement_refused_before_assay(policy, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", "test-token")
    monkeypatch.setenv("AIMARKET_HUB_URL", "https://hub.example.test")
    db = HubDatabase(tmp_path / "hub.db")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(tmp_path / "key"))
    with TestClient(app) as client:
        for headers in ({}, {"Authorization": "Bearer test-token"}):
            for alias in ALIASES:
                result = client.post("/ai-market/v2/federation/announce", json={"hub_url": alias}, headers=headers)
                assert result.status_code == 400, alias
        assert db.get_peer(BAD) is None


def test_gossip_and_pending_frontier_cannot_dial_or_restore_rejected_peer(policy, tmp_path, monkeypatch):
    import aimarket_hub.crawler as crawler_module
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda u: u.startswith(("https://", "http://")))
    db = HubDatabase(tmp_path / "hub.db")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.hub_url = "https://this.example.test"
    config.seed_list = [GOOD + "/.well-known/ai-market.json"]
    config.federation_assay = False
    crawler = Crawler(config=config, db=db, signer=Signer(tmp_path / "key"))
    dialled = []

    async def fake_crawl(url, depth, discoverer):
        dialled.append(url)
        return {"capabilities_count": 0, "new_peer_urls": list(ALIASES)}

    crawler._crawl_one = fake_crawl

    async def run():
        try:
            await crawler.crawl()
        finally:
            await crawler.close()

    asyncio.run(run())
    assert dialled == [GOOD + "/.well-known/ai-market.json"]
    assert db.get_peer(BAD) is None
    assert db.list_pending_peers() == []
    db.close()


def test_pending_row_ceiling_covers_many_paths_on_one_host_and_keeps_known_idempotent(policy, tmp_path):
    db = HubDatabase(tmp_path / "hub.db")
    outcomes = [db.announce_peer(Peer(url=f"https://new-peer.example.test/app{i}", name="test"),
                                 max_pending=2) for i in range(100)]
    assert outcomes.count("added") == 2
    assert outcomes.count("rejected_cap") == 98
    assert db.count_pending_peers() == 2
    assert db.count_pending_hosts() == 1
    assert db.announce_peer(Peer(url="https://new-peer.example.test/app0", name="changed"),
                            max_pending=2) == "known"
    assert db.get_peer("https://new-peer.example.test/app0").name == "test"
    db.close()


def test_public_announce_returns_429_when_pending_rows_full(policy, tmp_path, monkeypatch):
    import aimarket_hub.crawler as crawler_module
    monkeypatch.setattr(crawler_module, "_url_is_safe", lambda u: u.startswith(("http://", "https://")))
    monkeypatch.setenv("AIMARKET_FEDERATION_OPEN_MAX_PENDING", "2")
    monkeypatch.setenv("AIMARKET_HUB_URL", "https://this.example.test")
    db = HubDatabase(tmp_path / "hub.db")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(tmp_path / "key"))
    with TestClient(app) as client:
        first = client.post("/ai-market/v2/federation/announce", json={"hub_url": "https://new-peer.example.test/app0"})
        second = client.post("/ai-market/v2/federation/announce", json={"hub_url": "https://new-peer.example.test/app1"})
        full = client.post("/ai-market/v2/federation/announce", json={"hub_url": "https://new-peer.example.test/app2"})
        again = client.post("/ai-market/v2/federation/announce", json={"hub_url": "https://new-peer.example.test/app0"})
        assert first.status_code == second.status_code == again.status_code == 200
        assert full.status_code == 429
        assert db.count_pending_peers() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("database_source", ["config", "injected"])
async def test_crawler_uses_actual_database_policy_before_dns_or_http(tmp_path, monkeypatch, database_source):
    import socket
    from aimarket_hub import outbound_http

    actual = tmp_path / "actual"
    unrelated = tmp_path / "unrelated"
    actual.mkdir()
    unrelated.mkdir()
    blocked = "https://rejected.example/application"
    allowed = "https://allowed.example/application"
    for directory, denied in ((actual, blocked), (unrelated, allowed)):
        (directory / "peer-rejections.json").write_text(json.dumps({
            "version": 1, "peer_urls": [denied], "invoke_urls": [],
        }))
    monkeypatch.delenv("AIMARKET_PEER_REJECTIONS_FILE", raising=False)
    monkeypatch.setenv("AIMARKET_DB_PATH", str(unrelated / "hub.db"))
    monkeypatch.setenv("AIMARKET_CHAIN_REALM", "live")
    config = HubConfig()
    config.db_path = str(actual / "hub.db" if database_source == "config" else unrelated / "hub.db")
    db = HubDatabase(actual / "hub.db") if database_source == "injected" else None
    crawler = Crawler(config=config, db=db, signer=Signer(tmp_path / "key"))

    def forbidden(*args, **kwargs):
        pytest.fail("operator-rejected peer reached DNS or HTTP")

    async def forbidden_request(*args, **kwargs):
        forbidden()

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(outbound_http, "prepare_safe_request_async", forbidden_request)
    monkeypatch.setattr(crawler._http, "send", forbidden_request)
    try:
        assert realm.peer_inside(blocked)  # The unrelated environment policy permits it.
        assert not realm.peer_inside(blocked, db_path=crawler.db.db_path)
        assert not realm.peer_inside(allowed)
        assert realm.peer_inside(allowed, db_path=crawler.db.db_path)
        with pytest.raises(ValueError, match="peer rejected"):
            await crawler._safe_get(blocked + "/.well-known/ai-market.json")
    finally:
        await crawler.close()
        crawler.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("frontier_source", ["seed", "pending"])
async def test_custom_database_rejection_stops_stale_frontier_before_dns(tmp_path, monkeypatch, frontier_source):
    import aimarket_hub.crawler as crawler_module

    actual = tmp_path / "actual"
    unrelated = tmp_path / "unrelated"
    actual.mkdir()
    unrelated.mkdir()
    monkeypatch.delenv("AIMARKET_PEER_REJECTIONS_FILE", raising=False)
    monkeypatch.setenv("AIMARKET_DB_PATH", str(unrelated / "hub.db"))
    monkeypatch.setenv("AIMARKET_CHAIN_REALM", "live")
    blocked = "https://rejected.example/application"
    discovery = blocked + "/.well-known/ai-market.json"
    db = HubDatabase(actual / "hub.db")
    # Simulate a peer queued before the operator added its rejection.
    if frontier_source == "pending":
        db.upsert_peer(Peer(url=blocked, name="stale peer", well_known_url=discovery), status="pending")
    (actual / "peer-rejections.json").write_text(json.dumps({
        "version": 1, "peer_urls": [blocked], "invoke_urls": [],
    }))
    config = HubConfig()
    config.db_path = str(actual / "hub.db")
    config.hub_url = "https://controlled-hub.example"
    config.seed_list = [discovery] if frontier_source == "seed" else []
    config.federation_assay = False
    crawler = Crawler(config=config, db=db, signer=Signer(tmp_path / "key"))

    def forbidden(*args, **kwargs):
        pytest.fail("rejected frontier reached DNS-based URL validation")

    async def forbidden_crawl(*args, **kwargs):
        pytest.fail("rejected frontier reached the HTTP crawl")

    monkeypatch.setattr(crawler_module, "_url_is_safe", forbidden)
    monkeypatch.setattr(crawler, "_crawl_one", forbidden_crawl)
    try:
        stats = await crawler.crawl()
        assert stats == {"discovered": 0, "indexed": 0, "errors": 0, "peers_found": 0}
    finally:
        await crawler.close()
        db.close()


@pytest.mark.parametrize("crawler_header", ["X-AIMarket-Crawler", "Agent-Marketplace-Crawler"])
def test_inbound_crawler_uses_actual_database_policy_before_dns(tmp_path, monkeypatch, crawler_header):
    import aimarket_hub.api as api_module
    import aimarket_hub.crawler as crawler_module

    actual = tmp_path / "actual"
    unrelated = tmp_path / "unrelated"
    actual.mkdir()
    unrelated.mkdir()
    blocked = "https://rejected.example/application"
    allowed = "https://allowed.example/application"
    for directory, denied in ((actual, blocked), (unrelated, allowed)):
        (directory / "peer-rejections.json").write_text(json.dumps({
            "version": 1, "peer_urls": [denied], "invoke_urls": [],
        }))
    monkeypatch.delenv("AIMARKET_PEER_REJECTIONS_FILE", raising=False)
    monkeypatch.setenv("AIMARKET_DB_PATH", str(unrelated / "hub.db"))
    monkeypatch.setenv("AIMARKET_CHAIN_REALM", "live")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    monkeypatch.setenv("AIMARKET_HUB_URL", "https://controlled-hub.example")

    class ImmediatePool:
        _work_queue = SimpleNamespace(qsize=lambda: 0)

        def submit(self, function, *args):
            function(*args)

    monkeypatch.setattr(api_module, "_inbound_pool", lambda: ImmediatePool())
    checked = []

    def safe_url(url):
        checked.append(url)
        assert url == allowed, "operator-rejected inbound crawler reached the DNS guard"
        return True

    monkeypatch.setattr(crawler_module, "_url_is_safe", safe_url)
    config = HubConfig()
    config.db_path = str(unrelated / "hub.db")
    db = HubDatabase(actual / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(tmp_path / "key"))
    with TestClient(app) as client:
        assert realm.peer_inside(blocked)
        assert not realm.peer_inside(blocked, db_path=db.db_path)
        assert not realm.peer_inside(allowed)
        assert realm.peer_inside(allowed, db_path=db.db_path)
        for url in (blocked, allowed):
            response = client.get("/.well-known/ai-market.json", headers={crawler_header: url})
            assert response.status_code == 200
        assert {row["hub_url"] for row in db.list_inbound_federation()} == {blocked, allowed}
        assert db.get_peer(blocked) is None
        admitted = db.get_peer(allowed)
        assert admitted is not None and admitted.status == "pending"
        assert admitted.discoverer == "inbound-crawl"
        assert checked == [allowed]
