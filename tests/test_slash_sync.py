"""Federated slash sync + portable proof-of-misbehavior (F2/F6)."""

from pathlib import Path

import pytest
from aimarket_hub.signing import Signer
from aimarket_hub.slash_sync import ProofOfMisbehavior, SlashRegistry, verify_envelope


@pytest.fixture
def hubs(tmp_path: Path):
    hub_a = Signer(key_path=tmp_path / "hub_a_key")
    hub_b = Signer(key_path=tmp_path / "hub_b_key")
    consumer = Signer(key_path=tmp_path / "consumer_key")
    return hub_a, hub_b, consumer


def _pom(consumer: Signer, dispute_canonical: str = "dispute|agent-evil|did not deliver") -> ProofOfMisbehavior:
    return ProofOfMisbehavior(
        dispute_canonical=dispute_canonical,
        complainant_pubkey_b64=consumer.public_key_b64,
        signature_b64=consumer.sign_canonical(dispute_canonical),
    )


def test_slash_propagates_across_hubs(hubs, tmp_path):
    hub_a, hub_b, consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    env = reg_a.record_local_slash(
        provider_hub="agent-evil", slashed_usd=500.0, dispute_id="d1",
        reason="undelivered", signer=hub_a, pom=_pom(consumer),
    )
    # Hub B pulls hub A's signed log and ingests it.
    reg_b = SlashRegistry("https://hub-b.example")
    added = reg_b.ingest_remote([env], verifier=hub_b, require_pom=True)
    assert added == 1
    sig = reg_b.slash_signal("agent-evil")
    assert sig["slash_count"] == 1 and sig["distinct_issuers"] == 1
    assert reg_b.federated_penalty("agent-evil") == 0.5


def test_forged_issuer_signature_rejected(hubs):
    hub_a, hub_b, consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    env = reg_a.record_local_slash(
        provider_hub="agent-evil", slashed_usd=500.0, dispute_id="d1",
        reason="x", signer=hub_a, pom=_pom(consumer),
    )
    env["slashed_usd"] = 999999.0  # tamper after signing
    assert not verify_envelope(env, hub_b, require_pom=True)
    reg_b = SlashRegistry("https://hub-b.example")
    assert reg_b.ingest_remote([env], verifier=hub_b) == 0


def test_cross_hub_slash_without_pom_rejected(hubs):
    """A malicious hub cannot poison a competitor's agent without consumer-signed proof."""
    hub_a, hub_b, _consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    env = reg_a.record_local_slash(
        provider_hub="competitor-agent", slashed_usd=1000.0, dispute_id="fake",
        reason="smear", signer=hub_a, pom=None,
    )
    reg_b = SlashRegistry("https://hub-b.example")
    assert reg_b.ingest_remote([env], verifier=hub_b, require_pom=True) == 0
    assert reg_b.slash_signal("competitor-agent")["slash_count"] == 0


def test_invalid_pom_signature_rejected(hubs):
    hub_a, hub_b, consumer = hubs
    bad_pom = ProofOfMisbehavior(
        dispute_canonical="dispute|x",
        complainant_pubkey_b64=consumer.public_key_b64,
        signature_b64=consumer.sign_canonical("DIFFERENT MESSAGE"),  # signature/message mismatch
    )
    reg_a = SlashRegistry("https://hub-a.example")
    env = reg_a.record_local_slash(
        provider_hub="agent-evil", slashed_usd=10.0, dispute_id="d2",
        reason="x", signer=hub_a, pom=bad_pom,
    )
    reg_b = SlashRegistry("https://hub-b.example")
    assert reg_b.ingest_remote([env], verifier=hub_b, require_pom=True) == 0


def test_federated_penalty_saturates_with_distinct_issuers(hubs, tmp_path):
    _hub_a, hub_b, consumer = hubs
    reg = SlashRegistry("https://hub-local.example")
    # Three different hubs slash the same agent — cross-hub consensus.
    for i in range(3):
        issuer = Signer(key_path=tmp_path / f"issuer_{i}_key")
        src = SlashRegistry(f"https://hub-{i}.example")
        env = src.record_local_slash(
            provider_hub="agent-evil", slashed_usd=100.0, dispute_id=f"d{i}",
            reason="x", signer=issuer, pom=_pom(consumer),
        )
        reg.ingest_remote([env], verifier=hub_b, require_pom=True)
    assert reg.slash_signal("agent-evil")["distinct_issuers"] == 3
    assert reg.federated_penalty("agent-evil") == 0.875  # 1 - 0.5**3


