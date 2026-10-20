# Comprar créditos con tarjeta (Stripe Checkout)

> **English:** [card-topup.md](./card-topup.md) · **Русский:** [card-topup.ru.md](./card-topup.ru.md) · **Français:** [card-topup.fr.md](./card-topup.fr.md) · **中文:** [card-topup.zh.md](./card-topup.zh.md)
>
> Código: [`card_topup.py`](../aimarket_hub/card_topup.py) · rutas en [`hub_routes/accounts.py`](../aimarket_hub/hub_routes/accounts.py) · [`credits.py`](../aimarket_hub/credits.py) (`deposit`) · pruebas [`test_card_topup.py`](../tests/test_card_topup.py)

La [recarga con USDC](credits-topup.es.md) permite a un agente con cartera financiar su cuenta de
créditos. Una persona con tarjeta y sin cartera no podía. Esta puerta acepta una tarjeta como
cualquier tienda y deja el dinero en la **misma** línea del libro que una recarga USDC
(`CreditsLedger.deposit`, tipo `topup`); después, todo lo del carril de créditos — llamadas de pago,
[mandatos](mandates.es.md), subcontratación, A2A — funciona igual.

**Estado.** En el hub desde 2026-10-20 (3.16.0), **desactivado por defecto**. Probado contra un
sandbox real de Stripe el 2026-10-20: se abrió un pago de $5, se pagó con una tarjeta de prueba, el
webhook acreditó exactamente una vez y un reenvío del mismo evento por Stripe no acreditó nada. Esa
prueba también encontró un fallo que las pruebas unitarias no podían ver (el cliente HTTP enviaba el
formulario de una forma que httpx 0.28 rechaza); ahora una prueba ejecuta el código real del
cliente. Los modos de impuesto de abajo están cubiertos por pruebas con Stripe simulado en su borde
HTTP; en el sandbox se probó el modo por defecto (`off`).

## Contenido

