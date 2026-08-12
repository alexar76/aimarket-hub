"""Database index for capabilities, peers, receipts, and reputation events.

Supports SQLite (default) and PostgreSQL via DATABASE_URL env var.
Uses the DBBackend abstraction for dialect-agnostic queries.
"""

from __future__ import annotations

import contextlib
import json
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aimarket_hub.db_backend import DBBackend, create_backend
from aimarket_hub.migrations import Migrations
from aimarket_hub.models import Capability, InvocationStat, Peer, ReputationEvent


def _utc_cutoff(seconds_ago: float) -> str:
    """A UTC cutoff timestamp in SQLite's ``datetime()`` text format, computed in Python.

    Time-window queries compare ``created_at >= _utc_cutoff(...)`` instead of
    ``created_at >= datetime('now', ?)`` / ``julianday()`` so they are backend-portable:
    both SQLite and PostgreSQL compare the fixed ``YYYY-MM-DD HH:MM:SS`` prefix as TEXT
    (which sorts chronologically), and neither needs a dialect-specific date function the
    ``sqlite_to_pg`` translator cannot rewrite (the modifier is a bound param). The window
    is clamped non-negative so a misconfigured negative value can't invert the comparison."""
    secs = max(0.0, float(seconds_ago))
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")


def _parse_db_ts(value: Any) -> datetime | None:
    """Parse a stored ``created_at`` (SQLite ``YYYY-MM-DD HH:MM:SS`` or a PostgreSQL text
    variant with a sub-second / ``+00`` suffix) as UTC, reading the leading 19 chars."""
    s = str(value or "")
    if len(s) < 19:
        return None
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class HubDatabase:
    """Hub index database — SQLite or PostgreSQL via DATABASE_URL.

    Args:
        db_path: SQLite path (used when database_url is not set)
        database_url: PostgreSQL connection string (optional)
    """

    def __init__(
        self,
        db_path: str | Path = "data/hub.db",
        database_url: str = "",
    ):
        self._backend: DBBackend = create_backend(
            database_url=database_url, db_path=db_path,
        )
        # The file the backend ACTUALLY opened, not the argument we asked for. For the
        # bare `HubDatabase()` shape the argument is the hub default, which means "no
        # subsystem chose a path" — create_backend then lets AIMARKET_DB_PATH name the
        # file, so storing the argument here reported `data/hub.db` while every read and
        # write went to the env-var file. Anything using this attribute is doing
        # diagnostics (health endpoints, backup/migration tooling, log lines), and a
        # diagnostic that names the wrong file is worse than no diagnostic.
        resolved = getattr(self._backend, "db_path", None)
        # PostgreSQL has no file at all: keep the requested path as an inert record rather
        # than inventing one, and let `backend_type` say which world we are in.
        self.db_path = Path(resolved) if resolved is not None else Path(db_path)
        migrations = Migrations(self._backend)
        migrations.apply()
        self._conn = self._backend  # backward compat for tests

    def _migrate(self) -> None:
        # Keep for backward compat — actual migration runs via Migrations in __init__
        pass

    def _legacy_migrate(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
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
                invoke_url TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(capability_id, product_id, source_hub)
            );

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

            CREATE INDEX IF NOT EXISTS idx_caps_source ON capabilities(source_hub);
            CREATE INDEX IF NOT EXISTS idx_caps_cid ON capabilities(capability_id);
            CREATE INDEX IF NOT EXISTS idx_peers_status ON peers(status);
            CREATE INDEX IF NOT EXISTS idx_stats_ts ON invocation_stats(timestamp);
            CREATE INDEX IF NOT EXISTS idx_reputation_provider ON reputation_events(provider_hub);
        """)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after first release (idempotent)."""
        try:
            self._conn.execute("ALTER TABLE capabilities ADD COLUMN invoke_url TEXT DEFAULT ''")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE capabilities ADD COLUMN is_demo INTEGER DEFAULT 0")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass

    # ── Capabilities ──────────────────────────────────────────

    def upsert_capability(self, cap: Capability) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO capabilities
                (capability_id, product_id, name, version, description,
                 input_schema, output_schema, price_per_call_usd, p50_latency_ms,
                 success_rate_30d, source_hub, source_hub_name,
                 routed_price_usd, routing_fee_bps, trust_score, agent, prompt_template,
                 invoke_url, publisher_id, provider_pubkey, stake_usd, is_demo, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            cap.capability_id, cap.product_id, cap.name, cap.version, cap.description,
            json.dumps(cap.input_schema), json.dumps(cap.output_schema),
            cap.price_per_call_usd, cap.p50_latency_ms, cap.success_rate_30d,
            cap.source_hub, cap.source_hub_name,
            cap.routed_price_usd, cap.routing_fee_bps, cap.trust_score,
            cap.agent, cap.prompt_template, cap.invoke_url or "",
            cap.publisher_id or "", cap.provider_pubkey or "", cap.stake_usd or 0.0,
            1 if cap.is_demo else 0,
        ))
        self._conn.commit()

    def get_capability(self, product_id: str, capability_id: str, source_hub: str = "local") -> Capability | None:
        row = self._conn.execute(
            "SELECT * FROM capabilities WHERE product_id=? AND capability_id=? AND source_hub=?",
            (product_id, capability_id, source_hub),
        ).fetchone()
        return _row_to_capability(row) if row else None

    def find_by_capability_id(self, capability_id: str) -> Capability | None:
        """Best match when callers omit or mis-specify product_id (federated oracle caps)."""
        row = self._conn.execute(
            "SELECT * FROM capabilities WHERE capability_id=? ORDER BY trust_score DESC LIMIT 1",
            (capability_id,),
        ).fetchone()
        return _row_to_capability(row) if row else None

    def list_capabilities(self, source_hub: str | None = None, limit: int = 200) -> list[Capability]:
        if source_hub:
            rows = self._conn.execute(
                "SELECT * FROM capabilities WHERE source_hub=? ORDER BY trust_score DESC LIMIT ?",
                (source_hub, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM capabilities ORDER BY trust_score DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_capability(r) for r in rows]

    def list_capabilities_diverse(self, limit: int = 20) -> list[Capability]:
        """Browse catalogue without letting one high-trust peer monopolise the page.

        Empty-intent search used to be pure ``ORDER BY trust_score DESC``, so Platon (0.5)
        filled every slot and GAIA live relays never appeared on first paint. Round-robin
        across ``source_hub`` keeps trust ordering *within* each hub.
        """
        if limit <= 0:
            return []
        rows = self._conn.execute(
            "SELECT * FROM capabilities ORDER BY trust_score DESC LIMIT ?",
            (max(limit * 20, 200),),
        ).fetchall()
        by_hub: dict[str, list[Any]] = {}
        for row in rows:
            hub = str(row["source_hub"] or "local")
            by_hub.setdefault(hub, []).append(row)
        # Prefer hubs that already have something to sell first (stable alpha as tiebreak).
        hubs = sorted(by_hub.keys(), key=lambda h: (-len(by_hub[h]), h))
        out: list[Capability] = []
        idx = 0
        while len(out) < limit:
            progressed = False
            for hub in hubs:
                bucket = by_hub[hub]
                if idx < len(bucket):
                    out.append(_row_to_capability(bucket[idx]))
                    progressed = True
                    if len(out) >= limit:
                        break
            if not progressed:
                break
            idx += 1
        return out

    # Terms too common to say anything about which capability someone wants. Matching on
    # them is what let a query about one subject rank a capability about another first.
    _SEARCH_STOPWORDS = frozenset(
        "a an and the of for to in on with my me our your is are be get "
        "how what which that this it its from by as at or".split()
    )

    # Expand buyer shorthand so live-only GAIA SKUs (which say "relayed"/"attested", not
    # "live") still surface for intents like "live sensors" or "iot carbon".
    _SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
        "live": ("live", "relay", "relayed", "gaia", "iot"),
        "relay": ("relay", "relayed", "live", "gaia"),
        "iot": ("iot", "gaia", "sensor", "physical"),
        "physical": ("physical", "gaia", "iot", "sensor"),
        "sensor": ("sensor", "sensors", "gaia", "iot"),
        "carbon": ("carbon", "grid", "intensity"),
        "quake": ("quake", "earthquake", "seismic", "usgs"),
        "earthquake": ("earthquake", "quake", "seismic", "usgs"),
        "tide": ("tide", "tides", "noaa", "water"),
        "weather": ("weather", "open-meteo", "nws", "gaia"),
    }

    @classmethod
    def _expand_search_terms(cls, terms: list[str]) -> list[str]:
        expanded: list[str] = []
        seen: set[str] = set()
        for t in terms:
            for syn in cls._SEARCH_SYNONYMS.get(t, (t,)):
                if syn not in seen:
                    seen.add(syn)
                    expanded.append(syn)
        return expanded

    @staticmethod
    def _relevance_score(hit: float, n_terms: int) -> float:
        """Map internal hit weight to a stable 0..1 score for API clients.

        Max theoretical hit ≈ 5 * n_terms (3 id + 2 coverage per term). Clamp so a
        perfect cover lands near 1.0 without claiming precision we do not have.
        """
        if n_terms <= 0:
            return 0.0
        return round(min(1.0, hit / (5.0 * n_terms)), 4)

    def search_capabilities_ranked(
        self, query: str, limit: int = 20
    ) -> list[tuple[Capability, float]]:
        """Keyword search with real relevance scores (not a constant).

        Returns ``(capability, score)`` sorted by hit weight then trust. Empty / all-stopword
        queries use diversified browse with score ``0.0`` (browse, not a match claim).
        """
        raw_terms = [t.lower().strip(".,;:!?()[]\"'") for t in query.split()]
        terms = [t for t in raw_terms if len(t) > 1 and t not in self._SEARCH_STOPWORDS]
        if not terms:
            # All stopwords ("what is it for") — fall back to the unfiltered words rather
            # than silently returning the whole catalogue as if nothing was asked.
            terms = [t for t in raw_terms if len(t) > 1]
        if not terms:
            return [(c, 0.0) for c in self.list_capabilities_diverse(limit=limit)]

        recall_terms = self._expand_search_terms(terms)
        placeholders = " OR ".join(
            ["(LOWER(capability_id) LIKE ? OR LOWER(name) LIKE ? OR LOWER(description) LIKE ?)"]
            * len(recall_terms)
        )
        params: list[Any] = []
        for t in recall_terms:
            like = f"%{t}%"
            params.extend([like, like, like])
        # Over-fetch: ranking happens here, so the SQL LIMIT must not decide the answer.
        rows = self._conn.execute(
            f"SELECT * FROM capabilities WHERE {placeholders} LIMIT ?",
            [*params, max(limit * 10, 200)],
        ).fetchall()

        def score_row(row: Any) -> tuple[float, float]:
            cid = str(row["capability_id"] or "").lower()
            name = str(row["name"] or "").lower()
            desc = str(row["description"] or "").lower()
            hit = 0.0
            covered = 0
            # Rank on the buyer's original terms; synonyms only expand recall.
            for t in terms:
                where = 0.0
                if t in cid:
                    where = 3.0
                elif t in name:
                    where = 2.0
                elif t in desc:
                    where = 1.0
                else:
                    # Synonym hit still counts, but weaker than a literal term match.
                    for syn in self._SEARCH_SYNONYMS.get(t, ()):
                        if syn == t:
                            continue
                        if syn in cid or syn in name or syn in desc:
                            where = 0.75
                            break
                if where:
                    covered += 1
                    hit += where
            # Covering more of the query dominates a single strong hit: two matched words
            # out of three describes the request better than one word appearing in an id.
            hit += 2.0 * covered
            return (hit, float(row["trust_score"] or 0.0))

        scored: list[tuple[float, float, Any]] = []
        for row in rows:
            hit, trust = score_row(row)
            scored.append((hit, trust, row))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [
            (_row_to_capability(row), self._relevance_score(hit, len(terms)))
            for hit, _trust, row in scored[:limit]
        ]

    def search_capabilities(self, query: str, limit: int = 20) -> list[Capability]:
        """Keyword search ranked by how well each row matches, then by trust.

        Recall is still ANY-term (a buyer who adds a word should not lose results), but
        ordering used to be `ORDER BY trust_score DESC` alone — so relevance played no part
        at all. With the oracle family indexed at trust 0.265 and Platon at 0.5, "verifiable
        delay proof" returned platon.random ahead of chronos.eval and "cascade risk in a
        network" returned platon.beacon ahead of ablation.cascade: the right answer existed,
        was priced, and was unreachable through search.

        Scoring favours where the term matched — capability_id is what a caller ultimately
        passes, so a hit there means more than one in prose — and rewards covering more of
        the query. Trust becomes the tiebreaker it should always have been.
        """
        return [c for c, _ in self.search_capabilities_ranked(query, limit=limit)]

    def count_capabilities(self, source_hub: str | None = None) -> int:
        if source_hub:
            return self._conn.execute(
                "SELECT COUNT(*) FROM capabilities WHERE source_hub=?", (source_hub,)
            ).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]

    # SQL mirror of aimarket_hub.fulfillment.capability_is_fulfillable. Kept as SQL so a
    # count never has to load every row; kept next to count_capabilities so the two are
    # read together. Federated rows are always offerable — the peer owns execution.
    _OFFERABLE_LOCAL = (
        "source_hub='local' AND ("
        "COALESCE(invoke_url,'') != '' OR COALESCE(prompt_template,'') LIKE '{%')"
    )

    def count_offerable(self, source_hub: str | None = None) -> int:
        """Capabilities this hub can actually execute — what it may honestly advertise.

        ``count_capabilities`` counts the table, including rows with no execution path.
        Publishing that number told peers the hub had seventeen capabilities while its
        manifest listed five.
        """
        if source_hub == "local":
            return self._conn.execute(
                f"SELECT COUNT(*) FROM capabilities WHERE {self._OFFERABLE_LOCAL}"
            ).fetchone()[0]
        if source_hub:
            return self._conn.execute(
                "SELECT COUNT(*) FROM capabilities WHERE source_hub=?", (source_hub,)
            ).fetchone()[0]
        return self._conn.execute(
            f"SELECT COUNT(*) FROM capabilities WHERE source_hub != 'local' "
            f"OR ({self._OFFERABLE_LOCAL})"
        ).fetchone()[0]

    def count_federated(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM capabilities WHERE source_hub != 'local'"
        ).fetchone()[0]

    def clear_federated(self) -> None:
        """Remove all non-local capabilities (before re-crawl)."""
        self._conn.execute("DELETE FROM capabilities WHERE source_hub != 'local'")
        self._conn.commit()

    def delete_capability(self, capability_id: str, source_hub: str = "local") -> int:
        """Delete a single capability by id. Returns the number of rows removed."""
        cur = self._conn.execute(
            "DELETE FROM capabilities WHERE capability_id=? AND source_hub=?",
            (capability_id, source_hub),
        )
        self._conn.commit()
        return cur.rowcount

    # ── Peers ─────────────────────────────────────────────────

    def upsert_peer(self, peer: Peer) -> None:
        self._conn.execute("""
            INSERT OR REPLACE INTO peers
                (url, name, capabilities_count, last_crawl, trust_score,
                 well_known_url, manifest_url, public_key, depth, discoverer,
                 categories, trusted, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """, (
            peer.url, peer.name, peer.capabilities_count, peer.last_crawl,
            peer.trust_score, peer.well_known_url, peer.manifest_url,
            peer.public_key, peer.depth, peer.discoverer,
            json.dumps(peer.categories or []), int(bool(peer.trusted)),
        ))
        self._conn.commit()

    def list_peers(self) -> list[Peer]:
        rows = self._conn.execute(
            "SELECT * FROM peers WHERE status='active' ORDER BY trust_score DESC"
        ).fetchall()
        return [_row_to_peer(r) for r in rows]

    def get_peer(self, url: str) -> Peer | None:
        row = self._conn.execute("SELECT * FROM peers WHERE url=?", (url,)).fetchone()
        return _row_to_peer(row) if row else None

    def set_peer_trusted(self, url: str, trusted: bool) -> bool:
        """Operator approval: mark a peer trusted/untrusted. Returns True if a peer was updated."""
        cur = self._conn.execute(
            "UPDATE peers SET trusted=? WHERE url=?", (int(bool(trusted)), url)
        )
        self._conn.commit()
        return (cur.rowcount or 0) > 0

    def peer_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM peers WHERE status='active'").fetchone()[0]

    # ── Stats ─────────────────────────────────────────────────

    def record_invocation(self, stat: InvocationStat) -> None:
        self._conn.execute("""
            INSERT INTO invocation_stats
                (capability_id, product_id, source_hub, price_usd, latency_ms, success, timestamp, consumer_hub)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stat.capability_id, stat.product_id, stat.source_hub,
            stat.price_usd, stat.latency_ms, int(stat.success),
            stat.timestamp, stat.consumer_hub,
        ))
        self._conn.commit()

    def recent_stats(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM invocation_stats ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def stats_summary(self) -> dict[str, Any]:
        """Aggregate stats for the live ticker and public status pages."""
        total = self._conn.execute("SELECT COUNT(*) FROM invocation_stats").fetchone()[0]
        success = self._conn.execute(
            "SELECT COUNT(*) FROM invocation_stats WHERE success=1"
        ).fetchone()[0]
        avg_price = self._conn.execute(
            "SELECT AVG(price_usd) FROM invocation_stats"
        ).fetchone()[0] or 0
        avg_latency = self._conn.execute(
            "SELECT AVG(latency_ms) FROM invocation_stats"
        ).fetchone()[0] or 0
        now = time.time()
        hour_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
        day_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 86400))
        revenue_hour = self._conn.execute(
            "SELECT COALESCE(SUM(price_usd), 0) FROM invocation_stats WHERE timestamp >= ?",
            (hour_ago,),
        ).fetchone()[0]
        invocations_1h = self._conn.execute(
            "SELECT COUNT(*) FROM invocation_stats WHERE timestamp >= ?",
            (hour_ago,),
        ).fetchone()[0]
        invocations_24h = self._conn.execute(
            "SELECT COUNT(*) FROM invocation_stats WHERE timestamp >= ?",
            (day_ago,),
        ).fetchone()[0]
        failed_24h = self._conn.execute(
            "SELECT COUNT(*) FROM invocation_stats WHERE timestamp >= ? AND success=0",
            (day_ago,),
        ).fetchone()[0]
        p50_24h = self._latency_percentile(day_ago, 0.5)
        p95_24h = self._latency_percentile(day_ago, 0.95)
        rps_1h = round(invocations_1h / 3600.0, 4) if invocations_1h else 0.0
        demo_caps = self._conn.execute(
            "SELECT COUNT(*) FROM capabilities WHERE source_hub='local' AND is_demo=1"
        ).fetchone()[0]
        real_local_caps = self._conn.execute(
            "SELECT COUNT(*) FROM capabilities WHERE source_hub='local' AND COALESCE(is_demo, 0)=0"
        ).fetchone()[0]
        return {
            "total_invocations": total,
            "successful_invocations": success,
            "success_rate": success / total if total > 0 else 0,
            "avg_price_usd": round(avg_price, 4),
            "avg_latency_ms": round(avg_latency, 1),
            "revenue_per_hour_usd": round(revenue_hour, 4),
            "invocations_1h": int(invocations_1h),
            "invocations_24h": int(invocations_24h),
            "failed_invocations_24h": int(failed_24h),
            "rps_1h": rps_1h,
            "p50_latency_ms_24h": p50_24h,
            "p95_latency_ms_24h": p95_24h,
            "peers_count": self.peer_count(),
            "capabilities_count": self.count_capabilities(),
            # What the storefront may honestly show. `capabilities_count` counts the table,
            # including local rows with no execution path — the number that let the landing
            # page advertise "12 capabilities" while /manifest and /search served none of
            # them, because those read the filtered set. See count_offerable().
            "offerable_capabilities_count": self.count_offerable(),
            "federated_capabilities_count": self.count_federated(),
            "demo_capabilities_count": int(demo_caps),
            "real_local_capabilities_count": int(real_local_caps),
        }

    def _latency_percentile(self, since_iso: str, pct: float) -> float | None:
        """Nearest-rank p50/p95 latency (ms) for invocations since ``since_iso``."""
        count = self._conn.execute(
            "SELECT COUNT(*) FROM invocation_stats WHERE timestamp >= ?",
            (since_iso,),
        ).fetchone()[0]
        if not count:
            return None
        offset = max(0, int(math.ceil(count * pct)) - 1)
        row = self._conn.execute(
            "SELECT latency_ms FROM invocation_stats WHERE timestamp >= ? "
            "ORDER BY latency_ms ASC LIMIT 1 OFFSET ?",
            (since_iso, offset),
        ).fetchone()
        return round(float(row[0]), 1) if row else None

    # ── Reputation ────────────────────────────────────────────

    def record_reputation_event(self, event: ReputationEvent) -> None:
        self._conn.execute("""
            INSERT INTO reputation_events
                (event_type, provider_hub, capability_id, timestamp, price_usd, latency_ms, consumer_hub, signature)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_type, event.provider_hub, event.capability_id,
            event.timestamp, event.price_usd, event.latency_ms,
            event.consumer_hub, event.signature,
        ))
        self._conn.commit()

    def reputation_events_for(self, provider_hub: str, limit: int = 100) -> list[ReputationEvent]:
        rows = self._conn.execute(
            "SELECT * FROM reputation_events WHERE provider_hub=? ORDER BY timestamp DESC LIMIT ?",
            (provider_hub, limit),
        ).fetchall()
        return [_row_to_reputation(r) for r in rows]

    # ── Maintenance ───────────────────────────────────────────

    def close(self) -> None:
        self._backend.close()

    # ── Supply security ───────────────────────────────────────

    def supply_stake_get(self, publisher_id: str) -> float:
        row = self._conn.execute(
            "SELECT amount_usd, slashed_usd FROM supply_stakes WHERE publisher_id=?",
            (publisher_id,),
        ).fetchone()
        if not row:
            return 0.0
        d = dict(row)
        return max(0.0, float(d.get("amount_usd", 0)) - float(d.get("slashed_usd", 0)))

    def supply_stake_tx_exists(self, tx_hash: str) -> bool:
        """True if a non-empty stake tx_hash was already recorded (replay guard).

        Prevents a single on-chain deposit from being claimed as stake by (or for)
        multiple publishers to spoof trust scores / clear the stake gate.
        """
        tx = (tx_hash or "").strip()
        if not tx:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM supply_stakes WHERE tx_hash = ? LIMIT 1", (tx,)
        ).fetchone()
        return row is not None

    def supply_stake_add(self, publisher_id: str, amount_usd: float, tx_hash: str = "") -> float:
        row = self._conn.execute(
            "SELECT amount_usd FROM supply_stakes WHERE publisher_id=?",
            (publisher_id,),
        ).fetchone()
        if row:
            new_amount = float(dict(row)["amount_usd"]) + amount_usd
            self._conn.execute(
                "UPDATE supply_stakes SET amount_usd=?, tx_hash=?, updated_at=datetime('now') WHERE publisher_id=?",
                (new_amount, tx_hash, publisher_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO supply_stakes (publisher_id, amount_usd, slashed_usd, tx_hash) VALUES (?, ?, 0, ?)",
                (publisher_id, amount_usd, tx_hash),
            )
        self._conn.commit()
        return self.supply_stake_get(publisher_id)

    def supply_stake_slash(self, publisher_id: str, amount_usd: float) -> tuple[float, float]:
        remaining_before = self.supply_stake_get(publisher_id)
        slashed = min(remaining_before, amount_usd)
        self._conn.execute("""
            UPDATE supply_stakes SET slashed_usd = slashed_usd + ?, updated_at = datetime('now')
            WHERE publisher_id=?
        """, (slashed, publisher_id))
        self._conn.commit()
        return slashed, self.supply_stake_get(publisher_id)

    # ── Fault / slash event log (calibrated slashing) ─────────
    # Durable counters: a restart must neither amnesty a failure streak
    # (supply_fault_events) nor forget how much was already slashed today
    # (supply_slash_events → cool-down + rolling daily cap).

    def supply_fault_log(self, publisher_id: str, kind: str, product_id: str = "", capability_id: str = "", consumer_id: str = "") -> None:
        self._conn.execute(
            "INSERT INTO supply_fault_events (publisher_id, kind, product_id, capability_id, consumer_id) VALUES (?, ?, ?, ?, ?)",
            (publisher_id, kind, product_id, capability_id, consumer_id),
        )
        self._conn.commit()

    def supply_fault_count_recent(self, publisher_id: str, kind: str, window_s: float) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM supply_fault_events "
            "WHERE publisher_id=? AND kind=? AND created_at >= ?",
            (publisher_id, kind, _utc_cutoff(window_s)),
        ).fetchone()
        return int(row[0]) if row else 0

    def supply_fault_distinct_consumers_recent(self, publisher_id: str, kind: str, window_s: float) -> int:
        """Distinct non-empty consumer_ids that logged this fault kind in the window.

        Griefing defense for the verified-failure ladder: one buyer's repeated failures
        are one voice, so a slash needs several DISTINCT consumers to agree."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT consumer_id) FROM supply_fault_events "
            "WHERE publisher_id=? AND kind=? AND consumer_id != '' "
            "AND created_at >= ?",
            (publisher_id, kind, _utc_cutoff(window_s)),
        ).fetchone()
        return int(row[0]) if row else 0

    def supply_fault_clear(self, publisher_id: str, kind: str) -> None:
        self._conn.execute(
            "DELETE FROM supply_fault_events WHERE publisher_id=? AND kind=?",
            (publisher_id, kind),
        )
        self._conn.commit()

    def supply_slash_log(self, publisher_id: str, amount_usd: float, reason: str, evidence_kind: str = "") -> None:
        self._conn.execute(
            "INSERT INTO supply_slash_events (publisher_id, amount_usd, reason, evidence_kind) VALUES (?, ?, ?, ?)",
            (publisher_id, amount_usd, reason, evidence_kind),
        )
        self._conn.commit()

    def supply_slash_total_recent(self, publisher_id: str, hours: float = 24.0) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM supply_slash_events "
            "WHERE publisher_id=? AND created_at >= ?",
            (publisher_id, _utc_cutoff(hours * 3600)),
        ).fetchone()
        return float(row[0]) if row else 0.0

    def supply_slash_last_age_s(self, publisher_id: str) -> float | None:
        """Seconds since this publisher's most recent slash event, or None if never slashed.
        Age is computed in Python (MAX(created_at) → parse → subtract) rather than with
        ``julianday()``, which PostgreSQL does not have."""
        row = self._conn.execute(
            "SELECT MAX(created_at) FROM supply_slash_events WHERE publisher_id=?",
            (publisher_id,),
        ).fetchone()
        if not row or row[0] is None:
            return None
        ts = _parse_db_ts(row[0])
        if ts is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())

    def supply_slash_events_recent(self, publisher_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT amount_usd, reason, evidence_kind, created_at FROM supply_slash_events "
            "WHERE publisher_id=? ORDER BY id DESC LIMIT ?",
            (publisher_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Federated slash attestation persistence ───────────────

    def slash_attestation_save(self, issuer_hub: str, seq: int, envelope_json: str, tier: str) -> None:
        # INSERT OR IGNORE (not REPLACE): the attestation log is append-only, so a
        # same-(issuer, seq) write must never silently overwrite an already-recorded
        # envelope. Callers guard against re-persisting an existing key (ingest skips
        # known keys; authored slashes reserve a fresh seq), so IGNORE is a backstop
        # that preserves the first-written envelope rather than forging equivocation.
        self._conn.execute(
            "INSERT OR IGNORE INTO slash_attestations (issuer_hub, seq, envelope_json, tier) VALUES (?, ?, ?, ?)",
            (issuer_hub, seq, envelope_json, tier),
        )
        self._conn.commit()

    def slash_attestation_append(self, issuer_hub: str, env_factory: Any, tier: str, *, max_retries: int = 8) -> tuple[int, dict[str, Any]]:
        """Atomically allocate the next per-issuer ``seq`` and durably store an AUTHORED
        attestation, retrying on a ``(issuer_hub, seq)`` PK collision.

        This is the multi-worker/connection-safe replacement for author-time in-memory seq:
        two processes sharing the DB can compute the same ``MAX(seq)+1``, but only one INSERT
        wins the primary key — the loser rolls back (to drop its stale snapshot), re-reads the
        now-higher MAX, and retries. ``env_factory(seq)`` builds+signs the envelope for the
        reserved seq (its signature covers the seq, so it must be built per-attempt). Raises
        if no seq can be allocated, so the caller never serves a seq that isn't persisted."""
        for _ in range(max(1, max_retries)):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM slash_attestations WHERE issuer_hub=?",
                (issuer_hub,),
            ).fetchone()
            seq = int(row[0])
            env = env_factory(seq)
            try:
                self._conn.execute(
                    "INSERT INTO slash_attestations (issuer_hub, seq, envelope_json, tier) VALUES (?, ?, ?, ?)",
                    (issuer_hub, seq, json.dumps(env, ensure_ascii=False), tier),
                )
                self._conn.commit()
                return seq, env
            except Exception as exc:
                # End the failed transaction so the retry's SELECT sees a fresh snapshot
                # (including the concurrent writer's committed row), then decide.
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                if type(exc).__name__ in ("IntegrityError", "UniqueViolation"):
                    continue  # a concurrent writer took this seq — retry with fresh MAX
                raise
        raise RuntimeError(f"could not allocate a unique slash attestation seq for {issuer_hub}")

    def slash_attestation_load_all(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT issuer_hub, seq, envelope_json, tier FROM slash_attestations"
        ).fetchall()
        return [dict(r) for r in rows]

    def self_bond_register(self, agent_id: str, evm_address: str, ceiling_usd: float, bond_usd: float, token: str = "USDC", commitment: str = "") -> dict[str, Any]:
        row = self._conn.execute("SELECT agent_id FROM self_bonds WHERE agent_id=?", (agent_id,)).fetchone()
        if row:
            self._conn.execute(
                "UPDATE self_bonds SET evm_address=?, ceiling_usd=?, bond_usd=?, token=?, commitment=?, updated_at=datetime('now') WHERE agent_id=?",
                (evm_address, ceiling_usd, bond_usd, token, commitment, agent_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO self_bonds (agent_id, evm_address, ceiling_usd, bond_usd, token, commitment) VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, evm_address, ceiling_usd, bond_usd, token, commitment),
            )
        self._conn.commit()
        return self.self_bond_get(agent_id) or {}

    def self_bond_get(self, agent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM self_bonds WHERE agent_id=?", (agent_id,)).fetchone()
        return dict(row) if row else None

    def self_bond_record_slash(self, agent_id: str, amount_usd: float) -> None:
        self._conn.execute(
            "UPDATE self_bonds SET slashed_usd = slashed_usd + ?, updated_at = datetime('now') WHERE agent_id=?",
            (amount_usd, agent_id),
        )
        self._conn.commit()

    def supply_publish_log(self, publisher_id: str, product_id: str, invoke_url: str) -> None:
        self._conn.execute(
            "INSERT INTO supply_publish_events (publisher_id, product_id, invoke_url) VALUES (?, ?, ?)",
            (publisher_id, product_id, invoke_url),
        )
        self._conn.commit()

    def supply_publish_count_recent(self, publisher_id: str, hours: int = 1) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM supply_publish_events "
            "WHERE publisher_id=? AND created_at >= ?",
            (publisher_id, _utc_cutoff(hours * 3600)),
        ).fetchone()
        return int(row[0]) if row else 0

    def supply_capability_by_invoke_url(self, invoke_url: str) -> Capability | None:
        row = self._conn.execute(
            "SELECT * FROM capabilities WHERE invoke_url=? AND source_hub='local' LIMIT 1",
            (invoke_url,),
        ).fetchone()
        return _row_to_capability(row) if row else None

    def trust_add_edge(self, src: str, dst: str, weight: float, event_type: str) -> None:
        self._conn.execute(
            "INSERT INTO trust_graph_edges (src, dst, weight, event_type) VALUES (?, ?, ?, ?)",
            (src, dst, weight, event_type),
        )
        self._conn.commit()

    def trust_list_edges(self, limit: int = 500) -> list[tuple[str, str, float, str]]:
        rows = self._conn.execute(
            "SELECT src, dst, weight, event_type FROM trust_graph_edges ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["src"], r["dst"], float(r["weight"]), r["event_type"]) for r in rows]

    def supply_set_publisher_trust(self, publisher_id: str, trust_score: float) -> None:
        self._conn.execute(
            "UPDATE capabilities SET trust_score=?, stake_usd=? WHERE publisher_id=? AND source_hub='local'",
            (trust_score, self.supply_stake_get(publisher_id), publisher_id),
        )
        self._conn.commit()


def _row_to_capability(row: sqlite3.Row) -> Capability:
    d = dict(row)
    return Capability(
        capability_id=d.get("capability_id", ""),
        product_id=d.get("product_id", ""),
        name=d.get("name", ""),
        version=d.get("version", "v1"),
        description=d.get("description", ""),
        input_schema=_json_parse(d.get("input_schema", "{}")),
        output_schema=_json_parse(d.get("output_schema", "{}")),
        price_per_call_usd=d.get("price_per_call_usd", 0.35),
        p50_latency_ms=d.get("p50_latency_ms", 3000),
        success_rate_30d=d.get("success_rate_30d", 0.97),
        source_hub=d.get("source_hub", "local"),
        source_hub_name=d.get("source_hub_name", ""),
        routed_price_usd=d.get("routed_price_usd"),
        routing_fee_bps=d.get("routing_fee_bps", 0),
        trust_score=d.get("trust_score", 0.0),
        agent=d.get("agent", ""),
        prompt_template=d.get("prompt_template", ""),
        invoke_url=d.get("invoke_url", "") or "",
        publisher_id=d.get("publisher_id", "") or "",
        provider_pubkey=d.get("provider_pubkey", "") or "",
        stake_usd=float(d.get("stake_usd") or 0.0),
        is_demo=bool(d.get("is_demo", 0)),
    )


def _row_to_peer(row: sqlite3.Row) -> Peer:
    d = dict(row)
    return Peer(
        url=d.get("url", ""),
        name=d.get("name", ""),
        capabilities_count=d.get("capabilities_count", 0),
        last_crawl=d.get("last_crawl", ""),
        trust_score=d.get("trust_score", 0.0),
        well_known_url=d.get("well_known_url", ""),
        manifest_url=d.get("manifest_url", ""),
        public_key=d.get("public_key", ""),
        depth=d.get("depth", 0),
        discoverer=d.get("discoverer", ""),
        categories=_json_list(d.get("categories", "[]")),
        trusted=bool(d.get("trusted", 0)),
    )


def _row_to_reputation(row: sqlite3.Row) -> ReputationEvent:
    d = dict(row)
    return ReputationEvent(
        event_type=d.get("event_type", ""),
        provider_hub=d.get("provider_hub", ""),
        capability_id=d.get("capability_id"),
        timestamp=d.get("timestamp", ""),
        price_usd=d.get("price_usd", 0),
        latency_ms=d.get("latency_ms", 0),
        consumer_hub=d.get("consumer_hub", ""),
        signature=d.get("signature", ""),
    )


def _json_parse(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _json_list(val: Any) -> list[str]:
    """Parse a stored JSON list of category strings; tolerate legacy/missing values."""
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            return []
    return []
