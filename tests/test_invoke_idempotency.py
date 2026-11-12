"""Idempotency-Key on paid invokes: a retry after a timeout is answered, not bought again.

The outside review of 2026-10-22 asked what happens when "the client repeats a paid
operation after a timeout, when it is unknown whether the previous call completed". On the
credits rail the answer was: a second purchase.
"""
from __future__ import annotations

import time

import pytest

from tests._mandate_kit import balance, funded_account, hub, list_static

BODY = {"product_id": "demo-echo", "capability_id": "demo.echo@v1", "input": {"text": "hi"}, "source_hub": "local"}
INVOKE = "/ai-market/v2/invoke"


@pytest.fixture
def market(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path) as (client, db):
        list_static(db, price=0.004)
        account, api_key = funded_account(client, db, 1.0)
        yield client, db, account, api_key


def _rows(db) -> list[dict]:
    rows = db._conn.execute("SELECT * FROM invoke_idempotency").fetchall()
    db._conn.commit()
    return [dict(r) for r in rows]


def test_a_retry_with_the_same_key_is_answered_once_and_charged_once(market):
    client, db, account, api_key = market
    headers = {"X-API-Key": api_key, "Idempotency-Key": "order-7f3a"}
    first = client.post(INVOKE, json=BODY, headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["x-aimarket-idempotency"] == "stored"
    assert balance(db, account) == pytest.approx(0.996)
    again = client.post(INVOKE, json=BODY, headers=headers)
    assert again.status_code == 200
    assert again.headers["x-aimarket-idempotency"] == "replayed"
    assert again.json() == first.json()
    assert balance(db, account) == pytest.approx(0.996)        # charged once
    # Without a key every request is a purchase, as before.
    for _ in range(2):
        assert client.post(INVOKE, json=BODY, headers={"X-API-Key": api_key}).status_code == 200
    assert balance(db, account) == pytest.approx(0.988)


def test_the_same_key_with_another_body_is_refused(market):
    client, db, account, api_key = market
    headers = {"X-API-Key": api_key, "Idempotency-Key": "k-1"}
    assert client.post(INVOKE, json=BODY, headers=headers).status_code == 200
    other = client.post(INVOKE, json={**BODY, "input": {"text": "bye"}}, headers=headers)
    assert other.status_code == 422
    assert other.json()["error"] == "idempotency_key_reused"
    assert balance(db, account) == pytest.approx(0.996)


def test_keys_belong_to_the_credential_that_pays(market):
    client, db, account, api_key = market
    other_account, other_key = funded_account(client, db, 1.0)
    for key in (api_key, other_key):
        r = client.post(INVOKE, json=BODY, headers={"X-API-Key": key, "Idempotency-Key": "shared-name"})
        assert r.headers["x-aimarket-idempotency"] == "stored"
    assert balance(db, account) == pytest.approx(0.996)
    assert balance(db, other_account) == pytest.approx(0.996)
    assert all(api_key not in str(r) and other_key not in str(r) for r in _rows(db))   # hashed, never kept


def test_a_first_request_still_running_or_lost_is_never_run_again(market):
    client, db, account, api_key = market
    import hashlib
    import json

    from aimarket_hub.idempotency import credential_scope

    scope = credential_scope({"x-api-key": api_key})
    body = json.dumps(BODY).encode()
    db._conn.execute(
        "INSERT INTO invoke_idempotency (scope, idem_key, request_sha256, status, created_at) VALUES (?, ?, ?, 'running', ?)",
        (scope, "lost-1", hashlib.sha256(body).hexdigest(), time.time()))
    db._conn.commit()
    headers = {"X-API-Key": api_key, "Idempotency-Key": "lost-1", "Content-Type": "application/json"}
    busy = client.post(INVOKE, content=body, headers=headers)
    assert busy.status_code == 409 and busy.json()["error"] == "idempotency_in_progress"
    db._conn.execute("UPDATE invoke_idempotency SET created_at = ? WHERE idem_key = 'lost-1'", (time.time() - 3600,))
    db._conn.commit()
    lost = client.post(INVOKE, content=body, headers=headers)
    assert lost.status_code == 409 and lost.json()["error"] == "idempotency_outcome_unknown"
    assert balance(db, account) == pytest.approx(1.0)          # nothing ran, nothing charged
    # A day later the key is free again.
    db._conn.execute("UPDATE invoke_idempotency SET created_at = ? WHERE idem_key = 'lost-1'", (time.time() - 90_000,))
    db._conn.commit()
    assert client.post(INVOKE, content=body, headers=headers).status_code == 200
    assert balance(db, account) == pytest.approx(0.996)


def test_answers_from_before_any_money_moved_are_not_kept(market):
    client, db, account, api_key = market
    bad = client.post(INVOKE, json=BODY, headers={"X-API-Key": "aimk_not-a-key", "Idempotency-Key": "k-401"})
    assert bad.status_code == 401
    assert _rows(db) == []                                      # a fixed credential retries afresh
    malformed = client.post(INVOKE, json=BODY, headers={"X-API-Key": api_key, "Idempotency-Key": "no spaces allowed"})
    assert malformed.status_code == 400 and malformed.json()["error"] == "idempotency_key_invalid"


def test_other_rails_say_the_key_does_not_apply(market):
    client, db, account, api_key = market
    r = client.post(INVOKE, json=BODY, headers={"Idempotency-Key": "anon-1"})
    assert r.headers["x-aimarket-idempotency"] == "not-applicable"
    assert _rows(db) == []


def test_a_lost_connection_cannot_discard_the_completed_answer(market):
    import asyncio
    import json

    from aimarket_hub.idempotency import InvokeIdempotency

    _, db, _, api_key = market
    executions = []

    async def invoke(scope, receive, send):
        executions.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"result":"paid work"}'})

    scope = {
        "type": "http", "method": "POST", "path": INVOKE,
        "headers": [(b"x-api-key", api_key.encode()), (b"idempotency-key", b"lost-connection")],
    }

    async def receive():
        return {"type": "http.request", "body": json.dumps(BODY).encode()}

    async def disconnected(message):
        raise ConnectionError("the first response never reached the buyer")

    with pytest.raises(ConnectionError):
        asyncio.run(InvokeIdempotency(invoke, db._conn)(scope, receive, disconnected))

    response = []

    async def collect(message):
        response.append(message)

    asyncio.run(InvokeIdempotency(invoke, db._conn)(scope, receive, collect))
    assert len(executions) == 1
    assert (b"x-aimarket-idempotency", b"replayed") in response[0]["headers"]
    assert response[1]["body"] == b'{"result":"paid work"}'


