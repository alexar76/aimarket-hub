"""The AI Catalog door (aimarket_hub/ai_catalog.py, hub_routes/ai_catalog.py, docs/ai-catalog.md).

What a client that knows only AI Catalog (https://ai-catalog.io/, specification text of
2026-11-11) and MCP Server Cards (SEP-2127, modelcontextprotocol/ext-server-card 526201bb) gets
from this hub, checked against those documents:

* discovery: ``/.well-known/ai-catalog.json`` as ``application/ai-catalog+json``, cacheable and
  revalidated with an ETag, and a ``rel="ai-catalog"`` Link on ``HEAD /``;
* the catalogue has the structure the specification's CDDL gives, a ``host``, and entries for the
  hub's MCP Server Card, A2A Agent Card, capabilities, ARD registry and discovery document;
* every digest equals the SHA-256 of the bytes served at the entry's ``url``;
* the MCP Server Card validates against the published v1 JSON Schema, is served where the
  extension reserves a place for it, lists no tools, and agrees with what the live endpoint says;
* the nested catalogue lists every sellable capability with the descriptor and digest the ARD
  registry publishes;
* a capability copied from a peer is not named in the peer's ``urn:air`` namespace: it gets an
  identifier under this hub, which resolves;
* every entry the hub publishes carries its Ed25519 Trust Manifest and every catalogue a snapshot
  signature (ML-DSA-65 when the hub signs hybrid), both under the two signer profiles the hub
  serves the definitions of, and both refused once anything they cover changes.
"""
from __future__ import annotations

import hashlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from aimarket_hub import ai_catalog, ard, mcp_gateway
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.crawler import Crawler
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability, Peer
from aimarket_hub.signing import Signer

HUB = "https://hub.example.com"
FIXTURES = Path(__file__).parent / "fixtures" / "ai_catalog"


def _config(tmp_path, monkeypatch) -> HubConfig:
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    config = HubConfig()
    config.hub_url = HUB
    config.hub_name = "Example Hub"
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    return config


def _did_document(client) -> dict:
    return json.loads(client.get(ard.DID_PATH).content)


@pytest.fixture(params=["ed25519", "hybrid"])
def hub(request, monkeypatch, tmp_path):
    """A hub that signs with Ed25519 only, and one that also holds an ML-DSA-65 key."""
    config = _config(tmp_path, monkeypatch)
    db = HubDatabase(config.db_path)
    signer = Signer(config.signing_key_path, pqc=request.param == "hybrid")
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
        client.hybrid = request.param == "hybrid"
        yield client
    db.close()


def _path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _catalog(client, path=ai_catalog.WELL_KNOWN_PATH):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response, json.loads(response.content)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ── discovery ─────────────────────────────────────────────────


def test_catalog_is_served_at_the_well_known_uri_as_ai_catalog_json(hub):
    response, catalog = _catalog(hub)
    # Specification, "Location Independence": SHOULD be served as application/ai-catalog+json.
    assert response.headers["content-type"] == ai_catalog.MEDIA_TYPE
    assert "max-age=" in response.headers["cache-control"]
    assert response.headers["access-control-allow-origin"] == "*"
    assert catalog["specVersion"] == "1.0"
    assert ai_catalog.problems(catalog) == []
    # Built once per process: the same bytes on every request.
    assert hub.get(ai_catalog.WELL_KNOWN_PATH).content == response.content
    assert hub.head(ai_catalog.WELL_KNOWN_PATH).status_code == 200


def test_catalog_names_its_operator(hub):
    # Level 2 (Discoverable): a host object identifying the catalogue's operator.
    _response, catalog = _catalog(hub)
    assert catalog["host"] == {"displayName": "Example Hub", "identifier": "did:web:hub.example.com"}
    # The identifier is the DID whose document the hub serves.
    assert json.loads(hub.get(ard.DID_PATH).content)["id"] == catalog["host"]["identifier"]


def test_root_carries_the_ai_catalog_link_relation(hub):
    # "Link Relation Discovery": a client fetches the site and reads Link rel="ai-catalog".
    links = hub.head("/").headers.get_list("link")
    assert f'<{HUB}{ai_catalog.WELL_KNOWN_PATH}>; rel="ai-catalog"' in links
    # The ARD relation stays beside it.
    assert f'<{HUB}{ard.WELL_KNOWN_PATH}>; rel="ard"' in links
    assert "link" not in {k.lower() for k in hub.get("/ai-market/v2/health").headers}