def test_ingest_is_idempotent(hubs):
    hub_a, hub_b, consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    env = reg_a.record_local_slash(
        provider_hub="agent-evil", slashed_usd=5.0, dispute_id="d1",
        reason="x", signer=hub_a, pom=_pom(consumer),
    )
    reg_b = SlashRegistry("https://hub-b.example")
    assert reg_b.ingest_remote([env], verifier=hub_b) == 1
    assert reg_b.ingest_remote([env], verifier=hub_b) == 0  # re-pull is a no-op
    assert reg_b.slash_signal("agent-evil")["slash_count"] == 1


def test_federated_penalty_lowers_reputation_score(tmp_path):
    """End-to-end: a cross-hub slash signal actually drags the reputation score down."""
    from aimarket_hub.reputation_oracle import ReputationOracle

    oracle = ReputationOracle(signer=Signer(key_path=tmp_path / "oracle_key"))
    base = oracle.compute_reputation_score("agent-evil")
    penalized = oracle.compute_reputation_score("agent-evil", federated_penalty=1.0)
    assert penalized["federated_slash_penalty"] == 0.3
    assert penalized["score"] < base["score"]


def test_resolve_dispute_emits_federated_slash_with_consumer_pom(tmp_path):
    """Full vertical: consumer-signed dispute → resolve emits a signed attestation whose PoM
    is signed by the consumer (not the hub), so a peer accepts it and the agent's score drops
    at the peer too."""
    from aimarket_hub.reputation_oracle import ReputationOracle
    from aimarket_hub.slash_sync import SlashRegistry

    consumer = Signer(key_path=tmp_path / "consumer_key")
    hub_a_signer = Signer(key_path=tmp_path / "hub_a_key")
    hub_b_signer = Signer(key_path=tmp_path / "hub_b_key")

    reg_a = SlashRegistry("https://hub-a.example")
    oracle_a = ReputationOracle(signer=hub_a_signer, hub_url="https://hub-a.example", slash_registry=reg_a)
    oracle_a.stake_bond(provider_hub="agent-evil", amount_usd=10_000.0)

    # Consumer authors + signs the full dispute client-side (portable proof-of-misbehavior).
    from aimarket_hub.reputation_oracle import Dispute

    dispute = Dispute(
        dispute_id="cmplt-1", invocation_id="inv-1", provider_hub="agent-evil",
        consumer_hub="https://consumer.example", reason="undelivered", requested_slash_pct=0.5,
    )
    dispute.signature = consumer.sign_canonical(dispute.canonical())
    dispute.consumer_pubkey = consumer.public_key_b64
    oracle_a.submit_signed_dispute(dispute)

    res = oracle_a.resolve_dispute(dispute.dispute_id, slash_pct=0.5, ruling_note="confirmed")
    assert res["federated"] is True

    # Peer hub B pulls hub A's signed slash log and ingests it (require_pom on).
    reg_b = SlashRegistry("https://hub-b.example")
    assert reg_b.ingest_remote(reg_a.export(), verifier=hub_b_signer, require_pom=True) == 1

    # The cross-hub penalty drags down the score computed at hub B.
    oracle_b = ReputationOracle(signer=hub_b_signer, hub_url="https://hub-b.example")
    base = oracle_b.compute_reputation_score("agent-evil")["score"]
    fed = reg_b.federated_penalty("agent-evil")
    penalized = oracle_b.compute_reputation_score("agent-evil", federated_penalty=fed)["score"]
    assert fed > 0 and penalized < base


def test_issuer_binding_rejects_spoofed_pubkey(hubs, tmp_path):
    """A peer cannot serve an attestation whose issuer pubkey isn't the peer's known key."""
    hub_a, hub_b, consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    env = reg_a.record_local_slash(
        provider_hub="agent-evil", slashed_usd=5.0, dispute_id="d1",
        reason="x", signer=hub_a, pom=_pom(consumer),
    )
    reg_b = SlashRegistry("https://hub-b.example")
    # Bound to a DIFFERENT key than the envelope's issuer → rejected.
    wrong_key = Signer(key_path=tmp_path / "wrong_key").public_key_b64
    assert reg_b.ingest_remote([env], verifier=hub_b, expected_issuer_pubkey=wrong_key) == 0
    # Bound to the correct issuer key → accepted.
    assert reg_b.ingest_remote([env], verifier=hub_b, expected_issuer_pubkey=hub_a.public_key_b64) == 1


