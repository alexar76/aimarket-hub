"""Versioned database migrations for AIMarket Hub.

Manages schema creation and upgrades across SQLite and PostgreSQL.
Each migration has up/down SQL written for SQLite. The PostgresBackend
automatically translates SQL via sqlite_to_pg().

A `_migrations` table tracks applied versions.

CLI usage:
    python -m aimarket_hub.migrations up       # Apply pending migrations
    python -m aimarket_hub.migrations down     # Roll back last migration
    python -m aimarket_hub.migrations status   # Show migration status
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

from aimarket_hub.db_backend import DBBackend, create_backend

logger = logging.getLogger(__name__)

# ── Migration Registry ──────────────────────────────────────────────

MIGRATIONS: list[tuple[int, str, str, str]] = []


def _register(version: int, name: str, up: str, down: str) -> None:
    MIGRATIONS.append((version, name, up, down))


# Tables owned by the payment-channel ledger (channels.py). The ledger lives in its
# OWN database file (AIMARKET_CHANNELS_DB_PATH) and must therefore apply a SUBSET of
# the registry. That subset is DERIVED from the DDL below (see channel_ledger_versions)
# rather than hand-pinned to a version number: a hand-maintained pin silently skips the
# next channels migration, which is how a ledger ends up running against a schema it
# thinks it has.
CHANNEL_LEDGER_TABLES: tuple[str, ...] = (
    "channels",
    "debited_receipts",
    "consumed_deposits",
    "channel_holds",
    "channel_payout_obligations",
)


# All DDL is written for SQLite. PostgresBackend.executescript()
# translates to PostgreSQL dialect automatically (sqlite_to_pg):
#   INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
#   REAL → DOUBLE PRECISION
#   datetime('now') → NOW()
#   INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE
#   PRAGMA → (removed for PG)

# 001: Core hub tables (capabilities, peers)
_register(1, "001_core_tables", """
CREATE TABLE IF NOT EXISTS capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT 'v1',
    description TEXT DEFAULT '',
    input_schema TEXT DEFAULT '{}',
    output_schema TEXT DEFAULT '{}',
    price_per_call_usd REAL DEFAULT 0.35,
    p50_latency_ms INTEGER DEFAULT 3000,
    success_rate_30d REAL DEFAULT 0.97,
    source_hub TEXT NOT NULL,
    source_hub_name TEXT DEFAULT '',
    routed_price_usd REAL,
    routing_fee_bps INTEGER DEFAULT 0,
    trust_score REAL DEFAULT 0.0,
    agent TEXT DEFAULT '',
    prompt_template TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(capability_id, product_id, source_hub)
);

CREATE INDEX IF NOT EXISTS idx_caps_source ON capabilities(source_hub);
CREATE INDEX IF NOT EXISTS idx_caps_cid ON capabilities(capability_id);

CREATE TABLE IF NOT EXISTS peers (
    url TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capabilities_count INTEGER DEFAULT 0,
    last_crawl TEXT DEFAULT '',
    trust_score REAL DEFAULT 0.0,
    well_known_url TEXT DEFAULT '',
    manifest_url TEXT DEFAULT '',
    public_key TEXT DEFAULT '',
    depth INTEGER DEFAULT 0,
    discoverer TEXT DEFAULT '',
    status TEXT DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_peers_status ON peers(status);
""", """
DROP TABLE IF EXISTS capabilities;
DROP TABLE IF EXISTS peers;
""")

# 002: Invocation stats
_register(2, "002_invocation_stats", """
CREATE TABLE IF NOT EXISTS invocation_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    source_hub TEXT NOT NULL,
    price_usd REAL DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    timestamp TEXT DEFAULT (datetime('now')),
    consumer_hub TEXT DEFAULT 'local'
);

CREATE INDEX IF NOT EXISTS idx_stats_ts ON invocation_stats(timestamp);
""", """
DROP TABLE IF EXISTS invocation_stats;
""")

# 003: Reputation events
_register(3, "003_reputation_events", """
CREATE TABLE IF NOT EXISTS reputation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    provider_hub TEXT NOT NULL,
    capability_id TEXT,
    timestamp TEXT DEFAULT (datetime('now')),
    price_usd REAL DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    consumer_hub TEXT NOT NULL,
    signature TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reputation_provider ON reputation_events(provider_hub);
