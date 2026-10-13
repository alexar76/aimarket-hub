# subcontract-capability — `weather.witness@v1`

A provider that **hires two other providers through the hub that called it**, so hub
subcontracting ([`aimarket-protocol/mandates.md` §6](https://github.com/alexar76/aimarket-protocol/blob/main/mandates.md),
operator notes in [`docs/mandates.md`](../../docs/mandates.md), the full guide with the money flow in
[`docs/subcontracting.md`](../../docs/subcontracting.md)) can be seen working on a live hub.

For a place it buys GAIA's current weather and air-quality readings as children of the call
it is serving, compares them, and answers with both device-attested readings, whether they
describe the same place and moment, and each child's job node and work-receipt digest.

```
root buyer ──invoke weather.witness@v1──▶ hub ──X-AIMarket-Job (+ Grant)──▶ weather-witness
 (canary)     subcontract.allowance_usd    │                                   │
                                           │◀── invoke gaia.weather.read@v1 ───┤ same token,
                                           │◀── invoke gaia.air.read@v1 ───────┘ same grant
                                           └──routed──▶ GAIA (iot.modelmarket.dev)
```

**Honest scope.** This is a self-driven research demonstration, not organic demand: our own
buyer ([`scripts/subcontract_canary.py`](https://github.com/alexar76/aicom/blob/main/scripts/subcontract_canary.py)) calls our own
composite, which buys from our own GAIA, billed in the hub's internal credits. What it proves
is that every hop runs the production code path — the job token, the grant, the allowance
holds, the routing, the receipts — and that the result can be checked from outside. It does
not show that anyone else wants to buy a witnessed weather reading.

| File | What it is |
|---|---|
| `server.py` | The provider. Python 3.9+ and `cryptography`, nothing else. |
| `capability.json` | The listing. `invoke_url` and `provider_pubkey` are filled in by `publish.sh`. |
| `publish.sh` | Registers the listing through `POST /ai-market/v2/supply/register` (operator). |
| `weather-witness.service` | The systemd unit. |

The hub's test suite runs this exact `server.py` against the real hub app and runs the canary
against it: `aimarket-hub/tests/test_subcontract_example.py`.

## What it does on a call

1. **Refuses anything the hub did not send.** The invoke URL is public, so a request must
   carry an `X-AIMarket-Job` token that verifies against the hub's Ed25519 key
   (`signer_public_key` in `/.well-known/ai-market.json`, or pinned in
   `WITNESS_HUB_SIGNER_PUBKEY`), names `iss` = `WITNESS_HUB_URL`, has not expired, was issued
   for a call to `weather.witness@v1` (the hub gives a token to every provider it runs — this
   stops another provider replaying its own at this URL) and has not been served before.
   Nothing is bought, and the input is not even read, until it does.
2. **Buys both readings in parallel** through `WITNESS_HUB_URL` — `gaia.weather.read@v1` and
   `gaia.air.read@v1`, product `gaia.gateway`, **`source_hub: https://iot.modelmarket.dev`**.
   A federated child must name its `source_hub`: without it the hub looks for a local
   capability, finds only the peer's listing and answers 400.
   - **cost-plus** — the hub sent `X-AIMarket-Job-Grant` because the buyer set aside an
     allowance. The purchase carries the token and the grant and *no other payment* (the hub
     refuses a grant that arrives with one). The allowance pays the price and, for a resold
     peer, the routing fee.
   - **fixed price** — no grant. The purchase carries the token and the provider's own
     `X-API-Key`, under `WITNESS_DAILY_CAP_USD`. Two ceilings are reserved against the cap
     before buying and settled to what the readings cost afterwards; a timed-out or 5xx
     purchase counts at its ceiling. Without a key configured the provider answers 402 and
     spends nothing.
3. **Answers** with `{witness, place, weather, air, agreement, funding, job, children}`,
   signed (`X-Provider-Signature`) with its own key over the hub's request-bound canonical.
   One reading is not a witness: if either purchase fails it answers 502, and — cost-plus,
   §6.3 — the reading that was delivered is still paid for by the buyer.

"Agree" means the weather relay and the air-quality point are within 75 km (GAIA's own radius
for "this place's weather") and the two observations within 2 h (air quality is hourly).
Disagreement is reported, not an error: two honest sensors can differ, and saying so is the
point of a witness.

## Environment

| Variable | Default | |
|---|---|---|
| `WITNESS_HUB_URL` | `https://modelmarket.dev` | The one hub whose tokens it accepts and where it buys. |
| `WITNESS_HUB_API_URL` | = `WITNESS_HUB_URL` | Where purchases are sent, if not the public URL. |
| `WITNESS_HUB_SIGNER_PUBKEY` | fetched | Pin the hub's key. Unpinned, it is read once per process — restart after a hub key rotation. |
| `WITNESS_BIND` / `WITNESS_PORT` | `127.0.0.1` / `9475` | |
| `WITNESS_KEY_PATH` | `./provider_key` | Ed25519 key, PEM, created `0600` on first run. |
| `WITNESS_STATE_PATH` | next to the key | The daily-cap ledger (`daily_spend.json`). |
| `WITNESS_API_KEY` | unset | The provider's own prepaid account key. Unset = fixed-price mode off. Fund the account with a verified USDC top-up; a credit grant is not a cash deposit. |
| `WITNESS_PAYOUT_ADDRESS` | unset | Used by `publish.sh` to register the provider's actual EVM recipient for wallet-funded root purchases. |
| `WITNESS_DAILY_CAP_USD` | `0.05` | Most it spends of its own money per UTC day. |
| `WITNESS_CHILD_SOURCE_HUB` | `https://iot.modelmarket.dev` | The peer GAIA is routed to. |
| `WITNESS_CHILD_MAX_PRICE_USD` | `0.01` | Sent as each child's `max_price_usd`. The hub compares a routed child's price + fee **rounded up to whole cents**, so for a $0.001 reading anything under $0.01 refuses every purchase (409). |
| `WITNESS_CHILD_TIMEOUT_S` | `12` | Per purchase; both must finish inside the hub's 30 s provider timeout. |

Local run: `WITNESS_HUB_URL=http://127.0.0.1:9083 python3 server.py`, `curl 127.0.0.1:9475/healthz`.

## Going live on modelmarket.dev — operator runbook

Every step changes production and needs the owner's go-ahead; nothing here runs by itself.
Rehearse on UNI first (at the end).

**1. The provider, bound to loopback.** On the hub host (my-vps):

```bash
sudo useradd --system --home /var/lib/weather-witness --shell /usr/sbin/nologin weather-witness
sudo install -d -o weather-witness -g weather-witness -m 0700 /var/lib/weather-witness
sudo install -D -m 0755 server.py /usr/local/lib/weather-witness/server.py   # from the monorepo, not a host tree
sudo install -m 0644 weather-witness.service /etc/systemd/system/
sudo install -m 0600 /dev/null /etc/weather-witness.env    # WITNESS_API_KEY=… only if fixed price is wanted
python3 -c 'import cryptography'                          # else: apt install python3-cryptography
sudo systemctl daemon-reload && sudo systemctl enable --now weather-witness
curl -s http://127.0.0.1:9475/healthz
```

**2. A public invoke URL.** Add to the `modelmarket.dev` server block
(`deploy/nginx/modelmarket.dev.conf`, and whatever certbot made of it on the host), before
`location /`:

```nginx
    # weather.witness@v1 — a first-party provider on this host (examples/subcontract-capability).
    # The trailing slash on proxy_pass strips the prefix: /providers/weather-witness/invoke → /invoke.
    location /providers/weather-witness/ {
        proxy_pass http://127.0.0.1:9475/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 35s;
        client_max_body_size 64k;
    }
```

`sudo nginx -t && sudo systemctl reload nginx`, then
`curl -s https://modelmarket.dev/providers/weather-witness/healthz`. A public https URL is what
lets the listing pass the publish gate with `AIMARKET_ALLOW_LOCAL_PUBLISH` left unset on the
apex. The hub reaches it through its own public name.

**3. The collateral gate.** In production a publisher needs a $25 verified stake. For a
first-party provider the documented exemption waives the collateral only — credential,
ownership, rate limit and signature gates still apply:

```bash
# on the hub host, from the build tree; --no-build reuses the live image byte for byte
./scripts/deploy_hub_rebuild.sh --no-build --set AIMARKET_SUPPLY_OPERATOR_PUBLISHERS=aicom-weather-witness
```

The value replaces the variable: if it is already set, pass the existing list plus
`aicom-weather-witness`. (The alternative is a real $25 verified stake for that publisher.)

**4. Publish.** `AIMARKET_ADMIN_TOKEN=… ./publish.sh` — it reads `provider_pubkey` from the
running provider's `/healthz`, checks the provider verifies tokens from the same hub, and posts
`capability.json`. The apex's public manifest then shows `local_capabilities: 1`; the listing's
description says it is a research demo.

**5. A buyer for the canary.** Open signup is off on the apex, so the operator mints and
funds an account with the admin bearer (kept off the command line, as in `publish.sh`):

```bash
auth() { printf 'header = "Authorization: Bearer %s"\n' "$AIMARKET_ADMIN_TOKEN"; }
curl -sS -X POST https://modelmarket.dev/ai-market/v2/accounts --config <(auth) \
  -H 'Content-Type: application/json' -d '{"label": "subcontract canary (root buyer)"}'
#   → account_id and api_key; the key is shown ONCE — straight into a 0600 file
curl -sS -X POST https://modelmarket.dev/ai-market/v2/accounts/<account_id>/credit --config <(auth) \
  -H 'Content-Type: application/json' \
  -d '{"amount_usd": 1.0, "note": "subcontract canary", "reference": "subcontract-canary-1"}'
```

A dollar covers ~250 cost-plus runs ($0.002 witness + $0.002 readings). Run
`scripts/subcontract_canary.py --api-key-file <file>`. For `--fixed-price`, mint a SECOND
account the same way — the provider's own money — put its key in `/etc/weather-witness.env` as
`WITNESS_API_KEY=…` and restart the unit.

**Rehearsal.** UNI would be the natural place (virtual money, `AIMARKET_ALLOW_LOCAL_PUBLISH=1`,
gateway `172.17.0.1`, and `publish.sh` takes `HUB_URL` / `WITNESS_INVOKE_URL` /
`WITNESS_HEALTH_URL` for it), but the bubble has no GAIA — its satellites are khronos, kyma,
psephos, stoicheion, diktyon and horizon — so this composite cannot buy its two readings there.
The rehearsal that exists is the hub's test suite, which runs this `server.py` and the canary
against the real hub app. On the apex, the first canary run is the live check, and whatever
goes wrong it cannot cost the buyer more than the $0.002 witness plus the $0.01 allowance.

## Limits worth knowing

- One process per key: the daily cap's lock is in-process. The unit runs one.
- The job token lives 60 s; both purchases must be back well inside the hub's 30 s provider
  timeout, which is why they run in parallel with a 12 s timeout each.
- A reply that fails the hub's signature check is a provider fault (502, trust ding) — and the
  readings it consumed are still paid by a cost-plus buyer. So is a GAIA outage: a witness
  with one reading fails, and the market records that against this provider.
