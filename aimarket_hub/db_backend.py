"""Database backend abstraction — SQLite and PostgreSQL with dialect translation.

Provides a unified `DBBackend` protocol with two implementations:
  - `SQLiteBackend` — zero-dependency, single-file database (default)
  - `PostgresBackend` — psycopg_pool-backed, connection pooling, JSONB

When DATABASE_URL is not set, SQLite is used (backward-compatible default).
When DATABASE_URL is set (e.g., postgresql://user:pass@host:5432/db), PostgreSQL is used.

Factory:
    create_backend(database_url="") -> DBBackend

Path resolution (SQLite): an explicit ``db_path`` argument wins over the ambient
``AIMARKET_DB_PATH`` env var — see create_backend for why the reverse was a bug.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# The hub index database. Also the "caller expressed no preference" marker in
# create_backend, which is the only place AIMARKET_DB_PATH may fill in.
DEFAULT_DB_PATH = "data/hub.db"

# ── Protocol ────────────────────────────────────────────────────────


@runtime_checkable
class DBBackend(Protocol):
    """Protocol for database backends.

    Implementations: SQLiteBackend, PostgresBackend.
    """

    @property
    def backend_type(self) -> str: ...

    def execute(self, sql: str, params: tuple = ()) -> Any: ...

    def executemany(self, sql: str, params_list: list[tuple]) -> Any: ...

    def executescript(self, sql: str) -> Any: ...

    def fetchone(self) -> dict | None: ...

    def fetchall(self) -> list[dict]: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...

    def get_connection(self) -> Any: ...

    @property
    def rowcount(self) -> int: ...


# ── SQL Dialect Translation ─────────────────────────────────────────

_RE_INSERT_OR_REPLACE = re.compile(
    r"INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
_RE_DATETIME_NOW = re.compile(r"datetime\('now'\)", re.IGNORECASE)
_RE_PRAGMA = re.compile(r"PRAGMA\s+\w+.*?;", re.IGNORECASE)
_RE_AUTOINCREMENT = re.compile(r"AUTOINCREMENT", re.IGNORECASE)
_RE_INTEGER_PK = re.compile(r"INTEGER\s+PRIMARY\s+KEY", re.IGNORECASE)


def sqlite_to_pg(sql: str, table_name_for_upsert: str = "", unique_cols: list[str] | None = None) -> str:
    """Translate SQLite SQL to PostgreSQL SQL.

    Handles:
      - INSERT OR REPLACE → INSERT ... ON CONFLICT ... DO UPDATE
      - datetime('now') → NOW()
      - PRAGMA statements → no-op (PostgreSQL has its own WAL/FK)
      - AUTOINCREMENT → removed (SERIAL handles it)
      - INTEGER PRIMARY KEY → SERIAL PRIMARY KEY
      - ? placeholders → %s
    """
    result = sql

    # Remove PRAGMA lines (WAL, foreign_keys — PG handles these natively)
    result = _RE_PRAGMA.sub("-- pragma removed for PostgreSQL\n", result)

    # datetime('now') → NOW()
    result = _RE_DATETIME_NOW.sub("NOW()", result)

    # INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE
    # This is a best-effort translation — the caller should provide
    # table_name_for_upsert + unique_cols for precision.
    def _translate_upsert(m: re.Match) -> str:
        table = m.group(1)
        cols = m.group(2)
        vals = m.group(3)
        actual_table = table_name_for_upsert or table
        conflict_cols = unique_cols or ["id"]
        conflict_clause = ", ".join(conflict_cols)
        set_clause = ", ".join(
            f"{c.strip()} = EXCLUDED.{c.strip()}"
            for c in cols.split(",")
        )
        return (
            f"INSERT INTO {actual_table} ({cols}) VALUES ({vals})\n"
            f"ON CONFLICT ({conflict_clause}) DO UPDATE SET {set_clause}"
        )

    result = _RE_INSERT_OR_REPLACE.sub(_translate_upsert, result)

    # INTEGER PRIMARY KEY → SERIAL PRIMARY KEY (for table creation)
    result = _RE_INTEGER_PK.sub("SERIAL PRIMARY KEY", result)
    result = _RE_AUTOINCREMENT.sub("", result)

    # SQLite REAL → PostgreSQL DOUBLE PRECISION
    result = re.sub(r"\bREAL\b", "DOUBLE PRECISION", result, flags=re.IGNORECASE)

    # TEXT without size → TEXT (no change needed, PG supports TEXT)
    # But SQLite TEXT defaults — keep as-is

    # Replace SQLite ? placeholders with PostgreSQL %s
    # Must be careful not to replace ? inside string literals.
    # Simple approach: replace ? not inside quotes
    result = _replace_placeholders(result)

    return result


def _replace_placeholders(sql: str) -> str:
    """Replace ? placeholders with %s, skipping those inside string literals."""
    in_string = False
    string_char = ""
    out: list[str] = []
    i = 0
    while i < len(sql):
        c = sql[i]
        if in_string:
            out.append(c)
            if c == string_char and (i == 0 or sql[i - 1] != "\\"):
                in_string = False
        else:
            if c in ("'", '"'):
                in_string = True
                string_char = c
                out.append(c)
            elif c == "?":
                # Look ahead for non-placeholder ? (like in "??")
                out.append("%s")
            else:
                out.append(c)
        i += 1
    return "".join(out)


# ── SQLite Backend ──────────────────────────────────────────────────


class SQLiteBackend:
    """SQLite backend — zero-dependency, single-file database.

    Follows the existing patterns: WAL mode, foreign_keys ON,
    sqlite3.Row row factory.
    """

    def __init__(self, db_path: str | Path = "data/hub.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._last_cursor: Any = None
        logger.info("SQLite backend initialized: %s", self.db_path)

    @property
    def backend_type(self) -> str:
        return "sqlite"

    def cursor(self) -> Any:
        """Return a raw sqlite3.Cursor for backward compat with legacy code."""
        return self._conn.cursor()

    def execute(self, sql: str, params: tuple = ()) -> Any:
        self._last_cursor = self._conn.execute(sql, params)
        return self._last_cursor

    def executemany(self, sql: str, seq: list) -> Any:
        self._last_cursor = self._conn.executemany(sql, seq)
        return self._last_cursor

    def executescript(self, sql: str) -> Any:
        self._last_cursor = self._conn.executescript(sql)
        return self._last_cursor

    def fetchone(self) -> dict | None:
        if self._last_cursor:
            row = self._last_cursor.fetchone()
            return dict(row) if row else None
        return None

    def fetchall(self) -> list[dict]:
        if self._last_cursor:
            return [dict(r) for r in self._last_cursor.fetchall()]
        return []

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()
        logger.info("SQLite backend closed: %s", self.db_path)

    def get_connection(self) -> Any:
        """Return a context-managed connection that auto-closes on exit."""
        from contextlib import closing
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return closing(conn)

    @property
    def rowcount(self) -> int:
        return self._last_cursor.rowcount if self._last_cursor else 0


# ── PostgreSQL Backend ──────────────────────────────────────────────


# Leading whitespace / line comments / block comments / opening parens.
_RE_LEADING_NOISE = re.compile(r"^(?:\s+|--[^\n]*\n?|/\*.*?\*/|\()+", re.DOTALL)
# Anything that can write, lock, or advance state. Deliberately broad: a false "not
# read-only" only costs us holding the pooled connection until commit/rollback (the old
# behaviour), while a false "read-only" would release a connection out from under an
# uncommitted write or a row lock. Includes RETURNING (single_winner_statement builds
# `INSERT … RETURNING`, and a bare `SELECT … RETURNING` does not exist) and the
# `SELECT … INTO` / `FOR UPDATE` forms that look like reads but are not.
_RE_WRITE_KEYWORD = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COPY|CALL"
    r"|VACUUM|ANALYZE|REINDEX|SET|RESET|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|LOCK|INTO"
    r"|RETURNING|NEXTVAL|SETVAL|FOR\s+UPDATE|FOR\s+NO\s+KEY\s+UPDATE|FOR\s+SHARE"
    r"|FOR\s+KEY\s+SHARE|pg_advisory_lock|pg_advisory_xact_lock)\b",
    re.IGNORECASE,
)
_RE_LEADING_SELECT = re.compile(r"SELECT\b", re.IGNORECASE)


def is_read_only_sql(sql: str) -> bool:
    """True only when ``sql`` is certainly a plain read.

    Used to decide whether a pooled PostgreSQL connection can be returned as soon as the
    rows are materialised. Fail-safe direction: unsure ⇒ False.
    """
    stripped = _RE_LEADING_NOISE.sub("", sql)
    if not _RE_LEADING_SELECT.match(stripped):
        return False
    return _RE_WRITE_KEYWORD.search(stripped) is None


class _MaterialisedCursor:
    """The rows of a completed read, detached from the connection that produced them.

    Lets `PostgresBackend.execute` return a pooled connection immediately after a read
    while still honouring the `fetchone()`/`fetchall()`/`rowcount` contract the rest of
    the hub calls afterwards (often much later, or never — which is why holding the
    connection until the fetch was a leak, not a deferral).
    """

    __slots__ = ("_rows", "_index", "rowcount", "description")

    def __init__(self, rows: list, rowcount: int, description: Any = None):
        self._rows = rows
        self._index = 0
        self.rowcount = rowcount
        self.description = description

    @classmethod
    def from_cursor(cls, cursor: Any) -> _MaterialisedCursor:
        rows = list(cursor.fetchall())
        reported = getattr(cursor, "rowcount", None)
        try:
            rowcount = int(reported)
        except (TypeError, ValueError):
            rowcount = len(rows)
        if rowcount < 0:
            rowcount = len(rows)
        return cls(rows, rowcount, getattr(cursor, "description", None))

    def fetchone(self) -> Any:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchmany(self, size: int = 1) -> list:
        rows = self._rows[self._index:self._index + max(0, int(size))]
        self._index += len(rows)
        return rows

    def fetchall(self) -> list:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self) -> Any:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class _TranslatingConnection:
    """psycopg connection that runs every statement through :func:`sqlite_to_pg`.

    Deliberately thin: it forwards everything it does not need to touch, so a caller
    cannot tell it apart from the connection it wraps except that its SQL now arrives in
    the right dialect. Rows come back as :class:`_PgRow` (set as the pool's row factory),
    which is what makes ``row[0]`` and ``row["name"]`` both work like sqlite3.Row.
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: Any):
        self._raw = raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._raw.execute(sqlite_to_pg(sql), params)

    def executemany(self, sql: str, seq: Any) -> Any:
        return self._raw.cursor().executemany(sqlite_to_pg(sql), seq)

    def executescript(self, sql: str) -> Any:
        return self._raw.execute(sqlite_to_pg(sql))

    def cursor(self, *a: Any, **kw: Any) -> Any:
        return self._raw.cursor(*a, **kw)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def __getattr__(self, name: str) -> Any:
        # Anything else (info, closed, transaction(), …) belongs to psycopg.
        return getattr(self._raw, name)


