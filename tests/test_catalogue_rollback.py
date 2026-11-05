"""A crawl refuses a catalogue older than one already verified from the same peer.

Discovery draft -04 §5, first check. Before it, the only freshness rule was the seven-day
max age, so anyone who had kept an older catalogue that a peer signed could serve it again
inside that window and put back an entry (or a price) the peer had already withdrawn.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from aimarket_hub import cli
from aimarket_hub.config import HubConfig
from aimarket_hub.crawler import Crawler
from aimarket_hub.database import HubDatabase
from aimarket_hub.db_backend import create_backend
from aimarket_hub.migrations import Migrations
from aimarket_hub.models import Peer
from aimarket_hub.signing import Signer

PEER = "https://peer.example"
WELL_KNOWN = PEER + "/.well-known/ai-market.json"


def _at(hours_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tool(cid: str) -> dict:
    return {"name": cid.split("@")[0], "capability_id": cid, "product_id": cid.split("@")[0],
            "description": cid, "input_schema": {"type": "object"}, "price_per_call_usd": 0.01}


def _manifest(signer: Signer, generated_at: str, *cids: str) -> dict:
    m = {"protocol_version": "v2", "generated_at": generated_at,
         "capabilities_count": len(cids), "tools": [_tool(c) for c in cids]}
    m["signature"] = signer.sign_manifest(m)
    return m


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    monkeypatch.setattr("aimarket_hub.crawler._url_is_safe", lambda url: url.startswith("https://"))
    config = HubConfig()
    config.hub_url = "https://our-hub.example"
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    peer_signer = Signer(tmp_path / "peer.key")
    db = HubDatabase(config.db_path)
    db.upsert_peer(Peer(url=PEER, name="Peer", public_key=peer_signer.public_key_b64, trusted=True))
    crawler = Crawler(config=config, db=db, signer=Signer(config.signing_key_path))
    served: dict = {}

    def wk():
        return {"name": "Peer", "protocol_versions": ["v1", "v2"],
                "manifest_url": PEER + "/manifest", "capabilities_count": 1,
                "signer_public_key": served.get("key", peer_signer.public_key_b64)}

    async def get(url):
        return httpx.Response(200, json=wk() if ".well-known/" in url else served["manifest"])

    crawler._safe_get = AsyncMock(side_effect=get)
    yield crawler, db, peer_signer, served, tmp_path
    db.close()


def _ids(db: HubDatabase) -> set[str]:
    return {c.capability_id for c in db.list_capabilities(source_hub=PEER, limit=100)}


@pytest.mark.asyncio
async def test_an_older_catalogue_cannot_put_back_a_withdrawn_entry(hub):
    crawler, db, peer, served, _ = hub
    old = _manifest(peer, _at(3), "a@v1", "b@v1")
    new = _manifest(peer, _at(2), "a@v1")

    served["manifest"] = old
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 2
    served["manifest"] = new
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 1
    assert _ids(db) == {"a@v1"}
    assert db.get_catalogue_watermark(PEER)["generated_at"] == new["generated_at"]

    # The old catalogue still verifies and is well inside the seven-day max age.
    assert not crawler._manifest_too_stale(old)
    served["manifest"] = old
    result = await crawler._crawl_one(WELL_KNOWN, 0, "test")
    assert result["capabilities_count"] == 0
    assert _ids(db) == {"a@v1"}, "the withdrawn entry came back"
    assert db.get_catalogue_watermark(PEER)["generated_at"] == new["generated_at"]

    # The same catalogue served again is fine, and a newer one moves the record forward.
    served["manifest"] = new
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 1
    newer = _manifest(peer, _at(1), "a@v1", "c@v1")
    served["manifest"] = newer
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 2
    assert _ids(db) == {"a@v1", "c@v1"}
    assert db.get_catalogue_watermark(PEER)["generated_at"] == newer["generated_at"]


@pytest.mark.asyncio
async def test_a_refused_catalogue_keeps_the_stored_entries(hub):
    """Refusal counts as a failed retrieval: nothing is pruned (federation draft -02)."""
    crawler, db, peer, served, _ = hub
    served["manifest"] = _manifest(peer, _at(1), "a@v1", "b@v1")
    await crawler._crawl_one(WELL_KNOWN, 0, "test")
    served["manifest"] = _manifest(peer, _at(2), "z@v1")
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 0
    assert _ids(db) == {"a@v1", "b@v1"}


@pytest.mark.asyncio
async def test_a_new_pinned_key_starts_a_new_record(hub):
    crawler, db, peer, served, tmp_path = hub
    served["manifest"] = _manifest(peer, _at(1), "a@v1")
    await crawler._crawl_one(WELL_KNOWN, 0, "test")

    # The operator re-pins the peer to a new key; its clock is behind the old record.
    new_key = Signer(tmp_path / "peer-new.key")
    db.upsert_peer(Peer(url=PEER, name="Peer", public_key=new_key.public_key_b64, trusted=True))
    served["key"] = new_key.public_key_b64
    served["manifest"] = _manifest(new_key, _at(2), "b@v1")
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 1
    mark = db.get_catalogue_watermark(PEER)
    assert mark["public_key"] == new_key.public_key_b64
    assert mark["generated_at"] == served["manifest"]["generated_at"]


def test_an_undated_catalogue_after_a_dated_one_is_refused(hub):
    crawler, db, peer, _, _ = hub
    db.record_catalogue_watermark(PEER, public_key=peer.public_key_b64, generated_at=_at(1))
    undated = {"protocol_version": "v2", "tools": []}
    assert crawler._catalogue_rolled_back(PEER, peer.public_key_b64, undated)
    # With no record yet, the check has nothing to compare and stays out of the way.
    db.clear_catalogue_watermark(PEER)
    assert not crawler._catalogue_rolled_back(PEER, peer.public_key_b64, undated)


@pytest.mark.asyncio
async def test_a_far_future_catalogue_is_never_recorded(hub):
    """With the max age switched off, the future check is off too; one catalogue dated years
    ahead must not lock the peer out."""
    crawler, db, peer, served, _ = hub
    crawler.config.manifest_max_age_s = 0
    served["manifest"] = _manifest(peer, "2099-01-01T00:00:00Z", "a@v1")
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 1
    assert db.get_catalogue_watermark(PEER) is None
    served["manifest"] = _manifest(peer, _at(1), "b@v1")
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 1
    assert _ids(db) == {"b@v1"}


@pytest.mark.asyncio
async def test_the_preview_records_and_applies_the_same_floor(hub):
    crawler, db, peer, served, _ = hub
    crawler.config.federation_preview_capabilities = True
    wk = {"manifest_url": PEER + "/manifest"}
    served["manifest"] = _manifest(peer, _at(1), "a@v1")
    assert await crawler._preview_pending_manifest(wk, PEER, peer.public_key_b64) == 1
    assert db.get_catalogue_watermark(PEER)["generated_at"] == served["manifest"]["generated_at"]
    served["manifest"] = _manifest(peer, _at(2), "a@v1", "b@v1")
    assert await crawler._preview_pending_manifest(wk, PEER, peer.public_key_b64) == 0
    assert {r["capability_id"] for r in db.list_preview_capabilities(PEER)} == {"a@v1"}


@pytest.mark.asyncio
async def test_operator_reset_accepts_an_older_catalogue_again(hub, monkeypatch, capsys):
    """For a peer whose clock went backwards (draft -04 §5)."""
    crawler, db, peer, served, _ = hub
    served["manifest"] = _manifest(peer, _at(1), "a@v1")
    await crawler._crawl_one(WELL_KNOWN, 0, "test")
    served["manifest"] = _manifest(peer, _at(2), "b@v1")
    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 0

    monkeypatch.setenv("AIMARKET_DB_PATH", crawler.config.db_path)
    assert cli.main(["peers", "--reset-catalogue-time", PEER + "/"]) == 0
    assert "forgotten" in capsys.readouterr().out
    assert db.get_catalogue_watermark(PEER) is None
    assert cli.main(["peers", "--reset-catalogue-time", PEER]) == 1

    assert (await crawler._crawl_one(WELL_KNOWN, 0, "test"))["capabilities_count"] == 1
    assert _ids(db) == {"b@v1"}


def test_deleting_a_peer_forgets_its_record(hub):
    _, db, peer, _, _ = hub
    db.record_catalogue_watermark(PEER, public_key=peer.public_key_b64, generated_at=_at(1))
    assert db.delete_peer(PEER)
    assert db.get_catalogue_watermark(PEER) is None


def test_migration_adds_the_table_to_an_existing_database(tmp_path):
    path = tmp_path / "old.db"
    backend = create_backend(db_path=path)
    Migrations(backend).apply(target_version=65)
    backend.close()
    db = HubDatabase(path)
    try:
        assert db.get_catalogue_watermark(PEER) is None
        db.record_catalogue_watermark(PEER, public_key="k", generated_at="2026-11-04T12:00:00Z")
        db.record_catalogue_watermark(PEER, public_key="k", generated_at="2026-11-04T13:00:00Z")
        assert db.get_catalogue_watermark(PEER)["generated_at"] == "2026-11-04T13:00:00Z"
    finally:
        db.close()
