"""X-AIMarket-Buyer-Ref: one stable pseudonym per authenticated buyer, for the operator's
own providers only (provider_buyer_ref.py).

Attested Memory took a memory's owner from X-Payment-Channel, which the hub sends empty for
buyers on credits, x402 and trials (all one owner) and keys per process for channel buyers
(a new owner after each restart).
"""

from __future__ import annotations

from types import SimpleNamespace

from tests.test_capability_internal_token import CAP_TOKEN, _account, _hub

from aimarket_hub import provider_buyer_ref
from aimarket_hub.signing import Signer


def _call(**kw):
    base = dict(sandbox_mode=False, credit_account="", payer_wallet="", x_payment_channel=None)
    return SimpleNamespace(**{**base, **kw})


def _local(**kw):
    base = dict(x402_accepted=None, reserved=False, hold_rail="")
    return SimpleNamespace(**{**base, **kw})


def test_signer_keeps_the_seed_the_reference_is_derived_from(tmp_path):
    # provider_buyer_ref reads Signer._seed; a refactor that renames it must fail here,
    # not silently drop the header (every buyer would turn anonymous).
    seed = Signer(tmp_path / "key")._seed
    assert isinstance(seed, bytes) and len(seed) == 32


def test_reference_is_stable_across_restarts_and_differs_per_hub_and_buyer(tmp_path):
    first, again = Signer(tmp_path / "a"), Signer(tmp_path / "a")  # the same key file = a restart
    other_hub = Signer(tmp_path / "b")
    call, local = _call(credit_account="acct_1"), _local()
    ref = provider_buyer_ref.buyer_ref(first, call, local)
    assert ref.startswith("bref-") and len(ref) == 37
    assert provider_buyer_ref.buyer_ref(again, call, local) == ref
    assert provider_buyer_ref.buyer_ref(other_hub, call, local) != ref
    assert provider_buyer_ref.buyer_ref(first, _call(credit_account="acct_2"), local) != ref
    assert "acct_1" not in ref


def test_only_an_authenticated_buyer_is_named():
    ok = {"ok": True}
    cases = [
        (_call(credit_account="acct"), _local(), "credits:acct"),
        # credits win over a payment sent alongside, as on the debit side
        (_call(credit_account="acct", payer_wallet="0xAB"), _local(x402_accepted=ok), "credits:acct"),
        (_call(payer_wallet="0xAB"), _local(x402_accepted=ok), "wallet:0xab"),
        (_call(payer_wallet="0xAB"), _local(x402_accepted={"ok": False}), ""),
        (_call(x_payment_channel="ch_1"), _local(reserved=True, hold_rail="channel"), "channel:ch_1"),
        # a channel id alone (no held price, so no checked secret) names nobody
        (_call(x_payment_channel="ch_1"), _local(), ""),
        (_call(sandbox_mode=True, credit_account="acct"), _local(), ""),
        (_call(), _local(), ""),
        # routed by another hub: its credits and channel are that hub's, shared by all its buyers
        (_call(credit_account="acct_peer", routed_by_hub=True), _local(), ""),
        (_call(x_payment_channel="ch_peer", routed_by_hub=True), _local(reserved=True, hold_rail="channel"), ""),
        # ... but a payment the buyer made, verified here, still names that buyer
        (_call(payer_wallet="0xAB", routed_by_hub=True), _local(x402_accepted=ok), "wallet:0xab"),
    ]
    for call, local, expected in cases:
        assert provider_buyer_ref.buyer_identity(call, local) == expected, (call, local)


def test_delegated_spending_never_names_the_principal(tmp_path):
    signer = Signer(tmp_path / "key")
    own = provider_buyer_ref.buyer_ref(signer, _call(credit_account="acct_root"), _local())
    # A subcontractor paying from the root buyer's allowance: the call's account is the root buyer's.
    allowance = SimpleNamespace(job=SimpleNamespace(funded_by="allowance"), admission=None)
    assert provider_buyer_ref.buyer_ref(signer, _call(credit_account="acct_root", funding=allowance), _local()) == ""
    # An agent under the principal's mandate gets its own space, separate per agent and from the principal.
    def mandated(agent):
        adm = SimpleNamespace(account_id="acct_root", agent=agent)
        return _call(credit_account="acct_root", funding=SimpleNamespace(job=SimpleNamespace(funded_by="own"), admission=adm))
    a1 = provider_buyer_ref.buyer_ref(signer, mandated("did:key:agent-1"), _local())
    a2 = provider_buyer_ref.buyer_ref(signer, mandated("did:key:agent-2"), _local())
    assert a1 and a2 and len({own, a1, a2}) == 3
    assert provider_buyer_ref.buyer_identity(mandated("did:key:agent-1"), _local()) == "mandate:acct_root:did:key:agent-1"
    # An own-funded job (fixed-price subcontracting paid by the caller's own key) keeps the caller's account.
    own_job = SimpleNamespace(job=SimpleNamespace(funded_by="own"), admission=None)
    assert provider_buyer_ref.buyer_ref(signer, _call(credit_account="acct_root", funding=own_job), _local()) == own


