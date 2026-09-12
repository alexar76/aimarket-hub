#!/usr/bin/env python3
"""Copy one or more AIMarket Hub SQLite databases into initialized PostgreSQL.

The source files are opened read-only and are never changed. The PostgreSQL schema must
already have been initialized by ``python -m aimarket_hub.migrations up`` plus any
subsystem initialization. Inserts are idempotent so a failed run can be resumed.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import psycopg
from psycopg import sql


def _source_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _source_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    escaped = table.replace('"', '""')
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")')]


def migrate(sources: list[Path], dsn: str) -> dict[str, dict[str, int]]:
    report: dict[str, dict[str, int]] = {}
    with psycopg.connect(dsn) as dst:
        for source in sources:
            if not source.is_file():
                raise FileNotFoundError(source)
            uri = f"file:{source}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=30) as src:
                src.row_factory = sqlite3.Row
                source_report: dict[str, int] = {}
                for table in _source_tables(src):
                    columns = _source_columns(src, table)
                    with dst.cursor() as cur:
                        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                        if cur.fetchone()[0] is None:
                            raise RuntimeError(
                                f"target table {table!r} is absent; initialize schema first"
                            )
                        cur.execute(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name=%s",
                            (table,),
                        )
                        target_columns = {str(row[0]) for row in cur.fetchall()}
                    missing = set(columns) - target_columns
                    if missing:
                        raise RuntimeError(
                            f"target table {table!r} lacks source columns: {sorted(missing)}"
                        )

                    escaped = table.replace('"', '""')
                    rows = [tuple(row[col] for col in columns) for row in src.execute(
                        f'SELECT * FROM "{escaped}"'
                    )]
                    statement = sql.SQL(
                        "INSERT INTO {table} ({columns}) VALUES ({values}) "
                        "ON CONFLICT DO NOTHING"
                    ).format(
                        table=sql.Identifier(table),
                        columns=sql.SQL(', ').join(map(sql.Identifier, columns)),
                        values=sql.SQL(', ').join(sql.Placeholder() for _ in columns),
                    )
                    with dst.cursor() as cur:
                        if rows:
                            cur.executemany(statement, rows)
                        cur.execute(
                            sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
                        )
                        target_count = int(cur.fetchone()[0])
                    if target_count < len(rows):
                        raise RuntimeError(
                            f"row-count validation failed for {table}: "
                            f"source={len(rows)} target={target_count}"
                        )
                    source_report[table] = len(rows)
                report[source.name] = source_report
        # Explicit ids copied from SQLite do not advance PostgreSQL's BIGSERIAL
        # sequences. Without this, the first production INSERT reuses id=1 and fails
        # even though every migrated row is present.
        with dst.cursor() as cur:
            cur.execute(
                "SELECT table_schema, table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND column_default LIKE 'nextval(%'"
            )
            serial_columns = list(cur.fetchall())
            for schema, table, column in serial_columns:
                cur.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)",
                    (f'{schema}.{table}', column),
                )
                sequence = cur.fetchone()[0]
                if not sequence:
                    continue
                cur.execute(
                    sql.SQL(
                        "SELECT COALESCE(MAX({column}), 1), COUNT(*) > 0 FROM {schema}.{table}"
                    ).format(
                        column=sql.Identifier(column),
                        schema=sql.Identifier(schema),
                        table=sql.Identifier(table),
                    )
                )
                maximum, has_rows = cur.fetchone()
                cur.execute("SELECT setval(%s, %s, %s)", (sequence, maximum, has_rows))
        dst.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite', action='append', required=True, type=Path)
    parser.add_argument('--dsn', required=True)
    args = parser.parse_args()
    report = migrate(args.sqlite, args.dsn)
    for source, tables in report.items():
        print(f"{source}: {sum(tables.values())} rows across {len(tables)} tables")


if __name__ == '__main__':
    main()
