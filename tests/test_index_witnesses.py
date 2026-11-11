"""Admission of an index-only hub by witnesses (owner, 2026-11-08).

A hub that sells and relays nothing (a registry, a directory) gives the sandbox nothing to run, so
its assay was `review` forever. Its evidence is the federation's instead: hubs this one already
admitted, on at least AIMARKET_FEDERATION_INDEX_WITNESSES (default 2) different sites, list it as
admitted under exactly the key it advertises. One site counts once; the candidate's own site never
counts; a hub whose own sandbox probe failed cannot be vouched past it.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.federation_assay import (
    assay_blockers,
    gather_index_witnesses,
    run_assay,
    score_verdict,
    site_of,
)
from aimarket_hub.models import Peer
from aimarket_hub.signing import Signer

INDEX = "https://index.example"
HARD = [{"id": h, "ok": True} for h in (
    "url_public", "well_known_schema", "advertised_key", "manifest_schema", "manifest_signed",
    "manifest_fresh", "invoke_same_origin")]
NOTHING = [{"id": "sandbox_probe", "ok": None, "detail": "no publicly offered capability to probe"}]


@pytest.fixture
def resolvable(monkeypatch):
    import aimarket_hub.crawler as crawler

    monkeypatch.setattr(crawler, "_url_is_safe", lambda url: url.startswith(("http://", "https://")))


def test_site_of():
    assert site_of("https://hub.modelmarket.dev/x") == "modelmarket.dev"
    assert site_of("https://a.b.example.co.uk") == "example.co.uk"
    assert site_of("https://x402-gateway-1f21.up.railway.app") == "x402-gateway-1f21.up.railway.app"
    # RFC 5737 TEST-NET-3 — IP-literal host, not a fleet address.
    assert site_of("http://203.0.113.50:8080") == "203.0.113.50"


def test_witnesses_turn_review_into_pass_only_when_nothing_could_be_probed():
    witnessed = {"id": "index_witnesses", "ok": True, "detail": "2 sites"}
    assert score_verdict(HARD + NOTHING) == "review"
    assert score_verdict(HARD + NOTHING + [witnessed]) == "pass"
    failed_probe = [{"id": "sandbox_probe", "ok": False, "detail": "HTTP 500"}]
    assert score_verdict(HARD + failed_probe + [witnessed]) == "review"
    broken = [dict(c, ok=False) if c["id"] == "manifest_signed" else c for c in HARD]
    assert score_verdict(broken + NOTHING + [witnessed]) == "fail"


def test_a_missing_witness_is_the_named_blocker():
    dossier = {"verdict": "review",
               "checks": HARD + NOTHING + [{"id": "index_witnesses", "ok": False, "detail": "found 1"}]}
    assert [b["id"] for b in assay_blockers(dossier)] == ["index_witnesses"]


def _db_with_peers(tmp_path, urls):
    db = HubDatabase(tmp_path / "hub.db")
    for url in urls:
        db.upsert_peer(Peer(url=url, name=url, public_key="PK-" + url, trusted=True))
    return db


def _peers_doc(*, listed_key="KEY", status="active", url=INDEX):
    return {"peers": [{"url": url, "status": status, "public_key": listed_key}]}


def test_two_sites_vouch(tmp_path):
    db = _db_with_peers(tmp_path, ["https://a.one.example", "https://b.one.example", "https://two.example"])
    asked = []

    async def get(url):
        asked.append(url)
        return _peers_doc()

    ok, detail, witnesses = asyncio.run(gather_index_witnesses(INDEX, "KEY", db=db, get=get))
    assert ok is True and [w["site"] for w in witnesses] == ["one.example", "two.example"]
    # one.example is asked once: a second hub on the same site adds nothing.
    assert len(asked) == 2


def test_one_site_with_many_hubs_is_not_enough(tmp_path):
    db = _db_with_peers(tmp_path, ["https://a.one.example", "https://b.one.example", "https://c.one.example"])

    async def get(url):
        return _peers_doc()

    ok, detail, witnesses = asyncio.run(gather_index_witnesses(INDEX, "KEY", db=db, get=get))
    assert ok is False and len(witnesses) == 1 and "needs 2" in detail


def test_the_candidates_own_site_never_vouches(tmp_path):
    db = _db_with_peers(tmp_path, ["https://mirror.index.example", "https://two.example"])

    async def get(url):
        return _peers_doc()

    ok, _, witnesses = asyncio.run(gather_index_witnesses(INDEX, "KEY", db=db, get=get))
    assert ok is False and [w["site"] for w in witnesses] == ["two.example"]


@pytest.mark.parametrize("doc", [
    _peers_doc(listed_key="OTHER-KEY"),          # admitted someone else under that URL
    _peers_doc(status="pending"),                # merely knocked there too
    _peers_doc(url="https://index.example.evil"),
    {"peers": "not a list"},
])
def test_a_listing_that_does_not_match_is_not_a_witness(tmp_path, doc):
    db = _db_with_peers(tmp_path, ["https://one.example", "https://two.example"])

    async def get(url):
        return doc

    ok, _, witnesses = asyncio.run(gather_index_witnesses(INDEX, "KEY", db=db, get=get))
    assert ok is False and witnesses == []


def test_an_index_hub_is_admitted_end_to_end(tmp_path, resolvable, monkeypatch):
    peer = Signer(tmp_path / "index-key")
    signer = Signer(tmp_path / "key")
    db = _db_with_peers(tmp_path, ["https://one.example", "https://two.example"])
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest = {"protocol_version": "v2", "generated_at": generated, "capabilities_count": 0, "tools": []}
    manifest["signature"] = peer.sign_manifest(manifest)
    wk = {"name": "Index", "protocol_versions": ["v2"], "manifest_url": f"{INDEX}/ai-market/manifest",
          "mcp_endpoint": f"{INDEX}/ai-market/v2/invoke", "signer_public_key": peer.public_key_b64,
          "capabilities_count": 0}
    wk["signature"] = peer.sign_object(wk)

    async def get_json(url):
        if url.endswith("ai-market.json"):
            return wk
        if url.endswith("/federation/peers"):
            return _peers_doc(listed_key=peer.public_key_b64)
        return manifest

    async def judge_json(_body):
        return {"decision": "ok", "rationale": "n/a"}

    async def post_json(*_a, **_k):
        raise AssertionError("an index-only hub must not be invoked")

    db.upsert_peer(Peer(url=INDEX, name="Index", trusted=False), status="pending")
    monkeypatch.setenv("AIMARKET_FEDERATION_JUDGE_KEY", "test-judge")
    dossier = asyncio.run(run_assay(INDEX, config=HubConfig(), db=db, signer=signer, get_json=get_json,
                                    post_json=post_json, judge_json=judge_json, crawl_on_admit=False))
    by = {c["id"]: c for c in dossier["checks"]}
    assert by["index_witnesses"]["ok"] is True, by
    assert dossier["verdict"] == "pass"
    assert {w["site"] for w in dossier["sandbox"]["index_witnesses"]} == {"one.example", "two.example"}


def test_the_rule_can_be_switched_off(tmp_path, resolvable, monkeypatch):
    monkeypatch.setenv("AIMARKET_FEDERATION_INDEX_WITNESSES", "0")
    assert score_verdict(HARD + NOTHING) == "review"
    from aimarket_hub.federation_assay import index_witnesses_required

    assert index_witnesses_required() == 0