def test_peer_cannot_overwrite_local_authored_log(hubs):
    hub_a, hub_b, consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    reg_a.record_local_slash(
        provider_hub="agent-x", slashed_usd=5.0, dispute_id="d1",
        reason="x", signer=hub_a, pom=_pom(consumer),
    )
    # A peer envelope claiming to be from hub-a is ignored by hub-a's own registry.
    forged = {"issuer_hub": "https://hub-a.example", "seq": 1, "provider_hub": "agent-y"}
    assert reg_a.ingest_remote([forged], verifier=hub_b) == 0


# ═══════════════════════════════════════════════════════════════════════
# Two-tier federation: weak (no-PoM) attestations need cross-hub consensus
# ═══════════════════════════════════════════════════════════════════════


def _weak_env(tmp_path, i: int, provider="agent-gray"):
    issuer = Signer(key_path=tmp_path / f"weak_issuer_{i}_key")
    src = SlashRegistry(f"https://weak-hub-{i}.example")
    return src.record_local_slash(
        provider_hub=provider, slashed_usd=5.0, dispute_id=f"supply_{i}",
        reason="invoke_failure", signer=issuer, pom=None,
    )


def test_weak_attestation_stored_only_with_accept_weak(hubs, tmp_path):
    _hub_a, hub_b, _consumer = hubs
    env = _weak_env(tmp_path, 0)
    reg = SlashRegistry("https://hub-local.example")
    assert reg.ingest_remote([env], verifier=hub_b, require_pom=True) == 0  # legacy default
    assert reg.ingest_remote([env], verifier=hub_b, require_pom=True, accept_weak=True) == 1
    sig = reg.slash_signal("agent-gray")
    assert sig["weak_issuers"] == ["https://weak-hub-0.example"]
    assert sig["strong_issuers"] == []


def test_single_weak_issuer_moves_nothing(hubs, tmp_path):
    """One hub's mood is not evidence: a lone no-PoM attestation has zero penalty weight."""
    _hub_a, hub_b, _consumer = hubs
    reg = SlashRegistry("https://hub-local.example")
    reg.ingest_remote([_weak_env(tmp_path, 0)], verifier=hub_b, accept_weak=True)
    assert reg.federated_penalty("agent-gray") == 0.0


def test_weak_consensus_moves_penalty_at_half_weight(hubs, tmp_path):
    _hub_a, hub_b, _consumer = hubs
    reg = SlashRegistry("https://hub-local.example")
    for i in range(2):
        reg.ingest_remote([_weak_env(tmp_path, i)], verifier=hub_b, accept_weak=True)
    # 2 weak issuers = 1 strong-equivalent → 1 - 0.5**1 = 0.5
    assert reg.federated_penalty("agent-gray") == 0.5


def test_same_issuer_weak_then_strong_counts_once_as_strong(hubs, tmp_path):
    """An issuer that sends a weak (no-PoM) attestation and LATER a PoM'd one for the same
    provider must count exactly once — as strong. weak_issuers = all − strong (slash_sync
    set subtraction); a refactor to per-entry weak sets would double-count it. Pins that."""
    hub_a, hub_b, consumer = hubs
    reg = SlashRegistry("https://hub-local.example")
    # ONE issuer: first a weak envelope, then a strong one, same provider.
    mixed = SlashRegistry("https://mixed-hub.example")
    weak_env = mixed.record_local_slash(
        provider_hub="agent-gray", slashed_usd=5.0, dispute_id="supply_1",
        reason="verified_failure", signer=hub_a, pom=None,
    )
    strong_env = mixed.record_local_slash(
        provider_hub="agent-gray", slashed_usd=5.0, dispute_id="d2",
        reason="undelivered", signer=hub_a, pom=_pom(consumer),
    )
    reg.ingest_remote([weak_env, strong_env], verifier=hub_b, accept_weak=True)
    # Plus one INDEPENDENT lone weak issuer.
    reg.ingest_remote([_weak_env(tmp_path, 5)], verifier=hub_b, accept_weak=True)
    sig = reg.slash_signal("agent-gray")
    assert sig["strong_issuers"] == ["https://mixed-hub.example"]
    assert "https://mixed-hub.example" not in sig["weak_issuers"]
    assert sig["weak_issuers"] == ["https://weak-hub-5.example"]
    # 1 strong + 1 LONE weak (contributes 0) → 1 - 0.5**1 = 0.5, not 0.75.
    assert reg.federated_penalty("agent-gray") == 0.5


