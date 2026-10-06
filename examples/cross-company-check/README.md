# cross-company-check — `claim.audit@v1` and `claim.audit.direct@v1`

**Two independent companies, one job.** Independent AI's auditor, sold on
`independentai.network/hub`, hires Attested Memory's checks on Attested's own hub
(`hub.attestedmemory.net`) inside the buyer's subcontracting job:

```
buyer ──invoke claim.audit@v1 + allowance──▶ Independent's hub ──token + grant──▶ claim-audit (this provider)
                                                   │◀── invoke claim.check@v1 ──────────┤ same token,
                                                   │◀── invoke contradiction.scan@v1 ───┘ same grant
                                                   └── resold, paid from Independent's account ──▶ Attested's hub
```

It answers with both of Attested's findings, a combined verdict (`inconsistent` when the statements
contradict each other, otherwise Attested's status), the two children's job nodes and receipt digests,
and its own Ed25519 signature over the hub's request-bound canonical.

Everything that is not business logic — token verification against the hub's key, the one-time node,
cost-plus vs fixed-price funding, the daily cap, the transport — is
[`../subcontract-capability/server.py`](../subcontract-capability/server.py) (`weather.witness@v1`) verbatim.
Money flow, settlement between the two companies, and the HESTIA variant:
[`docs/subcontracting.md` → Hiring across companies](../../docs/subcontracting.md#hiring-across-companies).

| File | What it is |
|---|---|
| `server.py` | The provider. Python 3.9+ and `cryptography`; `eth-account` for `claim.audit.direct@v1`. Environment `AUDIT_*` (see `claim-audit.service`). |
| `capability.json` | The listing, registered through `POST /ai-market/v2/supply/register` (operator). |
| `capability-direct.json` | `claim.audit.direct@v1`: the same audit, its checks paid per call in USDC (below). |
| `claim-audit.service` | The systemd unit. Loopback only; nginx exposes `/hub/providers/claim-audit/` to the hub alone. |

Tests: `aimarket-hub/tests/test_cross_company_example.py` runs this exact `server.py` between TWO real hub
apps — Independent's (reselling Attested with a peer key) and Attested's (selling both checks) — and checks
every ledger: the buyer's allowance, Independent's account at Attested, and what Attested earned.

**Deployed 2026-10-04** on the Independent host (`claim-audit.service`, `:9476`, provider key
`5WiOATuxEnoXFL3E5CKFJzuUL4as2rmtCXJdcscspt4=`). `claim.audit@v1` is cost-plus only (no `AUDIT_API_KEY`):
it never spends its own money. Independent's account at Attested is prepaid with a USDC top-up
([`docs/credits-topup.md`](../../docs/credits-topup.md)); while it is empty, a job answers
`502 child_failed` and the buyer pays nothing. `claim.audit.direct@v1` pays from the provider's wallet
`0x9d24D267Cf8D9A8b9Ed104b4856cDe8830C266eF`; its first live audit paid Attested $0.031 in two Base
transactions, [`0xd8a41fb8…`](https://basescan.org/tx/0xd8a41fb865beba51d9e795f556dc3b77e6ce5bb20a0dceb2e0691aa791997531)
and [`0x0998c423…`](https://basescan.org/tx/0x0998c423bd7a4262648a731ae1ed3986beb0c0bbd3716f55f2986c84776bf832).

## `claim.audit.direct@v1` — the checks paid per call in USDC, no account at Attested

The same audit, settled the other way: the provider buys Attested's two checks on **Attested's own hub**
and pays each one on chain when it buys it, from its own wallet. Independent needs no account at
Attested and prepays nothing; Attested's hub sells the checks seller-direct and verifies every payment.

```
buyer ──invoke claim.audit.direct@v1 ($0.05)──▶ Independent's hub ──token──▶ claim-audit
claim-audit ──invoke claim.check@v1──▶ Attested's hub ──402: pay $0.022 to Attested, nonce, secret──▶
claim-audit ──transferWithAuthorization (USDC, Base)──▶ chain ──mined──▶
claim-audit ──same invoke + X-Payment / X-Payment-Nonce / X-Payment-Secret──▶ Attested's hub ──answer──▶
(contradiction.scan@v1 the same, in parallel; $0.009)
```

| Variable | Default | Meaning |
|---|---|---|
| `AUDIT_WALLET_KEY_PATH` | — | The provider's Base wallet, `{"private_key": "0x…"}`, 0600. Unset: `503 wallet_not_configured`. |
| `AUDIT_CHILD_PAYEES` | — | The ONLY addresses a 402 may name as payee (comma-separated). A 402 naming anyone else is paid nothing. |
| `AUDIT_CHILD_MAX_PRICE_USD` | `0.03` | The most it pays for one check. |
| `AUDIT_DAILY_CAP_USD` | `0.05` | What the wallet may spend in a UTC day; reserved before buying, settled to what was paid. |
| `AUDIT_BASE_RPC_URLS` | three public Base nodes | Where it reads the chain and sends its transactions. |
| `AUDIT_CONFIRMATIONS` | `2` | Blocks it waits for before presenting a payment. |
| `AUDIT_DIRECT_TIMEOUT_S` | `25` | One check, from the 402 to the answer. |

The answer's `children[].payment` names each transaction, the amount and the payee. The wallet is a hot
wallet on the server — keep a small float in it and refill it from the company's treasury: a stolen
server loses the float, not the treasury. It also pays the gas (a few hundred-thousandths of an ETH per
check on Base). Not atomic: the money moves before the work; if a check fails after it was paid, refunding
it is the seller's to do. The two purchases happen on Attested's hub, so they are not nodes of the buyer's
job on Independent's hub — the audit is, and its answer carries both transactions.

Tests: `aimarket-hub/tests/test_cross_company_direct.py` runs this `server.py` between Attested's real hub
(selling seller-direct) and Independent's real hub, against a fake Base node that executes the token's
`transferWithAuthorization` only for a valid EIP-712 signature — the Attested hub's own settlement code
verifies every payment. It also checks the payee allow-list, the ceiling and an empty wallet: nothing sent.
