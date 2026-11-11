"""The honest map and the onboarding funnel (owner, 2026-11-08).

- A peer no crawl has reached for more than AIMARKET_PEER_SILENT_DAYS (default 7) is labelled
  ``silent`` on /hubs and in /federation/peers. It is never removed or hidden, and it is ``live``
  again the moment a crawl reaches it.
- /hubs and /federation/funnel show where each hub stands: knock → assay → admitted → first routed
  call → first paid call, from what this hub recorded. Totals count hubs the operator did not pin.
- A knock answers with the last assay verdict and what blocks it, not only a link.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from aimarket_hub import federation_funnel, peer_liveness
from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import InvocationStat, Peer
from aimarket_hub.signing import Signer

ADMIN_TOKEN = "funnel-admin"
LIVE = "https://live-peer.example"
SILENT = "https://silent-peer.example"
KNOCKER = "https://knocker.example"


def _iso(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


@contextmanager
def _hub(monkeypatch, tmp_path, **env):
    env.setdefault("AIMARKET_ADMIN_TOKEN", ADMIN_TOKEN)
    env.setdefault("AIMARKET_FEDERATION_ASSAY", "0")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    with TestClient(app) as client:
        client.hub_db = db  # type: ignore[attr-defined]
        yield client


def _seed(db):
    db.upsert_peer(Peer(url=LIVE, name="Live", last_crawl=_iso(3600), trusted=True,
                        first_seen=_iso(20 * 86400)))
    db.upsert_peer(Peer(url=SILENT, name="Silent", last_crawl=_iso(8 * 86400 + 60), trusted=True))
    db.upsert_peer(Peer(url=KNOCKER, name="Knocker", trusted=False), status="pending")
    db.save_peer_assay({"url": KNOCKER, "verdict": "fail", "ran_at": _iso(600), "checks": [
        {"id": "well_known_schema", "ok": False, "detail": "well-known did not answer"}]})
    for price in (0.0, 0.004):
        db.record_invocation(InvocationStat(capability_id="c", product_id="p", source_hub=LIVE, price_usd=price,
                                            latency_ms=10, success=True, timestamp=_iso(100)))


def test_liveness_labels():
    now = time.time()
    assert peer_liveness.liveness(_iso(3600), now=now)["liveness"] == "live"
    silent = peer_liveness.liveness(_iso(8 * 86400 + 60), now=now)
    assert silent == {"liveness": "silent", "silent_days": 8}
    assert peer_liveness.liveness("", now=now)["liveness"] == "never_crawled"


def test_the_threshold_is_a_setting(monkeypatch):
    monkeypatch.setenv("AIMARKET_PEER_SILENT_DAYS", "10")
    assert peer_liveness.liveness(_iso(8 * 86400))["liveness"] == "live"
    monkeypatch.setenv("AIMARKET_PEER_SILENT_DAYS", "nonsense")
    assert peer_liveness.silent_days() == 7


def test_a_silent_peer_is_labelled_not_removed(monkeypatch, tmp_path):
    with _hub(monkeypatch, tmp_path) as client:
        _seed(client.hub_db)
        peers = client.get("/ai-market/v2/federation/peers").json()
        by_url = {p["url"]: p for p in peers["peers"]}
        assert by_url[SILENT]["liveness"] == "silent" and by_url[SILENT]["silent_days"] == 8
        assert by_url[LIVE]["liveness"] == "live"
        assert peers["silent_after_days"] == 7 and peers["count"] == 2
        page = client.get("/hubs").text
        assert "silent 8 days" in page and "silent-peer.example" in page


def test_a_silent_peer_is_live_again_after_a_crawl(monkeypatch, tmp_path):
    with _hub(monkeypatch, tmp_path) as client:
        db = client.hub_db
        _seed(db)
        db.upsert_peer(Peer(url=SILENT, name="Silent", last_crawl=_iso(10), trusted=True))
        by_url = {p["url"]: p for p in client.get("/ai-market/v2/federation/peers").json()["peers"]}
        assert by_url[SILENT]["liveness"] == "live"


def test_the_funnel_shows_each_step(monkeypatch, tmp_path):
    with _hub(monkeypatch, tmp_path) as client:
        _seed(client.hub_db)
        funnel = client.get("/ai-market/v2/federation/funnel").json()
        by_url = {h["url"]: h for h in funnel["hubs"]}
        live = by_url[LIVE]
        assert live["stage"] == "paid" and live["routed_calls"] == 2 and live["paid_calls"] == 1
        assert live["next_step"] == "selling"
        assert by_url[SILENT]["stage"] == "admitted"
        assert by_url[SILENT]["next_step"] == "not answering crawls for 8 days"
        knocker = by_url[KNOCKER]
        assert knocker["admitted"] is False and knocker["assay_verdict"] == "fail"
        assert "did not pass" in knocker["next_step"]
        assert funnel["totals"] == {"knocked": 3, "assay_passed": 2, "admitted": 2, "routed": 1, "paid": 1,
                                    "silent": 1}
        page = client.get("/hubs").text
        assert 'id="funnel"' in page and "got a paid call" in page


def test_pinned_hubs_are_listed_but_not_counted():
    peers = [Peer(url=LIVE, name="ours", last_crawl=_iso(10)), Peer(url=SILENT, name="theirs", last_crawl=_iso(10))]
    out = federation_funnel.build(peers=peers, pending=[], assay=lambda u: None, stats={},
                                  pinned=lambda p: p.url == LIVE)
    assert out["totals"]["admitted"] == 1
    assert [h["pinned_by_operator"] for h in out["hubs"]] == [False, True]


def test_a_peer_name_in_the_funnel_is_escaped(monkeypatch, tmp_path):
    with _hub(monkeypatch, tmp_path) as client:
        client.hub_db.upsert_peer(Peer(url="javascript:alert(1)", name="<script>x</script>",
                                       last_crawl=_iso(10), trusted=True))
        page = client.get("/hubs").text
        assert "<script>x</script>" not in page
        assert 'href="javascript:' not in page


@pytest.fixture
def resolvable(monkeypatch):
    import aimarket_hub.crawler as crawler

    monkeypatch.setattr(crawler, "_url_is_safe", lambda url: url.startswith(("http://", "https://")))


def test_a_knock_answers_with_the_last_verdict_and_its_blockers(monkeypatch, tmp_path, resolvable):
    with _hub(monkeypatch, tmp_path, AIMARKET_FEDERATION_OPEN="1") as client:
        _seed(client.hub_db)
        body = client.post("/ai-market/v2/federation/announce",
                           json={"hub_url": KNOCKER, "hub_name": "Knocker"}).json()
        assert body["status"] == "known"
        assert body["assay"]["verdict"] == "fail"
        assert body["assay"]["blockers"][0]["id"] == "well_known_schema"
        assert body["assay_url"].endswith("url=https%3A%2F%2Fknocker.example")
        assert body["funnel_url"].endswith("/hubs#funnel")


def test_a_first_knock_has_no_verdict_yet(monkeypatch, tmp_path, resolvable):
    with _hub(monkeypatch, tmp_path, AIMARKET_FEDERATION_OPEN="1") as client:
        body = client.post("/ai-market/v2/federation/announce",
                           json={"hub_url": "https://new-hub.example", "hub_name": "N"}).json()
        assert body["status"] == "pending"
        assert body["assay"] == {"verdict": None, "ran_at": "", "blockers": []}


def test_hubs_on_this_hubs_own_site_are_the_operators():
    from types import SimpleNamespace

    config = SimpleNamespace(hub_url="https://modelmarket.dev", seed_node_ids={}, seed_pubkeys={})
    own = federation_funnel.operators_own(config)
    assert own(Peer(url="https://hestia.modelmarket.dev", name="h")) is True
    assert own(Peer(url="https://modelmarket.dev/providers/x", name="p")) is True
    assert own(Peer(url="https://agent-exec.448c.org", name="a")) is False