def test_catalog_is_revalidated_with_its_entity_tag(hub):
    response, _catalog_doc = _catalog(hub)
    etag = response.headers["etag"]
    assert hub.get(ai_catalog.WELL_KNOWN_PATH, headers={"If-None-Match": etag}).status_code == 304
    assert hub.get(ai_catalog.WELL_KNOWN_PATH, headers={"If-None-Match": f'"other", W/{etag}'}).status_code == 304
    assert hub.get(ai_catalog.WELL_KNOWN_PATH, headers={"If-None-Match": '"other"'}).status_code == 200


# ── the entries ───────────────────────────────────────────────


def test_catalog_lists_the_hubs_cards_capabilities_registry_and_discovery_document(hub):
    _response, catalog = _catalog(hub)
    assert [(e["type"], _path(e["url"])) for e in catalog["entries"]] == [
        ("application/mcp-server-card+json", "/mcp/server-card"),
        ("application/a2a-agent-card+json", "/.well-known/agent-card.json"),
        ("application/ai-catalog+json", "/ai-catalog/v1/capabilities.json"),
        ("application/ai-registry+json", "/ard/v1"),
        ("application/json", "/.well-known/agent-marketplace"),
    ]
    for entry in catalog["entries"]:
        assert entry["url"].startswith(HUB + "/")
        assert entry["identifier"].startswith("urn:air:hub.example.com:")
        assert entry["publisher"] == {"identifier": "did:web:hub.example.com", "displayName": "Example Hub",
                                      "identityType": "did"}
        assert hub.get(_path(entry["url"])).status_code == 200, entry["url"]


def test_card_entries_do_not_repeat_what_the_cards_state(hub):
    # "that artifact is the authoritative source and displayName SHOULD be omitted"; the same
    # for description and version (specification, "Catalog Entry").
    _response, catalog = _catalog(hub)
    for entry in catalog["entries"][:2]:
        assert not {"displayName", "description", "version"} & set(entry)


def test_card_digests_pin_the_bytes_the_hub_serves(hub):
    _response, catalog = _catalog(hub)
    pinned = [e for e in catalog["entries"] if "digest" in e]
    assert [e["type"] for e in pinned] == ["application/mcp-server-card+json", "application/a2a-agent-card+json"]
    for entry in pinned:
        assert entry["digest"] == _sha256(hub.get(_path(entry["url"])).content)


def test_the_same_artefact_has_the_same_identifier_in_the_ard_manifest(hub):
    _response, catalog = _catalog(hub)
    ours = {e["type"]: e["identifier"] for e in catalog["entries"]}
    theirs = {e["type"]: e["identifier"] for e in json.loads(hub.get(ard.WELL_KNOWN_PATH).content)["entries"]}
    for media_type in theirs:
        assert ours[media_type] == theirs[media_type]


# ── the MCP Server Card ───────────────────────────────────────


def _card(client):
    response = client.get(ai_catalog.SERVER_CARD_PATH, headers={"Accept": "application/mcp-server-card+json"})
    assert response.status_code == 200, response.text
    return response, json.loads(response.content)


def test_server_card_validates_against_the_published_schema(hub):
    _response, card = _card(hub)
    schema = json.loads((FIXTURES / "server-card.schema.v1.json").read_text())
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/ServerCard", "$defs": schema["$defs"]},
        format_checker=jsonschema.FormatChecker(),
    )
    assert [e.message for e in validator.iter_errors(card)] == []
    assert ai_catalog.server_card_problems(card) == []


def test_server_card_is_served_where_the_extension_reserves_a_place_for_it(hub):
    response, card = _card(hub)
    remote = card["remotes"][0]
    # "MCP Servers MAY host their Server Card at GET <streamable-http-url>/server-card".
    assert remote == {
        "type": "streamable-http",
        "url": HUB + "/mcp",
        "supportedProtocolVersions": list(mcp_gateway.SUPPORTED_PROTOCOL_VERSIONS),
        "headers": remote["headers"],
    }
    assert HUB + ai_catalog.SERVER_CARD_PATH == remote["url"] + "/server-card"
    assert response.headers["content-type"] == "application/mcp-server-card+json"
    # discovery.md, "CORS Requirements" and "Caching".
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-methods"] == "GET"
    assert response.headers["access-control-allow-headers"] == "Content-Type, If-None-Match"
    assert response.headers["access-control-expose-headers"] == "ETag"
    assert "max-age=3600" in response.headers["cache-control"]
    etag = response.headers["etag"]
    assert hub.get(ai_catalog.SERVER_CARD_PATH, headers={"If-None-Match": etag}).status_code == 304
    assert hub.head(ai_catalog.SERVER_CARD_PATH).status_code == 200