class _PgRow:
    """A PostgreSQL row that behaves like ``sqlite3.Row``.

    The whole point of this module is to hide the dialect difference between the two
    backends, and a `dict_row` factory leaks one of the biggest: `sqlite3.Row` supports
    BOTH ``row["name"]`` and ``row[0]``, a dict supports only the first. 23 call sites in
    the hub read a scalar as ``fetchone()[0]`` — `SELECT COUNT(*)`, `SUM(...)`, and other
    aggregates that have no natural column name — and every one of them raised
    ``KeyError: 0`` the moment a deployment set DATABASE_URL. Fixing them one by one would
    have meant inventing names for aggregates (psycopg calls them ``count``/``sum``, which
    collide when a query has two) and would leave the next positional read to break again.

    Matches sqlite3.Row's contract deliberately, including the parts that look odd:
      * iteration yields VALUES, not keys — ``for a, b, c in row`` is used in the hub
      * ``keys()`` returns column names, which is also what makes ``dict(row)`` work
        (dict() prefers the mapping protocol when an object exposes ``keys``)
    """

    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: tuple[str, ...], values: tuple[Any, ...]):
        self._names = names
        self._values = values
        self._index = {name: i for i, name in enumerate(names)}

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._index[key]]
            except KeyError:
                raise KeyError(key) from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._names)

    def get(self, key: str, default: Any = None) -> Any:
        idx = self._index.get(key)
        return default if idx is None else self._values[idx]

    def __iter__(self):
        # sqlite3.Row iterates over values; `dict(row)` still works via keys().
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: Any) -> bool:
        return key in self._index if isinstance(key, str) else key in self._values

    def __repr__(self) -> str:
        return f"_PgRow({dict(zip(self._names, self._values))!r})"