# ── the receipt-binding nonce (spec §7.3.9), review of 2026-11-11 ──────────────


def test_a_malformed_client_nonce_is_not_kept_as_the_keys_answer(market):
    client, db, account, api_key = market
    headers = {"X-API-Key": api_key, "Idempotency-Key": "order-nonce-1"}
    bad = client.post(INVOKE, json=BODY, headers={**headers, "X-Client-Nonce": "short"})
    assert bad.status_code == 400 and bad.json()["error"] == "client_nonce_invalid"
    assert bad.headers["x-aimarket-idempotency"] == "not-applicable"
    assert balance(db, account) == pytest.approx(1.0)
    # Fixing the header and retrying under the same key runs the call, once.
    good = client.post(INVOKE, json=BODY, headers={**headers, "X-Client-Nonce": "buyer-nonce-0001"})
    assert good.status_code == 200, good.text
    assert good.headers["x-aimarket-idempotency"] == "stored"
    assert good.json()["receipt_binding"]["client_nonce"] == "buyer-nonce-0001"
    assert balance(db, account) == pytest.approx(0.996)


def test_a_retry_with_another_nonce_is_another_request_not_a_replay(market):
    client, db, account, api_key = market
    headers = {"X-API-Key": api_key, "Idempotency-Key": "order-nonce-2"}
    first = client.post(INVOKE, json=BODY, headers={**headers, "X-Client-Nonce": "nonce-AAAA-0001"})
    assert first.status_code == 200, first.text
    other = client.post(INVOKE, json=BODY, headers={**headers, "X-Client-Nonce": "nonce-BBBB-0002"})
    assert other.status_code == 422 and other.json()["error"] == "idempotency_key_reused"
    same = client.post(INVOKE, json=BODY, headers={**headers, "X-Client-Nonce": "nonce-AAAA-0001"})
    assert same.status_code == 200 and same.headers["x-aimarket-idempotency"] == "replayed"
    assert same.json()["receipt_binding"]["client_nonce"] == "nonce-AAAA-0001"
    assert same.headers["x-client-nonce-echo"] == "nonce-AAAA-0001"
    assert balance(db, account) == pytest.approx(0.996)   # charged once


def test_a_nonce_sent_twice_is_refused_by_both_layers(market):
    client, db, account, api_key = market
    for pair in (("short", "buyer-nonce-0001"), ("buyer-nonce-0001", "short")):
        r = client.post(INVOKE, json=BODY, headers=[
            ("X-API-Key", api_key), ("Idempotency-Key", "dup-" + pair[0]),
            ("X-Client-Nonce", pair[0]), ("X-Client-Nonce", pair[1])])
        assert r.status_code == 400 and r.json()["error"] == "client_nonce_invalid"
        assert r.headers["x-aimarket-idempotency"] == "not-applicable"
    assert balance(db, account) == pytest.approx(1.0)


def test_a_row_kept_before_the_nonce_was_fingerprinted_still_replays(market):
    import hashlib
    import json

    client, db, account, api_key = market
    raw = json.dumps(BODY, separators=(",", ":")).encode()
    headers = {"X-API-Key": api_key, "Idempotency-Key": "legacy-1", "Content-Type": "application/json"}
    first = client.post(INVOKE, content=raw, headers={**headers, "X-Client-Nonce": "nonce-AAAA-0001"})
    assert first.status_code == 200, first.text
    # How a hub before 3.20.0 kept it: the body alone.
    db._conn.execute("UPDATE invoke_idempotency SET request_sha256=?", (hashlib.sha256(raw).hexdigest(),))
    db._conn.commit()
    again = client.post(INVOKE, content=raw, headers={**headers, "X-Client-Nonce": "nonce-AAAA-0001"})
    assert again.status_code == 200 and again.headers["x-aimarket-idempotency"] == "replayed"
    other = client.post(INVOKE, content=raw, headers={**headers, "X-Client-Nonce": "nonce-BBBB-0002"})
    assert other.status_code == 422 and other.json()["error"] == "idempotency_key_reused"
    assert balance(db, account) == pytest.approx(0.996)
