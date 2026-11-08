"""Federation metadata must not resolve untrusted names on the event loop."""
from __future__ import annotations

import asyncio
import socket
import time

import pytest

from aimarket_hub.federation_urls import url_has_public_syntax


@pytest.mark.parametrize("url", [
    "https://hub.example/path", "http://8.8.8.8:9083/ai-market/manifest",
    "https://[2606:4700:4700::1111]/", "https://пример.рф/a",
    "https://hub.example./", "http://0x08080808/", "http://134744072/",
])
def test_public_syntax_does_not_resolve(url, monkeypatch):
    def no_dns(*args, **kwargs):
        raise AssertionError("a preflight must not resolve DNS")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    assert url_has_public_syntax(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://", "https://user@hub.example/",
    "https://@hub.example/", "https://user:password@hub.example/",
    "http://127.0.0.1", "http://2130706433", "http://0x7f000001",
    "http://0177.0.0.1", "http://127.1", "http://127.0.0.1./",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::7f00:1]/",
    "http://[2002:7f00:1::]/", "http://169.254.169.254/latest/meta-data",
    "http://224.0.0.1", "http://[ff02::1]/", "http://10.0.0.1",
    "http://localhost", "http://foo.localhost", "http://foo.internal",
    "http://foo.local", "http://container", "https://hub..example/",
    "https://-hub.example/", "https://hub.example:0/", "https://hub.example:65536/",
    "https://hub.example:no/", "https://hub.example\\@127.0.0.1/",
    "https://hub.example/a\nb", "https://hub.example/a b", "https://hub.example/\x7f",
    "http://127%2e0%2e0%2e1", "http://1.2.3.999", "http://[fe80::1%25eth0]/",
    "https://hub.example/" + "a" * 4096,
])
def test_malformed_or_local_syntax_is_refused(url):
    assert not url_has_public_syntax(url)


def test_private_dns_name_passes_only_preflight_and_transport_refuses(monkeypatch):
    from aimarket_hub.outbound_http import prepare_safe_request
    calls = []

    def private_dns(*args, **kwargs):
        calls.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private_dns)
    assert url_has_public_syntax("https://private.example/")
    assert calls == []
    with pytest.raises(ValueError):
        prepare_safe_request("https://private.example/")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_crawl_and_assay_preflights_keep_heartbeat_with_slow_dns(monkeypatch, tmp_path):
    from aimarket_hub.config import HubConfig
    from aimarket_hub.crawler import Crawler, _self_description
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.federation_assay import run_assay
    from aimarket_hub.signing import Signer

    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("AIMARKET_DB_PATH", ":memory:")
    monkeypatch.delenv("AIMARKET_PEER_REJECTIONS_FILE", raising=False)
    dns_calls = []

    def slow_dns(*args, **kwargs):
        dns_calls.append(args)
        time.sleep(0.12)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    db = HubDatabase(":memory:")
    signer = Signer(tmp_path / "key")
    peer_signer = Signer(tmp_path / "peer-key")
    base = "https://stranger.example"
    config = HubConfig();config.db_path = ":memory:"
    config.seed_list = [base + "/.well-known/ai-market.json"]
    config.hub_url = "https://local.example";config.federation_assay = False
    config.federation_auto_admit = False;config.federation_judge_key = ""
    config.federation_assay_sandbox = True
    crawler = Crawler(config=config, db=db, signer=signer)
    tools = [{"product_id": "prefilter-proof", "capability_id": f"read.item{i}@v1",
              "name": f"Read item {i}", "price_per_call_usd": 0,
              "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
              "endpoint": base + f"/read/{i}"} for i in range(20)]

    async def fake_crawl(url, depth, discoverer):
        assert _self_description({"mcp_endpoint": base + "/invoke"}, base)["mcp_endpoint"]
        assert crawler._index_manifest_capabilities({"tools": tools}, base, "Test") == 20
        await asyncio.sleep(0.005)
        return {"capabilities_count": 20, "new_peer_urls": []}

    crawler._crawl_one = fake_crawl
    manifest = {"protocol_version": "v1", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "capabilities_count": 1, "tools": tools[:1]}
    manifest["signature"] = peer_signer.sign_manifest(manifest)
    discovery = {"name": "Test", "protocol_versions": ["v2"],
                 "manifest_url": base + "/ai-market/manifest", "mcp_endpoint": base + "/invoke",
                 "signer_public_key": peer_signer.public_key_b64, "capabilities_count": 1}
    discovery["signature"] = peer_signer.sign_object(discovery)

    async def get_json(url):
        await asyncio.sleep(0.005)
        return discovery if url.endswith("ai-market.json") else manifest

    async def post_json(url, body, headers):
        await asyncio.sleep(0.005)
        return {"http_status": 200, "json": {}, "bytes": 2}

    done = False
    ticks = []

    async def heartbeat():
        while not done:
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.002)

    async def work():
        nonlocal done
        try:
            await crawler.crawl(clear_first=False)
            await run_assay(base, config=config, db=db, signer=signer,
                            get_json=get_json, post_json=post_json, crawl_on_admit=False)
        finally:
            done = True
            await crawler.close()

    try:
        await asyncio.gather(heartbeat(), work())
        assert dns_calls == []
        assert len(ticks) >= 3
        assert max(b-a for a, b in zip(ticks, ticks[1:])) < 0.08
    finally:
        db.close()
