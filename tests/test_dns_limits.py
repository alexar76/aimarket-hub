"""Slow libc resolvers cannot block the API loop or create an unbounded work queue."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from aimarket_hub import dns_limits


@pytest.fixture
def isolated_workers(monkeypatch):
    monkeypatch.setattr(dns_limits, "_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(dns_limits, "DEFAULT_DNS_TIMEOUT_S", 0.04)


@pytest.mark.asyncio
async def test_timeouts_keep_four_real_workers_bounded_and_loop_responsive(isolated_workers):
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    daemon = []

    def blocked():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            daemon.append(threading.current_thread().daemon)
        try:
            release.wait(2)
            return True
        finally:
            with lock:
                active -= 1

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        for _ in range(8):
            await asyncio.sleep(0.005)
            ticks += 1

    tasks = [asyncio.create_task(dns_limits.bounded_dns_call(blocked)) for _ in range(4)]
    try:
        await heartbeat()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(x, dns_limits.DNSCheckTimeout) for x in outcomes)
        assert ticks == 8 and peak == active == 4
        assert all(daemon)
        started = time.monotonic()
        with pytest.raises(dns_limits.DNSCheckBusy):
            await dns_limits.bounded_dns_call(lambda: True)
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    for _ in range(100):
        try:
            assert await dns_limits.bounded_dns_call(lambda: "recovered") == "recovered"
            break
        except dns_limits.DNSCheckBusy:
            await asyncio.sleep(0.005)
    else:
        pytest.fail("finished workers did not release capacity")


@pytest.mark.asyncio
async def test_cancellation_does_not_release_still_running_resolvers(isolated_workers):
    release = threading.Event()
    entered = []

    def blocked():
        entered.append(True)
        release.wait(2)

    tasks = [asyncio.create_task(dns_limits.bounded_dns_call(blocked, timeout_s=10)) for _ in range(4)]
    try:
        for _ in range(100):
            if len(entered) == 4:
                break
            await asyncio.sleep(0.005)
        assert len(entered) == 4
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(dns_limits.DNSCheckBusy):
            await dns_limits.bounded_dns_call(lambda: True)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_prepared_transport_uses_bounded_guard_and_propagates_values(isolated_workers, monkeypatch):
    from aimarket_hub import outbound_http
    calls = []

    def checked(url, *, invoke=False):
        calls.append((url, invoke, threading.current_thread().daemon))
        return url, {"Host": "example.test"}, {"sni_hostname": "example.test"}

    monkeypatch.setattr(outbound_http, "_checked_target", checked)
    result = await outbound_http.prepare_safe_request_async("https://example.test/path", invoke=True)
    assert result[1] == {"Host": "example.test"}
    assert calls == [("https://example.test/path", True, True)]


@pytest.mark.asyncio
async def test_announcement_slow_dns_yields_to_health_and_returns_by_deadline(isolated_workers, tmp_path, monkeypatch):
    from aimarket_hub import crawler
    from aimarket_hub.api import create_app
    from aimarket_hub.config import HubConfig
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.signing import Signer

    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": 1, "peer_urls": [], "invoke_urls": []}))
    monkeypatch.setenv("AIMARKET_PEER_REJECTIONS_FILE", str(policy))
    monkeypatch.setenv("AIMARKET_CHAIN_REALM", "live")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "0")
    monkeypatch.setenv("AIMARKET_DB_PATH", str(tmp_path / "hub.db"))
    # Health performs its own local accounting; leave enough of the DNS wait to
    # establish that health completed while validation was still pending.
    monkeypatch.setattr(dns_limits, "DEFAULT_DNS_TIMEOUT_S", 0.6)
    release = threading.Event()
    entered = []

    def slow_guard(url):
        entered.append(url)
        release.wait(2)
        return True

    monkeypatch.setattr(crawler, "_url_is_safe", slow_guard)
    db = HubDatabase(tmp_path / "hub.db")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.hub_url = "https://this.example.test"
    app = create_app(config=config, db=db, signer=Signer(tmp_path / "key"))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            pending = asyncio.create_task(client.post("/ai-market/v2/federation/announce", json={"hub_url": "https://slow-dns.example.test"}))
            await asyncio.sleep(0)
            started = time.monotonic()
            health = await client.get("/ai-market/v2/health")
            assert health.status_code == 200
            # Health must not wait out the DNS guard (release.wait up to 2s).
            # 0.5 was tight under a 14-minute CI suite; keep it well under the
            # blocked announce without flake on a loaded runner.
            assert time.monotonic() - started < 1.5
            assert not pending.done()
            response = await pending
            assert response.status_code == 400
            assert entered == ["https://slow-dns.example.test"]
            assert db.get_peer("https://slow-dns.example.test") is None
    finally:
        release.set()
        db.close()


def test_all_async_validation_calls_are_awaited():
    import ast
    from pathlib import Path
    from aimarket_hub.hub_routes import federation
    tree = ast.parse(Path(federation.__file__).read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "_validate_hub_url"]
    assert len(calls) == 5
    assert all(isinstance(parents[call], ast.Await) for call in calls)
