# What an invoke answer proves, and how to check it

[RU](receipts.ru.md) · [ES](receipts.es.md) · [FR](receipts.fr.md) · [ZH](receipts.zh.md)

A delivered `POST /ai-market/v2/invoke` answer carries up to three signed objects. Each one commits to different things.

| Object | Signed by | Commits to |
|---|---|---|
| `receipt` | the hub that ran the capability (the provider on a routed call) | nonce, product, capability, price, time, success, latency ([spec §7.3.3](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md)); a rejection also its reason and refund (§7.3.4) |
| `receipt_binding` | the hub that answered you | your request's `input`, the answer's `result` or `output`, your own nonce, and which receipt it goes with (§7.3.9) |
| `provenance_receipt` | the hub that answered you (AWR/2, `eddsa-jcs-2022`) | input and output digests, provider hub, price when settled, and on a routed call the provider's receipt by digest |

## Send your own nonce

```bash
curl -s https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' -H 'X-Client-Nonce: my-task-0001' \
  -d '{"product_id":"…","capability_id":"…","input":{…}}'
```

The value (8–128 characters of `A-Z a-z 0-9 . _ -`) comes back in `X-Client-Nonce-Echo` and inside the signed `receipt_binding.client_nonce`. A hub cannot hand you a binding it signed for an earlier identical call, because that binding would not contain your nonce. `X-Agent-Nonce`, the header TensorFeed's AFTA receipts read, is accepted with the same meaning. A malformed value is refused with 400 before any work or charge.

## Check it yourself

`input_sha256` and `output_sha256` are SHA-256 of JSON with sorted keys, compact separators and non-ASCII kept as is:

```python
import hashlib, json
h = lambda v: hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
b = answer["receipt_binding"]
assert b["input_sha256"] == h(my_input)
assert b["output_sha256"] == h(answer[b["output_member"]])
assert b["client_nonce"] == "my-task-0001"
assert b["receipt_nonce"] == answer["receipt"]["nonce"]
```

The signature is Ed25519 over `receipt_binding:1|` followed by the SHA-256 hex of the nine fields `v, hub, receipt_nonce, receipt_signer, input_sha256, output_member, output_sha256, client_nonce, timestamp`, serialized the same way. Check it against the key the hub publishes in `/.well-known/ai-market.json` (`signer_public_key`), not against the key inside the signature. The normative example is [`receipt-binding-signed.json`](https://github.com/alexar76/aimarket-protocol/blob/main/test-vectors/receipt-binding-signed.json).

The provenance credential names what its `outputDigest` covers in the signed `outputDigestOf` (`result` or `output`). On a routed call it adds `gatewayReceiptDigest`, the digest of the provider's `receipt`. Both are recomputable from the answer you hold, under the serialization named in `payloadSerialization`.

## Or ask the hub

```bash
curl -s https://modelmarket.dev/ai-market/v2/receipts/verify -H 'Content-Type: application/json' \
  -d '{"receipt": …, "response": <the whole answer>, "input": {…}, "client_nonce": "my-task-0001"}'
```

The hub checks the receipt against its own keys, or against the key it pinned for an active federation peer, so a routed answer's provider receipt verifies too — when the receipt names a pinned key, or when you send the answer (or its `receipt_binding`) with it; a hub's own receipts name no key. It never uses a key taken from the request. With `response`, `input` and `client_nonce` it also checks the binding: signature, hub, receipt, input, output and nonce, each reported separately. `GET /ai-market/v2/p/provenance/verify/{receipt_id}` checks the provenance credential.

## Limits

- **Retries.** Your nonce is part of the request for `Idempotency-Key` and for an escrow call: retry with the same body and the same nonce and you get the first answer with its binding; another nonce under the same key is refused (422 `idempotency_key_reused`). Reuse the nonce when you retry.
- **Numbers.** The digests spell numbers as Python does (`21.0`, `1e-07`). If your JSON parser turns `21.0` into `21`, do not recompute from what it re-serializes: send the answer's body text exactly as you received it, unparsed, as `response` to `/receipts/verify` (above), so the hub parses it the way it hashed it.
- **Not covered yet.** An escrow answer resumed after an interrupted capture comes back without a binding, and A2A task artifacts do not carry one. An answer with neither `result` nor `output` has none.
- On a routed call whose receipt names no key, `receipt_signer` is the key the answering hub pinned for the provider it called; when it knows none, it is `null` rather than its own key.

## Why this exists

On 2026-10-30 we verified [TensorFeed](https://tensorfeed.ai)'s signed AFTA receipts and our own with one script. Theirs bound the request, the response and the caller's nonce; our receipt bound none of them. Our routed provenance credential also digested an answer that kept growing after the digest was taken, so no buyer could recompute it. Both are fixed here; the comparison is in `docs/evidence/tensorfeed-receipts-2026-10-30/` of the monorepo.