def test_server_card_names_the_server_in_this_hosts_namespace(hub):
    _response, card = _card(hub)
    assert card["name"] == "com.example.hub/aimarket-hub-mcp"
    assert card["title"] == "Example Hub MCP"
    assert card["websiteUrl"] == HUB + "/"
    # The key header is optional and secret: free tools need no key.
    assert card["remotes"][0]["headers"] == [{
        "name": "X-API-Key", "isRequired": False, "isSecret": True,
        "description": card["remotes"][0]["headers"][0]["description"],
    }]


def test_server_card_lists_no_tools(hub):
    # SEP-2127, "Why Exclude Primitives?": a card does not enumerate tools, resources or prompts.
    _response, card = _card(hub)
    assert not {"tools", "resources", "prompts", "serverInfo", "instructions"} & set(card)
    # The older card, which scanners read for a tool list, is still served where it was.
    legacy = hub.get("/.well-known/mcp/server-card.json").json()
    assert legacy["tools"] and legacy["serverInfo"]["name"] == mcp_gateway.SERVER_NAME


def test_server_card_agrees_with_the_live_endpoint(hub):
    # discovery.md, "Consistency with Runtime Behavior": the card SHOULD NOT contradict the
    # serverInfo and the protocol versions a connected client observes.
    _response, card = _card(hub)
    for version in card["remotes"][0]["supportedProtocolVersions"]:
        answer = hub.post(_path(card["remotes"][0]["url"]), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        }, headers={"Accept": "application/json, text/event-stream"})
        assert answer.status_code == 200, answer.text
        # The endpoint frames its answer as one server-sent event.
        frame = next(line for line in answer.text.splitlines() if line.startswith("data:"))
        result = json.loads(frame[5:])["result"]
        assert result["protocolVersion"] == version
        assert result["serverInfo"]["version"] == card["version"]
        assert card["name"].endswith("/" + result["serverInfo"]["name"])


def test_server_card_name_can_be_the_operators_registry_name(monkeypatch, tmp_path):
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setenv("AIMARKET_MCP_SERVER_CARD_NAME", "io.github.example/hub")
    db = HubDatabase(config.db_path)
    try:
        with TestClient(create_app(config=config, db=db, signer=Signer(config.signing_key_path))) as client:
            assert _card(client)[1]["name"] == "io.github.example/hub"
    finally:
        db.close()


@pytest.mark.parametrize("override", ["no-slash", "two/slashes/here", "bad name/x", "a/" + "b" * 250])
def test_a_server_card_name_that_is_not_namespace_slash_name_is_ignored(override):
    assert ai_catalog.server_card_name(HUB, "aimarket-hub-mcp", override) == "com.example.hub/aimarket-hub-mcp"


@pytest.mark.parametrize(("url", "name"), [
    ("https://modelmarket.dev", "dev.modelmarket/srv"),
    ("https://hub.modelmarket.dev:8443/", "dev.modelmarket.hub/srv"),
    ("http://localhost:9083", "localhost/srv"),
    ("http://127.0.0.1:9083", "127.0.0.1/srv"),
])
def test_server_card_namespace_is_the_host_in_reverse(url, name):
    assert ai_catalog.server_card_name(url, "srv") == name
    assert ai_catalog.server_card_problems(ai_catalog.server_card(
        url, hub_name="H", server_name="srv", server_version="1.0.0", protocol_versions=["2025-06-18"])) == []


# ── what the hub sells ────────────────────────────────────────


def test_capabilities_catalog_lists_every_sellable_capability(hub):
    response, nested = _catalog(hub, ai_catalog.CAPABILITIES_PATH)
    assert response.headers["content-type"] == ai_catalog.MEDIA_TYPE
    assert ai_catalog.problems(nested) == []
    assert nested["host"]["identifier"] == "did:web:hub.example.com"
    assert [e["identifier"] for e in nested["entries"]] == [
        "urn:air:hub.example.com:fx:fx.rate@v1",
        "urn:air:hub.example.com:weather:weather.forecast@v1",
        "urn:air:hub.example.com:weather:weather.now@v1",
    ]
    for entry in nested["entries"]:
        assert entry["type"] == ard.CAPABILITY_TYPE
        assert entry["publisher"]["identifier"] == "did:web:hub.example.com"
        assert "aimarket" in entry["tags"]
        # The entry says what a generic client cannot read out of this protocol's descriptor.
        assert entry["displayName"] and entry["description"]
    etag = response.headers["etag"]
    assert hub.get(ai_catalog.CAPABILITIES_PATH, headers={"If-None-Match": etag}).status_code == 304