def _sqlite_like_row(cursor: Any) -> Any:
    """psycopg row factory producing :class:`_PgRow`."""
    names = tuple(d.name for d in (cursor.description or ()))

    def make(values: tuple[Any, ...]) -> _PgRow:
        return _PgRow(names, tuple(values))

    return make


class PostgresBackend:
    """PostgreSQL backend — connection pooling via psycopg_pool.

    Uses psycopg 3 with connection pool (min=2, max=8).
    Row results are returned as dicts (RealDictRow-compatible).

    DATABASE_URL is validated to reject dangerous options parameters
    that could enable SQL injection or path manipulation.
    """

    def __init__(self, database_url: str):
        from psycopg_pool import ConnectionPool

        # Reject connection strings with dangerous libpq options
        if "options=" in database_url.lower():
            # Only allow specific known-safe options
            if not re.search(r"options=-c\s*application_name=", database_url):
                raise ValueError(
                    "DATABASE_URL with custom 'options=' is rejected for security. "
                    "Use AIMARKET_PG_OPTIONS env var for safe option allowlisting."
                )
        self.database_url = database_url
        masked = re.sub(r":([^@]+)@", ":****@", database_url)
        logger.info("PostgreSQL backend initialized: %s", masked)

        self._last_conn: Any = None
        self._last_cursor: Any = None
        # True once the held connection has run a statement that needs commit/rollback.
        # A read-only statement on a clean connection is finished the moment its rows are
        # materialised, so the connection goes straight back to the pool.
        self._dirty = False

        self._pool = ConnectionPool(
            database_url,
            min_size=2,
            max_size=8,
            kwargs={"row_factory": _sqlite_like_row},
        )
        # Test connection on startup
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")
        logger.info("PostgreSQL connection pool ready (min=2, max=8)")

    @property
    def backend_type(self) -> str:
        return "postgresql"

    # ── Pool bookkeeping ────────────────────────────────────────────
    #
    # Every `getconn()` in this class MUST be matched by exactly one `putconn()`, on the
    # success path, the empty-result path and the exception path alike. It was not:
    # `execute()` checked a connection out and only `commit()`/`rollback()` ever handed one
    # back, so a read-only query — which by definition never commits — consumed a pool slot
    # for the life of the process. Eight SELECTs exhausted the pool and the ninth caller
    # blocked on `getconn()` forever, i.e. a read-heavy hub hangs. Worse, a second
    # `execute()` overwrote `_last_conn`, orphaning the first connection with no reference
    # left to return it.
    #
    # So: at most ONE connection is checked out at a time (`_acquire` reuses the held one,
    # which is also what keeps a multi-statement write in a single transaction), and it is
    # returned by `_release` — from commit, rollback, close, an exception, or the end of a
    # read.

    def _acquire(self) -> Any:
        if self._last_conn is None:
            self._last_conn = self._pool.getconn()
        return self._last_conn

    def _release(self) -> None:
        conn, self._last_conn = self._last_conn, None
        self._dirty = False
        if conn is not None:
            self._pool.putconn(conn)

    def _abandon(self, conn: Any) -> None:
        """Return a connection after a failed statement, rolling its transaction back.

        Without the rollback the connection goes back to the pool inside a failed
        transaction, and the next borrower's first statement dies with
        `InFailedSqlTransaction`. The rollback itself must not be able to swallow the
        original exception, hence the suppress.
        """
        with contextlib.suppress(Exception):
            conn.rollback()
        if conn is self._last_conn:
            self._release()
        else:  # pragma: no cover - defensive: _acquire always stores what it hands out
            self._pool.putconn(conn)

    def cursor(self) -> Any:
        """Return a psycopg cursor for backward compat with legacy code.

        Marks the connection dirty: this hands out a raw cursor, so we cannot see what is
        run on it and must assume it needs an explicit commit/rollback to be released.
        """
        conn = self._acquire()
        self._dirty = True
        return conn.cursor()

    def execute(self, sql: str, params: tuple = ()) -> Any:
        # Translate SQL dialect
        pg_sql = sqlite_to_pg(sql)
        read_only = not self._dirty and is_read_only_sql(pg_sql)
        conn = self._acquire()
        try:
            cursor = conn.execute(pg_sql, params)
        except Exception:
            self._abandon(conn)
            raise
        if read_only:
            # Materialise now and give the slot back. Deferring the fetch to
            # fetchone()/fetchall() (which may never be called at all) is precisely how the
            # connection leaked; an empty result set leaks the same as a full one.
            try:
                self._last_cursor = _MaterialisedCursor.from_cursor(cursor)
            except Exception:
                self._abandon(conn)
                raise
            self._release()
        else:
            self._last_cursor = cursor
            self._dirty = True
        return self._last_cursor

    def executemany(self, sql: str, seq: list) -> Any:
        pg_sql = sqlite_to_pg(sql)
        conn = self._acquire()
        try:
            self._last_cursor = conn.executemany(pg_sql, seq)
        except Exception:
            self._abandon(conn)
            raise
        self._dirty = True
        return self._last_cursor

    def executescript(self, sql: str) -> Any:
        conn = self._acquire()
        try:
            with conn.transaction():
                pg_sql = sqlite_to_pg(sql)
                for stmt in pg_sql.split(";"):
                    stmt = stmt.strip()
                    if stmt and not stmt.startswith("--"):
                        conn.execute(stmt)
        except Exception:
            self._abandon(conn)
            raise
        # conn.transaction() already committed the block, so nothing is pending: release
        # instead of waiting for a commit() the caller has no reason to make.
        self._last_cursor = None
        self._release()
        return None

    def fetchone(self) -> dict | None:
        if self._last_cursor:
            row = self._last_cursor.fetchone()
            return dict(row) if row else None
        return None

    def fetchall(self) -> list[dict]:
        if self._last_cursor:
            return [dict(r) for r in self._last_cursor.fetchall()]
        return []

    def commit(self) -> None:
        conn = self._last_conn
        if conn is None:
            return
        try:
            conn.commit()
        finally:
            self._release()

    def rollback(self) -> None:
        conn = self._last_conn
        if conn is None:
            return
        try:
            conn.rollback()
        finally:
            self._release()

    def close(self) -> None:
        # Hand the held connection back BEFORE closing the pool: a pool closed with a
        # connection still checked out leaks the underlying socket.
        with contextlib.suppress(Exception):
            self._release()
        self._pool.close()
        logger.info("PostgreSQL backend closed")

    @contextlib.contextmanager
    def get_connection(self) -> Any:
        """A pooled connection that TRANSLATES the SQL it is given.

        Handing out ``self._pool.connection()`` directly was the single biggest
        PostgreSQL defect in the hub: almost every caller works through
        ``with backend.get_connection() as conn: conn.execute(sql, params)`` — the channel
        ledger, the index, the reaper, the obligations — and a raw psycopg connection
        applies NO dialect translation. So on any deployment with DATABASE_URL set, the
        entire runtime query path ran SQLite SQL against PostgreSQL: ``?`` placeholders
        were never rewritten ("the query has 0 placeholders but N parameters were passed"),
        and ``datetime('now')`` was never rewritten either. The translator itself
        (sqlite_to_pg) was fine — it was simply bypassed, and only the DDL path
        (executescript) ever reached it, which is why migrations applied cleanly while
        every subsequent statement failed.
        """
        with self._pool.connection() as raw:
            yield _TranslatingConnection(raw)

    @property
    def rowcount(self) -> int:
        return self._last_cursor.rowcount if self._last_cursor else 0


