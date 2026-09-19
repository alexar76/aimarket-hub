#!/usr/bin/env python3
"""One-time migration: un-alias a hub whose subsystem databases were all ONE file.

Why this exists
---------------
``create_backend`` used to let the ambient ``AIMARKET_DB_PATH`` env var override an
explicit ``db_path`` argument. Every deploy that sets that var (every Docker image here
does) therefore handed the hub index, the payment-channel ledger and the provenance
plugin the SAME SQLite file while each subsystem kept reporting its own path. That was
fixed — an explicit argument now wins — which means the first start after the fix opens
``data/channels.db`` for the ledger for the very first time: a brand-new, EMPTY file.

An empty ledger is not a cosmetic problem. It loses the open channels (buyers' deposits
become invisible) and it resets ``consumed_deposits``, the replay guard that makes an
on-chain deposit single-use — an already-consumed deposit could be presented again.

This script copies the subsystem's tables out of the aliased file into the file the
subsystem now opens, verifies the row counts, and LEAVES THE ORIGINAL COMPLETELY INTACT.
Nothing is ever deleted or dropped; the aliased file keeps its copy of every row, so a
bad outcome is recoverable by pointing the env var back and re-running.

Usage
-----
    # what would happen (touches nothing)
    python scripts/split_aliased_sqlite_db.py --dry-run

    # do it, for the payment-channel ledger
    python scripts/split_aliased_sqlite_db.py --subsystem channels

    # explicit paths instead of the env vars
    python scripts/split_aliased_sqlite_db.py \
        --source data/hub.db --target data/channels.db --subsystem channels

Exit codes: 0 = done or nothing to do, 2 = refused (operator must act), 3 = verification
failed (target rolled back), 1 = usage/IO error.

The runbook lives next to this file: scripts/split_aliased_sqlite_db.md
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aimarket_hub.migrations import (  # noqa: E402
    CHANNEL_LEDGER_TABLES,
    channel_ledger_versions,
)

# Subsystem → (tables it owns, migration versions that built them, default target env var,
# default target path). Only these subsystems can be split; a table name that is not in one
# of these tuples never reaches SQL, which is also what keeps identifier interpolation safe.
SUBSYSTEMS: dict[str, dict[str, object]] = {
    "channels": {
        "tables": CHANNEL_LEDGER_TABLES,
        "versions": channel_ledger_versions,
        "env": "AIMARKET_CHANNELS_DB_PATH",
        "default": "data/channels.db",
    },
}

# Tables owned by the HUB index. Their presence alongside a subsystem's tables in one file
# is the signature of the aliased state — a standalone ledger file has the ledger tables
# and no capabilities/peers.
HUB_MARKER_TABLES = ("capabilities", "peers")

AUDIT_TABLE = "_db_split_audit"
AUDIT_DDL = (
    f"CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  performed_at TEXT NOT NULL,"
    "  subsystem TEXT NOT NULL,"
    "  source_path TEXT NOT NULL,"
    "  row_counts TEXT NOT NULL"
    ")"
)


class Refused(RuntimeError):
    """The split cannot proceed safely and an operator has to decide. Exit code 2."""


class VerifyFailed(RuntimeError):
    """Copied row counts did not match. The target transaction is rolled back. Exit 3."""


def _connect(path: Path, autocommit: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if autocommit:
        # We drive BEGIN/COMMIT/ROLLBACK by hand below: ATTACH is illegal inside a
        # transaction, and sqlite3's implicit-transaction mode would have opened one for us.
        conn.isolation_level = None
    return conn


def _tables(conn: sqlite3.Connection, schema: str = "main") -> set[str]:
    rows = conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table'"  # noqa: S608
    ).fetchall()
    return {r["name"] for r in rows}


def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {schema}.{table}").fetchone()  # noqa: S608
    return int(row["n"])


def _ddl_for(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """CREATE statements for a table and its indexes/triggers, in creation order."""
    rows = conn.execute(
        f"SELECT sql FROM {schema}.sqlite_master WHERE tbl_name = ? AND sql IS NOT NULL "  # noqa: S608
        "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name",
        (table,),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        sql = str(r["sql"]).strip()
        # Make replay safe: the target may already have been created by a fresh
        # Migrations run.
        for prefix, patched in (
            ("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "),
            ("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS "),
            ("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS "),
            ("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS "),
        ):
            if sql.upper().startswith(prefix.upper()) and "IF NOT EXISTS" not in sql.upper():
                sql = patched + sql[len(prefix):]
                break
        out.append(sql)
    return out


def _audit_rows(conn: sqlite3.Connection, subsystem: str, source: Path) -> list[dict]:
    if AUDIT_TABLE not in _tables(conn):
        return []
    rows = conn.execute(
        f"SELECT * FROM {AUDIT_TABLE} WHERE subsystem = ? AND source_path = ? "  # noqa: S608
        "ORDER BY id",
        (subsystem, str(source)),
    ).fetchall()
    return [dict(r) for r in rows]


def split_database(
    source: Path,
    target: Path,
    subsystem: str,
    dry_run: bool = False,
) -> dict:
    """Copy ``subsystem``'s tables from the aliased ``source`` into ``target``.

    Idempotent: a second run finds the rows already present and reports ``already_split``
    without writing. Non-destructive in both directions — the source is only ever read,
    and a target that already holds DIFFERENT data is refused rather than merged or
    overwritten.

    Returns a result dict with ``status`` in {``not_aliased``, ``already_split``,
    ``would_copy`` (dry run), ``copied``} plus the before/after row counts.
    """
    spec = SUBSYSTEMS.get(subsystem)
    if spec is None:
        raise Refused(
            f"unknown subsystem {subsystem!r}; known: {', '.join(sorted(SUBSYSTEMS))}"
        )
    tables: tuple[str, ...] = tuple(spec["tables"])  # type: ignore[arg-type]

    if not source.exists():
        raise Refused(f"source database {source} does not exist — nothing to migrate")
    if source.resolve() == target.resolve():
        raise Refused(
            f"source and target are the same file ({source}); the aliasing this fixes is "
            "exactly that state — pass the subsystem's own path as --target"
        )

    src = _connect(source)
    try:
        src_tables = _tables(src)
        present = tuple(t for t in tables if t in src_tables)
        if not present:
            return {
                "status": "not_aliased",
                "reason": (
                    f"{source} holds none of the {subsystem} tables "
                    f"({', '.join(tables)}), so nothing was ever aliased into it"
                ),
                "source": str(source), "target": str(target), "counts": {},
            }
        hub_present = [t for t in HUB_MARKER_TABLES if t in src_tables]
        source_counts = {t: _count(src, "main", t) for t in present}
        src_cols = {t: _columns(src, "main", t) for t in present}
        ddl = {t: _ddl_for(src, "main", t) for t in present}
        versions = sorted(spec["versions"]())  # type: ignore[operator]
        applied = []
        if "_migrations" in src_tables:
            marks = src.execute(
                "SELECT version, name, applied_at FROM _migrations ORDER BY version"
            ).fetchall()
            applied = [dict(m) for m in marks if int(m["version"]) in versions]
    finally:
        src.close()

    result: dict = {
        "status": "",
        "source": str(source),
        "target": str(target),
        "aliased_with_hub_tables": hub_present,
        "counts": source_counts,
    }

    # Inspect an EXISTING target read-only first. Opening a path with sqlite3 creates the
    # file, and a dry run (or a refusal) must not leave a stray empty database behind.
    if target.exists():
        probe = _connect(target)
        try:
            tgt_tables = _tables(probe)
            existing = {t: _count(probe, "main", t) for t in present if t in tgt_tables}
            prior = _audit_rows(probe, subsystem, source)

            if existing and any(n > 0 for n in existing.values()):
                if existing == source_counts:
                    result["status"] = "already_split"
                    result["target_counts"] = existing
                    result["prior_runs"] = len(prior)
                    return result
                raise Refused(
                    f"{target} already contains {subsystem} rows that do NOT match the "
                    f"source (target={existing}, source={source_counts}). Refusing to merge "
                    "or overwrite live data. Move the target aside (keep it — do not delete "
                    "it) and re-run, or reconcile by hand."
                )

            # Column drift: the target may have been created by a NEWER schema. Extra target
            # columns are fine (defaults apply); a source column with nowhere to go is not.
            for t in present:
                if t in tgt_tables:
                    missing = [
                        c for c in src_cols[t] if c not in _columns(probe, "main", t)
                    ]
                    if missing:
                        raise Refused(
                            f"table {t} in {target} lacks column(s) {missing} present in "
                            f"{source}; schema drift must be resolved before copying"
                        )
        finally:
            probe.close()

    if dry_run:
        result["status"] = "would_copy"
        result["migration_marks"] = [m["version"] for m in applied]
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    tgt = _connect(target, autocommit=True)
    try:
        # ATTACH first — SQLite refuses it inside a transaction.
        tgt.execute("ATTACH DATABASE ? AS src", (str(source),))
        try:
            tgt.execute("BEGIN IMMEDIATE")
            for t in present:
                for stmt in ddl[t]:
                    tgt.execute(stmt)
                cols = ", ".join(src_cols[t])
                tgt.execute(
                    f"INSERT INTO main.{t} ({cols}) SELECT {cols} FROM src.{t}"  # noqa: S608
                )

            # The copied tables are already at the schema these migrations produce. Without
            # these marks the ledger's own Migrations run would replay them against migrated
            # tables — ALTER TABLE ADD COLUMN on an existing column raises, and apply()
            # re-raises, so the hub would refuse to start.
            tgt.execute(
                "CREATE TABLE IF NOT EXISTS _migrations ("
                "  version INTEGER PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  applied_at TEXT DEFAULT (datetime('now'))"
                ")"
            )
            for m in applied:
                tgt.execute(
                    "INSERT OR IGNORE INTO _migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (int(m["version"]), str(m["name"]), str(m["applied_at"] or "")),
                )

            copied = {t: _count(tgt, "main", t) for t in present}
            if copied != source_counts:
                raise VerifyFailed(
                    f"row counts after copy {copied} != source {source_counts}; "
                    "rolling the target back, source untouched"
                )

            tgt.execute(AUDIT_DDL)
            tgt.execute(
                f"INSERT INTO {AUDIT_TABLE} (performed_at, subsystem, source_path, "  # noqa: S608
                "row_counts) VALUES (?, ?, ?, ?)",
                (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    subsystem,
                    str(source),
                    json.dumps(source_counts, sort_keys=True),
                ),
            )
            tgt.execute("COMMIT")
        except BaseException:
            tgt.execute("ROLLBACK")
            raise
        finally:
            tgt.execute("DETACH DATABASE src")
    finally:
        tgt.close()

    # Re-open and re-count: proof read back from the committed file, not from the
    # connection that wrote it.
    check = _connect(target)
    try:
        after = {t: _count(check, "main", t) for t in present}
    finally:
        check.close()
    if after != source_counts:
        raise VerifyFailed(
            f"post-commit verification failed: {target} holds {after}, source holds "
            f"{source_counts}. The source is intact — investigate before serving."
        )

    src2 = _connect(source)
    try:
        source_after = {t: _count(src2, "main", t) for t in present}
    finally:
        src2.close()
    if source_after != source_counts:
        raise VerifyFailed(
            f"source row counts changed during the copy ({source_after} != "
            f"{source_counts}) — the hub is probably still running. Stop it and re-check."
        )

    result["status"] = "copied"
    result["target_counts"] = after
    result["source_counts_after"] = source_after
    result["migration_marks"] = [m["version"] for m in applied]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip())
    ap.add_argument("--subsystem", default="channels", choices=sorted(SUBSYSTEMS))
    ap.add_argument(
        "--source", default="",
        help="the aliased database (default: $AIMARKET_DB_PATH, else data/hub.db)",
    )
    ap.add_argument(
        "--target", default="",
        help="the file the subsystem now opens (default: its own env var / default path)",
    )
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)

    spec = SUBSYSTEMS[args.subsystem]
    source = Path(
        args.source or os.environ.get("AIMARKET_DB_PATH", "").strip() or "data/hub.db"
    )
    target = Path(
        args.target
        or os.environ.get(str(spec["env"]), "").strip()
        or str(spec["default"])
    )

    try:
        result = split_database(source, target, args.subsystem, dry_run=args.dry_run)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except VerifyFailed as exc:
        print(f"VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 3
    except (sqlite3.Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "copied":
        print(
            f"\nOK: {args.subsystem} tables copied {source} -> {target}. "
            f"{source} was NOT modified — keep it until the hub has served from the new "
            "file successfully.",
        )
    elif result["status"] == "already_split":
        print(f"\nOK: {target} already matches {source} for {args.subsystem}; no write made.")
    elif result["status"] == "not_aliased":
        print(f"\nOK: nothing to do — {result['reason']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