def test_strong_and_weak_tiers_compose(hubs, tmp_path):
    hub_a, hub_b, consumer = hubs
    reg = SlashRegistry("https://hub-local.example")
    strong_src = SlashRegistry("https://strong-hub.example")
    strong_env = strong_src.record_local_slash(
        provider_hub="agent-gray", slashed_usd=50.0, dispute_id="d1",
        reason="undelivered", signer=hub_a, pom=_pom(consumer),
    )
    reg.ingest_remote([strong_env], verifier=hub_b)
    for i in range(2):
        reg.ingest_remote([_weak_env(tmp_path, i)], verifier=hub_b, accept_weak=True)
    # 1 strong + 2 weak (=1 strong-equivalent) → 1 - 0.5**2 = 0.75
    assert reg.federated_penalty("agent-gray") == 0.75


def test_weak_attestation_carries_self_verifying_evidence(hubs, tmp_path):
    hub_a, hub_b, _consumer = hubs
    reg_a = SlashRegistry("https://hub-a.example")
    rejection = {"type": "verification_rejection", "reason": "verify_failed",
                 "verify_score": 0.1, "signature": "receipt-sig"}
    env = reg_a.record_local_slash(
        provider_hub="agent-gray", slashed_usd=5.0, dispute_id="supply_1",
        reason="verified_failure", signer=hub_a, pom=None,
        evidence=rejection, evidence_kind="verification_rejection",
    )
    reg_b = SlashRegistry("https://hub-b.example")
    assert reg_b.ingest_remote([env], verifier=hub_b, accept_weak=True) == 1
    stored = next(iter(e for e in reg_b._entries.values()))
    assert stored["evidence"]["reason"] == "verify_failed"
    assert stored["evidence_kind"] == "verification_rejection"
    # Tampering with the signed core still invalidates the envelope.
    env2 = dict(env)
    env2["slashed_usd"] = 999.0
    assert not verify_envelope(env2, hub_b, require_pom=False)


# ═══════════════════════════════════════════════════════════════════════
# Registry persistence: the append-only log survives a restart
# ═══════════════════════════════════════════════════════════════════════


def test_registry_persists_across_restart(hubs, tmp_path):
    from aimarket_hub.database import HubDatabase

    hub_a, hub_b, consumer = hubs
    db = HubDatabase(db_path=str(tmp_path / "hub.db"))

    reg1 = SlashRegistry("https://hub-a.example", db=db)
    reg1.record_local_slash(
        provider_hub="agent-evil", slashed_usd=100.0, dispute_id="d1",
        reason="undelivered", signer=hub_a, pom=_pom(consumer),
    )
    peer_env = _weak_env(tmp_path, 7)
    reg1.ingest_remote([peer_env], verifier=hub_b, accept_weak=True)

    # "Restart": a fresh registry over the same DB restores entries, tiers AND seq.
    reg2 = SlashRegistry("https://hub-a.example", db=db)
    assert len(reg2.export()) == 1
    assert reg2.slash_signal("agent-evil")["slash_count"] == 1
    assert reg2.slash_signal("agent-gray")["weak_issuers"] == ["https://weak-hub-7.example"]
    env2 = reg2.record_local_slash(
        provider_hub="agent-evil", slashed_usd=1.0, dispute_id="d2",
        reason="again", signer=hub_a, pom=_pom(consumer),
    )
    assert env2["seq"] == 2  # monotonic seq restored, not reset to 1


