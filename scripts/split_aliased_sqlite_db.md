# Runbook — upgrading past the shared-database aliasing

Applies to any hub that ran with `AIMARKET_DB_PATH` set (every Docker image here sets it)
and is now moving to a build where `create_backend` lets an explicit `db_path` argument
win over that env var.

## What changed, and what breaks if you skip this

Before: `AIMARKET_DB_PATH` overrode the path each subsystem asked for, so the hub index,
the payment-channel ledger (`data/channels.db`) and the provenance plugin
(`data/provenance.db`) all wrote into the ONE file the env var named, while each subsystem
kept logging its own path.

After: the ledger really opens `data/channels.db`. On a hub that has been running aliased
that file does not exist yet, so it is created empty and:

* every **open channel disappears** — buyers' deposited balances are invisible and
  invocations against them fail;
* `consumed_deposits` is empty, which **resets the single-use guard on on-chain deposits**
  — a deposit that was already credited can be presented again.

The ledger's tables are still sitting in the aliased file. This script copies them across.

## Procedure

1. **Stop the hub.** The script verifies that the source row counts do not move while it
   runs and aborts (exit 3) if they do, but a stopped hub is the only safe way to do this.
2. **Back up the aliased file** (`cp`, or `sqlite3 <file> ".backup out.db"`). The script
   never writes to it, but take the backup anyway.
3. **Dry run** — reports what it would copy, writes nothing:

   ```sh
   cd aimarket-hub
   AIMARKET_DB_PATH=/data/hub.db python scripts/split_aliased_sqlite_db.py \
       --subsystem channels --dry-run
   ```

   * `status: not_aliased` — the source holds none of the ledger tables. Nothing to do;
     you were never aliased. Stop here.
   * `status: would_copy` — go on. Check the `counts` block against what you expect
     (`channels`, `debited_receipts`, `consumed_deposits`, `channel_holds`,
     `channel_payout_obligations`).
4. **Run it:**

   ```sh
   AIMARKET_DB_PATH=/data/hub.db AIMARKET_CHANNELS_DB_PATH=/data/channels.db \
       python scripts/split_aliased_sqlite_db.py --subsystem channels
   ```

   Paths can also be given as `--source` / `--target`. Success prints
   `status: "copied"` with `counts` (source), `target_counts` (after commit) and
   `source_counts_after` — all three must be equal, and the script fails if they are not.
5. **Start the hub** and confirm from the ledger side: open-channel count matches step 3,
   and a channel opened before the upgrade still invokes.
6. **Keep the aliased file.** Its copy of the ledger tables is your rollback: the tables
   are still there and untouched. Do not prune it until the new file has served
   successfully. The now-unused ledger tables inside it are harmless — the hub index does
   not read them.

## Exit codes

| Code | Meaning | Action |
| --- | --- | --- |
| 0 | `copied`, `already_split`, `not_aliased` | proceed |
| 2 | `REFUSED` | the target already holds *different* ledger rows, the source is missing, source == target, or a column is missing in the target. Nothing was written — read the message and decide. Move a conflicting target aside (keep it), do not delete it. |
| 3 | `VERIFICATION FAILED` | counts did not match. The target transaction was rolled back and the source is intact. Most likely the hub was still running. |
| 1 | usage / IO error | fix and re-run |

## Idempotency

Re-running is safe. The second run sees the rows already present with matching counts and
reports `already_split` without writing. It also records each successful split in a
`_db_split_audit` row in the target (timestamp, subsystem, source path, row counts). A
target holding rows that do **not** match the source is refused, never merged — so a run
against the wrong file cannot silently duplicate a ledger.

The copy also carries the ledger's `_migrations` marks across, so the hub does not replay
migrations against already-migrated tables on first start (a replayed
`ALTER TABLE channels ADD COLUMN secret_hash` raises, and `Migrations.apply()` re-raises).

## Other subsystems

Only `channels` is defined as a split target, because it is the one whose empty file has a
money consequence. The provenance plugin's tables are a cache — if it starts empty it
refills. If a future subsystem needs the same treatment, add it to `SUBSYSTEMS` in the
script (tables + the migration versions that build them) rather than passing table names in
by hand: only names from that table reach SQL.

## Alternative: keep one file on purpose

If a shared file was actually intended, do not run this script — point the subsystem's own
variable at the same path instead:

```sh
AIMARKET_CHANNELS_DB_PATH=$AIMARKET_DB_PATH
```

That is now an explicit choice, which is the whole point of the `create_backend` change.
