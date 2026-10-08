#!/usr/bin/env python3
"""Re-mark historical ``fail`` rows that were really incomplete input.

Rows written before the hub learned the ``incomplete`` class carry ``outcome='fail'``
and drag the success rate down for something no provider did wrong. They stay on the
tape either way — this only changes the mark and which bucket counts them.

It will not guess. You name the capabilities, and each one is verified against the
LIVE peer first: the script invokes it with a deliberately empty input and only
proceeds for capabilities that answer with an incomplete-input complaint (the same
test `classify_invoke_outcome` applies to new traffic). A capability that answers
anything else — a real error, a payment demand, or actual output — is skipped and
its rows are left alone.

Only rows with ``price_usd = 0`` are touched: a charged row was work delivered.
``--apply`` without ``--probe-url`` is refused: a live DB rewrite must not guess.

    python scripts/reclassify_incomplete_input.py --db /app/data/hub.db \
        --capability atlas.watchbox.check@v1 --probe-url https://atlas.modelmarket.dev
    # …prints the plan and changes nothing. Add --apply to write.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from aimarket_hub.invoke_outcome import reads_as_incomplete_input  # noqa: E402


def probe(peer_url: str, capability_id: str, product_id: str, timeout: float) -> tuple[bool, str]:
    """Ask the peer with an empty input. Returns (is_incomplete_input, reason)."""
    body = json.dumps({
        "product_id": product_id,
        "capability_id": capability_id,
        "input": {},
    }).encode()
    req = urllib.request.Request(
        peer_url.rstrip("/") + "/ai-market/v2/invoke",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body worth reading
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except Exception:
            return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — a probe that cannot run proves nothing
        return False, f"probe failed: {exc}"
    if payload.get("ok") is True or payload.get("success") is True:
        return False, "peer answered ok — not an input refusal"
    reason = str(payload.get("refuse_reason") or payload.get("error") or "")
    return reads_as_incomplete_input(reason), reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--capability", action="append", default=[], required=True)
    ap.add_argument("--product-id", default="")
    ap.add_argument("--probe-url", default="", help="peer base URL; required with --apply")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    if args.apply and not args.probe_url:
        print(
            "REFUSED: --apply without --probe-url would guess. "
            "Name a live --probe-url, or omit --apply to dry-run.",
            file=sys.stderr,
        )
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    planned = 0
    for capability_id in args.capability:
        product_id = args.product_id or capability_id.split(".", 1)[0]
        if args.probe_url:
            ok, reason = probe(args.probe_url, capability_id, product_id, args.timeout)
            print(f"probe {capability_id}: {'INCOMPLETE-INPUT' if ok else 'SKIP'} — {reason}")
            if not ok:
                continue
        rows = conn.execute(
            "SELECT COUNT(*) AS n, MIN(timestamp) AS first, MAX(timestamp) AS last "
            "FROM invocation_stats "
            "WHERE capability_id = ? AND outcome = 'fail' AND price_usd = 0",
            (capability_id,),
        ).fetchone()
        n = int(rows["n"] or 0)
        print(f"  {capability_id}: {n} row(s) {rows['first']} … {rows['last']}")
        planned += n
        if args.apply and n:
            conn.execute(
                "UPDATE invocation_stats SET outcome = 'incomplete' "
                "WHERE capability_id = ? AND outcome = 'fail' AND price_usd = 0",
                (capability_id,),
            )
    if args.apply:
        conn.commit()
        print(f"APPLIED: {planned} row(s) re-marked incomplete")
    else:
        print(f"DRY RUN: {planned} row(s) would be re-marked. Re-run with --apply.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
