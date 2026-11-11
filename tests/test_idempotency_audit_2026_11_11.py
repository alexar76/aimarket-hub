"""Audit 2026-11-11, credits-3: an Idempotency-Key record was dropped on a 403 (the root's output
blocked) even when subcontracted children had been bought, so the client's retry under the same key
ran a new job and paid for the children again. Such an answer is kept now."""
from __future__ import annotations

import json

from aimarket_hub.database import HubDatabase
from aimarket_hub.idempotency import IdempotencyStore


def _store(tmp_path):
    return IdempotencyStore(HubDatabase(str(tmp_path / "hub.db"))._conn)


def _row(store, key):
    return store._conn.execute("SELECT status, http_status FROM invoke_idempotency WHERE idem_key = ?",
                               (key,)).fetchone()


def test_a_blocked_answer_after_paid_children_is_kept(tmp_path):
    store = _store(tmp_path)
    assert store.claim("acct:a", "k1", "sha") is None
    body = json.dumps({"success": False, "error": "output_blocked",
                       "subcontracting": {"job_id": "j", "nodes": [], "spent_usd": 0.004}}).encode()
    store.finish("acct:a", "k1", 403, [(b"content-type", b"application/json")], body)
    row = _row(store, "k1")
    assert row is not None and row["status"] == "done" and row["http_status"] == 403


def test_a_blocked_answer_that_cost_nothing_is_still_dropped(tmp_path):
    store = _store(tmp_path)
    assert store.claim("acct:a", "k2", "sha") is None
    body = json.dumps({"success": False, "error": "output_blocked"}).encode()
    store.finish("acct:a", "k2", 403, [(b"content-type", b"application/json")], body)
    assert _row(store, "k2") is None
