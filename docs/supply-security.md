# Community supply security

Third-party capabilities published via `POST /ai-market/v2/supply/register` with an
`invoke_url` are guarded by stake, rate limits, LUMEN trust scoring, response
signatures, and discover/invoke trust floors.

## Flow

1. **Stake** — `POST /ai-market/v2/supply/stake` with `publisher_id`, `amount_usd`, `tx_hash` (required in prod).
2. **Publish** — manifest must include `publisher_id`, `provider_pubkey`, `invoke_url`.
3. **Discover** — low-trust and duplicate `invoke_url` listings are filtered.
4. **Invoke** — input sanitized, trust checked, provider response verified via
   `X-Provider-Signature` (Ed25519 over canonical JSON result).
5. **Slash** — failed invokes can slash stake and emit federated slash signals.

Per verified failure (failed invoke or invalid/missing `X-Provider-Signature`):

```
slash_usd = min(remaining_stake × 0.05, 5.0)
```

Trust edge on invoke: `+0.15` success / `−0.25` failure. Each slash also records a `−0.5` stake-weight edge and may emit a federated attestation via `SlashRegistry` (peer hubs pull `/reputation/slashes`).

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `AIMARKET_SUPPLY_MIN_STAKE_USD` | `10` (dev) / `25` (prod default) | Minimum stake to publish |
| `AIMARKET_SUPPLY_PUBLISH_PER_HOUR` | `5` | Anti-spam publish cap per publisher |
| `AIMARKET_SUPPLY_MIN_TRUST_DISCOVER` | `AIMARKET_MIN_TRUST_SCORE` | Search floor |
| `AIMARKET_SUPPLY_MIN_TRUST_INVOKE` | `0.35` | Invoke floor for `invoke_url` caps |
| `AIMARKET_SUPPLY_REQUIRE_RESPONSE_SIG` | on in prod | Require `X-Provider-Signature` |
| `AIMARKET_SUPPLY_PRODUCT_ALLOWLIST` | empty | Comma-separated `product_id` allowlist |
| `AIMARKET_SUPPLY_SECURITY_RELAXED` | `0` | Dev/test: disable stake + slash |
| `AIMARKET_ORACLE_FAMILY_URL` | oracle family hub | LUMEN `lumen.reputation@v1` endpoint |

ARGUS agents filter discovery client-side with `ARGUS_MIN_HUB_TRUST` (default `0.25`).

## Provider signing

Sign the **result object only** (not the wrapper):

```python
canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
signature = base64(ed25519_sign(canonical))
```

Return header: `X-Provider-Signature: <signature>`.

See `examples/hello-capability/server.py` for a working demo.
