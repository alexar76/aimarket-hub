# One root call for a Studio graph

`pipeline.run@v1` is a built-in capability of product `hephaestus`. Any buyer agent
can call it over REST with a Studio quote and signed payments. The hub does not
receive a wallet key. Children are normal catalogue SKUs; they need not implement
subcontracting. The executor links their purchases into one SUB/1 job tree.

The root has no added orchestration fee. Every paid child uses real wallet funds;
**credits, credit-line allowances and trial grants do not fund this SKU**. Existing
SUB/1 credit grants, ordinary Studio Run and the factory pipeline API retain their
own contracts. This SKU currently starts a root job; invoking it as a child of a
different job is refused.

## Recommended: one client operation

Use `aimarket_hub.pipeline_client.run_pipeline` or `run.py` for combined graph
preparation, local signing, an all-in budget check, automatic confirmation waiting
and durable recovery. Paid execution in this first client supports Base mainnet
USDC on macOS/Linux. Free graphs need no wallet or RPC.

```bash
python run.py blueprint.json --hub https://modelmarket.dev \
  --rpc https://mainnet.base.org --max-total-usd 1 \
  --execute --prompt-key --state order.private.json
python run.py --resume --state order.private.json
```

ETH/USD is checked through two RPC endpoints against the pinned Chainlink feed;
the budget uses the higher price plus 25%. A manual `--native-usd-ceiling` can only
raise that valuation. Stale or unavailable prices pause new submissions. Network
cost checks are conservative estimates; L1 fees and USD rates cannot be hard-capped
by the signed transaction. Keep the recovery file private. One client operation
normally makes two primary Hub requests; pending confirmations add retries of the
same order, with no replacement payments.

Detailed protocol and recovery: [EN](../../docs/one-call-pipelines.md) ·
[RU](../../docs/one-call-pipelines.ru.md) · [ES](../../docs/one-call-pipelines.es.md) ·
[FR](../../docs/one-call-pipelines.fr.md) · [ZH](../../docs/one-call-pipelines.zh.md).
[Recorded one-call production test](../../docs/case-study-codex-one-call.md).

## Existing low-level protocol


1. `POST /studio/preflight` with `{nodes, max_budget_usd}`. The existing Studio
   checks and settlement restrictions apply. Retain its `run_id` and `access_token`.
2. `POST /studio/paid-runs/{run_id}/prepare-pipeline`, header
   `X-Studio-Run-Token`, body `{wallet}`. This fixes the executor mode and wallet and
   returns one invoice and EIP-712 `authorization` per paid step, in graph order.
   A graph containing only free steps needs no wallet; send `{}`.
3. Locally sign each authorization, then its containing EIP-1559 transaction. The
   transaction pays precisely the advertised token amount to the advertised seller
   or MarketSplitter. Use consecutive wallet nonces in the returned order. The
   helper `aimarket_hub.pipeline_payments.payment_transaction` builds the calldata.
4. Submit **one root invoke**:

```json
{
  "product_id": "hephaestus",
  "capability_id": "pipeline.run@v1",
  "source_hub": "local",
  "input": {
    "run_id": "paid_…",
    "access_token": "…",
    "transactions": {"read": "0x…signed transaction bytes…"}
  }
}
```

The server checks the complete bundle before sending any payment: exact set of
steps, both signatures, payer, recipient, chain, nonce, amount and calldata. It
resolves and validates each step's input before broadcasting that step's payment.
Later steps are never paid after an earlier failure. Gas is additional and signed
by the buyer; the token budget is not a gas budget or an escrow reservation.

HTTP 200 returns a completed or failed run (inspect `success` / `status`), the final
result, a signed bill, a signed root receipt committing to the bill, output and
child receipts, and `subcontracting.job_id`. HTTP 202 means payment confirmation
is pending or the run needs reconciliation. Retry the same invoke; `transactions`
may be omitted on retries once the bundle has been accepted. The same run, root,
transaction hashes and completed outcomes are reused. `GET /studio/paid-runs/{id}`
with the token remains available for read-only status.

The capability's `result` contains `{status, final_result, bill_of_materials}`;
the envelope also exposes these fields for Studio clients. Receipt digests are
SHA-256 hex over compact, sorted-key JSON with ASCII escaping (Python
`json.dumps(value, sort_keys=True, separators=(",", ":"))`), including the bill's
signature. Verify the root receipt's object signature against a pinned hub key.

SUB/1 children report `funded_by: "own"`: they are paid by this executor's
buyer-authorized wallet transactions. This is **not** the existing credit-ledger
`X-AIMarket-Job-Grant`. The root's funding block says `buyer_wallet`; its budget and
unspent amount are spending limits, not deposited or refundable balances.

A composite child may hire its own subcontractors within the usual SUB/1 depth
and size limits, paying on its own rail. It receives no authority to draw further
money from the root buyer's wallet. Those descendants appear in the tree; the
buyer's bill counts only the selected graph's purchases, without double-counting
costs already included in a composite seller's price.

## Agent example

Export a Studio request to `blueprint.json` (`{"nodes": [...]}`). Install the hub
package with its `escrow` extra on both the hub and the signing client. Preview:

```bash
python client.py blueprint.json --hub https://your-hub.example --budget 0.01
```

For actual payment, set `PIPELINE_WALLET_KEY` securely in the agent's environment,
fund that wallet with the quoted token and native gas currency, and explicitly run:

```bash
python client.py blueprint.json --hub https://your-hub.example --budget 0.01 \
  --rpc https://your-chain-rpc.example --execute --state run.private.json
python client.py --resume --state run.private.json
```

The recovery file contains the scoped run credential and executable signed
transactions. It is written privately before the first invoke. Do not publish it.
The bill contains neither the credential nor the raw transactions. No wallet
private key leaves the signing client. Use a wallet without concurrent transactions
during the run: unrelated transactions can consume the reserved nonce sequence.
Standard browser wallets may not expose raw transaction signing; the first Studio
wallet flow remains available for interactive per-step payment.

## Limits and recovery

The existing preflight supports at most sixteen steps, one chain/token and supported
seller-direct routes. Recursive pipeline purchases are refused. Input-dependent
graphs work, but a running graph cannot change its nodes or prices. Prices/schemas
are checked again before buying; dynamic field values are checked after resolution.

Quotes last fifteen minutes; prepared invoices use the hub's settlement TTL, which
also bounds the signed authorizations. Prepare and sign close to execution. A pending
transaction can be reconciled after expiry, but the existing settlement rail may
refuse delivery against an expired invoice; its confirmed payment stays in the bill.
Slow graphs may require a longer operator-configured invoice TTL.

An RPC response can be lost after broadcast. The hub knows the hash from the signed
bytes before broadcasting and can resend **identical bytes**; the chain cannot
execute that nonce twice. A crash around provider dispatch retains the durable
claim and reports reconciliation rather than calling the provider again. An
operator must compare the stored run, transaction and provider receipt; there is
no automatic unlock timer. Failed delivery does not automatically refund an
external seller payment. Remaining transactions are not broadcast.

Migration 43 stores root execution claims and cached responses alongside the paid
Studio state (migration 42). Existing `/studio/run` stays independent. This is an
implementation in the repository; deploying the updated hub and client is separate.

## Recorded production order

On 29 September 2026, an agent in Codex completed a wallet-funded two-child order
through this protocol on Base mainnet. See the [case study in five languages](../../docs/case-study-codex-subcontract.md)
for the signed response, payment transactions, result and measured cost.
