"""Tests for the unified EVM+Solana network registry + health-checked RPC failover.

All offline: a fake transport records calls and is scripted to fail/succeed per URL; a fake
clock drives cooldown/return-to-default deterministically. No real network, no hanging.
"""
from __future__ import annotations

import os

import pytest

from aimarket_hub import chain_net as cn


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeTransport:
    """Scripted transport. ``down`` URLs raise (transport error → failover); ``errors`` URLs
    return a JSON-RPC error object; everything else returns a canned result."""

    def __init__(self, down=(), result=None, errors=()):
        self.down = set(down)
        self.errors = set(errors)
        self.result = result if result is not None else {"jsonrpc": "2.0", "id": 1, "result": "0x2105"}
        self.calls: list[str] = []

    def __call__(self, url, body, timeout):
        self.calls.append(url)
        if url in self.down:
            raise ConnectionError(f"down: {url}")
        if url in self.errors:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        return self.result


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip any AIMARKET_/legacy chain env so each test is hermetic."""
    for k in list(os.environ):
        if k.startswith(("AIMARKET_", "BASE_RPC", "ETHEREUM_RPC", "ARBITRUM_RPC", "SOLANA_RPC", "AIFACTORY_PAYMENT_RPC")):
            monkeypatch.delenv(k, raising=False)


# ═══════════════════════════════════ registry / selection ═══════════════════
class TestNetworkSelection:
    def test_default_is_base_with_demo_contracts(self):
        net = cn.active_network()
        assert net.id == "base" and net.is_evm and net.chain_id == 8453
        assert net.rpc_urls[0] == "https://mainnet.base.org"  # preferred default wins
        assert net.addresses["AIMarketEscrow"] == "0x3Df85a639EAB8B50DD14f09bdeB46D5FeF163017"
        assert net.addresses["USDC"].lower().startswith("0x833589")

    def test_env_selects_network(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_CHAIN", "solana")
        net = cn.active_network()
        assert net.id == "solana" and net.is_solana and net.cluster == "mainnet-beta"
        assert net.native_symbol == "SOL"

    def test_rpc_priority_explicit_then_legacy_then_presets(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_RPC_BASE", "https://my.node, https://my.backup")
        monkeypatch.setenv("BASE_RPC_URL", "https://legacy.node")  # explicit operator config
        net = cn.network("base")
        assert net.rpc_urls[0] == "https://my.node"        # new explicit list — preferred default
        assert net.rpc_urls[1] == "https://my.backup"
        assert net.rpc_urls[2] == "https://legacy.node"    # legacy config outranks public presets
        assert "https://mainnet.base.org" in net.rpc_urls[3:]  # presets are pure backups, last

    def test_rpc_urls_deduped(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_RPC_BASE", "https://mainnet.base.org")  # same as preset[0]
        net = cn.network("base")
        assert net.rpc_urls.count("https://mainnet.base.org") == 1

    def test_testnet_variant(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_TESTNET", "1")
        net = cn.network("base")
        assert net.testnet is True and net.chain_id == 84532
        assert net.rpc_urls[0] == "https://sepolia.base.org"

    def test_address_override_via_env(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_ADDR_BASE_ESCROW", "0xabc")
        net = cn.network("base")
        assert net.addresses["ESCROW"] == "0xabc"

    def test_adhoc_evm_network_via_env(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_CHAIN", "optimism")
        monkeypatch.setenv("AIMARKET_CHAIN_KIND", "evm")
        monkeypatch.setenv("AIMARKET_CHAIN_ID", "10")
        monkeypatch.setenv("AIMARKET_RPC_OPTIMISM", "https://mainnet.optimism.io")
        net = cn.active_network()
        assert net.id == "optimism" and net.is_evm and net.chain_id == 10
        assert net.rpc_urls == ("https://mainnet.optimism.io",)


# ═══════════════════════════════════ failover / priority ════════════════════
class TestRpcPoolFailover:
    def _spec(self, urls):
        return cn.NetworkSpec(id="base", kind=cn.EVM, display_name="Base", chain_id=8453,
                              cluster=None, rpc_urls=tuple(urls), native_symbol="ETH", explorer_tx="")

    def test_prefers_first_healthy_default(self):
        t = FakeTransport()
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        assert pool.call("eth_chainId") == "0x2105"
        assert t.calls == ["https://a"]  # default has priority — b never touched

    def test_fails_over_to_next_on_transport_error(self):
        t = FakeTransport(down={"https://a"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        assert pool.call("eth_chainId") == "0x2105"
        assert t.calls == ["https://a", "https://b"]  # a failed, fell over to b

    def test_all_down_fails_fast_not_hang(self):
        t = FakeTransport(down={"https://a", "https://b"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        with pytest.raises(cn.AllEndpointsDown):
            pool.call("eth_chainId")

    def test_returns_to_preferred_default_after_cooldown(self):
        clock = FakeClock()
        t = FakeTransport(down={"https://a"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t, cooldown=30.0, clock=clock)
        # 1st call: a down → b serves; a is demoted with a 30s cooldown.
        pool.call("eth_chainId")
        t.calls.clear()
        # within cooldown: a is skipped, straight to b.
        pool.call("eth_chainId")
        assert t.calls == ["https://b"]
        # a recovers and cooldown expires → a is re-probed and reclaimed as the default.
        t.down.clear()
        clock.advance(31.0)
        t.calls.clear()
        assert pool.call("eth_chainId") == "0x2105"
        assert t.calls == ["https://a"]  # back to the preferred default

    def test_node_error_propagates_without_failover(self):
        t = FakeTransport(errors={"https://a"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        with pytest.raises(cn.RpcError):
            pool.call("eth_getTransactionByHash", ["0x1"])
        assert t.calls == ["https://a"]  # reached a node, rejected on merits — b untouched

    def test_run_fails_over_on_raise_but_accepts_not_found_return(self):
        # fn raises on the first url (transport), returns a "not found" value on the second:
        # the not-found is a legitimate answer and must NOT trigger further failover.
        attempts = []

        def fn(url):
            attempts.append(url)
            if url == "https://a":
                raise ConnectionError("boom")
            return {"verified": False, "error": "not found"}

        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=FakeTransport())
        out = pool.run(fn)
        assert out == {"verified": False, "error": "not found"}
        assert attempts == ["https://a", "https://b"]

    def test_no_urls_raises(self):
        with pytest.raises(cn.ChainNetError):
            cn.RpcPool(self._spec([]), transport=FakeTransport())


# ═══════════════════════════════════ health ═════════════════════════════════
class TestHealth:
    def _spec(self, urls, kind=cn.EVM):
        return cn.NetworkSpec(id="base" if kind == cn.EVM else "solana", kind=kind,
                              display_name="N", chain_id=8453 if kind == cn.EVM else None,
                              cluster=None if kind == cn.EVM else "mainnet-beta",
                              rpc_urls=tuple(urls), native_symbol="ETH", explorer_tx="")

    def test_healthy_url_returns_first_up(self):
        t = FakeTransport(down={"https://a"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        assert pool.healthy_url() == "https://b"

    def test_healthy_url_none_when_all_down(self):
        t = FakeTransport(down={"https://a", "https://b"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        assert pool.healthy_url() is None  # caller degrades to "offline", no hang

    def test_evm_probe_is_chainid_solana_is_gethealth(self):
        seen = {}

        def transport(url, body, timeout):
            seen[url] = body["method"]
            return {"result": "ok"}

        cn.RpcPool(self._spec(["https://e"], cn.EVM), transport=transport).healthy_url()
        cn.RpcPool(self._spec(["https://s"], cn.SOLANA), transport=transport).healthy_url()
        assert seen["https://e"] == "eth_chainId"
        assert seen["https://s"] == "getHealth"

    def test_health_report_shape(self):
        t = FakeTransport(down={"https://b"})
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        rep = pool.health()
        assert rep["network"] == "base" and rep["healthy"] is True
        assert [e["healthy"] for e in rep["endpoints"]] == [True, False]


# ═══════════════════════════════════ audit hardening ════════════════════════
class TestHardening:
    def _spec(self, urls):
        return cn.NetworkSpec(id="base", kind=cn.EVM, display_name="Base", chain_id=8453,
                              cluster=None, rpc_urls=tuple(urls), native_symbol="ETH", explorer_tx="")

    def test_non_dict_response_fails_over_not_returned(self):
        # A broken endpoint returning a non-JSON-object must be treated as a transport failure
        # (fail over), never returned to the caller as a "result".
        class _T:
            def __init__(self): self.calls = []
            def __call__(self, url, body, timeout):
                self.calls.append(url)
                return "not-an-object" if url == "https://a" else {"result": "0x2105"}
        t = _T()
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=t)
        assert pool.call("eth_chainId") == "0x2105"
        assert t.calls == ["https://a", "https://b"]

    def test_health_marks_non_dict_down(self):
        class _T:
            def __call__(self, url, body, timeout):
                return "garbage" if url == "https://a" else {"result": "ok"}
        pool = cn.RpcPool(self._spec(["https://a", "https://b"]), transport=_T())
        rep = pool.health()
        assert [e["healthy"] for e in rep["endpoints"]] == [False, True]

    def test_malformed_float_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_RPC_TIMEOUT", "not-a-number")
        pool = cn.RpcPool(self._spec(["https://a"]), transport=FakeTransport())
        assert pool._timeout == 6.0  # default, no crash

    def test_non_http_scheme_dropped(self):
        with pytest.raises(cn.ChainNetError):  # file:// is the only url → nothing usable
            cn.RpcPool(self._spec(["file:///etc/passwd"]), transport=FakeTransport())
        pool = cn.RpcPool(self._spec(["file:///x", "https://b"]), transport=FakeTransport())
        assert [e.url for e in pool._endpoints] == ["https://b"]  # file:// silently dropped

    def test_redact_url_strips_credentials_and_path(self):
        assert cn.redact_url("https://user:KEY@rpc.example.com/v2/SECRET?k=1") == "https://rpc.example.com"
        assert cn.redact_url("https://rpc.example.com:8545/path") == "https://rpc.example.com:8545"
