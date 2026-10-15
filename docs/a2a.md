# A2A 1.0 on the hub: search and paid invoke

The hub is an [A2A 1.0](https://a2a-protocol.org/latest/specification/) agent at `POST /a2a`
(JSON-RPC binding), described by `GET /.well-known/agent-card.json`. It has two skills:

| Skill | What it does | Answer |
|---|---|---|
| `marketplace-search` | Search the live, policy-filtered catalogue — the same handler as `GET /ai-market/v2/search`. | a `Message` (nothing is stored) |
| `marketplace-invoke` | Buy and run one capability; the result comes with its signed receipts. | a `Task` |

The invoke skill is a **bridge, not a second payment path**. It posts the exact invoke bytes to
`/ai-market/v2/invoke` *through the app itself* (in-process, `httpx.ASGITransport`), so mandate
and allowance admission, credits holds, seller-direct x402 verification, the invoice nonce the
x402 middleware mints on every 402, the free trial, the rate limiter and the metrics are the
ones every other buyer goes through. The task record (`a2a_tasks.py`, migration 036) only
remembers what came back.

Every request carries `A2A-Version: 1.0` (anything else fails closed with `-32009`) and
`Content-Type: application/json`; the body is capped at 128 KiB — which also caps the invoke
input you can send this way.

## Search

```bash
curl -sS https://modelmarket.dev/a2a \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  --data '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{
    "messageId":"m-1","role":"ROLE_USER",
    "parts":[{"data":{"intent":"weather at a sensor","budget":0.05,"limit":5}}]}}}' \
  | jq '.result.message.parts[1].data.matches'
```

A text part works too (`{"text":"find a weather sensor under $0.05"}`). Each match carries the
`product_id`, `capability_id` and `source_hub` the invoke needs, its price and its input fields.

## Invoke

A message selects `marketplace-invoke` with a data part `{"invoke": {...}}`, or (for mandates)
one raw part holding the invoke JSON. The fields are the invoke body's own:

| Field | |
|---|---|
| `product_id`, `capability_id` | required, from search |
| `source_hub` | `"local"` (default) or the hub URL search returned |
| `input` | the capability's input object |
| `max_price_usd` | refuse (409 → `REJECTED`) if the price rose above this since search — send it |
| `verify` | Pay-on-Verified block ([pay-on-verified.md](pay-on-verified.md)) |
| `subcontract` | `{"allowance_usd", "max_depth"}` pass-through allowance ([subcontracting.md](subcontracting.md)) |

Anything else is refused as `-32602` before a task exists.

### Paid with credits

```bash
curl -sS https://modelmarket.dev/a2a \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' -H "X-API-Key: $AIMARKET_KEY" \
  --data '{"jsonrpc":"2.0","id":2,"method":"SendMessage","params":{"message":{
    "messageId":"8b1f…-uuid","role":"ROLE_USER","parts":[{"data":{"invoke":{
      "product_id":"gaia.gateway","capability_id":"gaia.weather.read@v1","source_hub":"local",
      "input":{"device_id":"om-wx-01"},"max_price_usd":0.01}}}]}}}'
```

```json
{"jsonrpc": "2.0", "id": 2, "result": {"task": {
  "id": "a2at_4911c9f0672109f10461db5a", "contextId": "…",
  "status": {"state": "TASK_STATE_COMPLETED", "timestamp": "…", "message": {"role": "ROLE_AGENT", "parts": [{"text": "gaia.weather.read@v1 completed ($0.001)"}]}},
  "artifacts": [
    {"artifactId": "result", "name": "result", "parts": [{"data": {"reading": "…"}}]},
    {"artifactId": "aimarket-receipt", "name": "aimarket-receipt", "parts": [{"data": {"nonce": "rcpt_…", "signature": "…"}}],
     "metadata": {"sourceHub": "local", "verifyEndpoint": "https://modelmarket.dev/ai-market/v2/receipts/verify"}},
    {"artifactId": "provenance", "name": "provenance", "parts": [{"data": {"awr_version": "2.0.0", "…": "…"}}],
     "metadata": {"digest_sri": "sha256-…", "receipt_url": "…"}}
  ],
  "metadata": {"aimarket": {"capability": {"…": "…"}, "price_usd": 0.001, "remaining_balance": 0.999, "latency_ms": 40}}
}}}
```

- `result` is a data part (a text part when the capability returned a string).
- `aimarket-receipt` is the signed gateway receipt. For a local capability, check it at
  `verifyEndpoint`; for a federated one it is signed by the provider, and its metadata names the
  signer's `/.well-known/ai-market.json` instead.
- `provenance` is the AWR/2 work receipt, present when the provenance plugin is installed.
- Task metadata carries, when present: `mandate`, `subcontracting`, `job`, `verification` (the
  Pay-on-Verified envelope — poll `/ai-market/v2/verification/{nonce}` while it is `pending`),
  `remaining_balance`, `sandbox`. `subcontracting` — the job's bill of materials — is in the
  Task's `metadata.aimarket` on a `FAILED` or `REJECTED` task too: subcontractors that delivered
  are paid even when the root fails, and the bill explains that charge
  ([subcontracting.md](subcontracting.md#subcontracting-over-a2a)).

### Paid with a mandate

A mandate's request proof signs **`POST /ai-market/v2/invoke`** and the sha256 of the exact
body bytes. The hub forwards bytes, not objects, so the client serializes the invoke once,
signs it, and sends:

- `X-AIMarket-Mandate` and `X-AIMarket-Mandate-Proof` as HTTP headers on `POST /a2a`;
- the invoke as **one raw part** — `{"raw": "<base64 of those bytes>", "mediaType": "application/json"}`.

A data part with mandate headers is refused (`-32602`): it would be re-serialized and fail as
`mandate_proof_invalid`. Bytes that differ from the signed ones fail the proof and the task
answers `AUTH_REQUIRED`; nothing is charged. A limit reached is `REJECTED` with
`status.message.metadata.aimarket = {"error": "mandate_limit", "limit": "perDay", …}`.

```python
from aimarket_agent import A2AClient, Mandate
from aimarket_agent.a2a import task_result, task_state

client = A2AClient(HUB, mandate=Mandate(document=doc, key=agent_key, hub_origin=HUB))
task = client.invoke("gaia.gateway", "gaia.weather.read@v1", {"device_id": "om-wx-01"}, max_price_usd=0.01)
assert task_state(task) == "TASK_STATE_COMPLETED" and task["receipt_verified"]
```

### Paid with x402 (a2a-x402 v0.2, standalone flow)

The card declares the extension `https://github.com/google-agentic-commerce/a2a-x402/blob/main/spec/v0.2`
with `required: false`.

1. An unpaid invoke on a priced capability (trial spent or off) answers
   `TASK_STATE_INPUT_REQUIRED` with the hub's own terms — the decoded `PAYMENT-REQUIRED` V2
   object, invoice nonce included:

   ```json
   "status": {"state": "TASK_STATE_INPUT_REQUIRED", "message": {"metadata": {
     "x402.payment.status": "payment-required",
     "x402.payment.required": {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "eip155:8453",
       "amount": "4000", "asset": "0x8335…", "payTo": "0x<seller>", "extra": {"nonce": "0x…"}}]},
     "aimarket": {"error": "payment_required", "needed": 0.004, "payment_ways": ["…"]}}}}
   ```

2. The client pays the seller on chain and replies **on the same task** (`taskId`, `contextId`):

   ```json
   {"messageId": "pay-1", "taskId": "a2at_…", "contextId": "…", "role": "ROLE_USER",
    "parts": [{"text": "x402 payment submitted"}],
    "metadata": {"x402.payment.status": "payment-submitted",
                 "x402.payment.payload": {"x402Version": 2, "scheme": "exact", "network": "eip155:8453",
                                          "payload": {"signature": "0x…", "authorization": {"…": "…"}},
                                          "txHash": "0x<settle transaction>"}}}
   ```

3. The task completes with `x402.payment.status: payment-completed` and
   `x402.payment.receipts: [{"success": true, "transaction": "0x…", "network": "eip155:8453", "payer": "0x…"}]`.

**Profile deviation — the one place this hub differs from the spec's reference flow.** The
reference merchant receives a signed EIP-3009 authorization and settles it through a
facilitator. This hub never settles: it holds no key and no gas and never custodies the money
(`settle.py`); it *verifies* a transfer the buyer already made to the listing's `payTo`. So
`x402.payment.payload` **must contain the settle `txHash`** (also read from `tx_hash`,
`transaction`, `settleTx`, `extra.txHash` or `payload.txHash`), or be a bare transaction hash. A
payload without one is answered `payment-failed` / `SETTLEMENT_FAILED` and the task stays
payable. A signed authorization may ride along and is then checked as well.

The invoice nonce the task was quoted is presented with the payment unless the payload names
its own. So is that invoice's secret. Redeeming a hub invoice needs `X-Payment-Secret`, the
`payment_secret` whose `sha256` is the nonce (the nonce and the transaction are public once
mined; see [`money-rails.md`](money-rails.md) §1.2), and the bridge presents it itself, from
the secret the invoice keeps (migration `040`). Clients need no change: the secret is never in
the task's status, metadata, history or stored invoke bytes, and the bridge ignores an
`X-Payment-Secret` the client sends. It presents the secret only for the task's **own** quoted
invoice — a payload naming another nonce gets none — so a task cannot redeem an invoice another
caller was quoted; whoever may continue the task (its id alone, for an anonymous one) is who
redeems its invoice. A task whose 402 came from a peer's own till has no secret at this hub to
present. The receipt is rebuilt from what the client sent and the hub accepted (the invoke
answer does not echo the settlement): `payer` appears only when the payload's authorization
names one. A rejected payment keeps the task in `INPUT_REQUIRED` with `payment-failed`, an
`x402.payment.error` (`DUPLICATE_NONCE`, `EXPIRED_PAYMENT`, `INVALID_SIGNATURE`,
`INVALID_AMOUNT`, `NETWORK_MISMATCH`, `SETTLEMENT_FAILED`) and fresh terms. Replying with
`x402.payment.status: payment-rejected` cancels the task.

A payment that was verified and then refused unserved (a missing input field, a provider
failure) stays redeemable: send the same payload again with the corrected invoke on the task,
and the same transaction buys it.

### Unpaid: the free trial

A caller that sends no key, no mandate and no payment is presented to the hub's trial ledger
under the same per-caller identity the MCP gateway uses (`mcp_gateway.visitor_for` of the
caller's address), so **one caller has one allowance whether it speaks MCP or A2A**. Only a
priced capability takes a trial. When the allowance is spent the same call is asked again
without the trial identity and the task shows the price (`INPUT_REQUIRED`,
`aimarket.trial_exhausted: true`).

## Task states

| Invoke answer | Task state | `status.message.metadata` |
|---|---|---|
| 200, delivered | `TASK_STATE_COMPLETED` | `payment-completed` + receipts when x402 paid |
| 402 with payable x402 terms | `TASK_STATE_INPUT_REQUIRED` | `payment-required`, `x402.payment.required` |
| 402 `payment_invalid`, 400 `payment_malformed` | `TASK_STATE_INPUT_REQUIRED` | `payment-failed`, `x402.payment.error`, fresh terms |
| 400 `incomplete_input` | `TASK_STATE_INPUT_REQUIRED` | `aimarket.missing` — reply with the complete invoke |
| 402 with no way to pay on chain, `mandate_required`; 401 (`invalid_api_key`, `mandate_proof_invalid`); a mandate whose account cannot pay | `TASK_STATE_AUTH_REQUIRED` | `aimarket.error` |
| 402 `mandate_limit` / `mandate_unfunded` / `listing_not_sellable`; 400, 403 (safety block, `mandate_scope`, `mandate_invalid`), 404, 409 `price_limit_exceeded`, 422 | `TASK_STATE_REJECTED` | `aimarket.error`, the hub's refusal as a data part |
| 429, 5xx, anything else | `TASK_STATE_FAILED` | `aimarket.error` |
| `payment-rejected` reply, or `CancelTask` while waiting | `TASK_STATE_CANCELED` | |

`INPUT_REQUIRED` and `AUTH_REQUIRED` wait for a follow-up message on the task: a payment, new
credentials in the HTTP headers, and/or a replacement invoke for the **same** capability (a
different product or capability is refused — start a new task). A mandated follow-up signs
either the replacement bytes or, with no raw part, the task's original bytes again (each proof
is single-use). A message to a finished task is `-32004`.

## Task management

- **Idempotency.** A retried `SendMessage` with the same `messageId` (per principal and
  `contextId`) returns the task the first attempt created and runs nothing — so a client that
  timed out behind a proxy does not pay twice. The same `messageId` with a different request is
  `-32602`. A retried follow-up with the same `messageId` likewise returns the task as it is.
  The claim is one conditional database statement: two concurrent copies cannot both run.
- **`GetTask`** (`id`, `historyLength`) and **`CancelTask`** (`id`) return the Task itself.
  Cancel works only on a waiting task (`INPUT_REQUIRED`, `AUTH_REQUIRED`); a running or finished
  one is `-32002`.
- **`ListTasks`** (`contextId`, `status`, `pageSize` ≤ 100, `pageToken`, `historyLength`,
  `statusTimestampAfter`, `includeArtifacts` — default false) returns the caller's own tasks,
  newest first, with `nextPageToken` (empty on the last page), `pageSize` and `totalSize`.
- **Who may see a task.** A task belongs to whoever created it: a credit account, a mandate, or
  an anonymous caller. An anonymous task is held by its unguessable id alone (like
  `/ai-market/v2/jobs/{id}`), and anyone presenting the id may pay for it. A task bound to an
  account needs that account's `X-API-Key`; one bound to a mandate needs a fresh
  `X-AIMarket-Mandate-Proof` signed over **`POST /a2a`** and the exact JSON-RPC body. Anyone else
  gets `-32001`. Anonymous callers list nothing.
- **What is stored.** Never an API key, a mandate proof, an x402 payload or a payment secret. The invoke bytes
  (base64) only while the task can resume, purged at a terminal state or expiry; their sha256
  stays. History keeps what was asked (product, capability, source hub, the payment *status*),
  not the input. Result and receipts stay as artifacts.
- **Expiry.** A waiting task fails after 15 minutes (`AIMARKET_A2A_TASK_TTL_S`) and forgets its
  input; a task left `WORKING` by a restart is marked `FAILED` after 30 minutes (its money settled
  on its own — check receipts and balance). Finished tasks are deleted after 7 days
  (`AIMARKET_A2A_TASK_RETENTION_S`).

## Not supported

Streaming (`SendStreamingMessage`, `SubscribeToTask`) and push notifications answer their
standard errors; `returnImmediately` is ignored (every call blocks until the task finishes or
waits for the client). URL parts are refused. Payment channels are not accepted over A2A (pay
with credits, a mandate or x402). Provider-side job headers (`X-AIMarket-Job`, `…-Grant`) are
refused rather than dropped: a provider buying inside a job does so at `/ai-market/v2/invoke`,
where the job tree is joined. A root buyer on A2A still opens a job with an allowance — only the
root changes protocol ([subcontracting.md](subcontracting.md#subcontracting-over-a2a)).

## Our own clients

- **Python** — `aimarket_agent.A2AClient` (aimarket-agent ≥ 2.4.0): `card()`, `search()`,
  `invoke()`, `pay_x402()`, `reject_payment()`, `resume()`, `get_task()`, `list_tasks()`,
  `cancel()`; receipts are verified against their signer's key (`receipt_verified`). The hub's
  own suite drives it against the real app: `tests/test_a2a_invoke.py`.
- **ARGUS** — `src/economy/a2a-client.ts` and the CLI:

  ```bash
  argus a2a search "weather at a sensor"
  argus a2a invoke gaia.weather.read@v1 --product gaia.gateway --input '{"device_id":"om-wx-01"}' --max-price 0.01
  argus a2a task a2at_…
  ```

  It pays with the owner's mandate when `ARGUS_MANDATE_FILE` and `ARGUS_AGENT_KEY` are set,
  else with `ARGUS_HUB_API_KEY`, else on the free trial.

## Operator

| Variable | Default | Effect |
|---|---|---|
| `AIMARKET_A2A_INVOKE` | `1` | `0` keeps `/a2a` discovery-only; the card then lists search alone. |
| `AIMARKET_A2A_TASK_TTL_S` | `900` | How long a task waits for payment or credentials. |
| `AIMARKET_A2A_TASK_RETENTION_S` | `604800` | How long a finished task stays readable. |

`SendMessage` blocks for as long as the invoke runs — up to ~300 s for a Pay-on-Verified call —
so a reverse proxy in front of the hub needs the same read timeout on `/a2a` as on
`/ai-market/v2/invoke`. A client cut off by a proxy loses only the answer: retrying with the same
`messageId` returns the task, it does not pay again.

Tasks live in the hub database (`a2a_tasks`, migration 036). Traffic is counted in
`aimarket_hub_a2a_requests_total{method,result}`; an invoke task is counted by the state it
reached (`completed`, `input_required`, `auth_required`, `rejected`, `failed`, `canceled`), and
the internal invoke is counted by the invoke metrics like any other.