# ── Single-winner claims ────────────────────────────────────────────

# Turns the INSERT into a conflict-tolerant statement whose OWN result says whether it
# inserted. Both clauses have identical syntax in SQLite and PostgreSQL. DO NOTHING covers
# UNIQUE/PRIMARY KEY conflicts only — a NOT NULL / CHECK / FK failure still raises, which
# is what we want (those are faults, not lost races).
_ON_CONFLICT_DO_NOTHING = "ON CONFLICT DO NOTHING"
_RETURNING_CLAIM = "RETURNING 1"
_RE_ON_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.IGNORECASE)
_RE_CONFLICT_ACTION = re.compile(r"\bDO\s+(NOTHING|UPDATE)\b", re.IGNORECASE)
# RETURNING landed in SQLite 3.35 (2021). Without it there is no per-statement way to tell
# the winner from the loser — see claim_unique.
_SQLITE_RETURNING_MIN_VERSION = (3, 35, 0)


def single_winner_statement(sql: str) -> str:
    """``sql`` (a plain INSERT) rewritten so the statement itself reports who inserted.

    Both clauses are appended here rather than by the caller so no claim site can forget
    one, and each is load-bearing:

    * **ON CONFLICT DO NOTHING** — the loser is a no-op, not an exception. Catching the
      IntegrityError instead is not safe on the shared SQLite connection the hub hands
      every worker thread: rolling back the failed statement reverts a concurrent
      winner's uncommitted row (measured: 3 of 8 racing threads "won" the same key), and
      leaving the transaction open strands the write lock until other connections time
      out.
    * **RETURNING** — the winner gets a row, the loser gets none. Row COUNTS cannot
      decide this: ``cursor.rowcount`` is filled from the connection-global
      ``sqlite3_changes()`` after the step releases the GIL, so a co-resident thread's
      statement overwrites it and every racer reads 0 (measured the same way).
      ``backend.rowcount`` is worse still — a shared attribute.

    A conflict clause the caller wrote itself must be DO NOTHING. ``DO UPDATE`` would make
    RETURNING yield a row for the LOSER too — every racer would read "I won" — and the
    substring test alone would happily append a second, contradictory clause. Refuse
    instead: a claim whose verdict cannot be trusted must not be built.
    """
    statement = sql.rstrip().rstrip(";")
    conflict = _RE_ON_CONFLICT.search(statement)
    if conflict is None:
        statement = f"{statement} {_ON_CONFLICT_DO_NOTHING}"
    else:
        action = _RE_CONFLICT_ACTION.search(statement, conflict.end())
        if action is None or action.group(1).lower() != "nothing":
            raise ValueError(
                "single-winner claim requires ON CONFLICT ... DO NOTHING; "
                f"refusing to build a claim from {sql!r}"
            )
    if " returning " not in f" {statement.lower()} ":
        statement = f"{statement} {_RETURNING_CLAIM}"
    return statement


