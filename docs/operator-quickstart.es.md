# Su propio hub

[English](operator-quickstart.md) · [Русский](operator-quickstart.ru.md) · [Español](operator-quickstart.es.md) · [Français](operator-quickstart.fr.md) · [中文](operator-quickstart.zh.md)

> La terminología sigue el [glosario de localización](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md) canónico. Los nombres de productos, identificadores de protocolo, URL, comandos de CLI y variables de entorno nunca se traducen.

Lo clonó, lo desplegó, y la pregunta justa es qué obtiene a cambio. Esta página es el camino más
corto desde un despliegue nuevo hasta un hub que de verdad puede cobrar: sin cadena, sin
contrato, sin cartera (wallet), sin cuenta con nadie.

## Tres minutos

```bash
pip install "aimarket-hub[pqc]"        # [pqc]: firmas poscuánticas (ver abajo)
export AIMARKET_CREDITS_ENABLED=1      # el riel de pago
export AIMARKET_ORACLE_FAMILY_URL=off  # puntúe usted a los publicadores (ver abajo)
export AIMARKET_PQC=1                  # firma híbrida: Ed25519 + ML-DSA-65
python -m aimarket_hub quickstart
python -m aimarket_hub serve
```

`quickstart` hace tres cosas que un hub nuevo no podría hacer por sí solo:

1. **Publica una capability que de verdad es suya y de verdad se ejecuta.** De otro modo, un
   catálogo nuevo está vacío o se compone de pares que no son suyos. La muestra es un *paquete
   estático*: un objeto JSON guardado en `prompt_template` que el manejador de invocación
   devuelve tal cual, así que no necesita proveedor, ni modelo, ni red. Sustitúyalo por su
   propio servicio indicando `invoke_url`, o edite el paquete.
2. **Emite una clave de comprador** con un pequeño saldo inicial, para que pueda completar una
   venta contra su propio hub antes de enseñárselo a nadie.
3. **Imprime los dos curl** que hacen esa venta.

También indica cómo firmará el hub y dónde están sus claves.

## Firmas poscuánticas desde el primer arranque

Las dos líneas de PQC de arriba hacen cosas distintas:

- **`[pqc]` permite que el hub vea a sus pares.** Instala el verificador ML-DSA-65. La
  comprobación de una firma poscuántica falla cerrada, así que un hub sin él nunca indexa a un
  par que firma de forma híbrida, y todos los hubs de AIMarket lo hacen.
- **`AIMARKET_PQC=1` hace que el hub firme de forma híbrida.** Su manifiesto, su documento de
  descubrimiento y sus recibos llevan una firma ML-DSA-65 junto a la Ed25519, y los pares fijan
  su clave poscuántica la primera vez que la ven.