def test_capability_entries_pin_the_descriptor_the_ard_registry_publishes(hub):
    _response, nested = _catalog(hub, ai_catalog.CAPABILITIES_PATH)
    registry = {e["identifier"]: e for e in hub.get(ard.REGISTRY_PATH + "/agents").json()["entries"]}
    assert set(registry) == {e["identifier"] for e in nested["entries"]}
    for entry in nested["entries"]:
        same = registry[entry["identifier"]]
        assert (entry["url"], entry["digest"], entry["version"]) == (same["url"], same["digest"], same["version"])
        descriptor = hub.get(_path(entry["url"]))
        assert entry["digest"] == _sha256(descriptor.content)
        # The extension carries the AIMarket Protocol v2 terms of the call; the price is a decimal
        # string, because the document is signed over its JCS bytes.
        terms = entry["extensions"][ai_catalog.EXTENSION_KEY]
        assert terms == {**same["metadata"]["aimarket"], "price_per_call_usd": "0.01"}
        assert same["metadata"]["aimarket"]["price_per_call_usd"] == 0.01
        assert terms["invoke_url"] == HUB + "/ai-market/v2/invoke"
        assert json.loads(descriptor.content)["capability_id"] == terms["capability_id"]


@pytest.mark.asyncio
async def test_a_peers_capability_gets_an_identifier_under_this_hub(monkeypatch, tmp_path):
    """Specification, "Catalog Projection", rule 3: an operator that is neither the artifact
    publisher nor its delegate MUST use a non-``urn:air`` identifier under its own control."""
    monkeypatch.setattr("aimarket_hub.crawler._url_is_safe", lambda url: url.startswith("https://"))
    config = _config(tmp_path, monkeypatch)
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
            _response, nested = _catalog(client, ai_catalog.CAPABILITIES_PATH)
            assert ai_catalog.problems(nested) == []
            (geo,) = [e for e in nested["entries"]
                      if e["extensions"][ai_catalog.EXTENSION_KEY]["source_hub"] == peer_url]
            prefix = HUB + ai_catalog.IDS_PATH + "/"
            assert geo["identifier"].startswith(prefix) and len(geo["identifier"]) == len(prefix) + 32
            assert not geo["identifier"].startswith("urn:air:")
            # The same capability from the same peer keeps its identifier.
            assert geo["identifier"] == ai_catalog.projected_identifier(HUB, peer_url, "geo", "geo.lookup@v1")
            assert geo["identifier"] == ai_catalog.projected_identifier(HUB + "/", peer_url + "/", "geo", "geo.lookup@v1")
            # Where it came from: the peer as publisher (unsigned), the coordinates in the extension.
            assert geo["publisher"] == {"identifier": "peer.example.org", "displayName": "peer.example.org",
                                        "identityType": "dns"}
            terms = geo["extensions"][ai_catalog.EXTENSION_KEY]
            assert (terms["source_hub"], terms["product_id"], terms["capability_id"]) == (peer_url, "geo", "geo.lookup@v1")
            assert terms["price_per_call_usd"] == "0.002"
            # This hub is not the publisher of a peer's listing, so it does not endorse it.
            assert "trustManifests" not in geo
            # The descriptor is the one the ARD registry pins, and the identifier resolves to it.
            descriptor = client.get(_path(geo["url"]))
            assert geo["digest"] == _sha256(descriptor.content)
            hop = client.get(_path(geo["identifier"]), follow_redirects=False)
            assert hop.status_code == 303 and hop.headers["location"] == geo["url"]
            missing = client.get(ai_catalog.IDS_PATH + "/" + "0" * 32, follow_redirects=False)
            assert missing.status_code == 404
            assert missing.headers["content-type"] == ard.PROBLEM_MEDIA_TYPE
    finally:
        await crawler.close()
        db.close()


# ── signatures ────────────────────────────────────────────────


def _documents(client):
    return [_catalog(client)[1], _catalog(client, ai_catalog.CAPABILITIES_PATH)[1]]


