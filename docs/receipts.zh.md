# invoke 应答证明了什么，以及如何核验

[EN](receipts.md) · [RU](receipts.ru.md) · [ES](receipts.es.md) · [FR](receipts.fr.md)

`POST /ai-market/v2/invoke` 成功交付的应答最多带有三个签名对象，各自承诺的内容不同。

| 对象 | 签名方 | 承诺的内容 |
|---|---|---|
| `receipt` | 实际运行该能力的 hub（路由调用时为提供方） | nonce、产品、能力、价格、时间、是否成功、延迟（[规范 §7.3.3](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md)）；拒绝时还包括原因和退款（§7.3.4） |
| `receipt_binding` | 回应你的 hub | 你请求中的 `input`、应答中的 `result` 或 `output`、你自己的 nonce，以及它对应哪张回执（§7.3.9） |
| `provenance_receipt` | 回应你的 hub（AWR/2，`eddsa-jcs-2022`） | 输入与输出摘要、提供方 hub、结算后的价格；路由调用时还有提供方回执的摘要 |

## 发送你自己的 nonce

```bash
curl -s https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' -H 'X-Client-Nonce: my-task-0001' \
  -d '{"product_id":"…","capability_id":"…","input":{…}}'
```

该值（8–128 个 `A-Z a-z 0-9 . _ -` 字符）会在 `X-Client-Nonce-Echo` 中返回，并写入已签名的 `receipt_binding.client_nonce`。hub 无法把先前为相同调用签过的绑定交给你，因为那份绑定里没有你的 nonce。TensorFeed 的 AFTA 回执所读取的 `X-Agent-Nonce` 头也被接受，含义相同。格式错误的值会在任何执行或扣费之前以 400 拒绝。

## 自行核验

`input_sha256` 与 `output_sha256` 是对 JSON 的 SHA-256：键排序、紧凑分隔符、非 ASCII 字符原样保留：

```python
import hashlib, json
h = lambda v: hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
b = answer["receipt_binding"]
assert b["input_sha256"] == h(my_input)
assert b["output_sha256"] == h(answer[b["output_member"]])
assert b["client_nonce"] == "my-task-0001"
assert b["receipt_nonce"] == answer["receipt"]["nonce"]
```

签名是 Ed25519，签名内容为 `receipt_binding:1|` 加上九个字段 `v, hub, receipt_nonce, receipt_signer, input_sha256, output_member, output_sha256, client_nonce, timestamp` 按同样方式序列化后的 SHA-256 十六进制值。请用 hub 在 `/.well-known/ai-market.json` 中公布的密钥（`signer_public_key`）核验，而不是签名对象里自带的密钥。规范示例见 [`receipt-binding-signed.json`](https://github.com/alexar76/aimarket-protocol/blob/main/test-vectors/receipt-binding-signed.json)。

溯源凭证在已签名字段 `outputDigestOf`（`result` 或 `output`）中说明其 `outputDigest` 覆盖的内容。路由调用时还会加入 `gatewayReceiptDigest`，即提供方 `receipt` 的摘要。两者都可按 `payloadSerialization` 指明的序列化方式，从你手中的应答重新计算。

## 或者让 hub 核验

```bash
curl -s https://modelmarket.dev/ai-market/v2/receipts/verify -H 'Content-Type: application/json' \
  -d '{"receipt": …, "response": <完整应答>, "input": {…}, "client_nonce": "my-task-0001"}'
```

hub 用自己的密钥，或用它为联邦中活跃对等方固定的密钥核验回执，因此路由应答中提供方的回执也能核验——前提是回执写明了已固定的密钥，或你把应答（或其 `receipt_binding`）一并发送；hub 自己的回执不写密钥。它从不使用请求中提供的密钥。若同时提供 `response`、`input` 和 `client_nonce`，它还会核验绑定：签名、hub、回执、输入、输出和 nonce，逐项分别报告。`GET /ai-market/v2/p/provenance/verify/{receipt_id}` 用于核验溯源凭证。

## 限制

- **重试。** 对 `Idempotency-Key` 和托管（escrow）调用而言，你的 nonce 是请求的一部分：用相同的请求体和相同的 nonce 重试，会得到第一次的应答及其绑定；同一个键换一个 nonce 会被拒绝（422 `idempotency_key_reused`）。重试时请沿用原来的 nonce。
- **数字。** 摘要按 Python 的方式书写数字（`21.0`、`1e-07`）。如果你的 JSON 解析器把 `21.0` 变成 `21`，不要用它重新序列化的结果来重算：把收到的应答正文原样（不解析）作为 `response` 发给 `/receipts/verify`（见上文），由 hub 按其计算哈希时的方式解析。
- **尚未覆盖。** 扣款中断后恢复的托管应答不带绑定，A2A 任务的产物也不带绑定。既没有 `result` 也没有 `output` 的应答没有绑定。
- 在回执未写明密钥的路由调用中，`receipt_signer` 是应答 hub 为其所调用的提供方固定的密钥；如果它不知道任何密钥，该字段为 `null`，而不是它自己的密钥。

## 为什么要这样做

2026-10-30，我们用同一个脚本核验了 [TensorFeed](https://tensorfeed.ai) 的签名 AFTA 回执和我们自己的回执。他们的回执绑定了请求、应答和调用方 nonce，我们的回执一项都没有绑定。我们的路由溯源凭证所摘要的应答在摘要之后还在继续增长，买方无法重新计算。两个问题都在此修复；对比材料见单体仓库的 `docs/evidence/tensorfeed-receipts-2026-10-30/`。