def test_reload_picks_up_peer_persisted_via_another_connection(hubs, tmp_path):
    """A background crawl persists ingested peer attestations through its OWN db
    connection; the app registry (bound to a different connection over the same file)
    must surface them after reload() — no cross-thread write to the app connection."""
    from aimarket_hub.database import HubDatabase

    hub_a, hub_b, consumer = hubs
    app_db = HubDatabase(db_path=str(tmp_path / "hub.db"))
    app_reg = SlashRegistry("https://hub-a.example", db=app_db)
    assert app_reg.slash_signal("agent-evil")["slash_count"] == 0

    # Simulate the crawl worker: a separate registry over a separate connection to the
    # SAME file ingests + persists a strong peer attestation.
    crawl_db = HubDatabase(db_path=str(tmp_path / "hub.db"))
    crawl_reg = SlashRegistry("https://hub-a.example", db=crawl_db)
    src = SlashRegistry("https://peer.example")
    env = src.record_local_slash(
        provider_hub="agent-evil", slashed_usd=5.0, dispute_id="d1",
        reason="x", signer=hub_b, pom=_pom(consumer),
    )
    assert crawl_reg.ingest_remote([env], verifier=hub_a, expected_issuer_pubkey=hub_b.public_key_b64) == 1

    # Before reload the app registry hasn't seen it; after reload it has.
    assert app_reg.slash_signal("agent-evil")["slash_count"] == 0
    app_reg.reload()
    assert app_reg.slash_signal("agent-evil")["slash_count"] == 1
    assert app_reg.federated_penalty("agent-evil") == 0.5


class _RaisingDB:
    """DB whose attestation writes always fail — models a locked/erroring connection."""

    def slash_attestation_load_all(self):
        return []

    def slash_attestation_append(self, *a, **k):
        raise RuntimeError("database is locked")

    def slash_attestation_save(self, *a, **k):
        raise RuntimeError("database is locked")


def test_authored_slash_persist_failure_is_fail_loud_and_does_not_advance_seq(tmp_path):
    """A locally-authored slash whose durable write fails must NOT be served: peers mirror
    our seq, so a served-but-unpersisted seq would regress on restart and collide. The
    seq must not advance and the caller must see the failure."""
    signer = Signer(tmp_path / "hub_key")
    reg = SlashRegistry("https://hub-a.example", db=_RaisingDB())
    with pytest.raises(RuntimeError):
        reg.record_local_slash(
            provider_hub="agent-evil", slashed_usd=5.0, dispute_id="d1",
            reason="x", signer=signer, pom=None,
        )
    # Nothing was served and the seq was not consumed — a retry reuses seq 1, not 2.
    assert reg.export() == []
    assert reg._seq == 0


def test_supply_slash_degrades_when_federation_persist_fails(tmp_path):
    """supply_security.slash wraps record_local_slash; a persist failure there must not
    crash the slash — the stake is still slashed locally, federated=False."""
    from unittest.mock import MagicMock

    from aimarket_hub.config import HubConfig
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.supply_security import SupplySecurity

    db = HubDatabase(db_path=str(tmp_path / "hub.db"))
    db.supply_stake_add("pub-a", 100.0)
    reg = SlashRegistry("https://hub-a.example", db=_RaisingDB())
    sec = SupplySecurity(db, HubConfig(), signer=Signer(tmp_path / "k"), slash_registry=reg)
    sec.policy.relaxed = False
    sec.lumen = MagicMock()
    sec.lumen.score_entity.return_value = {"score": 0.5}
    out = sec.slash("pub-a", 5.0, "manual")
    assert out["slashed_usd"] == 5.0        # stake slashed locally regardless
    assert out["federated"] is False        # federation degraded gracefully
    assert db.supply_stake_get("pub-a") == 95.0


def test_authored_seq_is_db_atomic_across_connections(hubs, tmp_path):
    """Two registries over SEPARATE connections to the same DB (models 2 uvicorn workers)
    author slashes; the DB-atomic seq allocator must hand out DISTINCT seqs — no overwrite
    of the append-only log, no equivocation on a reused seq."""
    from aimarket_hub.database import HubDatabase

    hub_a, _hub_b, consumer = hubs
    path = str(tmp_path / "hub.db")
    reg1 = SlashRegistry("https://hub-a.example", db=HubDatabase(db_path=path))
    reg2 = SlashRegistry("https://hub-a.example", db=HubDatabase(db_path=path))

    seqs = []
    for i in range(6):
        reg = reg1 if i % 2 == 0 else reg2   # interleave the two "workers"
        env = reg.record_local_slash(
            provider_hub=f"agent-{i}", slashed_usd=1.0, dispute_id=f"d{i}",
            reason="x", signer=hub_a, pom=_pom(consumer),
        )
        seqs.append(env["seq"])

    assert sorted(seqs) == [1, 2, 3, 4, 5, 6]  # unique + monotonic, no collision
    # The durable log holds all six distinct authored attestations.
    fresh = SlashRegistry("https://hub-a.example", db=HubDatabase(db_path=path))
    assert len(fresh.export()) == 6
    assert {e["seq"] for e in fresh.export()} == {1, 2, 3, 4, 5, 6}