def claim_unique(backend: DBBackend, sql: str, params: tuple = ()) -> bool:
    """Run an INSERT that a UNIQUE/PRIMARY KEY makes single-winner; True iff WE won.

    The atomic alternative to SELECT-then-INSERT: the database picks the winner, so two
    concurrent claimants can never both proceed. Used for money-critical single-use keys
    (a consumed stake-deposit hash), where every racer writes an IDENTICAL row — so "did
    I win?" can only come from the statement itself, never from reading the table back.

    The claim runs on its OWN connection (``get_connection``), not the backend's shared
    one. ``SQLiteBackend`` hands the same ``sqlite3.Connection`` to every worker thread,
    and concurrent ``commit()`` on it raises ``SystemError: … returned NULL without
    setting an exception`` out of CPython's sqlite3 — a claim that must be durable before
    it counts cannot ride on that. A private connection also keeps the claim out of
    whatever transaction the shared connection has open.

    The write is committed before returning: a claim only the writing transaction can see
    is not a claim. Every error propagates — a claim that could not be EVALUATED must
    never be reported as won.
    """
    is_sqlite = getattr(backend, "backend_type", "") == "sqlite"
    if is_sqlite and sqlite3.sqlite_version_info < _SQLITE_RETURNING_MIN_VERSION:
        # Fail closed rather than guess. The caller is guarding money with this claim.
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} has no RETURNING support (needs "
            f"{'.'.join(map(str, _SQLITE_RETURNING_MIN_VERSION))}+), so a single-winner "
            "claim cannot be decided; refusing rather than allowing a double claim"
        )
    statement = single_winner_statement(sql)
    if not is_sqlite:
        # The pooled connection is raw — the dialect translation PostgresBackend.execute
        # would normally apply (?, datetime('now'), …) has to happen here.
        statement = sqlite_to_pg(statement)
    with backend.get_connection() as conn:
        row = conn.execute(statement, params).fetchone()
        conn.commit()
    return row is not None


