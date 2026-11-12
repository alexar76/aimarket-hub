"""Receipt binding (spec §7.3.9) and recomputable provenance digests.

Found by checking our receipts against TensorFeed's AFTA receipts on 2026-10-30
(docs/evidence/tensorfeed-receipts-2026-10-30): theirs signed the request, the response and
the caller's nonce, ours signed none of them; and our routed provenance receipt digested an
answer that kept growing afterwards, so no buyer could recompute its outputDigest.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import receipt_binding
from aimarket_hub.models import Peer
from aimarket_hub.plugin import PluginRegistry
from aimarket_hub.signing import Signer

from tests.test_federated_invoke import (
    CAP, MCP_ENDPOINT, ORACLE_PEER, _Outbound, _Resp, _add_federated_capability, _add_peer,
    _build, _factory, _fed_invoke, _wire,
)

NONCE = "buyer-nonce-0001"


def _sri(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256-" + base64.b64encode(hashlib.sha256(raw).digest()).decode()


def _provenance_registry(monkeypatch, tmp_path):
    # Only the provenance tests need the plugin; the binding tests run without it (review of
    # 2026-11-11: a module-level importorskip skipped all of them where it was not installed).
    pytest.importorskip("aimarket_provenance")
    from aimarket_provenance.plugin import ProvenancePlugin

    # The plugin keeps its store at a path relative to the working directory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("AIMARKET_PROVENANCE_KEY_PATH", str(tmp_path / "provenance.key"))
    plugin = ProvenancePlugin()
    # Production starts discovered plugins before their routes are registered; a registry
    # handed to create_app is not started, and the receipt route keeps the store it saw.
    plugin.on_startup(object())
    return PluginRegistry(plugins=[plugin])


def _fetch_awr(client, answer):
    url = answer["provenance_receipt"]["receipt_url"]
    path = url[url.index("/ai-market/"):]
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# ── unit ──────────────────────────────────────────────────────────────────────


def test_binding_round_trip_and_every_field_is_load_bearing(tmp_path):
    signer = Signer(str(tmp_path / "k"))
    receipt = {"nonce": "rcpt_1", "signature": {"public_key": signer.public_key_b64}}
    answer = {"success": True, "result": {"x": "é"}, "receipt": receipt}
    binding = receipt_binding.build(answer, input_payload={"q": 1}, client_nonce=NONCE,
                                    hub_url="https://hub.test", signer=signer)
    assert binding["output_member"] == "result"
    assert binding["client_nonce"] == NONCE
    ok = receipt_binding.verify(binding, public_key_b64=signer.public_key_b64, response=answer,
                                input_payload={"q": 1}, client_nonce=NONCE)
    assert ok == {"signature_valid": True, "receipt_matches": True, "output_matches": True,
                  "input_matches": True, "client_nonce_matches": True, "valid": True}

    for field in receipt_binding.BINDING_FIELDS:
        forged = dict(binding)
        forged[field] = "changed"
        assert not receipt_binding.verify(forged, public_key_b64=signer.public_key_b64)["valid"], field

    swapped = dict(answer, result={"x": "e"})
    assert receipt_binding.verify(binding, public_key_b64=signer.public_key_b64,
                                  response=swapped)["output_matches"] is False
    other = Signer(str(tmp_path / "other"))
    assert receipt_binding.verify(binding, public_key_b64=other.public_key_b64)["valid"] is False


@pytest.mark.parametrize("value, ok", [
    ("short", False), ("has space here", False), ("x" * 129, False), ("ok-nonce.1_A", True),
])
def test_client_nonce_format(value, ok):
    nonce, error = receipt_binding.read_client_nonce({"X-Client-Nonce": value})
    assert (nonce == value and error is None) if ok else (nonce is None and error)


def test_afta_header_name_is_accepted():
    assert receipt_binding.read_client_nonce({"X-Agent-Nonce": NONCE}) == (NONCE, None)


# ── local invoke ──────────────────────────────────────────────────────────────


def test_local_answer_binds_input_output_and_nonce_and_digests_reproduce(tmp_path, monkeypatch):
    plugins = _provenance_registry(monkeypatch, tmp_path)
    app, db, config = _build(tmp_path, monkeypatch, crypto=False, plugins=plugins)
    _factory(monkeypatch)
    body = {**CAP, "source_hub": "local", "input": {"text": "hi"}}
    with TestClient(app) as client:
        response = client.post("/ai-market/v2/invoke", json=body, headers={"X-Client-Nonce": NONCE})
        assert response.status_code == 200, response.text
        answer = response.json()
        assert response.headers[receipt_binding.CLIENT_NONCE_ECHO_HEADER] == NONCE

        binding = answer["receipt_binding"]
        assert binding["client_nonce"] == NONCE
        assert binding["receipt_nonce"] == answer["receipt"]["nonce"]
        assert binding["output_sha256"] == receipt_binding.payload_sha256(answer["result"])
        assert binding["input_sha256"] == receipt_binding.payload_sha256(body["input"])

        checked = client.post("/ai-market/v2/receipts/verify", json={
            "receipt": answer["receipt"], "response": answer, "input": body["input"],
            "client_nonce": NONCE,
        }).json()
        assert checked["valid"] is True
        assert checked["binding"]["valid"] is True

        tampered = dict(answer, result={"translated": "adios"})
        bad = client.post("/ai-market/v2/receipts/verify", json={
            "receipt": answer["receipt"], "response": tampered}).json()
        assert bad["binding"]["output_matches"] is False and bad["binding"]["valid"] is False

        awr = _fetch_awr(client, answer)
    subject = awr["credentialSubject"]
    assert subject["outputDigestOf"] == "result"
    assert subject["outputDigest"] == _sri(answer["result"])
    assert subject["inputDigest"] == _sri(body["input"])


def test_a_malformed_client_nonce_is_refused_before_any_work(tmp_path, monkeypatch):
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    factory = _factory(monkeypatch)
    with TestClient(app) as client:
        response = client.post("/ai-market/v2/invoke", headers={"X-Client-Nonce": "no"},
                               json={**CAP, "source_hub": "local", "input": {"text": "hi"}})
    assert response.status_code == 400
    assert response.json()["error"] == "client_nonce_invalid"
    assert factory.calls == 0


def test_without_a_client_nonce_the_binding_says_so(tmp_path, monkeypatch):
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    _factory(monkeypatch)
    with TestClient(app) as client:
        response = client.post("/ai-market/v2/invoke",
                               json={**CAP, "source_hub": "local", "input": {"text": "hi"}})
    assert response.status_code == 200
    assert response.json()["receipt_binding"]["client_nonce"] is None
    assert receipt_binding.CLIENT_NONCE_ECHO_HEADER not in response.headers


# ── routed invoke ─────────────────────────────────────────────────────────────


def test_routed_answer_digests_reproduce_and_peer_receipt_verifies_by_pinned_key(tmp_path, monkeypatch):
    peer_signer = Signer(str(tmp_path / "peer"))
    peer_receipt = {"nonce": "peer-7", "product_id": "prod-x", "capability_id": "cap.x@v1",
                    "price_usd": 0.0, "timestamp": "2026-10-30T05:27:40Z", "success": True,
                    "latency_ms": 42}
    peer_receipt["signature"] = {**peer_signer.sign_receipt(peer_receipt),
                                 "public_key": peer_signer.public_key_b64}

    plugins = _provenance_registry(monkeypatch, tmp_path)
    app, db, config = _build(tmp_path, monkeypatch, crypto=False, plugins=plugins)
    _add_peer(db, ORACLE_PEER, categories=["oracle"])
    db.upsert_peer(Peer(url=ORACLE_PEER, name=ORACLE_PEER, capabilities_count=1,
                        well_known_url=f"{ORACLE_PEER}/.well-known/ai-market.json",
                        categories=["oracle"], trusted=True, public_key=peer_signer.public_key_b64))
    _add_federated_capability(db, ORACLE_PEER, price=0.001)
    _wire(monkeypatch, _Outbound(
        _Resp(200, {"ok": True, "output": {"reading": 7}, "receipt": peer_receipt}),
        well_known={"mcp_endpoint": MCP_ENDPOINT},
    ))
    with TestClient(app) as client:
        response = client.post("/ai-market/v2/invoke", headers={"X-Agent-Nonce": NONCE}, json={
            "product_id": "prod-x", "capability_id": "cap.x@v1", "source_hub": ORACLE_PEER,
            "input": {"q": "hi"}})
        assert response.status_code == 200, response.text
        answer = response.json()
        binding = answer["receipt_binding"]
        assert binding["output_member"] == "output"
        assert binding["receipt_signer"] == peer_signer.public_key_b64
        assert binding["client_nonce"] == NONCE

        checked = client.post("/ai-market/v2/receipts/verify", json={
            "receipt": answer["receipt"], "response": answer, "input": {"q": "hi"},
            "client_nonce": NONCE}).json()
        assert checked["valid"] is True and checked["signer"] == ORACLE_PEER
        assert checked["binding"]["valid"] is True

        awr = _fetch_awr(client, answer)
    subject = awr["credentialSubject"]
    assert subject["outputDigestOf"] == "output"
    assert subject["outputDigest"] == _sri(answer["output"])
    assert subject["gatewayReceiptDigest"] == _sri(answer["receipt"])


def test_a_receipt_signed_by_an_unpinned_key_does_not_verify(tmp_path, monkeypatch):
    stranger = Signer(str(tmp_path / "stranger"))
    receipt = {"nonce": "s-1", "product_id": "p", "capability_id": "c@v1", "price_usd": 0.0,
               "timestamp": "2026-10-30T00:00:00Z", "success": True, "latency_ms": 1}
    receipt["signature"] = {**stranger.sign_receipt(receipt), "public_key": stranger.public_key_b64}
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    with TestClient(app) as client:
        checked = client.post("/ai-market/v2/receipts/verify", json={"receipt": receipt}).json()
    assert checked["valid"] is False and checked["signer"] is None


def test_a_peer_cannot_sign_a_binding_in_another_hubs_name(tmp_path, monkeypatch):
    peer_signer = Signer(str(tmp_path / "peer"))
    receipt = {"nonce": "p-1", "product_id": "p", "capability_id": "c@v1", "price_usd": 0.0,
               "timestamp": "2026-10-30T00:00:00Z", "success": True, "latency_ms": 1}
    receipt["signature"] = {**peer_signer.sign_receipt(receipt), "public_key": peer_signer.public_key_b64}
    answer = {"success": True, "output": {"v": 1}, "receipt": receipt}
    forged = receipt_binding.build(answer, input_payload={}, client_nonce=None,
                                   hub_url="https://someone-else.example", signer=peer_signer)
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    db.upsert_peer(Peer(url=ORACLE_PEER, name=ORACLE_PEER, capabilities_count=1, trusted=True,
                        well_known_url=f"{ORACLE_PEER}/.well-known/ai-market.json",
                        public_key=peer_signer.public_key_b64))
    with TestClient(app) as client:
        checked = client.post("/ai-market/v2/receipts/verify", json={
            "receipt": receipt, "receipt_binding": forged, "response": answer}).json()
    assert checked["valid"] is True  # the receipt itself is the peer's and genuine
    assert checked["binding"]["signature_valid"] is True
    assert checked["binding"]["hub_matches"] is False and checked["binding"]["valid"] is False


# ── review of 2026-11-11 ──────────────────────────────────────────────────────

VECTOR = Path(__file__).resolve().parents[2] / "aimarket-protocol" / "test-vectors" / "receipt-binding-signed.json"


def test_the_normative_vector_verifies_with_this_code():
    if not VECTOR.exists():
        pytest.skip("the binding vector lives in the monorepo's aimarket-protocol/")
    vector = json.loads(VECTOR.read_text())
    response = vector["response"]
    binding = response["receipt_binding"]
    assert receipt_binding.binding_canonical(binding) == vector["canonical"]
    nonce = vector["request"]["headers"]["X-Client-Nonce"]
    check = receipt_binding.verify(binding, public_key_b64=vector["public_key"], response=response,
                                   input_payload=vector["request"]["input"], client_nonce=nonce)
    assert check["valid"] is True, check


def _peer_hub_receipt(peer_signer):
    # A hub signs its receipts without naming its key in them (local_delivery `_local_sign_receipt`).
    receipt = {"nonce": "peer-hub-9", "product_id": "prod-x", "capability_id": "cap.x@v1",
               "price_usd": 0.0, "timestamp": "2026-11-11T12:00:00Z", "success": True,
               "latency_ms": 12}
    receipt["signature"] = peer_signer.sign_receipt(receipt)
    assert "public_key" not in receipt["signature"]
    return receipt


def test_a_routed_hub_receipt_is_bound_to_the_peer_that_signed_it_not_to_this_hub(tmp_path, monkeypatch):
    peer_signer = Signer(str(tmp_path / "peer"))
    receipt = _peer_hub_receipt(peer_signer)
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    _add_peer(db, ORACLE_PEER, categories=["oracle"])
    db.upsert_peer(Peer(url=ORACLE_PEER, name=ORACLE_PEER, capabilities_count=1,
                        well_known_url=f"{ORACLE_PEER}/.well-known/ai-market.json",
                        categories=["oracle"], trusted=True, public_key=peer_signer.public_key_b64))
    _add_federated_capability(db, ORACLE_PEER, price=0.001)
    _wire(monkeypatch, _Outbound(
        _Resp(200, {"success": True, "result": {"reading": 7}, "receipt": receipt}),
        well_known={"mcp_endpoint": MCP_ENDPOINT},
    ))
    with TestClient(app) as client:
        response = client.post("/ai-market/v2/invoke", headers={"X-Client-Nonce": NONCE}, json={
            "product_id": "prod-x", "capability_id": "cap.x@v1", "source_hub": ORACLE_PEER,
            "input": {"q": "hi"}})
        assert response.status_code == 200, response.text
        answer = response.json()
        binding = answer["receipt_binding"]
        # Not this hub's key: it did not sign the provider's receipt.
        assert binding["receipt_signer"] == peer_signer.public_key_b64
        checked = client.post("/ai-market/v2/receipts/verify", json={
            "receipt": answer["receipt"], "response": answer, "input": {"q": "hi"},
            "client_nonce": NONCE}).json()
    assert checked["valid"] is True and checked["signer"] == ORACLE_PEER
    assert checked["binding"]["names_this_receipt"] is True
    assert checked["binding"]["valid"] is True


def test_a_binding_that_names_the_wrong_signer_does_not_name_this_receipt(tmp_path, monkeypatch):
    peer_signer = Signer(str(tmp_path / "peer"))
    receipt = _peer_hub_receipt(peer_signer)
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    db.upsert_peer(Peer(url=ORACLE_PEER, name=ORACLE_PEER, capabilities_count=1, trusted=True,
                        well_known_url=f"{ORACLE_PEER}/.well-known/ai-market.json",
                        public_key=peer_signer.public_key_b64))
    answer = {"success": True, "result": {"reading": 7}, "receipt": receipt}
    hub_signer = Signer(config.signing_key_path)   # the same key the app signs with
    # What the 2026-10-30 code bound: this hub's key as the signer of the peer's receipt.
    wrong = receipt_binding.build(answer, input_payload={}, client_nonce=None,
                                  hub_url=config.hub_url, signer=hub_signer,
                                  receipt_signer=hub_signer.public_key_b64)
    with TestClient(app) as client:
        checked = client.post("/ai-market/v2/receipts/verify", json={
            "receipt": receipt, "receipt_binding": wrong, "response": answer}).json()
    assert checked["binding"]["signature_valid"] is True
    assert checked["binding"]["names_this_receipt"] is False
    assert checked["binding"]["valid"] is False


def test_the_post_quantum_half_of_a_binding_is_pinned(tmp_path):
    hub = Signer(str(tmp_path / "hub"), pqc=True)
    other = Signer(str(tmp_path / "other"), pqc=True)
    if not hub.pq_public_key_b64:
        pytest.skip("ML-DSA-65 is not available here")
    receipt = {"nonce": "r-1", "signature": {"public_key": hub.public_key_b64}}
    answer = {"success": True, "result": {"x": 1}, "receipt": receipt}
    binding = receipt_binding.build(answer, input_payload={}, client_nonce=None,
                                    hub_url="https://hub.test", signer=hub)
    canonical = receipt_binding.binding_canonical(binding)
    forged = dict(binding, signature={**binding["signature"], **other._pq_fields(canonical)})
    assert receipt_binding.verify(binding, public_key_b64=hub.public_key_b64,
                                  pq_public_key_b64=hub.pq_public_key_b64)["signature_valid"] is True
    # Another ML-DSA key over the same bytes: valid only when nothing pins the PQ key.
    assert receipt_binding.verify(forged, public_key_b64=hub.public_key_b64,
                                  pq_public_key_b64=hub.pq_public_key_b64)["signature_valid"] is False


def test_a_routed_answer_without_a_result_member_is_committed_to_whole(tmp_path, monkeypatch):
    plugins = _provenance_registry(monkeypatch, tmp_path)
    peer_signer = Signer(str(tmp_path / "peer"))
    receipt = {"nonce": "peer-8", "product_id": "prod-x", "capability_id": "cap.x@v1",
               "price_usd": 0.0, "timestamp": "2026-11-11T12:00:00Z", "success": True, "latency_ms": 5}
    receipt["signature"] = {**peer_signer.sign_receipt(receipt), "public_key": peer_signer.public_key_b64}
    app, db, config = _build(tmp_path, monkeypatch, crypto=False, plugins=plugins)
    _add_peer(db, ORACLE_PEER, categories=["oracle"])
    db.upsert_peer(Peer(url=ORACLE_PEER, name=ORACLE_PEER, capabilities_count=1,
                        well_known_url=f"{ORACLE_PEER}/.well-known/ai-market.json",
                        categories=["oracle"], trusted=True, public_key=peer_signer.public_key_b64))
    _add_federated_capability(db, ORACLE_PEER, price=0.001)
    _wire(monkeypatch, _Outbound(
        _Resp(200, {"ok": True, "data": {"reading": 7}, "receipt": receipt}),
        well_known={"mcp_endpoint": MCP_ENDPOINT},
    ))
    with TestClient(app) as client:
        response = client.post("/ai-market/v2/invoke", json={
            "product_id": "prod-x", "capability_id": "cap.x@v1", "source_hub": ORACLE_PEER,
            "input": {"q": "hi"}})
        assert response.status_code == 200, response.text
        answer = response.json()
        awr = _fetch_awr(client, answer)
    subject = awr["credentialSubject"]
    assert "outputDigestOf" not in subject       # no member it could name
    assert subject["outputDigest"] != _sri(None)  # and not a digest of nothing


def test_a_browser_buyer_may_send_the_nonce_and_read_its_echo(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMARKET_CORS_ORIGINS", "https://app.example")
    app, db, config = _build(tmp_path, monkeypatch, crypto=False)
    _factory(monkeypatch)
    with TestClient(app) as client:
        pre = client.options("/ai-market/v2/invoke", headers={
            "Origin": "https://app.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-client-nonce"})
        assert pre.status_code == 200, pre.text
        response = client.post("/ai-market/v2/invoke", headers={
            "Origin": "https://app.example", "X-Client-Nonce": NONCE},
            json={**CAP, "source_hub": "local", "input": {"text": "hi"}})
    assert response.status_code == 200, response.text
    exposed = [h.strip().lower() for h in response.headers["access-control-expose-headers"].split(",")]
    assert receipt_binding.CLIENT_NONCE_ECHO_HEADER.lower() in exposed
