"""``Idempotency-Key`` for paid invokes: a retry after a timeout is not a second purchase.

A buyer whose invoke timed out cannot tell whether the call ran. An escrow invoke
with a channel secret uses the buyer's signed receipt ID as the retry key. Credits
and ledger channel invokes can supply an Idempotency-Key (1-128 characters from
A-Z a-z 0-9 . _ : -). The hub
keeps the first answer for 24 hours, keyed by the credential (hashed) and the key:

* the same key with the same body returns the stored answer, marked
  ``X-AIMarket-Idempotency: replayed`` — the provider does not run, nothing is charged;
* the same key with a different body is ``422 idempotency_key_reused``;
* the same key while the first request still runs is ``409 idempotency_in_progress``; a
  request that never finished (the process died) is ``409 idempotency_outcome_unknown`` after
  15 minutes. Reconcile against the payment ledger before starting another call.

Answers that come before any money could move (401, 403, 404, 413, 415, 422, 429, 503) are
not kept, so a retry with a fixed credential or after a rate limit is a fresh call. Everything
else is kept, including a 5xx: after a server error the outcome is uncertain, and replaying
the error is safe where re-running is not. Other rails answer ``X-AIMarket-Idempotency:
not-applicable`` and run as before.

The final response is saved before it is sent, so a failed send can be replayed.
Plain local escrow calls checkpoint a validated result before capture. Retrying a
checkpointed request resumes settlement without invoking the provider again. A crash
before that checkpoint remains an unknown outcome.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from typing import Any

from aimarket_hub.db_backend import returning_one

logger = logging.getLogger(__name__)

HEADER = "idempotency-key"
STATUS_HEADER = b"x-aimarket-idempotency"
KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
ESCROW_RECEIPT_RE = re.compile(r"(?:0[xX])?[a-fA-F0-9]{64}")
KEEP_S = 24 * 3600.0
RUNNING_UNKNOWN_AFTER_S = 15 * 60.0
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_STORED_BYTES = 4 * 1024 * 1024
NOT_KEPT = frozenset({401, 403, 404, 405, 413, 415, 422, 429, 503})
_SKIP_RESPONSE_HEADERS = frozenset({b"content-length", b"date", b"server", b"set-cookie", b"transfer-encoding"})
_INVOKE_PATH = "/ai-market/v2/invoke"


def _paid_subcontracting(body: bytes | None) -> bool:
    """Does an answer report subcontracted calls that cost money?

    Such an answer is kept whatever its status: a 403 (the root's output blocked) after children
    were bought used to be dropped, so the client's retry under the same Idempotency-Key ran a
    fresh job and bought the children again (audit 2026-11-11, credits-3)."""
    if not body:
        return False
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    sub = doc.get("subcontracting") if isinstance(doc, dict) else None
    if not isinstance(sub, dict):
        return False
    for field in ("spent_usd", "spent_from_allowance_usd"):
        try:
            if float(sub.get(field) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def credential_scope(headers: dict[str, str]) -> str:
    """Whose call this is, as a hash of the secret that pays for it, or "" for other rails."""
    api_key = (headers.get("x-api-key") or "").strip()
    if api_key:
        return "key:" + _sha256(api_key.encode())
    channel = (headers.get("x-payment-channel") or "").strip()
    secret = (headers.get("x-payment-channel-secret") or "").strip()
    if channel and secret:
        return "channel:" + _sha256(f"{channel}\n{secret}".encode())
    return ""


class IdempotencyStore:
    def __init__(self, conn: Any):
        self._conn = conn
        self._last_purge = 0.0

    def _purge(self, now: float) -> None:
        if now - self._last_purge < 300:
            return
        self._last_purge = now
        try:
            self._conn.execute("DELETE FROM invoke_idempotency WHERE status <> 'prepared' AND created_at < ?", (now - KEEP_S,))
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - housekeeping must not fail an invoke
            logger.warning("idempotency: purge failed: %s", exc)

    def claim(self, scope: str, key: str, request_sha256: str, *, now: float | None = None) -> dict[str, Any] | None:
        """None when this request owns the key now; otherwise the row that already holds it."""
        now = time.time() if now is None else now
        self._purge(now)
        # An expired row is gone for this key even before the purge gets to it.
        self._conn.execute("DELETE FROM invoke_idempotency WHERE scope = ? AND idem_key = ? AND status <> 'prepared' AND created_at < ?",
                           (scope, key, now - KEEP_S))
        won = returning_one(
            self._conn,
            "INSERT INTO invoke_idempotency (scope, idem_key, request_sha256, status, created_at) "
            "VALUES (?, ?, ?, 'running', ?) ON CONFLICT DO NOTHING RETURNING idem_key",
            (scope, key, request_sha256, now),
            commit=True,
        )
        if won is not None:
            return None
        row = self._conn.execute(
            "SELECT request_sha256, status, http_status, response_headers, response_body, created_at "
            "FROM invoke_idempotency WHERE scope = ? AND idem_key = ?", (scope, key),
        ).fetchone()
        return dict(row) if row else {"status": "running", "request_sha256": request_sha256, "created_at": now}

    def prepare_escrow(self, scope: str, key: str, response: Any, receipt: str, deadline: int, share: Any) -> dict[str, Any]:
        if len(response.body) > MAX_STORED_BYTES:
            raise ValueError("escrow response exceeds the recovery limit")
        saved = {
            "receipt": receipt,
            "deadline": deadline,
            "share": share,
            "headers": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in response.raw_headers
                        if k.lower() not in _SKIP_RESPONSE_HEADERS],
        }
        headers = json.dumps(saved)
        body = base64.b64encode(response.body).decode("ascii")
        row = returning_one(
            self._conn,
            "UPDATE invoke_idempotency SET status = 'prepared', http_status = ?, "
            "response_headers = ?, response_body = ? WHERE scope = ? AND idem_key = ? "
            "AND status = 'running' RETURNING idem_key",
            (response.status_code, headers, body, scope, key),
            commit=True,
        )
        if row is None:
            raise RuntimeError("escrow response checkpoint was not saved")
        return {"http_status": response.status_code, "response_headers": headers, "response_body": body}

    def resume_escrow(self, scope: str, key: str, held: dict[str, Any], *,
                      finalize: bool = True) -> dict[str, Any] | None:
        from aimarket_hub import channels
        from aimarket_hub.credits import CreditsLedger, PublisherShare

        saved = json.loads(held["response_headers"])
        receipt = saved["receipt"]
        if channels.hold_state(receipt) != "captured":
            if int(saved["deadline"]) <= int(time.time()):
                return self._expire_escrow(scope, key, held, receipt) if finalize else None
            capture = channels.capture_hold(receipt)
            if not capture.get("ok") and channels.hold_state(receipt) != "captured":
                return None
        if saved["share"] is not None:
            share = PublisherShare(*saved["share"])
            result = CreditsLedger(self._conn).pay_publisher(
                share.account_id, share.amount_usd, receipt_id=share.receipt_id, note=share.note,
            )
            if result.get("error"):
                return None
        if not finalize:
            return held
        headers = json.dumps(saved["headers"])
        self._conn.execute(
            "UPDATE invoke_idempotency SET status = 'done', response_headers = ?, finished_at = ? "
            "WHERE scope = ? AND idem_key = ? AND status = 'prepared'",
            (headers, time.time(), scope, key),
        )
        self._conn.commit()
        return {**held, "status": "done", "response_headers": headers}

    def _expire_escrow(self, scope: str, key: str, held: dict[str, Any], receipt: str) -> dict[str, Any] | None:
        """The buyer's authorization expired before capture: nothing is charged or delivered.

        Waiting would not help: the hold can no longer be captured, and while it stays held
        its channel can neither close nor expire. Release it, take the authorization out of
        its nonce window as a refused call does, and keep a definite answer for retries.
        """
        from aimarket_hub import channels

        if channels.hold_state(receipt) == "held":
            channels.release_hold(receipt)
        state = channels.hold_state(receipt)
        if state == "captured":
            return self.resume_escrow(scope, key, held)  # a capture won the race: deliver
        if state == "held":
            return None
        try:
            from aimarket_hub.escrow_bridge import authorization as bridge_auth
            from aimarket_hub.escrow_bridge import store as bridge_store

            bridge_auth.withdraw(receipt, bridge_store.AuthorizationStore(),
                                 reason="authorization expired before settlement")
        except Exception:  # noqa: BLE001 - the mirror abandons an expired authorization anyway
            logger.exception("escrow: could not withdraw expired authorization %s", receipt[:14])
        headers = json.dumps([["content-type", "application/json"]])
        body = base64.b64encode(json.dumps({
            "success": False, "error": "escrow_authorization_expired",
            "detail": "the payment authorization expired before the hub could settle this call; "
                      "nothing was charged. Sign a new authorization to call again.",
        }).encode()).decode("ascii")
        self._conn.execute(
            "UPDATE invoke_idempotency SET status = 'done', http_status = 409, response_headers = ?, "
            "response_body = ?, finished_at = ? WHERE scope = ? AND idem_key = ? AND status = 'prepared'",
            (headers, body, time.time(), scope, key),
        )
        self._conn.commit()
        return {**held, "status": "done", "http_status": 409, "response_headers": headers, "response_body": body}

    def finish(self, scope: str, key: str, status: int, headers: list[tuple[bytes, bytes]], body: bytes | None) -> None:
        try:
            if status in NOT_KEPT and not _paid_subcontracting(body):
                self._conn.execute("DELETE FROM invoke_idempotency WHERE scope = ? AND idem_key = ? AND status = 'running'",
                                   (scope, key))
            else:
                kept = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in headers
                        if k.lower() not in _SKIP_RESPONSE_HEADERS and k.lower() != STATUS_HEADER]
                self._conn.execute(
                    "UPDATE invoke_idempotency SET status = ?, http_status = ?, response_headers = ?, "
                    "response_body = ?, finished_at = ? WHERE scope = ? AND idem_key = ? "
                    "AND (status = 'running' OR (status = 'prepared' AND ? = 200 AND ? = 1))",
                    ("done" if body is not None else "too_large", int(status), json.dumps(kept),
                     base64.b64encode(body or b"").decode("ascii"), time.time(), scope, key, status, int(body is not None)),
                )
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001 - the answer is already on its way
            logger.error("idempotency: recording the answer for %s failed: %s", key, exc)


async def _send_json(send: Any, status: int, body: dict[str, Any], marker: bytes) -> None:
    raw = json.dumps({**body, "protocol_version": "v2"}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()),
        (b"cache-control", b"no-store"), (STATUS_HEADER, marker)]})
    await send({"type": "http.response.body", "body": raw})


class InvokeIdempotency:
    """ASGI layer around ``POST /ai-market/v2/invoke`` (see the module docstring)."""

    def __init__(self, app: Any, conn: Any):
        self.app = app
        self.store = IdempotencyStore(conn)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path") or "")
        root = str(scope.get("root_path") or "")
        if root and path.startswith(root):
            path = path[len(root):]        # behind a proxy that mounts the hub under a prefix
        if scope.get("type") != "http" or scope.get("method") != "POST" or path.rstrip("/") != _INVOKE_PATH:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        key = (headers.get(HEADER) or "").strip()
        escrow_candidate = bool(headers.get("x-payment-channel") and headers.get("x-payment-channel-secret")
                                and not any(headers.get(name) for name in (
                                    "x-api-key", "x-payment", "payment", "payment-signature",
                                    "x-aimarket-mandate", "x-aimarket-job", "x-aimarket-job-grant",
                                    "x-aimarket-sandbox-visitor",
                                )))
        if not key and not escrow_candidate:
            await self.app(scope, receive, send)
            return
        if key and not KEY_RE.fullmatch(key):
            await _send_json(send, 400, {"success": False, "error": "idempotency_key_invalid",
                                         "detail": "Idempotency-Key is 1-128 of A-Z a-z 0-9 . _ : -"}, b"rejected")
            return
        owner = credential_scope(headers)
        if not owner:
            await self.app(scope, receive, _marking(send, b"not-applicable"))
            return
        # The buyer's receipt-binding nonce (spec §7.3.9) is part of the request: a malformed one
        # is refused by the route before any money moves, so that refusal must not be kept as the
        # key's answer (it would replay for 24 h, and on the escrow rail lock the signed
        # authorization); and a retry under the same key with another nonce is another request,
        # not a replay that hands back the first call's binding (review of 2026-11-11).
        from starlette.datastructures import Headers

        from aimarket_hub.receipt_binding import read_client_nonce

        # Starlette's Headers, as the route reads them: the same value, and the same refusal of a
        # repeated nonce header.
        client_nonce, nonce_error = read_client_nonce(Headers(scope=scope))
        if nonce_error:
            await self.app(scope, receive, _marking(send, b"not-applicable"))
            return

        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                await _send_json(send, 413, {"success": False, "error": "request_too_large",
                                             "detail": f"an idempotent invoke body is at most {MAX_REQUEST_BYTES} bytes"},
                                 b"rejected")
                return
            chunks.append(chunk)
            more = bool(message.get("more_body"))
        body = b"".join(chunks)

        if escrow_candidate:
            try:
                document = json.loads(body)
            except (ValueError, UnicodeError):
                document = None
            authorization = document.get("payment_authorization") if isinstance(document, dict) else None
            if isinstance(authorization, dict):
                receipt = authorization.get("receiptId") or authorization.get("receipt_id")
                if isinstance(receipt, str) and ESCROW_RECEIPT_RE.fullmatch(receipt.strip()):
                    channel = headers["x-payment-channel"].strip()
                    secret = headers["x-payment-channel-secret"].strip()
                    owner = "escrow:" + _sha256(f"{channel}\n{secret}".encode())
                    key = receipt.strip().lower().removeprefix("0x")

        delivered = False

        async def replay_body() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if not key:
            await self.app(scope, replay_body, send)
            return

        request_hash = _sha256(body if client_nonce is None else body + b"\n" + client_nonce.encode())
        held = self.store.claim(owner, key, request_hash)
        if held is not None and held.get("request_sha256") != request_hash and client_nonce is not None \
                and held.get("request_sha256") == _sha256(body) and _kept_nonce_allows(held, client_nonce):
            # A row fingerprinted before the nonce was part of it (hubs before 3.20.0 hashed the
            # body alone): the same body, and nothing kept that says another nonce was sent. An
            # escrow checkpoint left by the deploy restart resumes instead of answering 422.
            request_hash = held["request_sha256"]
        if held is not None:
            if held.get("status") == "prepared" and held.get("request_sha256") == request_hash:
                try:
                    resumed = self.store.resume_escrow(owner, key, held)
                except Exception:
                    logger.exception("escrow: could not resume settlement")
                    resumed = None
                if resumed is None:
                    await _send_json(send, 409, {"success": False, "error": "escrow_settlement_pending"},
                                     b"in-progress")
                    return
                held = resumed
            await self._answer_held(send, held, request_hash)
            return

        if owner.startswith("escrow:"):
            def prepare_escrow(response: Any, receipt: str, deadline: int, share: Any) -> Any:
                if receipt.lower().removeprefix("0x") != key:
                    raise ValueError("escrow checkpoint receipt mismatch")
                held = self.store.prepare_escrow(owner, key, response, receipt, deadline, share)
                return lambda: self.store.resume_escrow(owner, key, held, finalize=False)

            scope.setdefault("state", {})["prepare_escrow_response"] = prepare_escrow

        status = 500
        kept_headers: list[tuple[bytes, bytes]] = []
        out: list[bytes] | None = []
        out_size = 0
        finished = False
        start_message = None

        async def capture(message: dict[str, Any]) -> None:
            nonlocal status, kept_headers, out, out_size, finished, start_message
            if message["type"] == "http.response.start":
                status = int(message["status"])
                kept_headers = list(message.get("headers") or [])
                start_message = {**message, "headers": [*kept_headers, (STATUS_HEADER, b"stored")]}
                return
            elif message["type"] == "http.response.body" and out is not None:
                chunk = message.get("body", b"")
                out_size += len(chunk)
                if out_size > MAX_STORED_BYTES:
                    out = None
                else:
                    out.append(chunk)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self.store.finish(owner, key, status, kept_headers, b"".join(out) if out is not None else None)
                finished = True
            if start_message is not None:
                await send(start_message)
                start_message = None
            await send(message)

        try:
            await self.app(scope, replay_body, capture)
        except BaseException:
            if not finished:
                self.store.finish(owner, key, 500, [(b"content-type", b"application/json")],
                                  json.dumps({"success": False, "error": "internal_error",
                                              "detail": "the first request with this key failed inside the hub; "
                                                        "reconcile it before paying again"}).encode())
            raise
        if not finished:
            logger.warning("idempotency: invoke ended without a complete answer")

    async def _answer_held(self, send: Any, held: dict[str, Any], request_sha256: str) -> None:
        if held.get("request_sha256") != request_sha256:
            await _send_json(send, 422, {"success": False, "error": "idempotency_key_reused",
                                         "detail": "this Idempotency-Key was used with a different request body "
                                                   "or X-Client-Nonce; retry with the original ones"},
                             b"rejected")
            return
        if held.get("status") == "running":
            age = time.time() - float(held.get("created_at") or 0)
            if age < RUNNING_UNKNOWN_AFTER_S:
                await _send_json(send, 409, {"success": False, "error": "idempotency_in_progress",
                                             "detail": "the first request with this key is still running; retry later"},
                                 b"in-progress")
            else:
                await _send_json(send, 409, {"success": False, "error": "idempotency_outcome_unknown",
                                             "detail": "the first request with this key never finished; "
                                                       "reconcile the payment ledger before starting another call"},
                                 b"unknown")
            return
        if held.get("status") == "too_large":
            await _send_json(send, 409, {"success": False, "error": "idempotency_answer_not_kept",
                                         "detail": f"the first request with this key was answered HTTP "
                                                   f"{held.get('http_status')}, too large to keep for replay"},
                             b"replayed")
            return
        stored = base64.b64decode(held.get("response_body") or "")
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in json.loads(held.get("response_headers") or "[]")]
        await send({"type": "http.response.start", "status": int(held.get("http_status") or 200),
                    "headers": [*headers, (b"content-length", str(len(stored)).encode()), (STATUS_HEADER, b"replayed")]})
        await send({"type": "http.response.body", "body": stored})


def _kept_nonce_allows(held: dict[str, Any], client_nonce: str) -> bool:
    """False when the kept answer's receipt binding names a different buyer nonce."""
    try:
        kept = json.loads(base64.b64decode(held.get("response_body") or "") or b"null")
    except (ValueError, TypeError):
        return True
    binding = kept.get("receipt_binding") if isinstance(kept, dict) else None
    bound = binding.get("client_nonce") if isinstance(binding, dict) else None
    return bound is None or bound == client_nonce


def _marking(send: Any, marker: bytes) -> Any:
    async def marked(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            message = {**message, "headers": [*(message.get("headers") or []), (STATUS_HEADER, marker)]}
        await send(message)
    return marked