def test_every_entry_the_hub_publishes_carries_its_signed_endorsement(hub):
    did_document = _did_document(hub)
    endorsed = 0
    for document in _documents(hub):
        assert ai_catalog.problems(document) == []
        for entry in document["entries"]:
            if "digest" not in entry:
                assert "trustManifests" not in entry      # nothing pinned, nothing to bind a signature to
                continue
            (manifest,) = entry["trustManifests"]
            assert manifest["contributor"] == manifest["signature"]["signer"] == "did:web:hub.example.com"
            assert manifest["signature"]["profile"] == ai_catalog.PROFILE_EDDSA
            assert set(manifest) == {"contributor", "subject", "signature"}
            assert manifest["subject"] == {k: entry[k] for k in ("identifier", "type", "digest", "version") if k in entry}
            assert ai_catalog.endorsement_problem(entry, manifest, did_document) == ""
            endorsed += 1
    assert endorsed == 5        # two cards, three capabilities


def test_an_endorsement_is_refused_once_what_it_covers_changes(hub):
    did_document = _did_document(hub)
    entry = _catalog(hub, ai_catalog.CAPABILITIES_PATH)[1]["entries"][0]
    (manifest,) = entry["trustManifests"]
    check = ai_catalog.endorsement_problem

    def changed(path, value):
        copy_ = json.loads(json.dumps(manifest))
        target = copy_
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return copy_

    # The entry is swapped under a valid manifest: the subject no longer matches it.
    assert check({**entry, "digest": "sha256:" + "0" * 64}, manifest, did_document) == "subject.digest is not the entry's"
    assert check({**entry, "version": "v9"}, manifest, did_document) == "subject.version is not the entry's"
    assert check({k: v for k, v in entry.items() if k != "version"}, manifest, did_document) == "subject.version is not the entry's"
    # The manifest is edited: the signature no longer covers it.
    for path, value in ((("subject", "digest"), "sha256:" + "0" * 64), (("subject", "identifier"), "urn:air:hub.example.com:x:y"),
                        (("signature", "issuedAt"), "2020-01-01T00:00:00Z"), (("extra",), True)):
        assert check(entry, changed(path, value), did_document) in (
            "the signature does not verify", f"subject.{path[-1]} is not the entry's"), path
    assert check(entry, changed(("contributor",), "did:web:other.example"), did_document) == "the signature does not verify"
    # Another origin's key signs for this hub's name: the name in the identifier decides.
    other = {**entry, "identifier": "urn:air:other.example:fx:fx.rate@v1"}
    assert "subject.identifier" in check(other, manifest, did_document)
    # A key that is listed but not as an assertion method is not accepted.
    not_asserted = {**did_document, "assertionMethod": [], "authentication": did_document["assertionMethod"]}
    assert check(entry, manifest, not_asserted) == "kid does not name exactly one assertion method of the signer"
    assert check(entry, manifest, {**did_document, "id": "did:web:other.example"}) == "the DID document is not the signer's"
    # A manifest signature cannot be passed off as a catalogue signature (the protected typ differs).
    assert ai_catalog.signature_problem(manifest, did_document, typ=ai_catalog.TYP_CATALOG) \
        == "the protected header must name typ ai-catalog+jws"


def test_an_endorsement_is_not_accepted_outside_its_time(hub):
    did_document = _did_document(hub)
    entry = _catalog(hub, ai_catalog.CAPABILITIES_PATH)[1]["entries"][0]
    (manifest,) = entry["trustManifests"]
    issued = datetime.fromisoformat(manifest["signature"]["issuedAt"].replace("Z", "+00:00"))
    assert ai_catalog.endorsement_problem(entry, manifest, did_document, now=issued - timedelta(seconds=200)) == ""   # skew
    assert ai_catalog.endorsement_problem(entry, manifest, did_document, now=issued - timedelta(hours=1)) \
        == "the endorsement is not valid yet"
    assert ai_catalog.endorsement_problem(entry, manifest, did_document, now=issued + timedelta(days=3650)) == ""