def test_no_reference_without_a_usable_signing_seed():
    assert provider_buyer_ref.buyer_ref(SimpleNamespace(), _call(credit_account="a"), _local()) == ""
    assert provider_buyer_ref.buyer_ref(SimpleNamespace(_seed=b"short"), _call(credit_account="a"), _local()) == ""


class _Resp:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = '{"success": true}'

    @staticmethod
    def json():
        return {"success": True, "result": {"ok": True}}


def _register(client, product_id: str, capability_id: str, invoke_url: str) -> None:
    seller = client.post("/ai-market/v2/accounts", json={"label": product_id}).json()
    r = client.post(
        "/ai-market/v2/supply/register",
        headers={"X-API-Key": seller["api_key"]},
        json={
            "product_id": product_id, "capability_id": capability_id, "name": "n",
            "description": "d", "invoke_url": invoke_url, "price_per_call_usd": 0.001,
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        },
    )
    assert r.status_code == 200, r.text


def _invoke(client, key: str, product_id: str, capability_id: str, extra: dict | None = None):
    r = client.post(
        "/ai-market/v2/invoke", headers={"X-API-Key": key, **(extra or {})},
        json={"product_id": product_id, "capability_id": capability_id, "input": {}, "source_hub": "local"},
    )
    assert r.status_code == 200, r.text


def test_gateway_provider_gets_one_reference_per_credit_account(monkeypatch, tmp_path):
    captured: list[dict] = []
    import aimarket_hub.outbound_http as outbound

    async def _post(url, **kwargs):
        captured.append(dict(kwargs.get("headers") or {}))
        return _Resp()

    monkeypatch.setattr(outbound, "safe_post", _post)
    with _hub(monkeypatch, tmp_path, AIMARKET_CAPABILITY_TOKEN=CAP_TOKEN,
              AIMARKET_INVOKE_HOST_GATEWAY="memory-market", AIMARKET_ALLOW_LOCAL_PUBLISH="1") as client:
        _register(client, "memory-write", "memory.write@v1",
                  "http://memory-market:8810/capabilities/memory-write/memory.write@v1/invoke")
        alice, bob = _account(client), _account(client)
        _invoke(client, alice, "memory-write", "memory.write@v1")
        _invoke(client, alice, "memory-write", "memory.write@v1")
        _invoke(client, bob, "memory-write", "memory.write@v1")

    refs = [h.get(provider_buyer_ref.HEADER) for h in captured]
    assert len(refs) == 3 and all(r and r.startswith("bref-") for r in refs)
    assert refs[0] == refs[1] != refs[2]
    assert all(h.get("X-AIMarket-Internal-Token") == CAP_TOKEN for h in captured)


def test_third_party_seller_never_gets_the_reference(monkeypatch, tmp_path):
    captured: list[dict] = []
    import aimarket_hub.crawler as crawler
    import aimarket_hub.outbound_http as outbound

    monkeypatch.setattr(crawler, "_url_is_safe", lambda url: url == "https://example.com/invoke")

    async def _post(url, **kwargs):
        captured.append(dict(kwargs.get("headers") or {}))
        return _Resp()

    monkeypatch.setattr(outbound, "safe_post", _post)
    with _hub(monkeypatch, tmp_path, AIMARKET_CAPABILITY_TOKEN=CAP_TOKEN,
              AIMARKET_INVOKE_HOST_GATEWAY="memory-market") as client:
        _register(client, "ext-svc", "ext.svc@v1", "https://example.com/invoke")
        _invoke(client, _account(client), "ext-svc", "ext.svc@v1")

    assert captured and provider_buyer_ref.HEADER not in captured[0]


def test_a_call_routed_by_another_hub_names_no_owner(monkeypatch, tmp_path):
    """A reselling hub pays here with its own key: all its buyers would share one memory owner."""
    captured: list[dict] = []
    import aimarket_hub.outbound_http as outbound

    async def _post(url, **kwargs):
        captured.append(dict(kwargs.get("headers") or {}))
        return _Resp()

    monkeypatch.setattr(outbound, "safe_post", _post)
    with _hub(monkeypatch, tmp_path, AIMARKET_CAPABILITY_TOKEN=CAP_TOKEN,
              AIMARKET_INVOKE_HOST_GATEWAY="memory-market", AIMARKET_ALLOW_LOCAL_PUBLISH="1") as client:
        _register(client, "memory-write", "memory.write@v1",
                  "http://memory-market:8810/capabilities/memory-write/memory.write@v1/invoke")
        peer_hub = _account(client)
        routed = {"X-AIMarket-Routing-Hub": "https://peer.example", "X-AIMarket-Buyer": "b-0123456789abcdef0123"}
        _invoke(client, peer_hub, "memory-write", "memory.write@v1", routed)
        _invoke(client, peer_hub, "memory-write", "memory.write@v1",
                {**routed, "X-AIMarket-Buyer": "b-fedcba9876543210fedc"})
        _invoke(client, peer_hub, "memory-write", "memory.write@v1")  # the same account calling for itself

    assert len(captured) == 3
    assert provider_buyer_ref.HEADER not in captured[0] and provider_buyer_ref.HEADER not in captured[1]
    assert captured[2].get(provider_buyer_ref.HEADER, "").startswith("bref-")
    assert all(h.get("X-AIMarket-Internal-Token") == CAP_TOKEN for h in captured)
