"""Caller-selected callbacks use bounded DNS and exact, capped HTTP delivery.

Every network response is an in-process mock; no internal or external service is read.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import json
import socket
import threading
import time

import httpx
import pytest

from aimarket_hub import a2a_push, dns_limits, outbound_http, owner_notify


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["a2a", "owner"])
async def test_callback_validation_has_bounded_resolvers_and_yields_to_loop(monkeypatch, kind):
    monkeypatch.setattr(dns_limits, "_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(dns_limits, "DEFAULT_DNS_TIMEOUT_S", 0.06)
    monkeypatch.delenv("AIMARKET_A2A_PUSH_ALLOW_HTTP", raising=False)
    release = threading.Event()
    entered = []

    def slow_guard(url):
        entered.append(threading.current_thread().daemon)
        release.wait(2)
        return True

    monkeypatch.setattr(outbound_http, "_url_is_safe", slow_guard)
    function, body, error = (
        (a2a_push.parse_config_async, {"url": "https://callback.example.test/hook"}, a2a_push.PushConfigError)
        if kind == "a2a" else
        (owner_notify.parse_channel_async, {"kind": "webhook", "url": "https://callback.example.test/hook"},
         owner_notify.NotifyConfigError)
    )
    jobs = [asyncio.create_task(function(body)) for _ in range(4)]
    ticks = 0
    try:
        while len(entered) < 4:
            await asyncio.sleep(0.002)
        for _ in range(4):
            await asyncio.sleep(0.005)
            ticks += 1
        started = time.monotonic()
        with pytest.raises(error, match="busy or timed out"):
            await function(body)
        assert time.monotonic() - started < 0.1
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        assert all(isinstance(outcome, error) for outcome in outcomes)
        assert ticks == 4 and len(entered) == 4 and all(entered)
        # Timed-out callers do not free the four still-blocked DNS workers.
        with pytest.raises(error, match="busy or timed out"):
            await function(body)
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)


class CallbackStream(httpx.AsyncByteStream):
    def __init__(self, body, state, *, stall=False):
        self.body, self.state, self.stall = body, state, stall

    async def __aiter__(self):
        if self.stall:
            await asyncio.sleep(1)
        yield self.body

    async def aclose(self):
        self.state["closed"] = True


def callback_transport(monkeypatch, body=b'{"ok":true}', *, encoding=None, status=200, stall=False):
    state = {"requests": [], "closed": False}
    real_client = httpx.AsyncClient

    async def prepare(url, *, invoke=False):
        assert invoke is False, "callbacks must never inherit operator invoke exemptions"
        return "https://93.184.216.34/hook", {"Host": "callback.example.test"}, {"sni_hostname": "callback.example.test"}

    async def handler(request):
        state["requests"].append(request)
        headers = {"Location": "http://127.0.0.1/private"}
        if encoding:
            headers["Content-Encoding"] = encoding
        return httpx.Response(status, headers=headers, stream=CallbackStream(body, state, stall=stall))

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(outbound_http, "prepare_safe_request_async", prepare)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


def test_owner_delivery_preserves_exact_hmac_body_through_installed_transport(monkeypatch):
    state = callback_transport(monkeypatch)

    class Store:
        def record(self, *args, **kwargs):
            pass

    notifier = owner_notify.OwnerNotifier(Store(), None, retry_delays=())
    channel = {"channel_id": "local-test", "kind": "webhook",
               "target_json": json.dumps({"url": "https://callback.example.test/hook", "secret": "local-test-key"})}
    try:
        # Production sends on notification workers; asyncio.run never nests in the ASGI loop.
        result = notifier._pool.submit(notifier.send_test, channel).result(timeout=2)
    finally:
        notifier._pool.shutdown(wait=True)
    assert result == (True, "ok") and state["closed"]
    request, = state["requests"]
    signature = dict(field.split("=", 1) for field in request.headers["X-AIMarket-Signature"].split(","))
    expected = hmac.new(b"local-test-key", signature["t"].encode() + b"." + request.content,
                        hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, signature["v1"])
    assert b'"event":"mandate.approval.test"' in request.content
    assert request.headers["Host"] == "callback.example.test"


def test_owner_reply_compression_bomb_is_refused_before_unbounded_decode(monkeypatch):
    state = callback_transport(monkeypatch, gzip.compress(b"x" * (8 << 20)), encoding="gzip")
    with pytest.raises(outbound_http.ResponseTooLarge):
        owner_notify._http("POST", "https://callback.example.test/hook", {}, b'{}')
    assert state["closed"] and len(state["requests"]) == 1


def test_owner_delivery_obeys_total_deadline(monkeypatch):
    state = callback_transport(monkeypatch, stall=True)
    monkeypatch.setattr(owner_notify, "DELIVERY_TIMEOUT_S", 0.02)
    with pytest.raises(httpx.ReadTimeout):
        owner_notify._http("POST", "https://callback.example.test/hook", {}, b'{}')
    assert state["closed"]


def test_owner_background_callback_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(owner_notify, "MAX_PENDING_DELIVERIES", 2)
    release = threading.Event()
    entered = threading.Event()
    calls = []

    class Store:
        def list(self, account):
            return [{"channel_id": "one", "kind": "webhook", "target_json": '{}'}]

        def record(self, *args, **kwargs):
            pass

    def blocked_delivery(*args):
        calls.append(True)
        entered.set()
        release.wait(2)
        return 200, "ok"

    notifier = owner_notify.OwnerNotifier(Store(), None, post=blocked_delivery, retry_delays=(), workers=1)
    try:
        # Target omitted here would raise before transport; use a pure delivery stand-in
        # so the proof measures admission to ThreadPoolExecutor, not the webhook parser.
        monkeypatch.setattr(notifier, "_deliver", blocked_delivery)
        for _ in range(100):
            notifier._fan_out("local-test", "requested", {})
        assert entered.wait(0.5)
        assert len(notifier._futures) == 2 and len(calls) == 1
    finally:
        release.set()
        notifier._pool.shutdown(wait=True)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["a2a", "owner"])
async def test_callback_redirect_cannot_reach_private_destination(monkeypatch, kind):
    state = callback_transport(monkeypatch, status=302)
    if kind == "a2a":
        monkeypatch.delenv("AIMARKET_A2A_PUSH_ALLOW_HTTP", raising=False)
        status, _ = await a2a_push._safe_post("https://callback.example.test/hook", {}, {})
    else:
        status, _ = await asyncio.to_thread(owner_notify._http, "POST", "https://callback.example.test/hook", {}, b'{}')
    assert status == 302 and len(state["requests"]) == 1 and state["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["a2a", "owner"])
async def test_callback_dns_rebinding_refuses_before_any_http(monkeypatch, kind):
    monkeypatch.delenv("AIMARKET_A2A_PUSH_ALLOW_HTTP", raising=False)
    answers = iter(["93.184.216.34", "127.0.0.1"])

    def dns(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (next(answers), port))]

    def no_http(*args, **kwargs):
        raise AssertionError("HTTP client constructed after private DNS answer")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpx, "AsyncClient", no_http)
    with pytest.raises(ValueError, match="blocked network"):
        if kind == "a2a":
            await a2a_push._safe_post("https://callback.example.test/hook", {}, {})
        else:
            await asyncio.to_thread(owner_notify._http, "POST", "https://callback.example.test/hook", {}, b'{}')


@pytest.mark.asyncio
async def test_owner_public_registration_remains_responsive_during_dns(monkeypatch, tmp_path):
    from aimarket_hub.api import create_app
    from aimarket_hub.config import HubConfig
    from aimarket_hub.credits import CreditsLedger
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.signing import Signer

    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "0")
    monkeypatch.setattr(dns_limits, "_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(dns_limits, "DEFAULT_DNS_TIMEOUT_S", 0.2)
    release = threading.Event()
    entered = threading.Event()

    def slow_guard(url):
        entered.set()
        release.wait(2)
        return True

    monkeypatch.setattr(outbound_http, "_url_is_safe", slow_guard)
    db = HubDatabase(tmp_path / "hub.db")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(tmp_path / "key"))
    account = CreditsLedger(db._conn).create_account("local-test", grant_usd=0)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            pending = asyncio.create_task(client.post("/ai-market/v2/mandates/notify",
                headers={"X-API-Key": account["api_key"]},
                json={"kind": "webhook", "url": "https://callback.example.test/hook"}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.002)
            assert entered.is_set()
            start = time.monotonic()
            healthy = await client.get("/ai-market/v2/health")
            assert healthy.status_code == 200 and time.monotonic() - start < 0.15
            assert not pending.done()
            response = await pending
            assert response.status_code == 400 and response.json()["error"] == "channel_invalid"
            assert db._conn.execute("SELECT COUNT(*) FROM owner_notify_channels").fetchone()[0] == 0
    finally:
        release.set()
        db.close()


@pytest.mark.asyncio
async def test_mcp_a2a_ard_cannot_choose_arbitrary_internal_route_or_url(monkeypatch, tmp_path):
    from aimarket_hub import ard
    from aimarket_hub.api import create_app
    from aimarket_hub.config import HubConfig
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.signing import Signer

    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "0")
    db = HubDatabase(tmp_path / "hub.db")
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    app = create_app(config=config, db=db, signer=Signer(tmp_path / "key"))
    paths = []

    @app.middleware("http")
    async def record_path(request, call_next):
        paths.append(request.url.path)
        return await call_next(request)

    @app.api_route("/private-sentinel", methods=["GET", "POST"])
    async def sentinel():
        pytest.fail("a public protocol wrapper dispatched to an arbitrary internal route")

    async def no_network(*args, **kwargs):
        pytest.fail("protocol discovery or rejected input opened a network transport")

    def no_dns(*args, **kwargs):
        pytest.fail("protocol discovery or rejected input attempted DNS")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)

    def rpc(response):
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            for uri in ("http://127.0.0.1/private-sentinel", "file:///etc/passwd", "http://169.254.169.254/latest"):
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                    "method": "resources/read", "params": {"uri": uri}})
                assert rpc(response)["error"]["code"] == -32601
                response = await client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
                    "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                        "message": {"messageId": "local-file-test", "role": "ROLE_USER", "parts": [{"url": uri}]}}})
                assert "error" in response.json()
            for run_id in ("../../private-sentinel", "http://127.0.0.1/private-sentinel", "%2e%2e%2fprivate-sentinel"):
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "pipeline_status", "arguments": {"run_id": run_id, "access_token": "a" * 24}}})
                assert rpc(response)["result"]["isError"] is True
            response = await client.get(ard.WELL_KNOWN_PATH, params={"url": "http://127.0.0.1/private-sentinel"})
            assert response.status_code == 200
            assert set(paths) <= {"/mcp", "/a2a", ard.WELL_KNOWN_PATH,
                                  "/.well-known/agent-card.json", "/.well-known/mcp/server-card.json"}
    finally:
        db.close()
