"""The hub's MCP gateway as a PUBLIC endpoint — https://modelmarket.dev/mcp.

This is the URL a stranger pastes into Claude Desktop or Cursor, so the gateway is no
longer only a peer-to-peer surface and three properties decide whether that paste is
worth anything:

  * ``/mcp`` answers at the apex, not only under ``/ai-market`` — a registry listing
    carries one URL and clients expect the short one;
  * a newcomer reaches the hub's trial tier at all, under an identity of their own —
    the gateway used to send no visitor header, so the first thing a stranger met was
    the payment wall, and one shared identity would have spent the whole allowance on
    whoever arrived first;
  * ``source_hub`` survives search → invoke — most of the catalogue is federated, and
    without it the hub looks locally and answers 404.

A caller who supplies a payment channel is a customer, not a visitor, and must never be
put on the trial tier — that is pinned here too.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.mcp_gateway import visitor_for
from aimarket_hub.models import Capability
from aimarket_hub.signing import Signer

# TestClient reports this as the peer; declaring it trusted is what lets a test pose as
# several different callers through one proxy, exactly as nginx does in production.
PROXY = "testclient"


@contextmanager
def _hub_client(monkeypatch, tmp_path, **env):
    """A hub app built AFTER `env` is applied (create_app snapshots env into closures)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / f"hub-{len(list(tmp_path.iterdir()))}"
    root.mkdir(parents=True, exist_ok=True)
    config = HubConfig()
    config.db_path = str(root / "hub.db")
    config.signing_key_path = str(root / "key")
    db = HubDatabase(root / "hub.db")
    db.upsert_capability(Capability(
        capability_id="gaia.weather.read@v1",
        product_id="prod-gaia",
        name="weather.read",
        description="Attested weather reading from a physical sensor.",
        price_per_call_usd=0.02,
        source_hub="https://iot.modelmarket.dev",
        source_hub_name="GAIA",
    ))
    db.upsert_capability(Capability(
        capability_id="hub.echo@v1",
        product_id="prod-hub",
        name="echo",
        description="Local echo capability.",
        price_per_call_usd=0.01,
    ))
    db.upsert_capability(Capability(
        capability_id="hub.ping@v1",
        product_id="prod-hub",
        name="ping",
        description="Free local capability.",
        price_per_call_usd=0.0,
    ))
    app = create_app(config=config, db=db, signer=Signer(root / "key"))
    with TestClient(app) as client:
        yield client


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def upstream(monkeypatch):
    """Capture the internal invoke the gateway makes on the caller's behalf."""
    calls: list[dict] = []

    async def fake_post(self, url, json=None, headers=None, **kwargs):  # noqa: A002
        calls.append({"url": url, "body": json or {}, "headers": headers or {}})
        return _FakeResponse({"success": True, "sandbox": True, "output": {"ok": True},
                              "receipt": {"nonce": "n-test"}})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return calls


def _rpc(client, method, params=None, headers=None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers or {},
    )


def _sse(response) -> dict:
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no data: frame in {response.text!r}")


def _call(client, tool, arguments, headers=None):
    return _sse(_rpc(client, "tools/call", {"name": tool, "arguments": arguments}, headers))


def _as(ip: str) -> dict:
    """Headers that make a request look like it arrived from `ip` through the proxy."""
    return {"X-Forwarded-For": ip}


# --- the endpoint exists where people paste it -------------------------------------------

def test_apex_mcp_answers_initialize(monkeypatch, tmp_path):
    with _hub_client(monkeypatch, tmp_path) as client:
        r = _rpc(client, "initialize", {"protocolVersion": "2025-03-26"})
    assert r.status_code == 200
    assert r.headers.get("mcp-session-id")
    assert _sse(r)["result"]["serverInfo"]["name"] == "aimarket-hub-mcp"