def test_each_catalog_carries_a_snapshot_signature_post_quantum_when_the_hub_signs_hybrid(hub):
    did_document = _did_document(hub)
    profile = ai_catalog.PROFILE_ML_DSA_65 if hub.hybrid else ai_catalog.PROFILE_EDDSA
    for document in _documents(hub):
        signature = document["signature"]
        assert signature["signer"] == document["host"]["identifier"] == "did:web:hub.example.com"
        assert signature["profile"] == profile
        header = json.loads(ard.b64url_decode(signature["jws"].split(".")[0]))
        assert header == {"alg": "ML-DSA-65" if hub.hybrid else "EdDSA", "typ": "ai-catalog+jws",
                          "kid": "did:web:hub.example.com#" + ("hub-ml-dsa-65" if hub.hybrid else "hub-ed25519")}
        assert ai_catalog.signature_problem(document, did_document, typ=ai_catalog.TYP_CATALOG) == ""
        # The signature covers the whole document: every entry, the host, nested manifests.
        for change in (
            lambda d: d["entries"][0].__setitem__("url", "https://evil.example/card"),
            lambda d: d["entries"].pop(),
            lambda d: d["entries"].append({"identifier": "urn:air:evil.example:mcp:x", "type": "x", "url": "https://evil.example/"}),
            lambda d: d["host"].__setitem__("displayName", "Someone Else"),
            lambda d: d.__setitem__("specVersion", "1.1"),
            lambda d: d.__setitem__("added", 1),
            lambda d: d["entries"][0]["trustManifests"][0]["signature"].__setitem__("jws", "e30..AA"),
        ):
            tampered = json.loads(json.dumps(document))
            change(tampered)
            assert ai_catalog.signature_problem(tampered, did_document, typ=ai_catalog.TYP_CATALOG) \
                == "the signature does not verify"
        # additionalSignatures is reserved and outside the payload: adding it changes nothing.
        assert ai_catalog.signature_problem({**document, "additionalSignatures": [1]}, did_document,
                                            typ=ai_catalog.TYP_CATALOG) == ""


def test_the_did_document_lists_the_post_quantum_key_as_an_rfc_9964_jwk(hub):
    did_document = _did_document(hub)
    methods = {m["id"]: m for m in did_document["verificationMethod"]}
    assert did_document["assertionMethod"] == list(methods)
    assert methods["did:web:hub.example.com#hub-ed25519"]["publicKeyJwk"].keys() == {"kty", "crv", "x"}
    if not hub.hybrid:
        assert list(methods) == ["did:web:hub.example.com#hub-ed25519"]
        return
    jwk = methods["did:web:hub.example.com#hub-ml-dsa-65"]["publicKeyJwk"]
    assert (jwk["kty"], jwk["alg"], set(jwk)) == ("AKP", "ML-DSA-65", {"kty", "alg", "pub"})
    assert len(ard.b64url_decode(jwk["pub"])) == 1952        # FIPS 204, ML-DSA-65 public key


def test_a_post_quantum_signature_is_not_replaced_by_a_classical_one(hub):
    """A reader that asks for ML-DSA-65 gets no answer from an Ed25519 signature, and the reverse:
    the profile is selected by its exact name and never inferred."""
    if not hub.hybrid:
        pytest.skip("needs the hub that holds an ML-DSA-65 key")
    did_document = _did_document(hub)
    document = _catalog(hub)[1]
    downgraded = json.loads(json.dumps(document))
    downgraded["signature"]["profile"] = ai_catalog.PROFILE_EDDSA
    assert ai_catalog.signature_problem(downgraded, did_document, typ=ai_catalog.TYP_CATALOG) \
        == "the protected header must name alg EdDSA"
    # The Ed25519 key cannot stand in for the ML-DSA-65 one, nor a key the signature brings along.
    wrong_key = json.loads(json.dumps(did_document))
    wrong_key["verificationMethod"][1]["publicKeyJwk"] = wrong_key["verificationMethod"][0]["publicKeyJwk"]
    assert ai_catalog.signature_problem(document, wrong_key, typ=ai_catalog.TYP_CATALOG) == "the key is not a public ML-DSA-65 key"
    foreign = json.loads(json.dumps(document))
    foreign["signature"]["profile"] = "did-web-v1"
    assert "is not one this verifier implements" in ai_catalog.signature_problem(foreign, did_document, typ=ai_catalog.TYP_CATALOG)