# ── Factory ─────────────────────────────────────────────────────────


def create_backend(
    database_url: str = "",
    db_path: str | Path | None = None,
) -> DBBackend:
    """Create the appropriate database backend.

    If database_url is set → PostgreSQL.
    Otherwise → SQLite (backward-compatible default).

    Args:
        database_url: PostgreSQL connection string (optional)
        db_path: SQLite database path (used if database_url is empty). ``None`` (or the
            hub's own default) means "no subsystem chose a path" → AIMARKET_DB_PATH.

    Returns:
        SQLiteBackend or PostgresBackend

    Path precedence — an explicit argument WINS over ``AIMARKET_DB_PATH``. The env var
    used to win over the argument, which silently aliased every SQLite database in the
    process onto one file: the channel ledger asks for ``data/channels.db`` and the
    provenance plugin for ``data/provenance.db``, but any deploy that sets
    AIMARKET_DB_PATH (every Docker image here does) handed all three the hub file while
    each subsystem kept reporting its own path. An ambient default must not silently
    override a caller that named a file — that is not configuration, it is aliasing.

    ``AIMARKET_DB_PATH`` keeps its documented job: it relocates the HUB database, i.e.
    the case where the caller asked for the hub default (or asked for nothing). An
    explicit path that disagrees with the env var is honoured and the ignored env var is
    logged once per distinct pair, so the remaining ambiguity is loud rather than silent.
    Operators who WANT one shared file must now say so per subsystem (e.g.
    ``AIMARKET_CHANNELS_DB_PATH=$AIMARKET_DB_PATH``).
    """
    url = database_url or os.environ.get("DATABASE_URL", "")

    if url and url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(url)

    env_path = os.environ.get("AIMARKET_DB_PATH", "").strip()
    requested = None if db_path is None else str(db_path)
    if requested is None or requested == DEFAULT_DB_PATH:
        # Nobody chose a file (or they chose the hub default): AIMARKET_DB_PATH names it.
        return SQLiteBackend(env_path or DEFAULT_DB_PATH)
    if env_path and os.path.abspath(env_path) != os.path.abspath(requested):
        _warn_ignored_env_path(requested, env_path)
    return SQLiteBackend(requested)