def test_peer_path_still_answers(monkeypatch, tmp_path):
    """`mcp_endpoint` in the well-known manifest points here; it must not move."""
    with _hub_client(monkeypatch, tmp_path) as client:
        r = client.post("/ai-market/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 200
    assert _sse(r)["result"] == {}


def test_apex_get_describes_the_trial(monkeypatch, tmp_path):
    with _hub_client(monkeypatch, tmp_path) as client:
        body = client.get("/mcp").json()
    assert body["service"] == "aimarket-hub-mcp"
    assert body["transport"] == "streamable-http"
    assert body["trial"] == "per-caller"
    assert set(body["tools"]) == {"market_search", "market_invoke"}


def test_both_paths_expose_the_same_tools(monkeypatch, tmp_path):
    with _hub_client(monkeypatch, tmp_path) as client:
        apex = {t["name"] for t in _sse(_rpc(client, "tools/list"))["result"]["tools"]}
        peer = client.post("/ai-market/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert apex == {t["name"] for t in _sse(peer)["result"]["tools"]}


def test_manifest_advertises_the_hosted_endpoint_first(monkeypatch, tmp_path):
    """Discovery surfaces read this list; a stdio-only list says 'install something first'."""
    with _hub_client(monkeypatch, tmp_path) as client:
        servers = client.get("/.well-known/ai-market.json").json()["mcp_servers"]
    hosted = servers[0]
    assert hosted["transport"] == "streamable-http"
    assert hosted["url"].endswith("/mcp")
    assert "/ai-market/mcp" not in hosted["url"], "advertise the short URL people can paste"
    assert set(hosted["tools"]) == {"market_search", "market_invoke"}


# --- the trial tier has to reach a newcomer ----------------------------------------------

def test_invoke_presents_the_caller_to_the_trial_ledger(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"})
    assert len(upstream) == 1
    visitor = upstream[0]["headers"].get("X-AIMarket-Sandbox-Visitor")
    assert visitor, (
        "no visitor header — a stranger's first invoke meets the payment wall instead of "
        "the free trial, which is the whole value of a pasteable URL"
    )
    assert 8 <= len(visitor) <= 64, "the hub refuses trial ids outside 8-64 chars"


def test_two_callers_get_two_trial_identities(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path, AIMARKET_TRUSTED_PROXIES=PROXY) as client:
        args = {"product_id": "prod-hub", "capability_id": "hub.echo@v1"}
        _call(client, "market_invoke", args, headers=_as("203.0.113.7"))
        _call(client, "market_invoke", args, headers=_as("198.51.100.9"))
    first, second = (c["headers"]["X-AIMarket-Sandbox-Visitor"] for c in upstream)
    assert first != second, (
        "both callers shared one trial identity — the endpoint would spend its whole "
        "allowance on whoever arrives first and 402 everybody after"
    )


def test_one_caller_keeps_one_trial_identity(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path, AIMARKET_TRUSTED_PROXIES=PROXY) as client:
        args = {"product_id": "prod-hub", "capability_id": "hub.echo@v1"}
        _call(client, "market_invoke", args, headers=_as("203.0.113.7"))
        _call(client, "market_invoke", args, headers=_as("203.0.113.7"))
    assert upstream[0]["headers"]["X-AIMarket-Sandbox-Visitor"] == \
        upstream[1]["headers"]["X-AIMarket-Sandbox-Visitor"]


def test_trial_identity_is_not_the_client_address(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path, AIMARKET_TRUSTED_PROXIES=PROXY) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"},
              headers=_as("203.0.113.7"))
    visitor = upstream[0]["headers"]["X-AIMarket-Sandbox-Visitor"]
    assert "203.0.113.7" not in visitor
    assert visitor.startswith("mcpx-")


def test_a_forged_forwarding_header_cannot_mint_identities(monkeypatch, tmp_path, upstream):
    """With no proxy declared, the peer is the only address the hub may believe."""
    with _hub_client(monkeypatch, tmp_path) as client:
        args = {"product_id": "prod-hub", "capability_id": "hub.echo@v1"}
        _call(client, "market_invoke", args, headers=_as("203.0.113.7"))
        _call(client, "market_invoke", args, headers=_as("198.51.100.9"))
    assert upstream[0]["headers"]["X-AIMarket-Sandbox-Visitor"] == \
        upstream[1]["headers"]["X-AIMarket-Sandbox-Visitor"]


def test_a_paying_caller_is_not_put_on_the_trial_tier(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {
            "product_id": "prod-hub", "capability_id": "hub.echo@v1",
            "payment_channel": "chan-123", "payment_channel_secret": "s3cret",
        })
    headers = upstream[0]["headers"]
    assert "X-AIMarket-Sandbox-Visitor" not in headers
    assert headers["X-Payment-Channel"] == "chan-123"


def test_caller_is_named_on_the_internal_hop(monkeypatch, tmp_path, upstream):
    """So the hub's per-IP limiter bounds each visitor, not the gateway as a whole."""
    with _hub_client(monkeypatch, tmp_path, AIMARKET_TRUSTED_PROXIES=PROXY) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"},
              headers=_as("203.0.113.7"))
    assert upstream[0]["headers"].get("X-Forwarded-For") == "203.0.113.7"


def test_visitor_for_is_stable_and_opaque():
    assert visitor_for("203.0.113.7") == visitor_for("203.0.113.7")
    assert visitor_for("203.0.113.7") != visitor_for("203.0.113.8")
    assert visitor_for("") == visitor_for("anonymous")   # no address is still one bucket


def test_a_free_capability_does_not_spend_a_trial(monkeypatch, tmp_path, upstream):
    """The hub consumes the allowance before it looks at the price.

    Attaching the identity to a free invoke would cap an unlimited capability at three
    calls, and — because the sandbox branch runs ahead of the payment gate — would also
    replace the eventual 402 with a 429 that carries no price.
    """
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.ping@v1"})
    assert "X-AIMarket-Sandbox-Visitor" not in upstream[0]["headers"]


def test_an_unknown_capability_is_treated_as_priced(monkeypatch, tmp_path, upstream):
    """Guessing 'free' would send an unpaid invoke at something that is not."""
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-?", "capability_id": "unknown@v1"})
    assert "X-AIMarket-Sandbox-Visitor" in upstream[0]["headers"]


def test_a_spent_allowance_is_reported_as_the_payment_wall(monkeypatch, tmp_path):
    """The hub answers 429 trial_quota_exhausted, which carries no price and reads to an
    agent as 'retry later'. The caller needs the payment gate's answer instead."""
    calls: list = []

    async def fake_post(self, url, json=None, headers=None, **kwargs):  # noqa: A002
        calls.append(headers or {})
        if "X-AIMarket-Sandbox-Visitor" in (headers or {}):
            return _FakeResponse({"error": "trial_quota_exhausted"}, status_code=429)
        return _FakeResponse({"success": False, "error": "payment_required", "needed": 0.01},
                             status_code=402)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    with _hub_client(monkeypatch, tmp_path) as client:
        text = _call(client, "market_invoke", {"product_id": "prod-hub",
                                               "capability_id": "hub.echo@v1"})["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload["error"] == "payment_required"
    assert payload["needed"] == 0.01, "the price is what makes the 402 actionable"
    assert payload["trial_exhausted"] is True
    assert len(calls) == 2 and "X-AIMarket-Sandbox-Visitor" not in calls[1]


def test_a_working_invoke_is_not_retried(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"})
    assert len(upstream) == 1


def test_the_internal_hop_stays_on_loopback(monkeypatch, tmp_path, upstream):
    """Routed through the public URL, nginx appends its own hop and the caller we named
    is discarded — every invoke would land in one rate-limit bucket."""
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"})
    assert upstream[0]["url"].startswith("http://127.0.0.1:")


def test_the_internal_hop_targets_this_process_not_a_fixed_port(monkeypatch, tmp_path, upstream):
    """A constant 9083 sends the invoke to whatever else holds that port.

    Observed for real during review: a hub listening elsewhere posted into an unrelated hub
    process, which answered "Unknown capability" instead of failing.
    """
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"})
    port = int(upstream[0]["url"].split(":")[2].split("/")[0])
    # TestClient's ASGI scope reports port 80, so a hard-coded 9083 is visibly wrong here.
    assert port == 80, f"the invoke went to :{port}, not the port this request arrived on"


def test_an_explicit_internal_base_wins(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path,
                     AIMARKET_INTERNAL_BASE="http://127.0.0.1:65246") as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1"})
    assert upstream[0]["url"].startswith("http://127.0.0.1:65246/")


def test_a_stream_probe_gets_the_specified_refusal(monkeypatch, tmp_path):
    """A client opening the server-initiated stream must see 405, not a JSON document it
    will read as an immediately-dropped stream."""
    with _hub_client(monkeypatch, tmp_path) as client:
        r = client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert r.status_code == 405
    assert "POST" in r.headers.get("allow", "")


def test_session_delete_is_accepted(monkeypatch, tmp_path):
    with _hub_client(monkeypatch, tmp_path) as client:
        assert client.delete("/mcp").status_code == 204


def test_a_disabled_trial_is_reported_as_disabled(monkeypatch, tmp_path):
    """Hard-coding 'per-caller' would let a redeploy that drops the trial still look healthy."""
    import aimarket_hub.sandbox_trials as sandbox
    monkeypatch.setattr(sandbox, "sandbox_enabled", lambda: False)
    with _hub_client(monkeypatch, tmp_path) as client:
        assert client.get("/mcp").json()["trial"] == "disabled"


# --- source_hub must survive search -> invoke --------------------------------------------

def test_search_reports_the_source_hub(monkeypatch, tmp_path):
    with _hub_client(monkeypatch, tmp_path) as client:
        text = _call(client, "market_search", {"intent": "weather"})["result"]["content"][0]["text"]
    matches = json.loads(text)["matches"]
    gaia = next(m for m in matches if m["capability_id"] == "gaia.weather.read@v1")
    assert gaia["source_hub"] == "https://iot.modelmarket.dev", (
        "an agent cannot invoke a federated capability it was never told the origin of"
    )


def test_invoke_forwards_the_source_hub(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {
            "product_id": "prod-gaia", "capability_id": "gaia.weather.read@v1",
            "source_hub": "https://iot.modelmarket.dev",
        })
    assert upstream[0]["body"]["source_hub"] == "https://iot.modelmarket.dev", (
        "without it the hub looks for the capability locally and answers 404 — which is "
        "most of the catalogue"
    )


def test_local_capabilities_stay_local(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path) as client:
        _call(client, "market_invoke", {"product_id": "prod-hub", "capability_id": "hub.echo@v1",
                                        "source_hub": "local"})
    assert "source_hub" not in upstream[0]["body"]


def test_invoke_still_requires_its_identifiers(monkeypatch, tmp_path, upstream):
    with _hub_client(monkeypatch, tmp_path) as client:
        result = _call(client, "market_invoke", {"capability_id": "hub.echo@v1"})["result"]
    assert result["isError"] is True
    assert not upstream
