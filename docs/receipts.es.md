# Qué prueba una respuesta de invoke y cómo comprobarlo

[EN](receipts.md) · [RU](receipts.ru.md) · [FR](receipts.fr.md) · [ZH](receipts.zh.md)

Una respuesta entregada de `POST /ai-market/v2/invoke` lleva hasta tres objetos firmados. Cada uno se compromete con cosas distintas.

| Objeto | Firmado por | Se compromete con |
|---|---|---|
| `receipt` | el hub que ejecutó la capacidad (en una llamada enrutada, el proveedor) | nonce, producto, capacidad, precio, hora, éxito, latencia ([especificación §7.3.3](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md)); en un rechazo, también el motivo y el reembolso (§7.3.4) |
| `receipt_binding` | el hub que te respondió | el `input` de tu solicitud, el `result` u `output` de la respuesta, tu propio nonce y a qué recibo corresponde (§7.3.9) |
| `provenance_receipt` | el hub que te respondió (AWR/2, `eddsa-jcs-2022`) | resúmenes de entrada y salida, hub proveedor, precio liquidado y, en una llamada enrutada, el recibo del proveedor por resumen |

## Envía tu propio nonce

```bash
curl -s https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' -H 'X-Client-Nonce: my-task-0001' \
  -d '{"product_id":"…","capability_id":"…","input":{…}}'
```

El valor (8–128 caracteres de `A-Z a-z 0-9 . _ -`) vuelve en `X-Client-Nonce-Echo` y dentro del campo firmado `receipt_binding.client_nonce`. Un hub no puede darte una vinculación firmada antes para una llamada idéntica, porque no contendría tu nonce. Se acepta `X-Agent-Nonce`, la cabecera que leen los recibos AFTA de TensorFeed, con el mismo significado. Un valor mal formado se rechaza con 400 antes de cualquier trabajo o cobro.

## Compruébalo tú mismo

`input_sha256` y `output_sha256` son SHA-256 de JSON con claves ordenadas, separadores compactos y caracteres no ASCII tal cual:

```python
import hashlib, json
h = lambda v: hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
b = answer["receipt_binding"]
assert b["input_sha256"] == h(my_input)
assert b["output_sha256"] == h(answer[b["output_member"]])
assert b["client_nonce"] == "my-task-0001"
assert b["receipt_nonce"] == answer["receipt"]["nonce"]
```

La firma es Ed25519 sobre `receipt_binding:1|` seguido del SHA-256 hex de los nueve campos `v, hub, receipt_nonce, receipt_signer, input_sha256, output_member, output_sha256, client_nonce, timestamp`, serializados igual. Verifícala con la clave que el hub publica en `/.well-known/ai-market.json` (`signer_public_key`), no con la clave que trae la firma. El ejemplo normativo es [`receipt-binding-signed.json`](https://github.com/alexar76/aimarket-protocol/blob/main/test-vectors/receipt-binding-signed.json).

La credencial de procedencia indica qué cubre su `outputDigest` en el campo firmado `outputDigestOf` (`result` u `output`). En una llamada enrutada añade `gatewayReceiptDigest`, el resumen del `receipt` del proveedor. Ambos se recalculan a partir de la respuesta que tienes, con la serialización indicada en `payloadSerialization`.

## O pregúntale al hub

```bash
curl -s https://modelmarket.dev/ai-market/v2/receipts/verify -H 'Content-Type: application/json' \
  -d '{"receipt": …, "response": <la respuesta completa>, "input": {…}, "client_nonce": "my-task-0001"}'
```

El hub comprueba el recibo con sus propias claves o con la clave que fijó para un par activo de la federación, así que también verifica el recibo del proveedor en una respuesta enrutada, cuando el recibo nombra una clave fijada o cuando envías con él la respuesta (o su `receipt_binding`); los recibos propios de un hub no nombran clave. Nunca usa una clave tomada de la solicitud. Con `response`, `input` y `client_nonce` comprueba además la vinculación: firma, hub, recibo, entrada, salida y nonce, cada uno por separado. `GET /ai-market/v2/p/provenance/verify/{receipt_id}` comprueba la credencial de procedencia.

## Límites

- **Reintentos.** Tu nonce forma parte de la petición para `Idempotency-Key` y para una llamada con escrow: un reintento con el mismo cuerpo y el mismo nonce recibe la primera respuesta con su vínculo; otro nonce con la misma clave se rechaza (422 `idempotency_key_reused`). Reutiliza el nonce al reintentar.
- **Números.** Los resúmenes escriben los números como Python (`21.0`, `1e-07`). Si tu analizador JSON convierte `21.0` en `21`, no recalcules sobre lo que vuelve a serializar: envía el texto del cuerpo de la respuesta tal como lo recibiste, sin analizarlo, como `response` a `/receipts/verify` (arriba), para que el hub lo analice como lo resumió.
- **Aún no cubierto.** Una respuesta con escrow reanudada tras un cobro interrumpido vuelve sin vínculo, y los artefactos de tareas A2A no lo llevan. Una respuesta sin `result` ni `output` no tiene vínculo.
- En una llamada enrutada cuyo recibo no nombra clave, `receipt_signer` es la clave que el hub que responde fijó para el proveedor al que llamó; si no conoce ninguna, es `null` y no su propia clave.

## Por qué existe

El 30-10-2026 verificamos con un solo script los recibos AFTA firmados de [TensorFeed](https://tensorfeed.ai) y los nuestros. Los suyos vinculaban la solicitud, la respuesta y el nonce del cliente; el nuestro, nada de eso. Nuestra credencial de procedencia enrutada también resumía una respuesta que seguía creciendo después, así que ningún comprador podía recalcularla. Ambos quedan corregidos aquí; la comparación está en `docs/evidence/tensorfeed-receipts-2026-10-30/` del monorrepositorio.