""", """
DROP TABLE IF EXISTS reputation_events;
""")

# 004: Payment channels
_register(4, "004_channels", """
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    balance_cents INTEGER NOT NULL,
    original_deposit_cents INTEGER NOT NULL,
    used_cents INTEGER NOT NULL DEFAULT 0,
    token TEXT NOT NULL DEFAULT 'USDT',
    chain TEXT NOT NULL DEFAULT 'base',
    wallet TEXT NOT NULL DEFAULT '',
    tx_hash TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    settle_tx_hash TEXT NOT NULL DEFAULT '',
    closed_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status);
CREATE INDEX IF NOT EXISTS idx_channels_wallet ON channels(wallet);
CREATE INDEX IF NOT EXISTS idx_channels_expires ON channels(expires_at) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS debited_receipts (
    receipt_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
""", """
DROP TABLE IF EXISTS debited_receipts;
DROP TABLE IF EXISTS channels;
""")

# 005: Provenance receipts
_register(5, "005_provenance_receipts", """
CREATE TABLE IF NOT EXISTS provenance_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL,
    provider_hub TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    parent_receipts TEXT DEFAULT '[]',
    timestamp TEXT NOT NULL,
    issuer_pubkey_b64 TEXT NOT NULL,
    proof_value TEXT NOT NULL,
    tee_attestation TEXT,
    latency_ms INTEGER DEFAULT 0,
    price_usd REAL DEFAULT 0.0,
    invocation_nonce TEXT DEFAULT '',
    reputation_score REAL,
    raw_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_prov_receipt_id ON provenance_receipts(receipt_id);
CREATE INDEX IF NOT EXISTS idx_prov_model ON provenance_receipts(model_id);
CREATE INDEX IF NOT EXISTS idx_prov_provider ON provenance_receipts(provider_hub);
CREATE INDEX IF NOT EXISTS idx_prov_timestamp ON provenance_receipts(timestamp);
""", """
DROP TABLE IF EXISTS provenance_receipts;
""")

# 006: Peer self-declared categories (federation discovery / monitor filtering)
# `down` actually drops the column (SQLite >= 3.35 / PostgreSQL) so a
# rollback→reapply round-trip is clean — a no-op down would unregister the
# migration while leaving the column, making the next `up` fail with
# "duplicate column" and aborting hub startup.
_register(6, "006_peer_categories", """
ALTER TABLE peers ADD COLUMN categories TEXT DEFAULT '[]';
""", """
ALTER TABLE peers DROP COLUMN categories;
""")

# 007: Per-channel debit secret (SHA-256 hash). Channels opened after this migration return a
# one-time `channel_secret` to the owner; debit requires it (PAYAUTH — a leaked channel id alone
# can no longer drain the channel). Legacy channels (empty hash) keep working for back-compat.
_register(7, "007_channel_secret", """
ALTER TABLE channels ADD COLUMN secret_hash TEXT NOT NULL DEFAULT '';
""", """
ALTER TABLE channels DROP COLUMN secret_hash;
""")

# 008: Operator-approved federation peers (anti-TOFU). A peer's manifests are indexed only when
# trusted=1 — set by an out-of-band seed pin or explicit operator approval. First contact (and
# every subsequent crawl) of an unapproved peer records it but never indexes, so a malicious hub
# can't inject capabilities just by being crawled twice.
_register(8, "008_peer_trusted", """
ALTER TABLE peers ADD COLUMN trusted INTEGER NOT NULL DEFAULT 0;
""", """
ALTER TABLE peers DROP COLUMN trusted;
""")

# 009: Direct invoke URL for community-published capabilities
_register(9, "009_capability_invoke_url", """
ALTER TABLE capabilities ADD COLUMN invoke_url TEXT DEFAULT '';
""", """
ALTER TABLE capabilities DROP COLUMN invoke_url;
""")

# 010: Community supply security (stake, LUMEN graph, publisher identity)
_register(10, "010_supply_security", """
ALTER TABLE capabilities ADD COLUMN publisher_id TEXT DEFAULT '';
ALTER TABLE capabilities ADD COLUMN provider_pubkey TEXT DEFAULT '';
ALTER TABLE capabilities ADD COLUMN stake_usd REAL DEFAULT 0;

CREATE TABLE IF NOT EXISTS supply_stakes (
    publisher_id TEXT PRIMARY KEY,
    amount_usd REAL NOT NULL DEFAULT 0,
    slashed_usd REAL NOT NULL DEFAULT 0,
    token TEXT DEFAULT 'USDC',
    tx_hash TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supply_publish_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    invoke_url TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_supply_publish_pub ON supply_publish_events(publisher_id, created_at);
CREATE INDEX IF NOT EXISTS idx_supply_publish_url ON supply_publish_events(invoke_url);

CREATE TABLE IF NOT EXISTS trust_graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    weight REAL NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trust_edges_dst ON trust_graph_edges(dst);
""", """
DROP TABLE IF EXISTS trust_graph_edges;
DROP TABLE IF EXISTS supply_publish_events;
DROP TABLE IF EXISTS supply_stakes;
""")


# 011: Self-bond — consumer-side cost/conduct bond (ARGUS self-slash enforcement).
# The agent stakes (supply_stakes) and registers a bonded spend CEILING + the client
# self-bond commitment; /self-bond/slash slashes that stake on a declared-vs-observed
# breach and federates the attestation through the same slash registry publishers use.
_register(11, "011_self_bonds", """
CREATE TABLE IF NOT EXISTS self_bonds (
    agent_id TEXT PRIMARY KEY,
    evm_address TEXT DEFAULT '',
    ceiling_usd REAL NOT NULL DEFAULT 0,
    bond_usd REAL NOT NULL DEFAULT 0,
    token TEXT DEFAULT 'USDC',
    commitment TEXT DEFAULT '',
    slashed_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
""", """
DROP TABLE IF EXISTS self_bonds;
""")


# 012: Consumed deposit transactions — single-use guard for channel funding.
# A verified on-chain deposit may fund exactly ONE channel. The verifier
# (web.backend...on_chain.verify_tx_payment) is stateless — it only checks
# recipient/amount/token/confirmations — so without this table a single real
# USDC deposit could be replayed to POST /channel/open unlimited times, each
# minting a channel with a full spendable balance (PAYAUTH-002, double-credit).
# The (chain, tx_hash) PRIMARY KEY makes the "claim" atomic even across worker
# processes/threads sharing the DB.
_register(12, "012_consumed_deposits", """
CREATE TABLE IF NOT EXISTS consumed_deposits (
    chain TEXT NOT NULL DEFAULT '',
    tx_hash TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chain, tx_hash)
);
""", """
DROP TABLE IF EXISTS consumed_deposits;
""")

_register(13, "013_capability_is_demo", """
ALTER TABLE capabilities ADD COLUMN is_demo INTEGER DEFAULT 0;
""", """
-- SQLite cannot DROP COLUMN in older versions; no-op down for is_demo
SELECT 1;
""")

# 014: Escrow holds for Pay-on-Verified settlement (auth/capture on payment channels).
# A hold reserves channel funds at invoke time (balance_cents drops immediately, so a
# pending verification can never be double-spent); capture moves the reservation into
# used_cents + debited_receipts, release restores balance. Keyed by the invoke receipt
# nonce so hold replay protection is exactly as strong as debit replay protection.
_register(14, "014_channel_holds", """
CREATE TABLE IF NOT EXISTS channel_holds (
    receipt_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'held',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_holds_channel ON channel_holds(channel_id, status);
""", """
DROP TABLE IF EXISTS channel_holds;
""")

# 015: Pay-on-Verified settlement records — one row per verified invoke, carrying the
# buyer intent + provider output for the background Metis verdict, the evolving
# verification envelope, and the final receipt served by GET /verification/{nonce}.
# Pending rows are re-queued on hub startup so a restart never strands a hold.
_register(15, "015_verified_settlements", """
CREATE TABLE IF NOT EXISTS verified_settlements (
    nonce TEXT PRIMARY KEY,
    product_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    channel_id TEXT DEFAULT '',
    provider_id TEXT DEFAULT '',
    price_usd REAL DEFAULT 0,
    intent TEXT DEFAULT '',
    output_json TEXT DEFAULT '{}',
    mode TEXT DEFAULT 'fast',
    status TEXT NOT NULL DEFAULT 'pending',
    envelope_json TEXT DEFAULT '{}',
    rejection_json TEXT DEFAULT '',
    receipt_json TEXT DEFAULT '{}',
    attempts INTEGER DEFAULT 0,
    engine_attempts INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_vs_status ON verified_settlements(status);
""", """
DROP TABLE IF EXISTS verified_settlements;
""")

# 016: Slash calibration + durable federation log.
# supply_fault_events replaces the in-memory consecutive-failure counter (a restart
# must not amnesty a failure streak); supply_slash_events records every individual
# slash so the cool-down and rolling daily cap survive restarts and stay auditable;
# slash_attestations persists the federated SlashRegistry (this hub's authored
# signed log — including its monotonic seq — plus verified peer entries with their
# strong/weak evidence tier).
_register(16, "016_slash_calibration", """
CREATE TABLE IF NOT EXISTS supply_fault_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    product_id TEXT DEFAULT '',
    capability_id TEXT DEFAULT '',
    consumer_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_supply_faults ON supply_fault_events(publisher_id, kind, created_at);

CREATE TABLE IF NOT EXISTS supply_slash_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher_id TEXT NOT NULL,
    amount_usd REAL NOT NULL DEFAULT 0,
    reason TEXT DEFAULT '',
    evidence_kind TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_supply_slash_pub ON supply_slash_events(publisher_id, created_at);

CREATE TABLE IF NOT EXISTS slash_attestations (
    issuer_hub TEXT NOT NULL,
    seq INTEGER NOT NULL,
    envelope_json TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'strong',
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (issuer_hub, seq)
);
""", """
DROP TABLE IF EXISTS slash_attestations;
DROP TABLE IF EXISTS supply_slash_events;
DROP TABLE IF EXISTS supply_fault_events;
""")


# 017: Honest channel accounting — payout obligations + hold resolution notes.
#
# channel_payout_obligations: a channel deposit is verified as a payment to the
# PLATFORM's settlement wallet, so the unspent remainder at close/expiry is already the
# operator's money. Nothing in this process may send value back, so the remainder is
# recorded as an explicit, queryable DEBT to the depositor instead of being logged as a
# "refund" that never happened (ACCT-001). One row per channel (channel_id PRIMARY KEY)
# keeps close + expiry-sweep idempotent.
#
# channel_holds.resolution_note: why a hold left 'held' — in particular the reaper's
# auditable record for an orphaned hold (hold committed, settlement row never written),
# which previously froze a buyer's balance forever with no reaper.
_register(17, "017_channel_payout_obligations", """
CREATE TABLE IF NOT EXISTS channel_payout_obligations (
    channel_id TEXT PRIMARY KEY,
    wallet TEXT NOT NULL DEFAULT '',
    chain TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'close_remainder',
    status TEXT NOT NULL DEFAULT 'owed',
    deposit_tx_hash TEXT NOT NULL DEFAULT '',
    payout_tx_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    settled_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_obligations_status ON channel_payout_obligations(status);
CREATE INDEX IF NOT EXISTS idx_obligations_wallet ON channel_payout_obligations(wallet, status);

ALTER TABLE channel_holds ADD COLUMN resolution_note TEXT NOT NULL DEFAULT '';
""", """
ALTER TABLE channel_holds DROP COLUMN resolution_note;
DROP TABLE IF EXISTS channel_payout_obligations;
""")


# 018: escrow-backed channels (opt-in bridge). A channel funded by an on-chain
# AIMarketEscrow deposit records WHICH escrow channel backs it, so the invoke path can
# check a buyer's DebitAuthorization against the right contract state and the mirror knows
# what to submit against. Empty for every transfer-funded channel, which is all of them
# until an operator enables the bridge.
_register(18, "018_channel_escrow_binding", """
ALTER TABLE channels ADD COLUMN escrow_channel TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_channels_escrow ON channels(escrow_channel);
""", """
DROP INDEX IF EXISTS idx_channels_escrow;
ALTER TABLE channels DROP COLUMN escrow_channel;
""")


# 019: supply_fault_events.consumer_id — griefing-resistant slash calibration needs
# distinct consumers. Migration 016's CREATE included the column in source, but hubs
# that applied an earlier 016 CREATE (without it) never got an ALTER. ADD COLUMN is
# idempotent in apply() via duplicate-column tolerance below.
_register(19, "019_supply_fault_consumer_id", """
ALTER TABLE supply_fault_events ADD COLUMN consumer_id TEXT DEFAULT '';
""", """
ALTER TABLE supply_fault_events DROP COLUMN consumer_id;
""")

# 020: Signed supply-chain admission decisions.  The table intentionally stores
# only the manifest digest and bounded findings, never the submitted manifest or
# its evidence URLs.  Alien Monitor can therefore read the public audit stream
# without gaining access to customer dossiers.
_register(20, "020_supply_chain_admission", """
CREATE TABLE IF NOT EXISTS supply_chain_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT NOT NULL UNIQUE,
    publisher_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'advisory',
    status TEXT NOT NULL DEFAULT 'unavailable',
    decision TEXT DEFAULT '',
    score INTEGER,
    risk_tier TEXT DEFAULT '',
    findings_json TEXT DEFAULT '[]',
    remediations_json TEXT DEFAULT '[]',
    owasp_risks_json TEXT DEFAULT '[]',
    signature TEXT DEFAULT '',
    auditor_pubkey TEXT DEFAULT '',
    metis_status TEXT DEFAULT 'skipped',
    metis_verification_id TEXT DEFAULT '',
    metis_detail_json TEXT DEFAULT '{}',
    error_code TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_supply_audits_created
ON supply_chain_audits(created_at);
CREATE INDEX IF NOT EXISTS idx_supply_audits_capability
ON supply_chain_audits(capability_id, product_id);
CREATE INDEX IF NOT EXISTS idx_supply_audits_metis
ON supply_chain_audits(metis_status, updated_at);
""", """
DROP TABLE IF EXISTS supply_chain_audits;
""")


# 021: What the admission gate actually verified, plus the signed observer reports
# that make a declared permission falsifiable.  Both are bounded: the audit row
# keeps counts and booleans (never an evidence URL), and a violation report keeps
# only the digest of the declaration it contradicts — never the dossier.
_register(21, "021_supply_admission_attestations", """
ALTER TABLE supply_chain_audits ADD COLUMN attestations_json TEXT DEFAULT '{}';
ALTER TABLE supply_chain_audits ADD COLUMN permissions_sha256 TEXT DEFAULT '';
ALTER TABLE supply_chain_audits ADD COLUMN permissions_json TEXT DEFAULT '{}';

CREATE TABLE IF NOT EXISTS supply_permission_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    permission TEXT NOT NULL,
    permissions_sha256 TEXT NOT NULL,
    reporter_pubkey TEXT NOT NULL,
    signature TEXT NOT NULL,
    consumer_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (capability_id, permission, permissions_sha256, reporter_pubkey)
);
CREATE INDEX IF NOT EXISTS idx_supply_violations_capability
ON supply_permission_violations(product_id, capability_id, permissions_sha256);
CREATE INDEX IF NOT EXISTS idx_supply_violations_publisher
ON supply_permission_violations(publisher_id, created_at);
""", """
DROP TABLE IF EXISTS supply_permission_violations;
ALTER TABLE supply_chain_audits DROP COLUMN permissions_json;
ALTER TABLE supply_chain_audits DROP COLUMN permissions_sha256;
ALTER TABLE supply_chain_audits DROP COLUMN attestations_json;
""")

# 022: Surface federation pin mismatches to operators. The crawler used to only
# log "public key changed! Rejecting (possible takeover)" and leave the peer
# looking healthy on /federation/peers — ATLAS stayed frozen in the catalogue
# for days with no UI signal. Persist the reject reason + advertised key; keep
# status=key_mismatch so the peers list can show it without reading logs.
_register(22, "022_peer_pin_reject", """
ALTER TABLE peers ADD COLUMN pin_reject_reason TEXT DEFAULT '';
ALTER TABLE peers ADD COLUMN advertised_public_key TEXT DEFAULT '';
""", """
ALTER TABLE peers DROP COLUMN advertised_public_key;
ALTER TABLE peers DROP COLUMN pin_reject_reason;
""")

# ── Subsystem selection ─────────────────────────────────────────────


def _touches_table(sql: str, table: str) -> bool:
    """Whether a migration's DDL references `table` as a table name.

    Word-boundary match so a COLUMN named channel_id or an index named
    idx_channels_status does not count as touching the `channels` table.
    """
    return re.search(rf"(?<![\w]){re.escape(table)}(?![\w])", sql) is not None


# Open federation (AIMARKET_FEDERATION_OPEN). Three additions, all inert while the
# flag is off:
#   * peers.first_seen        — when this hub first heard of the peer, so a newly
#                               appeared stranger can be told apart from an old one.
#   * peer_preview_capabilities — what a PENDING peer claims to offer. Deliberately a
#                               SEPARATE table, never `capabilities`: search, routing,
#                               invoke and the published manifest all read
#                               `capabilities`, so an unapproved peer's rows cannot
#                               reach any of them even if a future query forgets a
#                               filter. The isolation is structural, not a WHERE clause.
#   * federation_inbound      — who crawled US. Until now a peer could index this hub
#                               and the operator had no way to know it existed.
_register(23, "023_open_federation", """
    ALTER TABLE peers ADD COLUMN first_seen TEXT DEFAULT '';

    CREATE TABLE IF NOT EXISTS peer_preview_capabilities (
        peer_url TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        product_id TEXT NOT NULL DEFAULT '',
        name TEXT DEFAULT '',
        description TEXT DEFAULT '',
        price_per_call_usd REAL DEFAULT 0,
        categories TEXT DEFAULT '[]',
        seen_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (peer_url, capability_id, product_id)
    );

    CREATE INDEX IF NOT EXISTS idx_preview_peer ON peer_preview_capabilities(peer_url);

    CREATE TABLE IF NOT EXISTS federation_inbound (
        hub_url TEXT PRIMARY KEY,
        user_agent TEXT DEFAULT '',
        first_seen TEXT DEFAULT (datetime('now')),
        last_seen TEXT DEFAULT (datetime('now')),
        hits INTEGER DEFAULT 1
    );
""", """
    DROP TABLE IF EXISTS federation_inbound;
    DROP INDEX IF EXISTS idx_preview_peer;
    DROP TABLE IF EXISTS peer_preview_capabilities;
""")


# Credits (AIMARKET_CREDITS_ENABLED) — the payment rail that needs no chain. Three tables,
# all inert while the flag is off:
#   * credit_accounts — prepaid balances in MILLICENTS. Not cents: the channel ledger bills
#                       whole cents rounded up, which cannot express the $0.0059 this
#                       ecosystem actually averages per call. `held_mc` is the reservation
#                       column, kept separate from `balance_mc` so an in-flight invoke's
#                       money is visibly neither spendable nor spent.
#   * credit_holds    — one row per reservation, keyed on the invoke's receipt id, so a
#                       hold survives a crash and can be released by id exactly once.
#   * collateral_mc   — a publisher's posted stake. Deliberately NOT `spent_mc`: collateral is
#                       money the operator is holding against future misbehaviour and may have
#                       to return, so counting it as revenue overstates earnings by the whole
#                       stake the moment somebody publishes.
#   * credit_ledger   — append-only history. The operator holds prepaid money (the same
#                       custody ACCT-001 describes for channels), so every movement needs
#                       a row that can be shown to whoever paid.
_register(24, "024_credits_rail", """
    CREATE TABLE IF NOT EXISTS credit_accounts (
        account_id TEXT PRIMARY KEY,
        key_hash TEXT NOT NULL UNIQUE,
        label TEXT DEFAULT '',
        balance_mc INTEGER NOT NULL DEFAULT 0,
        held_mc INTEGER NOT NULL DEFAULT 0,
        spent_mc INTEGER NOT NULL DEFAULT 0,
        granted_mc INTEGER NOT NULL DEFAULT 0,
        collateral_mc INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS credit_holds (
        receipt_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        amount_mc INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'held',
        created_at TEXT DEFAULT (datetime('now')),
        resolved_at TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_credit_holds_account ON credit_holds(account_id);

    CREATE TABLE IF NOT EXISTS credit_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        amount_mc INTEGER NOT NULL,
        receipt_id TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_credit_ledger_account ON credit_ledger(account_id);
""", """
    DROP INDEX IF EXISTS idx_credit_ledger_account;
    DROP TABLE IF EXISTS credit_ledger;
    DROP INDEX IF EXISTS idx_credit_holds_account;
    DROP TABLE IF EXISTS credit_holds;
    DROP TABLE IF EXISTS credit_accounts;
""")



# x402 receivables (AIMARKET_X402_ACCEPT). A verified EIP-3009 authorization is money the
# payer has PROMISED on chain, not money that has arrived: submitting it needs an RPC and
# gas, and that happens out of band like the escrow sweep. One row per authorization, keyed
# on the nonce so a replay cannot buy a second call, and carrying its own settled/failed
# state so the unsettled total is a number the operator can actually see.
_register(25, "025_x402_payments", """
    CREATE TABLE IF NOT EXISTS x402_payments (
        nonce TEXT PRIMARY KEY,
        payer TEXT NOT NULL,
        amount_atomic TEXT NOT NULL DEFAULT '0',
        amount_usd REAL NOT NULL DEFAULT 0,
        asset TEXT DEFAULT '',
        network TEXT DEFAULT '',
        receipt_id TEXT DEFAULT '',
        capability_id TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'accepted',
        settle_tx_hash TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now')),
        settled_at TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_x402_status ON x402_payments(status);
    CREATE INDEX IF NOT EXISTS idx_x402_payer ON x402_payments(payer);
""", """
    DROP INDEX IF EXISTS idx_x402_payer;
    DROP INDEX IF EXISTS idx_x402_status;
    DROP TABLE IF EXISTS x402_payments;
""")


# Idempotent operator/checkout top-ups. Payment processors retry webhooks and operators
# retry commands after timeouts; without a unique external reference either case credits
# the same money twice. The row is committed atomically with the balance movement.
_register(26, "026_credit_topup_references", """
    CREATE TABLE IF NOT EXISTS credit_topups (
        reference TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        amount_mc INTEGER NOT NULL,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_credit_topups_account ON credit_topups(account_id);
""", """
    DROP INDEX IF EXISTS idx_credit_topups_account;
    DROP TABLE IF EXISTS credit_topups;
""")


# Post-quarantine assay dossiers. A pending peer's well-known copy is not evidence;
# this table stores the last deterministic scorecard (crypto / origin / sandbox
# receipt). Verdict never writes ``peers.trusted``. See federation_assay.py.
_register(27, "027_peer_assays", """
    CREATE TABLE IF NOT EXISTS peer_assays (
        peer_url TEXT PRIMARY KEY,
        verdict TEXT NOT NULL,
        checks_json TEXT NOT NULL DEFAULT '[]',
        sandbox_json TEXT NOT NULL DEFAULT '{}',
        advertised_key TEXT DEFAULT '',
        ran_at TEXT DEFAULT (datetime('now')),
        note TEXT DEFAULT ''
    );
""", """
    DROP TABLE IF EXISTS peer_assays;
""")


_register(28, "028_peer_self_description", """
    -- What the peer says about itself, kept apart from what this hub determined.
    -- Every one of these is already published in /.well-known/ai-market.json and was read,
    -- used for one decision, and thrown away — so the monitor grew its own hand-written
    -- tables of the same facts, which then drifted from the peers they described.
    -- `declared_id` is a CLAIM and is never an identity: see aimarket_hub/peer_identity.py.
    -- `mcp_endpoint` is stored for display only. Routing re-resolves it live, because a
    -- stored endpoint is a stale endpoint the moment a peer moves.
    ALTER TABLE peers ADD COLUMN description TEXT DEFAULT '';
    ALTER TABLE peers ADD COLUMN hub_version TEXT DEFAULT '';
    ALTER TABLE peers ADD COLUMN declared_id TEXT DEFAULT '';
    ALTER TABLE peers ADD COLUMN mcp_endpoint TEXT DEFAULT '';
""", """
    ALTER TABLE peers DROP COLUMN mcp_endpoint;
    ALTER TABLE peers DROP COLUMN declared_id;
    ALTER TABLE peers DROP COLUMN hub_version;
    ALTER TABLE peers DROP COLUMN description;
""")


_register(29, "029_peer_pq_identity", """
    -- The peer's POST-QUANTUM public key, pinned the same way its Ed25519 key is.
    --
    -- Without this column `verify_hybrid` reads `pq_public_key` out of the signature object,
    -- which makes the PQ layer useless against the only adversary it exists for: one who can
    -- forge Ed25519 forges it against the pinned classical key and attaches an ML-DSA keypair of
    -- their own. A post-quantum signature is worth exactly what the pinning of its public key is
    -- worth.
    --
    -- It has to be collected NOW, while classical signatures can still authenticate it. After
    -- Ed25519 falls, a PQ key seen for the first time cannot be attributed to anyone.
    --
    -- `advertised_pq_public_key` mirrors `advertised_public_key`: the last key that FAILED the
    -- pin check, kept so the federation desk can show what changed instead of a healthy-looking
    -- row beside a frozen catalogue.
    ALTER TABLE peers ADD COLUMN pq_public_key TEXT DEFAULT '';
    ALTER TABLE peers ADD COLUMN advertised_pq_public_key TEXT DEFAULT '';
""", """
    ALTER TABLE peers DROP COLUMN advertised_pq_public_key;
    ALTER TABLE peers DROP COLUMN pq_public_key;
""")

def channel_ledger_versions() -> list[int]:
    """Migration versions whose DDL touches a channel-ledger table.

    Derived from the registered SQL, so a NEW channels migration is picked up
    automatically — the channel ledger can never silently run on a stale schema
    because someone forgot to bump a hand-written target_version.
    """
    return sorted(
        version
        for version, _name, up, _down in MIGRATIONS
        if any(_touches_table(up, t) for t in CHANNEL_LEDGER_TABLES)
    )


SUBSYSTEM_VERSIONS: dict[str, Any] = {"channels": channel_ledger_versions}


# ── Migrations Manager ──────────────────────────────────────────────


class Migrations:
    """Apply and track database migrations."""

    def __init__(self, backend: DBBackend):
        self._backend = backend
        self._ensure_migrations_table()

    def _ensure_migrations_table(self) -> None:
        try:
            self._backend.execute(
                "CREATE TABLE IF NOT EXISTS _migrations ("
                "  version INTEGER PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  applied_at TEXT DEFAULT (datetime('now'))"
                ")"
            )
            self._backend.commit()
        except Exception as exc:
            logger.warning("Could not create _migrations table: %s", exc)

    def applied_versions(self) -> set[int]:
        try:
            self._backend.execute("SELECT version FROM _migrations ORDER BY version")
            rows = self._backend.fetchall()
            return {r["version"] for r in rows}
        except Exception:
            return set()

    def pending(self) -> list[tuple[int, str, str, str]]:
        applied = self.applied_versions()
        return [m for m in MIGRATIONS if m[0] not in applied]

    def apply(self, target_version: int | None = None, subsystem: str = "") -> int:
        """Apply pending migrations.

        target_version: legacy upper bound (kept for existing callers).
        subsystem: apply only the migrations that subsystem's tables need — used by
            the payment-channel ledger, which owns a separate database file. The
            version set is derived from the DDL (SUBSYSTEM_VERSIONS), so it cannot
            go stale the way a hard-coded target_version does.
        """
        pending = self.pending()
        if target_version is not None:
            pending = [m for m in pending if m[0] <= target_version]
        if subsystem:
            resolver = SUBSYSTEM_VERSIONS.get(subsystem)
            if resolver is None:
                raise ValueError(f"unknown migration subsystem: {subsystem}")
            wanted = set(resolver())
            pending = [m for m in pending if m[0] in wanted]

        applied_count = 0
        for version, name, sql_up, _ in sorted(pending, key=lambda m: m[0]):
            logger.info("Applying migration %03d: %s", version, name)
            # Atomic: schema change + _migrations bookkeeping in ONE commit.
            # Previous two-commit pattern allowed crash between them, leaving
            # schema applied but not registered → next start would re-apply
            # and potentially double-INSERT seed data (EXP-77).
            try:
                self._backend.executescript(sql_up)
                self._backend.execute(
                    "INSERT INTO _migrations (version, name) VALUES (?, ?)",
                    (version, name),
                )
                self._backend.commit()
                applied_count += 1
                logger.info("  OK %03d", version)
            except Exception as exc:
                # ALTER ADD COLUMN on a DB that already has the column (CREATE evolved
                # in source after the migration was first applied) — treat as applied
                # so a redeploy cannot abort hub startup over a no-op schema drift.
                # SQLite: "duplicate column name: …"
                # Postgres/psycopg: 'column "…" of relation "…" already exists'
                msg = str(exc).lower()
                if "duplicate column" in msg or (
                    "already exists" in msg and "column" in msg
                ):
                    logger.warning("  SKIP %03d (already present): %s", version, exc)
                    self._backend.execute(
                        "INSERT INTO _migrations (version, name) VALUES (?, ?)",
                        (version, name),
                    )
                    self._backend.commit()
                    applied_count += 1
                    continue
                logger.error("  FAILED %03d: %s", version, exc)
                raise
        return applied_count

    def rollback(self, steps: int = 1) -> int:
        self._backend.execute(
            "SELECT version, name FROM _migrations ORDER BY version DESC LIMIT ?",
            (steps,),
        )
        rows = self._backend.fetchall()
        rolled = 0
        for row in reversed(rows):
            version = int(row["version"])
            name = row["name"]
            for v, _n, _, sql_down in MIGRATIONS:
                if v == version:
                    logger.info("Rolling back %03d: %s", version, name)
                    self._backend.executescript(sql_down)
                    self._backend.execute(
                        "DELETE FROM _migrations WHERE version = ?",
                        (version,),
                    )
                    self._backend.commit()
                    rolled += 1
                    break
        return rolled

    def status(self) -> list[dict[str, Any]]:
        applied = self.applied_versions()
        return [
            {"version": v, "name": n, "applied": v in applied}
            for v, n, _, _ in sorted(MIGRATIONS, key=lambda m: m[0])
        ]


# ── CLI ─────────────────────────────────────────────────────────────


def _cli() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = os.environ.get("DATABASE_URL", "")
    db_path = os.environ.get("AIMARKET_DB_PATH", "data/hub.db")
    backend = create_backend(database_url=database_url, db_path=db_path)
    migrations = Migrations(backend)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "up":
        count = migrations.apply()
        print(f"\nApplied {count} migration(s)")
    elif cmd == "down":
        steps = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        count = migrations.rollback(steps)
        print(f"\nRolled back {count} migration(s)")
    elif cmd == "status":
        rows = migrations.status()
        print(f"\nBackend: {backend.backend_type}")
        print(f"{'Version':>8}  {'Name':<30}  {'Status'}")
        print("-" * 55)
        for r in rows:
            s = "OK" if r["applied"] else "PENDING"
            print(f"{r['version']:>8}  {r['name']:<30}  {s}")
    else:
        print("Usage: python -m aimarket_hub.migrations [up|down|status]")
        sys.exit(1)

    backend.close()


if __name__ == "__main__":
    _cli()