- [En breve](#en-breve)
- [Qué ve el comprador](#qué-ve-el-comprador)
- [Qué comprueba el hub](#qué-comprueba-el-hub)
- [Impuesto sobre las ventas: tres modos](#impuesto-sobre-las-ventas-tres-modos)
- [Un webhook que nunca llegó](#un-webhook-que-nunca-llegó)
- [Reembolsos y disputas](#reembolsos-y-disputas)
- [Límites y ajustes](#límites-y-ajustes)
- [Lo que esta puerta no hace](#lo-que-esta-puerta-no-hace)
- [Operador](#operador)
- [Probar en el sandbox de Stripe, paso a paso](#probar-en-el-sandbox-de-stripe-paso-a-paso)

## En breve

```bash
# 1. Abrir un pago de $10 de crédito para su cuenta.
curl -s -X POST $HUB/ai-market/v2/account/topup/card -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' -d '{"amount_usd": 10}'
# → 201 {"topup_id": "ctp_…", "checkout_url": "https://checkout.stripe.com/…", "status": "open",
#        "amount_usd": 10, "charge_usd": 10, "fee_usd": 0, "expires_at": "…", "status_url": "…"}

# 2. Una persona abre checkout_url y paga. Stripe la devuelve a status_url.

# 3. El crédito llega cuando Stripe confirma; el estado se puede leer en cualquier momento:
curl -s $HUB/ai-market/v2/card-topups/ctp_…
```

Un agente no puede pagar con tarjeta por sí mismo: entrega `checkout_url` a su persona y espera (o
consulta el estado). Ese es el sentido de esta puerta: paga la persona y el agente gasta dentro de
su [mandato](mandates.es.md).

## Qué ve el comprador

Una página alojada por Stripe (`checkout.stripe.com`) con una línea: «Prepaid credit on <host del
hub>: $10.00» (más «incl. $x card fee» si el operador fija un recargo). Con un modo de impuesto
activo, la página pide además una dirección de facturación y muestra el impuesto; con Managed
Payments aparecen los términos y el nombre de vendedor de Stripe. La página caduca en **una hora**;
un pago nuevo está a una petición. Tras pagar, Stripe lleva al comprador a `status_url`, que responde
en JSON con el estado de la recarga.

Ningún número de tarjeta llega al hub: nunca lo ve. El alcance PCI queda en Stripe.

## Qué comprueba el hub

Stripe llama a `POST /ai-market/v2/stripe/webhook`. Antes de leer nada del evento, la
`Stripe-Signature` debe verificarse: HMAC-SHA256 sobre `t.payload` con el secreto del endpoint, `t`
de menos de cinco minutos, comparación en tiempo constante (se aceptan varias firmas `v1` mientras se
rota un secreto). Después, la sesión del evento se compara con la fila que abrió el hub — nunca al
revés:

| Comprobación | Por qué |
|---|---|
| el id de recarga es uno que abrió este hub | un evento nombra una recarga; no puede crearla |
| el id de sesión es el que Stripe dio al abrirla | una sesión pagada de otro pago no puede presentarse por esta |
| `payment_status == "paid"` | una sesión completada pero no pagada (asíncrona) aún no acredita nada |
| `currency == "usd"` y los importes coinciden con el cargo pedido por el hub, según el modo de impuesto con el que se abrió el pago ([abajo](#impuesto-sobre-las-ventas-tres-modos)) | el importe y la cuenta son del hub, no del evento |
| con una clave real (`sk_live_…`, `rk_live_…`), la sesión es `livemode` | el dinero de prueba nunca acredita un hub real |

El crédito va a la cuenta vinculada al abrir el pago, por el importe pedido. Ni el recargo por
tarjeta ni el impuesto se acreditan. La línea del libro usa el id de sesión como referencia, así que
un evento reenviado — o dos entregas a la vez — acredita una sola vez. Stripe conoce el id de la
recarga, nunca la cuenta.

Los eventos sobre otra cosa se reconocen (`200`) y se ignoran, para que Stripe no los reintente
durante días. `checkout.session.expired` cierra la fila.

## Impuesto sobre las ventas: tres modos

Quién calcula y debe el impuesto sobre las ventas (IVA/VAT, GST, sales tax de EE. UU.) lo decide el
operador. Es un solo ajuste, `AIMARKET_CARD_TOPUP_TAX`, y el hub guarda el modo **en la fila de cada
pago**: un pago abierto con un modo se comprueba con ese modo aunque el operador cambie el ajuste
antes de que se pague.

| Modo | Vendedor | Quién calcula el impuesto | Quién lo declara y paga | Comisión de Stripe además de la de tarjeta |
|---|---|---|---|---|
| `off` (por defecto) | el operador | nadie, en Stripe | el operador, fuera de Stripe | — |
| `stripe_tax` | el operador | Stripe Tax, por la dirección del comprador | el operador (Stripe Tax prepara los informes) | tarifa de Stripe Tax |
| `managed_payments` | **Stripe** (comerciante registrado, merchant of record) | Stripe | Stripe | 3,5 % por operación |

**Qué cambia en el pago que abre el hub:**

| Modo | Parámetros extra de la Checkout Session | Versión de la API |
|---|---|---|
| `off` | ninguno | `2024-06-20` |
| `stripe_tax` | `automatic_tax[enabled]=true`, `billing_address_collection=required`, `price_data[tax_behavior]`, `product_data[tax_code]` | `2024-06-20` |
| `managed_payments` | `managed_payments[enabled]=true`, `price_data[tax_behavior]`, `product_data[tax_code]`. Sin `automatic_tax`, `billing_address_collection` ni parámetros de métodos de pago: Stripe los rechaza junto a Managed Payments, porque recoge la dirección y elige los métodos por sí mismo. | `2025-03-31.basil` (la primera con Managed Payments); el hub también relee ese pago con ella |

**Impuesto encima o incluido.** `AIMARKET_CARD_TOPUP_TAX_BEHAVIOR`:

- `exclusive` (por defecto): el impuesto se suma encima. Una recarga de $10 para un comprador de un
  país con IVA del 21 % le cuesta $12.10; el hub comprueba que el `amount_subtotal` de la sesión es
  exactamente $10 y que `amount_total` es exactamente `amount_subtotal + total_details.amount_tax`
  — ningún descuento ni otra línea puede moverlo. Se acreditan $10; los $2.10 quedan en la fila como `tax_usd`.
- `inclusive`: el impuesto está dentro del precio. El comprador paga $10, el impuesto va incluido y
  el operador recibe menos; el hub comprueba que `amount_total` es exactamente $10 y que el impuesto
  no es negativo. Se acreditan $10.

**El código fiscal del producto**: `AIMARKET_CARD_TOPUP_TAX_CODE`, por defecto `txcd_10000000`
(«General — Electronically Supplied Services»). Para un marketplace de capacidades de IA pueden
encajar mejor `txcd_10105002` («IA como servicio — en la nube — uso empresarial») o `txcd_10701410`
(«Servicios de información entregados electrónicamente — uso empresarial»); el código correcto es
una pregunta para el asesor fiscal del operador. Con Managed Payments, el código debe estar en la
lista de Stripe «Eligible for Managed Payments».

**Un ajuste que el hub no entiende cierra la puerta** en lugar de abrir pagos a ciegas: un modo
desconocido, un código fiscal que no sea `txcd_` + 8 cifras o un comportamiento distinto de
`exclusive`/`inclusive` hacen que el well-known diga `"enabled": false` con el motivo.

**Qué ven el comprador y el well-known.** `payment_rails.credits.topup_card.tax` es
`{"mode": "off"}` o `{"mode", "behavior", "seller_of_record": "operator" | "stripe"}`. Una vez
pagada, el estado de la recarga incluye `tax: {mode, behavior, tax_usd}`.

### Managed Payments: leer antes de activarlo

Stripe decide quién puede usarlo, y sus condiciones importan aquí:

- **Solo ventas directas.** La página de elegibilidad de Stripe dice que el producto debe venderse
  «directamente a los clientes, no a través de una plataforma o marketplace». El hub es un
  marketplace: el crédito comprado aquí se gasta en capacidades de proveedores independientes. Si
  Stripe acepta crédito prepago de un marketplace en Managed Payments lo decide Stripe; pregunte a
  Stripe antes de contar con ello.
- **Solo productos digitales y automatizados**, con un código fiscal elegible; empresa en un país
  admitido (la lista incluye los países de la UE, Reino Unido, Suiza, Noruega, EE. UU., Canadá,
  Australia, Hong Kong, Japón y Singapur).
- **Stripe puede reembolsar** una compra en 60 días para evitar un contracargo, y aplica las reglas
  de desistimiento. Cada reembolso llega al hub como `charge.refunded` ([abajo](#reembolsos-y-disputas)).
- Actívelo en el Dashboard (Settings → Managed Payments) antes de fijar el modo, o Stripe rechazará
  abrir el pago (`502 stripe_refused`, sin cargo).

### Stripe Tax: antes de activarlo

En el Dashboard: Tax → ajustes (dirección de origen, código fiscal por defecto) y **registros**:
Stripe Tax solo cobra impuesto donde el operador está registrado. Sin registro para el país del
comprador, el impuesto es cero, y el hub lo acepta (`amount_tax: 0`).

## Un webhook que nunca llegó

`POST /ai-market/v2/card-topups/{id}/refresh` pide la sesión a Stripe y aplica las mismas
comprobaciones. Puede llamarlo quien tenga el id (con límite por llamante): solo puede acreditar la
cuenta vinculada al pago. Así, una recarga pagada nunca se pierde por una entrega perdida.

## Reembolsos y disputas

- **`charge.refunded`**: un reembolso hecho en Stripe (por el operador o, con Managed Payments, por
  el propio Stripe) marca la recarga como `refunded` y registra un error con la cuenta y el importe.
  El crédito **no** se retira automáticamente: puede estar ya gastado, y dejar un saldo en negativo
  es una decisión, no un reflejo. El operador lo corrige a mano.
- **`charge.dispute.created`**: un contracargo marca la recarga como `disputed` y registra un error.
  La cuenta no se congela automáticamente.

## Límites y ajustes

| Variable | Por defecto | Efecto |
|---|---|---|
| `AIMARKET_CARD_TOPUP_MIN_USD` | `5` | Recarga mínima (nunca por debajo de los $0.50 de Stripe). |
| `AIMARKET_CARD_TOPUP_MAX_USD` | `100` | Recarga máxima por pago. |
| `AIMARKET_CARD_TOPUP_DAILY_USD` | `200` | Lo máximo que una cuenta puede comprar con tarjeta en 24 h. Los pagos abiertos cuentan hasta que caducan (1 hora), porque cualquiera puede pagarse aún. |
| `AIMARKET_CARD_TOPUP_FEE_PCT` | `0` | Recargo en porcentaje, mostrado antes de pagar y no acreditado. `0`: el operador asume la comisión de la tarjeta. Donde los recargos por tarjeta estén restringidos, deje `0`. |
| `AIMARKET_CARD_TOPUP_FEE_FIXED_USD` | `0` | Recargo fijo por pago. |
| `AIMARKET_CARD_TOPUP_TAX` | `off` | `off`, `stripe_tax` o `managed_payments` ([arriba](#impuesto-sobre-las-ventas-tres-modos)). |
| `AIMARKET_CARD_TOPUP_TAX_BEHAVIOR` | `exclusive` | `exclusive` (encima) o `inclusive` (dentro del precio). |
| `AIMARKET_CARD_TOPUP_TAX_CODE` | `txcd_10000000` | El código fiscal del producto en Stripe. |

El límite diario es lo que acota una tarjeta robada: la disputa llega cuando el crédito puede estar
ya gastado, y el hub no puede recuperar crédito gastado.

## Lo que esta puerta no hace

- **Retirar crédito** tras un reembolso o una disputa: se registra y se corrige a mano.
- **Congelar una cuenta** por una disputa.
- **Emitir facturas.** Stripe envía su propio recibo al correo del pagador si la cuenta de Stripe
  lo tiene activado (con Managed Payments, siempre). La factura legal a nombre del operador y la
  contabilización de la venta son asunto del operador y dependen de su jurisdicción.

## Operador

### Abrir la puerta

1. Una cuenta de Stripe a nombre del operador. **Su país no se puede cambiar después**; para dinero
   real debe ser el país de la entidad legal que recibe los pagos.
2. Una **clave restringida** con un solo permiso, *Checkout Sessions: Write* (Developers → API keys
   → Create restricted key → Custom permissions). Es todo lo que necesita el hub: abrir un pago y
   leerlo. Póngala en `STRIPE_SECRET_KEY_FILE` (docker secret) o en `STRIPE_SECRET_KEY`.
3. Un endpoint de webhook en el Dashboard: `https://<hub>/ai-market/v2/stripe/webhook`, eventos
   `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
   `checkout.session.expired`, `charge.dispute.created`, `charge.refunded`. Su secreto de firma va en
   `STRIPE_WEBHOOK_SECRET_FILE` (o `STRIPE_WEBHOOK_SECRET`).
4. `AIMARKET_CARD_TOPUP_ENABLED=1`; el carril de créditos debe estar activo (`AIMARKET_CREDITS_ENABLED=1`).
5. Opcionalmente, un modo de impuesto (`AIMARKET_CARD_TOPUP_TAX`) tras los pasos de ese modo en el Dashboard.

El well-known indica si la puerta está abierta y, si no, por qué: `payment_rails.credits.topup_card` =
`{"enabled", "provider": "stripe-checkout", "create", "status", "currency", "min_usd", "max_usd",
"daily_usd", "fee_pct", "fee_fixed_usd", "tax"}` o `{"enabled": false, "reason": "…"}`.

### Rutas

| Ruta | Quién | Qué |
|---|---|---|
| `POST /ai-market/v2/account/topup/card` | la `X-API-Key` de la cuenta | abrir un pago (`{"amount_usd"}`) → `201` con `checkout_url`; 6 por minuto y cuenta |
| `GET /ai-market/v2/card-topups/{id}` | quien tenga el id | estado; la cuenta solo para su propia clave |
| `POST /ai-market/v2/card-topups/{id}/refresh` | quien tenga el id | conciliar con Stripe, acreditar si está pagado |
| `POST /ai-market/v2/stripe/webhook` | Stripe (firmado) | eventos |
| `GET /ai-market/v2/account/topups` | la clave de la cuenta | lista `card_topups` junto a las recargas USDC `topups` |

### Errores

| Código | `error` | Significado |
|---|---|---|
| 400 | `amount_invalid`, `amount_out_of_range` | no son céntimos enteros, o fuera de min/max |
| 400 | `signature_invalid` | la firma del webhook no se verifica (no se lee nada) |
| 401 | `api_key_required` | falta `X-API-Key` o es desconocida |
| 429 | `daily_topup_limit`, `too_many_attempts` | límite diario superado (cuentan los pagos abiertos) o demasiados pagos por minuto |
| 502 | `stripe_refused` | Stripe no abrió el pago (por ejemplo Managed Payments sin activar o falta un permiso de la clave); no hubo cargo |
| 503 | `card_topup_unavailable`, `stripe_unavailable` | la puerta está cerrada (el motivo lo explica) o Stripe no responde |

El webhook responde `{"received": true, "outcome": …}`: `credited`, `already_credited`, `pending`,
`refused` (los importes no cuadran: se registra como error y no se acredita nada), `expired`,
`disputed`, `refunded` o `ignored`.

### Tabla

`card_topups` (migraciones 056 y 057): una fila por pago — `topup_id`, `account_id`, `amount_cents`
(crédito), `charge_cents` (lo que paga la tarjeta antes de impuestos), `status` (`new` → `open` →
`credited` / `expired` / `failed` / `disputed` / `refunded`), `session_id`, `payment_intent`,
`livemode`, `credited_mc`, `tax_mode`, `tax_behavior`, `tax_cents`, marcas de tiempo.

## Probar en el sandbox de Stripe, paso a paso

Así se probó la puerta el 2026-10-20; repítalo antes de pasar a real y tras cambiar el modo de
impuesto. Aquí no se mueve dinero real.

1. **Un sandbox.** Regístrese en stripe.com y omita todos los pasos de activación (documentos,
   cuenta bancaria). En el Dashboard abra el sandbox (aparece un aviso «Sandbox» arriba).
2. **Una clave restringida** solo con *Checkout Sessions: Write*. Cópiela y guárdela directamente
   desde el portapapeles, para que nunca se pegue en un chat ni en una terminal:
   `pbpaste > ~/.aimarket-stripe-test-key && chmod 600 ~/.aimarket-stripe-test-key`.
3. **La Stripe CLI**, para traer los eventos del sandbox a un hub en su máquina. Si Homebrew falla
   (Command Line Tools antiguas), use la compilación para macOS de las releases oficiales de GitHub y
   compruebe la suma con `stripe-mac-checksums.txt`. Después `stripe login` y elija el **sandbox**
   (no la cuenta real ni el antiguo «Test mode»).
4. **El secreto de webhook de la CLI:** `stripe listen --print-secret > ~/.aimarket-stripe-whsec`.
5. **Un hub local** con `AIMARKET_CREDITS_ENABLED=1 AIMARKET_CARD_TOPUP_ENABLED=1
   STRIPE_SECRET_KEY_FILE=~/.aimarket-stripe-test-key STRIPE_WEBHOOK_SECRET_FILE=~/.aimarket-stripe-whsec`
   (y el modo de impuesto a probar).
6. **Reenviar eventos:** `stripe listen --forward-to http://127.0.0.1:<puerto>/ai-market/v2/stripe/webhook
   --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.expired,charge.dispute.created,charge.refunded`.
   `stripe trigger checkout.session.completed` debe mostrar `[200]`: la firma se verificó (con un
   secreto erróneo sería `[400]`).
7. **Un pago:** `POST /ai-market/v2/account/topup/card {"amount_usd": 5}` con la clave de una cuenta;
   una persona abre `checkout_url` y paga con `4242 4242 4242 4242`, cualquier fecha futura y cualquier CVC.
8. **Comprobar:** la CLI muestra `checkout.session.completed` → `[200]`; la recarga está `credited`;
   el libro de la cuenta tiene exactamente una línea `topup` para ella. Luego
   `stripe events resend <evt_…>`: de nuevo `[200]`, y el saldo no se mueve.
9. **Opcional:** una disputa con la tarjeta `4000 0000 0000 0259` → la recarga pasa a `disputed`; un
   reembolso hecho en el Dashboard → `refunded`.
