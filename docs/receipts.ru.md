# Что доказывает ответ invoke и как это проверить

[EN](receipts.md) · [ES](receipts.es.md) · [FR](receipts.fr.md) · [ZH](receipts.zh.md)

Успешный ответ `POST /ai-market/v2/invoke` несёт до трёх подписанных объектов. Каждый из них фиксирует своё.

| Объект | Кто подписывает | Что фиксирует |
|---|---|---|
| `receipt` | хаб, который выполнил функцию (при маршрутизации это хаб-исполнитель) | nonce, продукт, функцию, цену, время, успех, задержку ([спецификация §7.3.3](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md)); для отказа ещё причину и возврат (§7.3.4) |
| `receipt_binding` | хаб, который ответил вам | `input` вашего запроса, `result` или `output` ответа, ваш собственный nonce и то, к какой квитанции он относится (§7.3.9) |
| `provenance_receipt` | хаб, который ответил вам (AWR/2, `eddsa-jcs-2022`) | дайджесты входа и выхода, хаб-исполнитель, цену после расчёта, а при маршрутизации дайджест квитанции исполнителя |

## Отправьте свой nonce

```bash
curl -s https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' -H 'X-Client-Nonce: my-task-0001' \
  -d '{"product_id":"…","capability_id":"…","input":{…}}'
```

Значение (8–128 символов из `A-Z a-z 0-9 . _ -`) вернётся в заголовке `X-Client-Nonce-Echo` и внутри подписанного `receipt_binding.client_nonce`. Хаб не сможет отдать вам привязку, подписанную раньше для такого же вызова: в ней не будет вашего nonce. Заголовок `X-Agent-Nonce`, который читают квитанции AFTA у TensorFeed, принимается с тем же смыслом. Неверное значение отклоняется с кодом 400 до любой работы и списания.

## Проверьте сами

`input_sha256` и `output_sha256` — это SHA-256 от JSON с отсортированными ключами, компактными разделителями и не-ASCII символами как есть:

```python
import hashlib, json
h = lambda v: hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
b = answer["receipt_binding"]
assert b["input_sha256"] == h(my_input)
assert b["output_sha256"] == h(answer[b["output_member"]])
assert b["client_nonce"] == "my-task-0001"
assert b["receipt_nonce"] == answer["receipt"]["nonce"]
```

Подпись — Ed25519 над строкой `receipt_binding:1|`, за которой идёт SHA-256 (hex) девяти полей `v, hub, receipt_nonce, receipt_signer, input_sha256, output_member, output_sha256, client_nonce, timestamp`, сериализованных так же. Проверяйте её ключом, который хаб публикует в `/.well-known/ai-market.json` (`signer_public_key`), а не ключом из самой подписи. Нормативный пример — [`receipt-binding-signed.json`](https://github.com/alexar76/aimarket-protocol/blob/main/test-vectors/receipt-binding-signed.json).

Provenance-квитанция сама называет, от чего посчитан её `outputDigest`: подписанное поле `outputDigestOf` (`result` или `output`). При маршрутизации добавляется `gatewayReceiptDigest` — дайджест `receipt` исполнителя. Оба пересчитываются из ответа, который у вас на руках, по правилу из `payloadSerialization`.

## Или спросите хаб

```bash
curl -s https://modelmarket.dev/ai-market/v2/receipts/verify -H 'Content-Type: application/json' \
  -d '{"receipt": …, "response": <весь ответ>, "input": {…}, "client_nonce": "my-task-0001"}'
```

Хаб проверяет квитанцию своими ключами или ключом, закреплённым за активным пиром федерации, поэтому квитанция исполнителя из маршрутизированного ответа тоже проверяется — если квитанция называет закреплённый ключ или вы отправляете вместе с ней ответ (или его `receipt_binding`); собственные квитанции хаба ключа не называют. Ключ из запроса он не использует никогда. Если переданы `response`, `input` и `client_nonce`, он проверяет и привязку: подпись, хаб, квитанцию, вход, выход и nonce, каждый пункт отдельно. Provenance-квитанцию проверяет `GET /ai-market/v2/p/provenance/verify/{receipt_id}`.

## Ограничения

- **Повторы.** Ваш nonce входит в запрос для `Idempotency-Key` и для escrow-вызова: повтор с тем же телом и тем же nonce получает первый ответ с его привязкой, а другой nonce под тем же ключом отклоняется (422 `idempotency_key_reused`). При повторе отправляйте тот же nonce.
- **Числа.** Дайджесты записывают числа так, как это делает Python (`21.0`, `1e-07`). Если ваш JSON-парсер превращает `21.0` в `21`, не пересчитывайте по тому, что он сериализует заново: отправьте текст тела ответа ровно таким, каким его получили, без разбора, как `response` в `/receipts/verify` (выше) — хаб разберёт его так же, как хешировал.
- **Пока не покрыто.** Escrow-ответ, возобновлённый после прерванного списания, приходит без привязки; артефакты A2A-задач её не несут. У ответа без `result` и `output` привязки нет.
- При маршрутизированном вызове, если квитанция не называет ключ, `receipt_signer` — ключ, который отвечающий хаб закрепил за вызванным провайдером; если такого ключа нет, там `null`, а не собственный ключ хаба.

## Зачем это

30.10.2026 мы проверили одним скриптом подписанные квитанции AFTA у [TensorFeed](https://tensorfeed.ai) и наши. Их квитанция связывала запрос, ответ и nonce вызывающего, наша — ничего из этого. Кроме того, наша provenance-квитанция при маршрутизации считала дайджест от ответа, который после этого продолжал расти, и покупатель не мог его пересчитать. Оба дефекта исправлены здесь; сравнение лежит в `docs/evidence/tensorfeed-receipts-2026-10-30/` монорепозитория.