La clave poscuántica es un segundo archivo junto a la clásica:
`<AIMARKET_SIGNING_KEY_PATH>_mldsa`, por defecto `data/hub_signing_key_mldsa`. Haga copia de
seguridad de ambas a la vez y manténgalas en un mismo volumen. Si pierde el archivo ML-DSA, cada
par que lo haya fijado rechaza su hub hasta que lo vuelva a fijar
(`POST /federation/peers/repin`). Contexto:
[migración poscuántica](https://github.com/alexar76/aicom/blob/main/docs/pqc-migration.es.md).

## Cómo le pagan

Hay dos rieles y puede usar uno o los dos.

| | Créditos (`X-API-Key`) | Canales (`X-Payment-Channel`) |
|---|---|---|
| Configuración | una variable de entorno | contrato de depósito en garantía (escrow) que usted despliega en Base + 5 ajustes previos |
| Importe mínimo facturable | $0.00001 | $0.01 (céntimos enteros, redondeando hacia arriba) |
| Quién tiene el dinero | usted, prepago | usted, prepago (depósito on-chain) |
| Qué necesita el comprador | un cliente HTTP | una cartera con fondos y una firma typed data |

**Créditos** es el riel que funciona con `AIFACTORY_CRYPTO_ENABLED=0`, que es el valor por
defecto. Al activarlo, un precio publicado significa dinero: una capability de pago responde
`402` con los rieles que acepta, y una invocación con una `X-API-Key` válida reserva el precio
antes de que el proveedor se ejecute, lo cobra si tiene éxito y lo devuelve ante cualquier fallo.

```
POST /ai-market/v2/accounts                    # un comprador emite una clave (con límite de frecuencia)
GET  /ai-market/v2/account                     # el saldo del comprador
GET  /ai-market/v2/account/ledger              # cada movimiento de su propio dinero
POST /ai-market/v2/accounts/{id}/credit        # usted le recarga  (admin bearer)
POST /ai-market/v2/accounts/{id}/status        # usted lo desactiva  (admin bearer)
GET  /ai-market/v2/stats/live                  # summary.credits: ganado frente a adeudado
```

El dinero entra solo por `/credit`, y esa ruta es solo suya. Use lo que use para cobrar —una
factura, un pago en su web, una transferencia de stablecoin que vio llegar—, llama a `/credit`
cuando ya tiene el dinero. El hub no finge haber cobrado nada.

### Lo que asume

Un saldo prepagado es dinero de su cliente en su libro. Por eso mismo `stats/live` publica
`outstanding_credit_usd` junto a `credits_earned_usd`: lo primero es un pasivo, lo segundo es
ingreso, y un hub que no los distingue acaba gastando uno creyendo que es el otro. Nada en el hub
puede enviar valor, así que los reembolsos los hace usted.

### Ajustes

| Variable | Por defecto | Significado |
|---|---|---|
| `AIMARKET_CREDITS_ENABLED` | `0` | el riel en sí |
| `AIMARKET_CREDITS_OPEN_SIGNUP` | `1` | si un desconocido puede emitir su propia clave |
| `AIMARKET_CREDITS_FREE_GRANT_USD` | `0.05` | saldo inicial de una clave autoemitida |
| `AIMARKET_CREDITS_SIGNUPS_PER_HOUR` | `5` | límite por dirección de registros autoservicio |
| `AIMARKET_ECOSYSTEM_FILE` | `ecosystem.json` junto a la base de datos | qué cuentas, servidores y carteras son su propio ecosistema: sus llamadas son SELF, no demanda externa; vea [clases de tráfico](traffic-classes.es.md) |
| `AIMARKET_OPERATOR_ACCOUNTS` | vacío | atajo antiguo para `self.accounts` de ese archivo (ids de cuenta separados por comas); se fusiona con él |
| `AIMARKET_ROUTING_FEE_BPS` | `100` | su comisión cuando intermedia la capability de otro hub |
| `AIMARKET_PQC` | sin definir = auto | sin definir: un hub nuevo firma híbrido (Ed25519 + ML-DSA-65), un hub con clave ML-DSA sigue firmando con ella, uno solo Ed25519 sigue así; `1` / `0` lo fijan |

## Dejar que otros publiquen en su hub

Una cuenta de créditos es una identidad, no solo una cartera, así que también es la forma en que
alguien publica una capability con usted: sin editar `.env`, sin reiniciar, sin cadena.

```bash
curl -X POST $HUB/ai-market/v2/accounts                      # obtienen una clave
curl -X POST $HUB/ai-market/v2/accounts/<id>/credit \
     -H "Authorization: Bearer $ADMIN" -d '{"amount_usd": 30}'   # usted recibe su dinero
curl -X POST $HUB/ai-market/v2/supply/stake \
     -H "X-API-Key: <key>" -d '{"amount_usd": 25}'               # depositan la garantía
curl -X POST $HUB/ai-market/v2/supply/register \
     -H "X-API-Key: <key>" -d @manifest.json                     # publican
```

La clave habla exactamente por un publicador: su propia cuenta. No puede depositar garantía por
otro ni publicar en nombre de nadie más, algo que el token de publicación compartido nunca pudo
impedir.

La garantía es un cargo real contra su saldo, así que es dinero que usted retiene y puede
recortar mediante slashing (recorte de la garantía). Se registra como un tipo propio de
colateral, distinto del centinela de desarrollo, y supera la comprobación de garantía de
producción. La vía on-chain sigue existiendo y sigue exigiendo un `tx_hash` verificado, pero un
hub cuya imagen no incluye el verificador de depósitos ya no está obligado a elegir entre «sin
colateral» y «una comprobación inservible».

Confianza en los publicadores: con `AIMARKET_ORACLE_FAMILY_URL=off` no hay oráculo al que
consultar, así que los publicadores reciben el arranque neutral documentado y la comprobación de
confianza no se aplica. Apúntela a una instancia de LUMEN que usted opere si quiere puntuación.

## Pagar a sus vendedores

Cuando el publicador de una capability tiene aquí una cuenta de créditos, cada venta completada
se reparte automáticamente: `AIMARKET_PUBLISHER_SHARE_BPS` (70% por defecto) se le abona a él y
el resto es suyo. Es la vía de ganancias del vendedor que el hub nunca tuvo: el libro de canales
no puede enviar valor y la única tabla de obligaciones reembolsa a los depositantes, no a los
proveedores. Antes de esto, un vendedor podía publicar, recibir invocaciones y aun así tener que
pedirle que le transfiriera a mano una décima de céntimo.

`stats/live` informa del reparto con honestidad: `credits_earned_usd` es el bruto (lo que gastaron
los compradores), `publisher_payouts_usd` lo que fue a los vendedores y `operator_net_usd` lo que
usted se quedó. Una llamada fallida no paga a nadie: el pago sale del mismo cobro que se hace al
comprador.

## Intermediar otros hubs

Cuando enruta una invocación a un par por el que usted no vendió, la comisión de enrutamiento se
**reserva antes de pedirle nada al par**, en el riel que use el comprador. Quien llama sin pago
recibe un `402` con la comisión; un par que se niega no le cuesta nada al comprador; a un par que
cobra menos que su precio publicado se le factura el importe menor. Si no quiere cobrar por
intermediar, ponga `AIMARKET_ROUTING_FEE_BPS=0`: esa es la forma de desactivarlo, no una
cabecera que falta.

Un canal respaldado por depósito en garantía no puede autorizar una comisión de enrutamiento (no
hay autorización firmada para ella), así que esa combinación se rechaza en vez de registrarse
como ingreso que nunca podría cobrar. Los compradores por esa vía deben usar una cuenta de
créditos.

### Revender un par que cobra

Intermediar solo mueve dinero si puede pagar a la otra parte. Tenga una cuenta de créditos en el
par e indíquela:

```bash
export AIMARKET_PEER_API_KEYS="https://peer.example=aimk_your_key_there"
```

Ahora una llamada enrutada a ese par es una reventa: a su comprador se le cobra el precio del
catálogo más su comisión, usted paga al par desde su cuenta allí y la comisión es su margen. Sin
clave, el `402` del par llega a un comprador que no tiene cuenta allí y no puede hacer nada con
él; por eso la federación en vivo solo ha contenido pares que no cobran.

A los hubs pares se les llama en su `/ai-market/v2/invoke`, no en su `mcp_endpoint`: ese endpoint
habla MCP JSON-RPC sobre SSE y responde a un sobre enrutado con `Method not found` dentro de un
cuerpo `text/event-stream`. El enrutamiento entre hubs fue imposible hasta que se corrigió.

## Aceptar pagos x402

Una venta de catálogo va directa al vendedor. El 402 indica el `payout_address` del listado y un
`nonce` de factura, y solo su cuerpo JSON lleva un `payment_secret` con
`nonce = sha256(payment_secret)`. El comprador paga a esa cartera on-chain con un
`transferWithAuthorization` EIP-3009 firmado sobre el nonce y reintenta con
`X-Payment: <tx hash>` (o `PAYMENT-SIGNATURE`), `X-Payment-Nonce` y `X-Payment-Secret`. El
secreto es lo que demuestra que el comprador es quien pagó: la transacción y su nonce son
públicos en cuanto se minan. El hub verifica la transferencia y nunca retiene el dinero
(`aimarket_hub/settle.py`): solo paga la transferencia que movió esa autorización, y cada
autorización es una reclamación propia, así que dos en una misma transacción compran dos
llamadas. En una llamada federada, un pago viaja junto a su clave de par solo cuando el hub lo
liquidó en esa misma petición, con su propio nonce y guardando el secreto. Un hub sin clave para
ese par que no liquidó nada es un relé y reenvía las cabeceras de pago del comprador, secreto
incluido. Todo pago está vinculado (una transferencia simple al vendedor se rechaza;
`AIMARKET_SETTLE_REQUIRE_BINDING=0` se ignora), así que las herramientas de comprador escritas
para un hub anterior, que enviaban solo `X-Payment` y `X-Payment-Nonce`, se rechazan hasta que
envíen también el secreto. La pasarela MCP lo acepta como `x_payment_secret` y el puente A2A lo
presenta por sí mismo. La migración `040` añade la columna de factura que lo guarda, y
`X-Payment-Secret` está en la lista de cabeceras permitidas de CORS, así que un cliente de
navegador en un origen de `AIMARKET_CORS_ORIGINS` también puede canjearlo.
`AIMARKET_X402_ACCEPT=0` mantiene el modo de solo descubrimiento.

Los créditos y los canales de pago siguen siendo comodidades de prepago. No son la forma en que se
paga a un publicador con cartera.

## Dejar que desconocidos le encuentren

Siempre hay dos puertas de observación y ambas llevan a cuarentena, no a confianza inmediata: un
`POST /federation/announce` sin autenticar y el descubrimiento recíproco, cuando un par le rastrea
y se identifica. En ambos casos el par queda `pending` y `trusted=false`. Después se ejecuta
automáticamente una prueba en sandbox; un `pass` lo indexa sin pulsar Approve. Fail y review
quedan pendientes para `/operator`. Una tabla de vista previa aparte guarda lo que un par
pendiente dice ofrecer; esas filas nunca pueden llegar a la búsqueda ni al enrutamiento porque no
están en la tabla `capabilities`. Vea `docs/join-the-federation.md`.

## Valerse por sí mismo

Dos valores por defecto apuntan al despliegue de referencia, y probablemente no quiera ninguno:

- **Confianza en los publicadores.** `AIMARKET_ORACLE_FAMILY_URL` apunta por defecto a la
  instancia de LUMEN de otro operador. Si lo deja así, su capacidad de publicar depende de la
  disponibilidad de ese operador: una capability publicada mientras no responde se guarda sin
  puntuación y cada invocación suya responde 502. `off` indica que este hub no tiene oráculo de
  confianza: los publicadores reciben el arranque neutral documentado y la comprobación no se
  aplica. Apúntela a su propia instancia si opera una.
- **Semillas de la federación.** La lista de semillas incluida son los propios satélites del hub
  de referencia. Son un catálogo para explorar, no oferta suya; vea `AIMARKET_SEED_LIST`.

## Nombre

El código es Apache-2.0 (MIT en el resto del ecosistema) y puede explotarlo comercialmente, fijar
sus propias comisiones y quedárselas todas; nada desvía una parte a ningún sitio. No hay licencia
de marca, así que no llame «AIMarket» a su despliegue ni sugiera un respaldo. «Implements the
AIMarket Protocol» e «interoperates with <peer>» son exactas y válidas. Nadie puede afirmar
«certified» ni «conformant»: todavía no hay un conjunto de pruebas de conformidad.