def test_the_same_catalogue_is_signed_once(hub, monkeypatch):
    # Rebuilt on every request, the document keeps its bytes: one signature per content, so the
    # ETag changes when an offer changes and not when a minute passes.
    monkeypatch.setenv("AIMARKET_AI_CATALOG_CACHE_S", "0")
    calls = []
    real = ai_catalog.sign_catalog
    monkeypatch.setattr(ai_catalog, "sign_catalog", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = hub.get(ai_catalog.CAPABILITIES_PATH)
    for _ in range(3):
        again = hub.get(ai_catalog.CAPABILITIES_PATH)
        assert again.content == first.content and again.headers["etag"] == first.headers["etag"]
    assert len(calls) == 1


def test_the_signer_profiles_are_defined_where_their_names_point(hub):
    for profile in (ai_catalog.PROFILE_EDDSA, ai_catalog.PROFILE_ML_DSA_65):
        assert profile.startswith("https://modelmarket.dev" + ai_catalog.PROFILES_PATH + "/")
        response = hub.get(urlparse(profile).path)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/markdown")
        assert profile in response.text and "## 3. Key discovery and authorisation" in response.text
    assert hub.get(ai_catalog.PROFILES_PATH + "/did-web-v1").status_code == 404


def test_the_documented_check_runs_against_a_hub(hub, monkeypatch):
    """docs/ai-catalog.md, "Checking a catalogue": the block as it is printed there, with its two
    site-specific lines (how to fetch, which hub) pointed at the test hub."""
    text = (Path(__file__).parent.parent / "docs" / "ai-catalog.md").read_text(encoding="utf-8")
    block = text.split("### Checking a catalogue", 1)[1].split("```python\n", 1)[1].split("```", 1)[0]
    fetch = "def get(url): return urllib.request.urlopen(url).read()\n"
    site = 'hub, did = "https://modelmarket.dev", "did:web:modelmarket.dev"\n'
    assert block.count(fetch) == 1 and block.count(site) == 1
    block = block.replace(fetch, "").replace(site, f'hub, did = "{HUB}", "did:web:hub.example.com"\n')
    try:
        import rfc8785  # noqa: F401 - the block imports it; this only finds out whether it is installed
    except ImportError:
        # Not a dependency of the hub. The hub's own canonicaliser gives the same RFC 8785 bytes.
        import awr

        monkeypatch.setitem(sys.modules, "rfc8785", types.SimpleNamespace(dumps=awr.jcs.canonicalize))
    namespace = {"get": lambda url: hub.get(_path(url)).content}
    exec(compile(block, "docs/ai-catalog.md", "exec"), namespace)  # noqa: S102 - our own documented example
    assert namespace["header"]["alg"] == ("ML-DSA-65" if hub.hybrid else "EdDSA")
    # The block fails when the catalogue it reads was changed after signing.
    tampered = json.loads(hub.get(ai_catalog.WELL_KNOWN_PATH).content)
    tampered["entries"][0]["url"] = HUB + "/.well-known/agent-card.json"
    namespace = {"get": lambda url: (json.dumps(tampered).encode() if url.endswith("ai-catalog.json")
                                     else hub.get(_path(url)).content)}
    with pytest.raises(Exception):  # noqa: B017, PT011 - an AssertionError or the library's InvalidSignature
        exec(compile(block, "docs/ai-catalog.md", "exec"), namespace)  # noqa: S102


@pytest.mark.parametrize("url", ["https://hub.example.com:8443", "https://example.com/hub", "http://localhost:9083"])
def test_a_hub_without_a_root_did_web_publishes_its_catalogues_unsigned(url, monkeypatch, tmp_path):
    config = _config(tmp_path, monkeypatch)
    config.hub_url = url
    db = HubDatabase(config.db_path)
    db.upsert_capability(Capability(capability_id="fx.rate@v1", product_id="fx", name="FX rate",
                                    description="Foreign exchange rate.", price_per_call_usd=0.01, trust_score=0.5,
                                    invoke_url="https://provider.example/fx"))
    try:
        with TestClient(create_app(config=config, db=db, signer=Signer(config.signing_key_path))) as client:
            for document in _documents(client):
                assert ai_catalog.problems(document) == []
                assert "signature" not in document
                assert not any("trustManifests" in e for e in document["entries"])
    finally:
        db.close()


# ── identity without a did:web ────────────────────────────────


@pytest.mark.parametrize(("url", "identifier"), [
    ("https://hub.example.com", "did:web:hub.example.com"),
    ("https://hub.example.com:8443", "did:web:hub.example.com%3A8443"),
    ("https://example.com/hub", "example.com"),       # under a path: no DID document to resolve
    ("http://localhost:9083", "localhost"),
])
def test_host_identifier_is_the_did_when_the_hub_has_one(url, identifier):
    assert ai_catalog.host_info(url, "H") == {"displayName": "H", "identifier": identifier}
    assert ai_catalog.hub_publisher(url, "H")["identityType"] == ("did" if identifier.startswith("did:") else "dns")


# ── the checker ───────────────────────────────────────────────


def test_the_specifications_own_example_passes_the_checker():
    example = json.loads((FIXTURES / "spec-example.ai-catalog.json").read_text())
    assert ai_catalog.problems(example) == []


def _entry(**changes):
    entry = {"identifier": "urn:air:example.com:mcp:x", "type": "application/mcp-server-card+json",
             "url": "https://example.com/mcp/server-card"}
    entry.update(changes)
    return {k: v for k, v in entry.items() if v is not ...}


@pytest.mark.parametrize(("document", "problem"), [
    ([], "not a JSON object"),
    ({"entries": []}, "specVersion"),
    ({"specVersion": "1", "entries": []}, "specVersion"),
    ({"specVersion": "1.0"}, "entries must be an array"),
    ({"specVersion": "1.0", "host": {"identifier": "x"}, "entries": []}, "host needs a displayName"),
    ({"specVersion": "1.0", "entries": [_entry(identifier=...)]}, "needs an identifier"),
    ({"specVersion": "1.0", "entries": [_entry(type=...)]}, "needs a type"),
    ({"specVersion": "1.0", "entries": [_entry(data={})]}, "exactly one of url and data"),
    ({"specVersion": "1.0", "entries": [_entry(url=...)]}, "exactly one of url and data"),
    ({"specVersion": "1.0", "entries": [_entry(url="ftp://example.com/x")]}, "not an http(s) URL"),
    ({"specVersion": "1.0", "entries": [_entry(tags="mcp")]}, "tags must be an array"),
    ({"specVersion": "1.0", "entries": [_entry(digest="md5:" + "0" * 32)]}, "digest"),
    ({"specVersion": "1.0", "entries": [_entry(digest="sha256:" + "A" * 64)]}, "digest"),
    ({"specVersion": "1.0", "entries": [_entry(digest="sha256:" + "0" * 63)]}, "digest"),
    ({"specVersion": "1.0", "entries": [_entry(publisher={"identifier": "x"})]}, "publisher needs"),
    ({"specVersion": "1.0", "entries": [_entry(extensions={"plainkey": 1})]}, "neither reverse-DNS nor a URL"),
    ({"specVersion": "1.0", "entries": [_entry(trustManifests=[{"contributor": "did:web:example.com"}])]},
     "an empty manifest must be omitted"),
    ({"specVersion": "1.0", "entries": [_entry(trustManifests=[{"contributor": "acme", "extensions": {"com.example.x": 1}}])]},
     "contributor must be an absolute URI"),
    ({"specVersion": "1.0", "entries": [_entry(trustManifests=[{
        "contributor": "did:web:example.com",
        "signature": {"signer": "did:web:example.com", "profile": "did-web-v1", "issuedAt": "2026-11-12T00:00:00Z", "jws": "a..b"}}])]},
     "a signed manifest needs a subject"),
    ({"specVersion": "1.0", "entries": [_entry(digest="sha256:" + "0" * 64, trustManifests=[{
        "contributor": "did:web:example.com",
        "subject": {"identifier": "urn:air:example.com:mcp:x", "type": "application/mcp-server-card+json",
                    "digest": "sha256:" + "1" * 64},
        "signature": {"signer": "did:web:example.com", "profile": "did-web-v1", "issuedAt": "2026-11-12T00:00:00Z", "jws": "a..b"}}])]},
     "subject.digest must equal the entry's digest"),
    ({"specVersion": "1.0", "entries": [_entry(trustManifests=[{
        "contributor": "did:web:example.com", "subject": {},
        "signature": {"signer": "did:web:other.example", "profile": "p", "issuedAt": "t", "jws": "a..b"}}])]},
     "signature.signer must equal contributor"),
    ({"specVersion": "1.0", "signature": {"signer": "did:web:example.com"}, "entries": []},
     "signature needs signer, profile, issuedAt and jws"),
    ({"specVersion": "1.0", "entries": [_entry(), _entry()]}, "repeats"),
    ({"specVersion": "1.0", "entries": [_entry(version="1"), _entry(version="1")]}, "repeats"),
    ({"specVersion": "1.0", "entries": [_entry(type="application/ai-catalog+json", url=...,
                                               data={"specVersion": "1.0", "entries": [_entry(type=...)]})]},
     "entries[0].data: entries[0] needs a type"),
])
def test_the_checker_names_what_breaks_the_structure(document, problem):
    assert any(problem in p for p in ai_catalog.problems(document)), ai_catalog.problems(document)


def test_the_checker_accepts_what_the_specification_allows():
    # Two versions of one identifier, an inline artefact, an empty catalogue, unknown members.
    assert ai_catalog.problems({"specVersion": "1.0", "entries": []}) == []
    assert ai_catalog.problems({"specVersion": "1.7", "futureMember": 1, "entries": [
        _entry(version="1.0.0"), _entry(version="2.0.0"),
        _entry(identifier="urn:air:example.com:mcp:y", url=..., data={"name": "x/y"}, unknown=True,
               extensions={"https://ai-catalog.org/extensions/metadata": {}, "com.example.score": 1}),
    ]}) == []
