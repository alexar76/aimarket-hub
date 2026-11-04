"""The ARD door (aimarket_hub/ard.py, hub_routes/ard.py, docs/ard.md).

What a client that knows only Agent Resource Discovery (IETF DAWN drafts
draft-jimenez-dawn-ard-initial-contact-00 and draft-bu-dawn-ard-discovery-00) gets from
this hub, checked against those drafts:

* initial contact: ``/.well-known/ard.json`` as ``application/ard+json``, cacheable, and a
  ``rel="ard"`` Link on ``HEAD /``;
* every digest in the manifest and in the registry equals the SHA-256 of the bytes served at
  the entry's ``url``;
* every ``trustManifest`` verifies under the key of the ``did:web`` document the hub serves,
  binds the entry's coordinates, and is refused once anything in it changes;
* lookup: ``POST /search`` (required text, filters, pages bound to their query, problem
  details), ``POST /explore`` and ``GET /agents``;
* a federated listing is named after the peer that publishes it and goes unsigned.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from aimarket_hub import ard
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.crawler import Crawler
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
from aimarket_hub.signing import Signer

HUB = "https://hub.example.com"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    config = HubConfig()
    config.hub_url = HUB
    config.hub_name = "Example Hub"
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    db = HubDatabase(config.db_path)
    signer = Signer(config.signing_key_path)
    for cap_id, product, name, description in (
        ("weather.now@v1", "weather", "Weather now", "Current weather for a city: temperature and wind."),
        ("weather.forecast@v1", "weather", "Forecast", "A three-day weather forecast for a city."),
        ("fx.rate@v1", "fx", "FX rate", "Foreign exchange rate between two currencies."),
    ):
        db.upsert_capability(Capability(
            capability_id=cap_id, product_id=product, name=name, description=description,
            price_per_call_usd=0.01, trust_score=0.5, invoke_url=f"https://provider.example/{cap_id}",
            input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        ))
    with TestClient(create_app(config=config, db=db, signer=signer)) as client:
        yield client, signer, db
    db.close()


def _manifest(client):
    response = client.get(ard.WELL_KNOWN_PATH)
    assert response.status_code == 200, response.text
    return response, json.loads(response.content)


def _path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _search(client, body):
    return client.post(ard.REGISTRY_PATH + "/search", json=body)


# ── initial contact ───────────────────────────────────────────


def test_manifest_is_served_at_the_well_known_uri_as_ard_json(hub):
    client, _signer, _db = hub
    response, manifest = _manifest(client)
    # Initial contact §4.3.1: another media type is the outcome "no manifest".
    assert response.headers["content-type"] == ard.MANIFEST_MEDIA_TYPE
    assert "max-age=" in response.headers["cache-control"]
    assert response.headers["access-control-allow-origin"] == "*"
    types = [e["type"] for e in manifest["entries"]]
    assert types == [ard.REGISTRY_TYPE, ard.A2A_CARD_TYPE, ard.MCP_CARD_TYPE, ard.DISCOVERY_DOCUMENT_TYPE]
    registry = manifest["entries"][0]
    assert registry["url"] == HUB + ard.REGISTRY_PATH
    assert registry["identifier"] == "urn:air:hub.example.com:registry:catalogue"
    for entry in manifest["entries"]:
        assert entry["identifier"].startswith("urn:air:hub.example.com:")
        assert ("url" in entry) != ("data" in entry), "exactly one of url or data (§3.3)"

    etag = response.headers["etag"]
    assert client.get(ard.WELL_KNOWN_PATH, headers={"If-None-Match": etag}).status_code == 304
    assert client.head(ard.WELL_KNOWN_PATH).status_code == 200
    # Built once per process: the same bytes on every request.
    assert client.get(ard.WELL_KNOWN_PATH).content == response.content


def test_root_carries_the_ard_link_relation(hub):
    client, _signer, _db = hub
    # Initial contact §4.3.2: the client sends HEAD / and reads the Link field, whatever the status.
    response = client.head("/")
    assert f'<{HUB}{ard.WELL_KNOWN_PATH}>; rel="ard"' in response.headers.get_list("link")
    assert "link" not in {k.lower() for k in client.get("/ai-market/v2/health").headers}


def test_card_digests_pin_the_bytes_the_hub_serves(hub):
    client, _signer, _db = hub
    _response, manifest = _manifest(client)
    pinned = [e for e in manifest["entries"] if "digest" in e]
    assert {e["type"] for e in pinned} == {ard.A2A_CARD_TYPE, ard.MCP_CARD_TYPE}
    for entry in pinned:
        served = client.get(_path(entry["url"])).content
        assert entry["digest"] == "sha256:" + hashlib.sha256(served).hexdigest(), entry["url"]
    # The discovery document is re-signed every minute; a digest of it would go stale.
    discovery = next(e for e in manifest["entries"] if e["type"] == ard.DISCOVERY_DOCUMENT_TYPE)
    assert "digest" not in discovery
    assert client.get(_path(discovery["url"])).status_code == 200


# ── trustManifest and did:web ─────────────────────────────────


def test_trust_manifests_verify_under_the_served_did_document(hub):
    client, signer, _db = hub
    did_doc = client.get(ard.DID_PATH)
    assert did_doc.status_code == 200
    assert did_doc.headers["content-type"] == ard.DID_MEDIA_TYPE
    document = did_doc.json()
    assert document["id"] == "did:web:hub.example.com"
    method = document["verificationMethod"][0]
    assert method["id"] in document["assertionMethod"]
    x = method["publicKeyJwk"]["x"]
    public_key_b64 = base64.b64encode(ard.b64url_decode(x)).decode()
    assert public_key_b64 == signer.public_key_b64, "the DID names the hub's own signing key"

    _response, manifest = _manifest(client)
    for entry in manifest["entries"]:
        tm = entry["trustManifest"]
        assert tm["identity"] == document["id"]
        assert tm["subject"] == {k: entry[k] for k in ("identifier", "type", "version", "digest") if k in entry}
        header = json.loads(ard.b64url_decode(tm["signature"].split(".")[0]))
        assert header == {"alg": "EdDSA", "kid": method["id"]}
        assert tm["signature"].split(".")[1] == "", "detached JWS: empty payload segment"
        assert ard.verify_trust_manifest(tm, public_key_b64)


def test_a_changed_trust_manifest_no_longer_verifies(hub):
    client, signer, _db = hub
    _response, manifest = _manifest(client)
    tm = manifest["entries"][1]["trustManifest"]
    assert ard.verify_trust_manifest(tm, signer.public_key_b64)
    for mutate in (
        lambda m: m["subject"].__setitem__("digest", "sha256:" + "00" * 32),
        lambda m: m["subject"].__setitem__("identifier", "urn:air:evil.example:agent:a2a"),
        lambda m: m.__setitem__("identity", "did:web:evil.example"),
        lambda m: m.__setitem__("issuedAt", "2000-01-01T00:00:00Z"),
    ):
        changed = json.loads(json.dumps(tm))
        mutate(changed)
        assert not ard.verify_trust_manifest(changed, signer.public_key_b64)
    stranger = base64.b64encode(hashlib.sha256(b"another hub").digest()).decode()
    assert not ard.verify_trust_manifest(tm, stranger)


def test_no_did_web_off_an_https_origin_root():
    assert ard.did_web("https://hub.example.com") == "did:web:hub.example.com"
    assert ard.did_web("https://hub.example.com/") == "did:web:hub.example.com"
    assert ard.did_web("https://hub.example.com:8443") == "did:web:hub.example.com%3A8443"
    assert ard.did_web("https://example.com/hub") == ""
    assert ard.did_web("http://localhost:9083") == ""


# ── the registry ──────────────────────────────────────────────


def test_search_returns_scored_signed_entries_whose_descriptor_matches_the_digest(hub):
    client, signer, _db = hub
    response = _search(client, {"query": {"text": "weather forecast for a city"}})
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    ids = [r["identifier"] for r in results]
    assert "urn:air:hub.example.com:weather:weather.forecast@v1" in ids
    assert results[0]["score"] >= results[-1]["score"]
    for item in results:
        assert isinstance(item["score"], int) and 0 <= item["score"] <= 100
        assert item["source"] == HUB
        assert item["type"] == ard.CAPABILITY_TYPE
        descriptor = client.get(_path(item["url"]))
        assert descriptor.status_code == 200
        assert descriptor.headers["content-type"] == ard.CAPABILITY_TYPE
        assert item["digest"] == "sha256:" + hashlib.sha256(descriptor.content).hexdigest()
        body = json.loads(descriptor.content)
        assert body["identifier"] == item["identifier"]
        assert body["protocol"] == "aimarket-v2"
        assert body["invoke"]["url"] == HUB + "/ai-market/v2/invoke"
        assert body["invoke"]["body"]["capability_id"] == body["capability_id"]
        tm = item["trustManifest"]
        # §3.5.1: a verifier compares the subject with the item it holds, so the item must
        # carry every coordinate the subject binds (version included). Caught live on
        # modelmarket.dev 2026-11-04, where results lacked version.
        assert tm["subject"] == {k: item[k] for k in ("identifier", "type", "version", "digest") if k in item}
        assert "version" in item
        assert ard.verify_trust_manifest(tm, signer.public_key_b64)


def test_search_requires_text_and_refuses_what_it_does_not_evaluate(hub):
    client, _signer, _db = hub
    cases = [
        ({"query": {}}, "query.text is required"),
        ({"query": {"text": "weather", "filter": {"okf:regulatoryZone": ["JP"]}}}, "unsupported filter term"),
        ({"query": {"text": "weather", "filter": {"type": []}}}, "non-empty array"),
        ({"query": {"text": "weather"}, "federation": "everywhere"}, "federation must be"),
        ({"query": {"text": "weather"}, "pageSize": 0}, "positive integer"),
        ({"query": {"text": "weather"}, "pageToken": "not-a-token"}, "not one this registry issued"),
    ]
    for body, detail in cases:
        response = _search(client, body)
        assert response.status_code == 400, body
        assert response.headers["content-type"] == ard.PROBLEM_MEDIA_TYPE
        problem = response.json()
        assert problem["error"] == "INVALID_ARGUMENT" and problem["status"] == 400
        assert detail in problem["detail"]
    raw = client.post(ard.REGISTRY_PATH + "/search", content=b"{not json",
                      headers={"Content-Type": "application/json"})
    assert raw.status_code == 400 and raw.json()["error"] == "INVALID_ARGUMENT"


def test_search_pages_are_bound_to_their_query(hub):
    client, _signer, _db = hub
    first = _search(client, {"query": {"text": "weather"}, "pageSize": 1}).json()
    assert len(first["results"]) == 1 and "nextPageToken" in first
    second = _search(client, {"query": {"text": "weather"}, "pageSize": 1,
                              "pageToken": first["nextPageToken"]}).json()
    assert second["results"][0]["identifier"] != first["results"][0]["identifier"]
    other = _search(client, {"query": {"text": "exchange rate"}, "pageSize": 1,
                             "pageToken": first["nextPageToken"]})
    assert other.status_code == 400 and "different query" in other.json()["detail"]


def test_search_filters_and_federation_modes(hub):
    client, _signer, _db = hub
    only_fx = _search(client, {"query": {"text": "weather rate", "filter": {
        "identifier": "urn:air:hub.example.com:fx:fx.rate@v1"}}}).json()["results"]
    assert [r["identifier"] for r in only_fx] in ([], ["urn:air:hub.example.com:fx:fx.rate@v1"])
    none = _search(client, {"query": {"text": "weather", "filter": {"publisher": "elsewhere.example"}}})
    assert none.json()["results"] == []
    assert _search(client, {"query": {"text": "weather"}, "federation": "referrals"}).json()["referrals"] == []
    assert "referrals" not in _search(client, {"query": {"text": "weather"}, "federation": "none"}).json()


def test_agents_lists_every_entry_in_identifier_order(hub):
    client, _signer, _db = hub
    response = client.get(ard.REGISTRY_PATH + "/agents")
    assert response.status_code == 200
    ids = [e["identifier"] for e in response.json()["entries"]]
    assert ids == sorted(ids) and len(ids) == 3
    paged = client.get(ard.REGISTRY_PATH + "/agents", params={"pageSize": 2}).json()
    rest = client.get(ard.REGISTRY_PATH + "/agents",
                      params={"pageSize": 2, "pageToken": paged["nextPageToken"]}).json()
    assert [e["identifier"] for e in paged["entries"] + rest["entries"]] == ids
    tools = {e["identifier"]: e["capabilities"][0] for e in response.json()["entries"]}
    weather_ids = sorted(i for i in ids if ":weather:" in i)
    weather = client.get(ard.REGISTRY_PATH + "/agents",
                         params=[("capabilities", tools[i]) for i in weather_ids])
    assert [e["identifier"] for e in weather.json()["entries"]] == weather_ids, "repeated parameter = OR"
    both = client.get(ard.REGISTRY_PATH + "/agents", params=[
        ("capabilities", tools[weather_ids[0]]), ("publisher", "elsewhere.example")])
    assert both.json()["entries"] == [], "different terms = AND"
    unknown = client.get(ard.REGISTRY_PATH + "/agents", params={"color": "blue"})
    assert unknown.status_code == 400 and unknown.json()["error"] == "INVALID_ARGUMENT"


def test_explore_counts_facets(hub):
    client, _signer, _db = hub
    response = client.post(ard.REGISTRY_PATH + "/explore", json={
        "resultType": {"facets": [{"field": "type"}, {"field": "publisher", "limit": 1}]}})
    assert response.status_code == 200, response.text
    facets = response.json()["facets"]
    assert facets["type"]["buckets"] == [{"value": ard.CAPABILITY_TYPE, "count": 3}]
    assert facets["publisher"]["buckets"] == [{"value": "hub.example.com", "count": 3}]
    bad = client.post(ard.REGISTRY_PATH + "/explore", json={"resultType": {"facets": [{"field": "price"}]}})
    assert bad.status_code == 400 and bad.json()["error"] == "INVALID_ARGUMENT"


def test_descriptor_of_an_unknown_capability_is_a_problem(hub):
    client, _signer, _db = hub
    response = client.get(ard.REGISTRY_PATH + "/capability",
                          params={"product_id": "nope", "capability_id": "nope@v1"})
    assert response.status_code == 404
    assert response.json()["error"] == "NOT_FOUND"


def test_registry_describes_itself(hub):
    client, _signer, _db = hub
    info = client.get(ard.REGISTRY_PATH).json()
    assert info["endpoints"]["search"]["url"] == HUB + ard.REGISTRY_PATH + "/search"
    assert info["signing"]["identity"] == "did:web:hub.example.com"
    assert info["protocol"] == "aimarket-v2"


# ── names and federated listings ──────────────────────────────


def test_urn_fields_cannot_be_split_by_an_odd_id():
    name = ard.urn("hub.example.com", "a:b/c", "x y?#")
    assert name == "urn:air:hub.example.com:a%3Ab%2Fc:x%20y%3F%23"
    assert ard.urn_publisher(name) == "hub.example.com"
    assert len(name.split(":")) == 5


def test_a_federated_listing_is_named_after_its_publisher_and_left_unsigned():
    cap = Capability(capability_id="geo.lookup@v1", product_id="geo", name="Geo",
                     description="Where a place is.", source_hub="https://peer.example.org",
                     price_per_call_usd=0.002)
    made = ard.capability_entry(cap, HUB, display_name="Geo lookup", access_mode="paid", tags=["maps"])
    assert made.entry["identifier"] == "urn:air:peer.example.org:geo:geo.lookup@v1"
    assert made.source == "https://peer.example.org"
    assert not made.local
    assert made.entry["tags"] == ["maps"]
    # The descriptor tells a buyer to call through THIS hub, naming the peer as source_hub.
    descriptor = json.loads(made.descriptor)
    assert descriptor["invoke"]["url"] == HUB + "/ai-market/v2/invoke"
    assert descriptor["invoke"]["body"]["source_hub"] == "https://peer.example.org"


@pytest.mark.asyncio
async def test_a_crawled_peer_listing_reaches_search_unsigned(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    monkeypatch.setattr("aimarket_hub.crawler._url_is_safe", lambda url: url.startswith("https://"))
    config = HubConfig()
    config.hub_url = HUB
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    peer_url = "https://peer.example.org"
    peer_signer = Signer(tmp_path / "peer.key")
    signer = Signer(config.signing_key_path)
    db = HubDatabase(config.db_path)
    db.upsert_peer(Peer(url=peer_url, name="Peer", public_key=peer_signer.public_key_b64, trusted=True))
    wk = {"name": "Peer", "protocol_versions": ["v1", "v2"], "manifest_url": peer_url + "/manifest",
          "capabilities_count": 1, "signer_public_key": peer_signer.public_key_b64}
    manifest = {
        "protocol_version": "v2", "generated_at": datetime.now(timezone.utc).isoformat(),
        "capabilities_count": 1,
        "tools": [{"name": "geo", "capability_id": "geo.lookup@v1", "product_id": "geo",
                   "description": "Where a place is on the map.", "input_schema": {},
                   "price_per_call_usd": 0.002}],
    }
    manifest["signature"] = peer_signer.sign_manifest(manifest)
    crawler = Crawler(config=config, db=db, signer=signer)

    async def get(url):
        return httpx.Response(200, json=wk if ".well-known/" in url else manifest)

    crawler._safe_get = AsyncMock(side_effect=get)
    try:
        assert await crawler._crawl_one(peer_url + "/.well-known/ai-market.json", 0, "test")
        with TestClient(create_app(config=config, db=db, signer=signer)) as client:
            results = _search(client, {"query": {"text": "where a place is on the map"}}).json()["results"]
            geo = next(r for r in results if r["identifier"] == "urn:air:peer.example.org:geo:geo.lookup@v1")
            assert geo["source"] == peer_url
            assert "trustManifest" not in geo, "this hub is not the publisher of a peer's listing"
            descriptor = client.get(_path(geo["url"]))
            assert geo["digest"] == "sha256:" + hashlib.sha256(descriptor.content).hexdigest()
    finally:
        await crawler.close()
        db.close()
