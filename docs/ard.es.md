# ARD — el catálogo del hub para clientes de Agent Resource Discovery

> **English:** [ard.md](./ard.md) · **Русский:** [ard.ru.md](./ard.ru.md) · **Français:** [ard.fr.md](./ard.fr.md) · **中文:** [ard.zh.md](./ard.zh.md)
>
> Código: [`ard.py`](../aimarket_hub/ard.py) · [`hub_routes/ard.py`](../aimarket_hub/hub_routes/ard.py) · Pruebas: [`tests/test_ard.py`](../tests/test_ard.py) · Comprobación del esquema: [`docs/ard-conformance/`](./ard-conformance/)

Agent Resource Discovery (ARD) es un conjunto de Internet-Drafts presentados el 30-10-2026 al
grupo de trabajo DAWN del IETF por autores de Google, Ericsson y otras organizaciones:

* [draft-jimenez-dawn-ard-initial-contact-00](https://datatracker.ietf.org/doc/draft-jimenez-dawn-ard-initial-contact/) —
  cómo encuentra un cliente un manifiesto (`/.well-known/ard.json`, la relación de enlace `ard`, SVCB, DNS-SD);
* [draft-bu-dawn-ard-discovery-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-discovery/) —
  el formato de entrada y de manifiesto, los nombres `urn:air`, el `trustManifest` firmado y la
  búsqueda en el registro (`POST /search`, `POST /explore`, `GET /agents`);
* [draft-bu-dawn-ard-architecture-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-architecture/) — roles y flujos.

Desde la versión 3.18.0 cada hub responde también en estos formatos. Es otra puerta al mismo
catálogo, como las puertas MCP y A2A; AIMarket Protocol v2 no cambia. Un cliente ARD encuentra
el hub y sus capacidades; llama y paga como en v2 (`POST /ai-market/v2/invoke`). El pago, los
recibos y la admisión en la federación no forman parte de ARD y siguen en v2.

## Lo que sirve el hub

| Ruta | Método | Qué es |
|---|---|---|
| `/.well-known/ard.json` | GET, HEAD | El manifiesto ARD del hub, `application/ard+json`. Se puede guardar en caché una hora, con ETag. |
| `/` | HEAD, GET | Lleva `Link: <…/.well-known/ard.json>; rel="ard"` para los clientes que solo conocen el origen. |
| `/.well-known/did.json` | GET, HEAD | El documento `did:web` al que se refieren las firmas `trustManifest` del hub. |
| `/ard/v1` | GET | Lo que implementa este registro (ARD no lo define; un cliente puede ignorarlo). |
| `/ard/v1/search` | POST | Búsqueda: texto, filtros, páginas. |
| `/ard/v1/explore` | POST | Recuentos de facetas sobre lo que selecciona una consulta. |
| `/ard/v1/agents` | GET | Todas las entradas, ordenadas por identificador. |
| `/ard/v1/capability` | GET, HEAD | El descriptor de una capacidad: el documento al que apunta el `url` de una entrada. |

Todas son públicas y de solo lectura.

## El manifiesto

`/.well-known/ard.json` enumera los recursos propios del hub, no su catálogo:

| Entrada | `type` | `url` | Fijada por `digest` |
|---|---|---|---|
| el registro | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| el hub como agente A2A | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | sí |
| el hub como servidor MCP | `application/mcp-server-card+json` | `<hub>/.well-known/mcp/server-card.json` | sí |
| el documento de discovery de AIMarket | `application/json` | `<hub>/.well-known/agent-marketplace` | no |

Un `digest` es el SHA-256 de los bytes que el hub sirve en `url`, obtenidos a través del propio
hub al construir el manifiesto. El documento de discovery no lleva digest porque se vuelve a
firmar cada minuto (su `generated_at` está dentro de su propia firma Ed25519); un digest suyo
quedaría obsoleto en menos de un minuto.

Un cliente que quiere el catálogo sigue la entrada del registro, como describen los borradores:
un manifiesto puede constar de entradas que apuntan a un registro, y el registro responde a las
búsquedas.

## Entradas del catálogo

El registro devuelve una entrada por cada capacidad que vende el hub, el mismo conjunto que
muestran las puertas HTTP, MCP y A2A: ejecutables, públicas y con una ruta vendible.

| Campo ARD | Valor |
|---|---|
| `identifier` | `urn:air:<publisher>:<product_id>:<capability_id>` |
| `displayName` | el nombre visible en el catálogo |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `description` | la descripción de la capacidad |
| `capabilities` | el nombre de herramienta de la capacidad (el nombre de la herramienta MCP) |
| `version` | el `version` de v2 (`v1` salvo que el editor fije otro) |
| `digest` | SHA-256 de los bytes del descriptor en `url` |
| `tags` | para una fila federada, las categorías que declara su hub |
| `metadata.aimarket` | `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifest` | para una capacidad que publica este hub; véase «Firmas» |

`<publisher>` es el host que publica el listado: este hub para una capacidad local, el hub par
para una capacidad indexada de un par admitido. El espacio de nombres y el nombre son el
`product_id` y el `capability_id` de v2, de modo que un identificador se convierte de nuevo en una
llamada v2 sin cambios. Un carácter que rompería el nombre (`:`, `/`, `?`, `#`, espacios) se
codifica con `%`.

El descriptor en `url` contiene lo que necesita un comprador y nada que cambie por sí solo: los
esquemas, el precio, la petición de llamada (`POST <hub>/ai-market/v2/invoke` con `product_id`,
`capability_id`, `source_hub`, `input`), el endpoint y el nombre de la herramienta MCP, y enlaces a
la tarjeta A2A, al catálogo v2 firmado y al documento de discovery. La tasa de éxito medida, la
latencia y las puntuaciones de confianza quedan fuera: cambian cada pocos minutos, y la entrada
fija el descriptor por su digest.

## Firmas

Cada entrada que publica este hub lleva un `trustManifest` (draft-bu-dawn-ard-discovery-00 §3.5):

* `identity` es `did:web:<host>`. Su documento DID en `/.well-known/did.json` lista primero
  la clave Ed25519 del hub: la misma que su documento de discovery de AIMarket publica como
  `signer_public_key`. Desde el hub 3.21.0, un hub que firma en modo híbrido lista ahí también su
  clave ML-DSA-65, para la [puerta AI Catalog](./ai-catalog.es.md); las firmas `trustManifest` de
  ARD usan solo la clave Ed25519.
* `subject` repite el `identifier`, el `type`, el `version` y el `digest` de la entrada.
* `signature` es un JWS compacto separado (RFC 7515, apéndice F): `<header>..<signature>`, con la
  cabecera `{"alg": "EdDSA", "kid": "did:web:<host>#hub-ed25519"}`, calculado sobre
  `base64url(header) "." base64url(JCS(trustManifest sin signature))`, con JCS según el RFC 8785.

Una entrada que el hub indexó de un par no lleva `trustManifest`: este hub no es su editor. Los
borradores tratan esa entrada como una pista de descubrimiento (§6.2). Su `source` en los
resultados de búsqueda es la URL del hub par.

Para comprobar una entrada (Python, con `cryptography` y `rfc8785`):

```python
import base64, hashlib, json, urllib.request
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def get(url): return urllib.request.urlopen(url).read()

hub = "https://modelmarket.dev"
entry = json.loads(get(hub + "/.well-known/ard.json"))["entries"][1]
tm = entry["trustManifest"]
assert tm["identity"] == "did:web:modelmarket.dev"
assert tm["subject"] == {k: entry[k] for k in ("identifier", "type", "version", "digest") if k in entry}
assert entry["digest"] == "sha256:" + hashlib.sha256(get(entry["url"])).hexdigest()

jwk = json.loads(get(hub + "/.well-known/did.json"))["verificationMethod"][0]["publicKeyJwk"]
header, _, signature = tm["signature"].split(".")
body = {k: v for k, v in tm.items() if k != "signature"}
Ed25519PublicKey.from_public_bytes(unb64(jwk["x"])).verify(
    unb64(signature), (header + "." + b64(rfc8785.dumps(body))).encode())
```

## Búsqueda

**`POST /ard/v1/search`** recibe `{"query": {"text", "filter"}, "federation", "pageSize", "pageToken"}`.

* `query.text` es obligatorio, hasta 2.000 caracteres. Pasa por la búsqueda de catálogo del propio
  hub (léxica y con coincidencia multilingüe de conceptos en EN, RU, ES, FR y ZH), así que el
  orden es el que da `/ai-market/v2/search`.
* `score` es la relevancia de esa búsqueda llevada a 0..100. Ordena texto, no calidad ni
  seguridad.
* Claves de `filter`: `type`, `tags`, `capabilities`, `publisher`, `identifier`,
  `trustManifest.identity`. Un valor es una cadena o un arreglo de cadenas; los valores de una
  clave se combinan con O, las claves con Y. Cualquier otra clave se rechaza con 400: un filtro
  ignorado en silencio devolvería lo que el cliente pidió excluir.
* `pageSize` vale 10 por defecto, como máximo 100. `nextPageToken` está ligado a la consulta que
  lo produjo; un token enviado con otra consulta se rechaza.
* `federation`: el hub copia los catálogos de los pares que admite (federación AIMarket), así que
  `auto` y `none` responden desde el índice local, que ya contiene las entradas de los pares
  admitidos. No se reenvía ninguna consulta. `referrals` añade por ahora una lista `referrals`
  vacía.

**`POST /ard/v1/explore`** recibe `{"query": {…}, "resultType": {"facets": [{"field", "limit"}]}}`.
Aquí `query.text` es opcional. Campos de faceta: `type`, `tags`, `capabilities`, `publisher`.

**`GET /ard/v1/agents`** devuelve `{"entries": […], "nextPageToken": …}` ordenado por
`identifier`. Los filtros son parámetros de consulta con los nombres de las claves de filtro;
repetir un parámetro significa O (`?capabilities=a&capabilities=b`). `pageSize` y `pageToken`
como arriba.

Los errores son problem details del RFC 9457 (`application/problem+json`) con los códigos
`error` de los borradores: `INVALID_ARGUMENT` (400), `NOT_FOUND` (404).

## De una entrada a una llamada

```bash
curl -s https://modelmarket.dev/.well-known/ard.json
curl -s -X POST https://modelmarket.dev/ard/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "weather in Paris"}, "pageSize": 3}'
curl -s '<el url de un resultado>'      # el descriptor: su miembro "invoke" es la petición v2
```

El `invoke.body` del descriptor es una petición de llamada v2 con un `input` vacío; rellénelo y
pague como en cualquier llamada v2 ([agent-first-call.es.md](./agent-first-call.es.md)).

## Límites

* **EdDSA, no ES256.** Los borradores hacen ES256 de implementación obligatoria. Este hub solo
  tiene una clave Ed25519, así que sus firmas son EdDSA (RFC 8037); un verificador que solo
  implemente ES256 no puede comprobarlas.
* **El tipo de medio de la capacidad no está registrado.** `application/vnd.aimarket.capability+json`
  es un tipo del árbol de proveedores; ARD no tiene tipo para una sola capacidad invocable detrás
  de una pasarela, solo para un servidor MCP o un agente A2A completo.
* **Sin búsqueda vectorial semántica y sin referrals.** El estatuto de DAWN deja la búsqueda y el
  ranking fuera de su alcance; el hub responde con la búsqueda de catálogo que ya tiene.
* **Un hub que no está en la raíz de un origen https** (con un prefijo de ruta, o por http en
  desarrollo) no tiene identidad `did:web`: sus entradas se sirven sin firmar y
  `/.well-known/did.json` responde 404.
* **`GET /agents` y sus parámetros de filtro** los borradores solo los nombran; las formas de
  arriba son las de este hub.

## Detrás de un proxy inverso

Un proxy delante del hub debe pasarle estas rutas: `/.well-known/ard.json`, `/.well-known/did.json`, `/.well-known/agent-card.json`, `/.well-known/mcp/server-card.json`, `/.well-known/agent-marketplace` y todo lo que cuelga de `/ard/`. Una tarjeta que el
proxy envíe a otro sitio deja mal su digest en el manifiesto, y sin `/ard/` el registro no se ve.

Un hub bajo una ruta (`https://example.org/hub`) tiene su manifiesto en `/hub/.well-known/ard.json`. Responda al
`/.well-known/ard.json` del origen con una redirección hacia él (los clientes siguen como máximo una), como muestra
[production-deployment.es.md](./production-deployment.es.md). Un hub así no tiene identidad `did:web` y sirve sus
entradas sin firmar.

## Conformidad

El manifiesto, una página de `GET /agents`, una petición de búsqueda y una respuesta de búsqueda
producidos por este código se validan con el CDDL del draft-bu-dawn-ard-discovery-00 §5
([`ard-conformance/check.sh`](./ard-conformance/check.sh)); las copias rotas a propósito no pasan.
El CDDL del borrador necesita dos cambios antes de que una herramienta CDDL lo acepte: redefine el
tipo del preludio `any`, y la herramienta usada (cddl 0.12.14) no procesa la elección de grupo
`url // data` dentro del mapa de la entrada, así que la comprobación usa una forma equivalente con
dos alternativas de mapa. Los dos archivos están en la carpeta.

## Ajustes

| Variable | Por defecto | Significado |
|---|---|---|
| `AIMARKET_ARD_CACHE_S` | `60` | Cuántos segundos se conserva el catálogo, como entradas ARD, entre reconstrucciones. `0` lo reconstruye en cada petición. |

El manifiesto se construye una vez por proceso: todo lo que fija no cambia mientras el proceso
sigue en marcha.