# Pairs already reported by _warn_ignored_env_path. The hub constructs a handful of
# backends per process (hub index, channel ledger, provenance, migration CLI); the
# operator needs the warning once per pair, not once per construction.
_reported_path_overrides: set[tuple[str, str]] = set()


def _warn_ignored_env_path(requested: str, env_path: str) -> None:
    key = (requested, env_path)
    if key in _reported_path_overrides:
        return
    _reported_path_overrides.add(key)
    logger.warning(
        "SQLite path %s was requested explicitly; ignoring AIMARKET_DB_PATH=%s for this "
        "backend. These were the SAME file before (the env var overrode the argument) — "
        "point this subsystem's own path variable at %s if a shared file was intended.",
        requested, env_path, env_path,
    )
    if os.path.exists(env_path) and not os.path.exists(requested):
        # The upgrade shape. On a hub that ran with AIMARKET_DB_PATH set, this
        # subsystem's tables were created INSIDE the env file, and this process is about
        # to start a brand-new empty one — losing, for the channel ledger, both the open
        # channels and the consumed_deposits replay guard that makes an on-chain deposit
        # single-use. This cannot be auto-migrated from here (the factory does not know
        # which tables belong to the caller), and refusing would break every first start,
        # so it is reported at ERROR with the operator action. See README "Upgrading past
        # the shared-database aliasing".
        logger.error(
            "%s does not exist yet while %s does: if this hub ran with the env var set, "
            "THIS SUBSYSTEM'S EXISTING TABLES ARE STILL IN %s and the new file starts "
            "empty. Migrate them (or point this subsystem's own path variable at %s) "
            "BEFORE serving — for the channel ledger an empty database also resets the "
            "single-use deposit guard.",
            requested, env_path, env_path, env_path,
        )
