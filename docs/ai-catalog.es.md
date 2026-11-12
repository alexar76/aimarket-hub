# AI Catalog: el catálogo del hub para clientes de AI Catalog y su tarjeta de servidor MCP

> **English:** [ai-catalog.md](./ai-catalog.md) · **Русский:** [ai-catalog.ru.md](./ai-catalog.ru.md) · **Français:** [ai-catalog.fr.md](./ai-catalog.fr.md) · **中文:** [ai-catalog.zh.md](./ai-catalog.zh.md)
>
> Code: [`ai_catalog.py`](../aimarket_hub/ai_catalog.py) · [`hub_routes/ai_catalog.py`](../aimarket_hub/hub_routes/ai_catalog.py) · Tests: [`tests/test_ai_catalog.py`](../tests/test_ai_catalog.py) · Signer profiles: [`did-web-eddsa-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-eddsa-v1.md), [`did-web-ml-dsa-65-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-ml-dsa-65-v1.md)

[AI Catalog](https://ai-catalog.io/) es un formato JSON para listar artefactos de IA de cualquier tipo:
servidores MCP, agentes A2A, skills, conjuntos de datos, otros catálogos. Se redacta en un repositorio de
trabajo de la Linux Foundation ([Agent-Card/ai-catalog](https://github.com/Agent-Card/ai-catalog)) con gente de
los proyectos MCP y A2A. Un catálogo es una lista de entradas; cada una nombra un artefacto (`identifier`),
dice qué es (`type`) y lo enlaza (`url`) o lo lleva dentro (`data`).

MCP lo usa para el descubrimiento. La extensión Server Card ([SEP-2127](https://modelcontextprotocol.io/seps/2127-mcp-server-cards), estado Final) dice que un cliente que solo
conoce un dominio pide `/.well-known/ai-catalog.json`, toma las entradas de tipo
`application/mcp-server-card+json` y sigue el `url` de cada una hasta una tarjeta de servidor, que le dice
dónde conectarse.

Desde el hub 3.21.0 todos los hubs responden en estos formatos. Como las puertas MCP, A2A y
[ARD](./ard.es.md), es otra entrada al mismo catálogo. Nada cambia en AIMarket Protocol v2: el cliente
encuentra aquí el hub y lo que vende, y llama y paga a la manera de v2 (`POST /ai-market/v2/invoke`).

Escrito contra el texto de la especificación AI Catalog del 11-11-2026 (commit `e2b425f9`, `specVersion`
`1.0`) y la extensión Server Card en [`modelcontextprotocol/ext-server-card@526201bb`](https://github.com/modelcontextprotocol/ext-server-card/tree/526201bbc80231daa40ffcdecfc9da4e54e5dc93), la instantánea que nombra SEP-2127. El texto de AI Catalog es un
borrador de trabajo y sigue cambiando.

## Qué sirve el hub

| Ruta | Qué es |
|---|---|
| `/.well-known/ai-catalog.json` | El catálogo del hub, `application/ai-catalog+json`. Cacheable una hora, con ETag. |
| `/` | Lleva `Link: <…/.well-known/ai-catalog.json>; rel="ai-catalog"`, junto al enlace `rel="ard"`. |
| `/mcp/server-card` | El punto MCP del hub como tarjeta de servidor, `application/mcp-server-card+json`. |
| `/ai-catalog/v1/capabilities.json` | Un catálogo anidado: todas las capacidades que vende el hub. |
| `/ai-catalog/v1/ids/<id>` | Un identificador que el hub dio a la capacidad de un par; redirige (303) a su descriptor. |
| `/ai-catalog/profiles/<name>` | La definición de un perfil de firma (véase «Firmas»). |
| `/.well-known/did.json` | El documento `did:web` que nombran las firmas (compartido con la puerta ARD). |

Todas responden a `GET` y `HEAD`, son públicas y de solo lectura.

## El catálogo

`/.well-known/ai-catalog.json` lista los artefactos propios del hub:

| Entrada | `type` | `url` | `digest` |
|---|---|---|---|
| el hub como servidor MCP | `application/mcp-server-card+json` | `<hub>/mcp/server-card` | sí |
| el hub como agente A2A | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | sí |
| lo que vende el hub | `application/ai-catalog+json` | `<hub>/ai-catalog/v1/capabilities.json` | — |
| lo mismo, con búsqueda | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| el documento de discovery de AIMarket | `application/json` | `<hub>/.well-known/agent-marketplace` | — |

* `host` nombra al operador: `displayName` es el nombre del hub e `identifier` su `did:web` (el nombre de host
  cuando el hub no tiene `did:web`: servido bajo una ruta, por http o en un puerto).
* Un `digest` es `sha256:` más el SHA-256 en hexadecimal minúsculo de los bytes que el hub sirve en `url`,
  tomados a través del propio hub al construir el catálogo. Las tres últimas entradas no lo llevan: sus
  documentos cambian con las ofertas (el documento de discovery se vuelve a firmar cada minuto).
* Las dos entradas de tarjetas no llevan `displayName`, `description` ni `version`. Cada tarjeta declara los
  suyos, y la especificación pide que una entrada no repita un valor que puede divergir del artefacto.
* Un artefacto tiene el mismo `identifier` aquí y en el manifiesto ARD.

El catálogo es de nivel 2 (Discoverable) de los tres niveles de conformidad, con digests y con las firmas
descritas más abajo.

## La tarjeta de servidor MCP

`/mcp/server-card` es el lugar que reserva la extensión: `<streamable-http-url>/server-card`.

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
  "name": "dev.modelmarket/aimarket-hub-mcp",
  "version": "1.3.0",
  "title": "AIMarket Hub MCP",
  "description": "Agent capability marketplace: search the catalogue, call priced and free tools, run pipelines.",
  "websiteUrl": "https://modelmarket.dev/",
  "repository": {"url": "https://github.com/alexar76/aimarket-hub", "source": "github"},
  "remotes": [{
    "type": "streamable-http",
    "url": "https://modelmarket.dev/mcp",
    "supportedProtocolVersions": ["2025-06-18", "2025-03-26", "2024-11-05"],
    "headers": [{"name": "X-API-Key", "isRequired": false, "isSecret": true, "description": "…"}]
  }]
}
```

(La forma que sirve un hub en `https://modelmarket.dev`; `version` y las versiones de protocolo son las del
hub en ejecución.)

* `name` es el host al revés y el nombre del servidor, de modo que dos operadores de este software tienen dos
  nombres. Un operador cuyo servidor tiene nombre en el registro MCP lo fija con
  `AIMARKET_MCP_SERVER_CARD_NAME` (por ejemplo `io.github.alexar76/aimarket-hub`); un valor que no sea
  `namespace/name` se ignora.
* La tarjeta no lista herramientas. La extensión deja herramientas, recursos y prompts a `tools/list`, porque
  una lista estática puede diferir de lo que recibe un cliente conectado.
* La tarjeta concuerda con el punto en vivo: su `version` es la `serverInfo.version` que devuelve `initialize`,
  y cada versión de protocolo que lista es una que el punto negocia. Los tests comprueban ambas cosas contra un
  hub en ejecución.
* La respuesta lleva las cabeceras CORS y de caché que pide la extensión (`Access-Control-*`,
  `Cache-Control: public, max-age=3600`, un `ETag`, `304` ante `If-None-Match`).
* La tarjeta anterior en `/.well-known/mcp/server-card.json` se mantiene. Lista herramientas, y de ella dependen
  los escáneres que leen una tarjeta en lugar de llamar a `tools/list`.

## Lo que vende el hub

`/ai-catalog/v1/capabilities.json` tiene una entrada por cada capacidad que muestra la búsqueda del propio hub:
el mismo conjunto que muestran las puertas HTTP, MCP, A2A y ARD, y ninguna oferta que el catálogo oculte.

| Campo | Valor |
|---|---|
| `identifier` | véase abajo |
| `displayName`, `description` | el nombre en el catálogo y la descripción de la capacidad |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `version` | la `version` de v2 (`v1` si el publicador no fijó otra) |
| `digest` | SHA-256 de los bytes del descriptor en `url` |
| `tags` | `aimarket` y, para una fila federada, las categorías que declara su hub |
| `publisher` | quién publica la oferta: este hub (`did:web`) o el nombre de host del par |
| `extensions["dev.modelmarket.aimarket"]` | los términos v2: `protocol`, `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifests` | para una capacidad que publica este hub; véase «Firmas» |

`url`, `digest` y `version` son los que el registro ARD publica para la misma capacidad, así que ambas puertas
fijan los mismos bytes del descriptor. `price_per_call_usd` es aquí una cadena decimal (`"0.01"`): el documento
se firma sobre sus bytes JCS, y el canonicalizador de este protocolo rechaza las fracciones, que las
implementaciones en distintos lenguajes serializan de forma distinta.

**Los identificadores** siguen las reglas de proyección de la especificación («Catalog Projection»):

* Una capacidad que este hub lista por sí mismo es `urn:air:<este host>:<product_id>:<capability_id>`, el
  nombre que le da la puerta ARD.
* Una capacidad copiada de un par admitido la publica ese par, y este hub no ha leído el identificador propio
  del par. La especificación dice que un operador que no es el publicador «MUST use a non-`urn:air`
  identifier under its own control». Esa entrada se llama `<hub>/ai-catalog/v1/ids/<32 caracteres hex>`:
  estable para el mismo par, producto y capacidad, y resoluble (303 al descriptor). De dónde viene está en la
  entrada: `publisher` nombra el host del par y la extensión contiene `source_hub`, `product_id` y
  `capability_id`.

La puerta ARD nombra la misma capacidad federada `urn:air:<host del par>:…`. Las dos puertas difieren aquí
porque difieren las dos especificaciones.

## Firmas

La especificación define un perfil de firma, `did-web-v1`, que solo admite una clave ES256 (P-256). Un hub
tiene una clave Ed25519 y, cuando firma en modo híbrido, una clave ML-DSA-65; no tiene clave P-256. Por eso el
hub firma en los contenedores de la propia especificación bajo dos perfiles que define este protocolo. La
especificación lo prevé: un perfil definido fuera de ella se nombra con una URL bajo control de su autor, y un
lector que no implementa un perfil trata la firma como no verificada y evalúa el resto de forma independiente
(«Profile Selection»).

| Perfil | Algoritmo | Clave en el documento DID | Dónde lo usa el hub |
|---|---|---|---|
| `https://modelmarket.dev/ai-catalog/profiles/did-web-eddsa-v1` | `EdDSA`, Ed25519 (RFC 8037) | `#hub-ed25519`, un JWK `OKP` | un Trust Manifest en cada entrada que el hub publica y fija por digest; la firma de instantánea de un hub sin clave ML-DSA-65 |
| `https://modelmarket.dev/ai-catalog/profiles/did-web-ml-dsa-65-v1` | `ML-DSA-65` (FIPS 204; registro JOSE en RFC 9964) | `#hub-ml-dsa-65`, un JWK `AKP` | la firma de instantánea de cada documento de catálogo, cuando el hub firma en modo híbrido |

Ambos siguen a `did-web-v1` en todo salvo el algoritmo y el tipo de clave: un firmante `did:web` raíz sin
puerto, un `kid` que es el DID del firmante más un fragmento, una clave que el documento DID lista bajo
`assertionMethod` y que controla el firmante, y ningún material de clave en la cabecera JWS. Cada URL sirve su
definición.

* **El Trust Manifest de una entrada** contiene `contributor` (el `did:web` del hub), `subject` (el
  `identifier`, `type`, `digest` y `version` de la entrada) y `signature`. Es el aval del publicador para esa
  versión: el firmante es `did:web:` más el segmento de publicador del nombre `urn:air` de la entrada. Una
  entrada copiada de un par no lo lleva, porque este hub no es su publicador.
* **La firma de instantánea de un catálogo** es el `signature` de nivel superior. Cubre todo el documento:
  cada entrada, el `host`, los manifiestos anidados.
* Una firma es un JWS compacto separado (RFC 7515, apéndice F) sobre los bytes JCS (RFC 8785) del objeto
  firmado sin su propio `signature.jws`. La cabecera protegida es `{"alg", "kid", "typ"}`, con `typ`
  `ai-catalog-trust-manifest+jws` o `ai-catalog+jws`.
* El mismo contenido se firma una vez: el manifiesto de una entrada por versión, la instantánea de un
  documento por contenido. Un catálogo conserva sus bytes y su ETag hasta que cambia una oferta.
* Un hub sin `did:web` raíz (bajo una ruta, por http, en un puerto) publica sus catálogos sin firmar.

### Poscuántica

Admitir este formato no debilita la protección poscuántica del hub, por tres razones.

1. No se sustituye nada. El documento de discovery v2, el manifiesto y los recibos conservan sus firmas
   híbridas (Ed25519 y ML-DSA-65 juntas); un par sigue fijando ambas claves a partir de ellos.
2. El catálogo lleva las mismas dos capas. Un lector que exige la firma de instantánea ML-DSA-65 y el Trust
   Manifest Ed25519 de la entrada está protegido mientras resista cualquiera de los dos algoritmos, que es lo
   que da una firma híbrida. Quien pueda falsificar Ed25519 puede falsificar un manifiesto, y no puede producir
   la firma ML-DSA-65 del catálogo en el que ese manifiesto se encuentra.
3. No hay retroceso. Un perfil se selecciona por su nombre exacto y nunca se infiere, así que una firma
   Ed25519 no responde a un lector que pide ML-DSA-65. Es la regla de la especificación y el verificador de
   `ai_catalog.signature_problem` la aplica.

Lo que queda fuera: un lector que solo implementa `did-web-v1` ve estos documentos como no firmados (nivel 2).
Una firma ML-DSA-65 ocupa 3 309 bytes, así que se coloca una vez por documento y no en cada entrada; un catálogo
anidado al que se llega por `url` es un documento aparte con su propia firma.

### Comprobar un catálogo

Una comprobación reducida, en Python con `cryptography`, `rfc8785` y (para un hub que firma en modo híbrido)
`dilithium-py`. Verifica el digest de la primera entrada, el Trust Manifest de esa entrada y la firma de
instantánea del catálogo. Un verificador completo aplica además las reglas de las definiciones de los perfiles
(la clave está bajo `assertionMethod` y la controla el firmante, los tiempos, los miembros de la cabecera);
lo hacen `aimarket_hub.ai_catalog.signature_problem` y `endorsement_problem`.

```python
import base64, hashlib, json, urllib.request
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def get(url): return urllib.request.urlopen(url).read()

hub, did = "https://modelmarket.dev", "did:web:modelmarket.dev"
catalog = json.loads(get(hub + "/.well-known/ai-catalog.json"))
keys = {m["id"]: m["publicKeyJwk"] for m in json.loads(get(hub + "/.well-known/did.json"))["verificationMethod"]}

def signing_input(obj):
    """The protected header of a signed object, and the bytes its signature covers."""
    header = obj["signature"]["jws"].split(".")[0]
    payload = {**obj, "signature": {k: v for k, v in obj["signature"].items() if k != "jws"}}
    payload.pop("additionalSignatures", None)
    return json.loads(unb64(header)), (header + "." + b64(rfc8785.dumps(payload))).encode()

# 1. An entry: the digest pins the card, the Trust Manifest binds the release, Ed25519 signs it.
entry = catalog["entries"][0]
assert entry["digest"] == "sha256:" + hashlib.sha256(get(entry["url"])).hexdigest()
manifest = entry["trustManifests"][0]
assert manifest["contributor"] == manifest["signature"]["signer"] == did
assert manifest["subject"] == {k: entry[k] for k in ("identifier", "type", "digest", "version") if k in entry}
header, message = signing_input(manifest)
assert header == {"alg": "EdDSA", "kid": did + "#hub-ed25519", "typ": "ai-catalog-trust-manifest+jws"}
Ed25519PublicKey.from_public_bytes(unb64(keys[header["kid"]]["x"])).verify(
    unb64(manifest["signature"]["jws"].split(".")[2]), message)

# 2. The catalogue: one signature over the whole document, ML-DSA-65 when the hub signs hybrid.
header, message = signing_input(catalog)
assert header["typ"] == "ai-catalog+jws" and header["kid"].startswith(did + "#")
assert catalog["signature"]["signer"] == catalog["host"]["identifier"] == did
signature = unb64(catalog["signature"]["jws"].split(".")[2])
if header["alg"] == "ML-DSA-65":
    from dilithium_py.ml_dsa import ML_DSA_65
    assert ML_DSA_65.verify(unb64(keys[header["kid"]]["pub"]), message, signature)
else:
    Ed25519PublicKey.from_public_bytes(unb64(keys[header["kid"]]["x"])).verify(signature, message)
```

Los tests ejecutan este bloque tal como está impreso en la versión inglesa contra un hub de cada tipo
(`test_the_documented_check_runs_against_a_hub`).

## Cómo convive con el formato de AIMarket

El formato propio del hub no se sustituye ni se bifurca. Un documento AI Catalog que sirve este hub es un
documento AI Catalog válido para cualquier cliente, con los añadidos de AIMarket en los dos lugares que la
especificación abre para añadidos:

* el mapa `extensions` de una entrada, bajo la clave DNS inversa `dev.modelmarket.aimarket`, para los términos
  que necesita un comprador (precio, modo de acceso, dónde invocar);
* firmas bajo nombres de perfil propios, en los objetos Signature de la especificación.

Un cliente que solo conoce AI Catalog lee las entradas e ignora ambos, como le indica la especificación
(«Unrecognized Members», «Profile Selection»). Un cliente que conoce AIMarket lee el mismo documento y obtiene
los términos y las firmas. El documento de discovery v2 firmado, con su fijación de claves y su lista de pares
admitidos, figura como una de las entradas.

## Ajustes

| Variable | Por defecto | Significado |
|---|---|---|
| `AIMARKET_MCP_SERVER_CARD_NAME` | sin definir | El `name` de la tarjeta, cuando el operador tiene uno en el registro MCP. |
| `AIMARKET_AI_CATALOG_CACHE_S` | `60` | Segundos que se conserva el catálogo de capacidades antes de reconstruirlo desde la búsqueda del hub. |
| `AIMARKET_PQC` | véase `signing.py` | Si el hub firma en modo híbrido; decide el perfil de la firma de instantánea. |

## Límites

* No es nivel 3 (Trusted): ese nivel nombra `did-web-v1` y el hub no tiene clave ES256. Un lector que solo
  implementa `did-web-v1` trata las firmas del hub como no verificadas.
* La especificación AI Catalog es un borrador de trabajo. Su sección de confianza cambió varias veces en
  octubre y noviembre de 2026 (ADR 0025 a 0029). La puerta sigue el texto del 11-11-2026.
* Sin paginación: el catálogo de capacidades es un solo documento. La especificación no define ninguna; el
  registro ARD en `/ard/v1` pagina y busca el mismo catálogo.
* No se enlaza el catálogo anidado de un hub par: las capacidades de un par figuran aquí como las copias de
  este hub, con identificadores de este hub.
* El hub no lee catálogos de otros sitios. HISTOR sí, para encontrar tarjetas de servidor ([`histor/histor/cards.py`](https://github.com/alexar76/histor/blob/main/histor/cards.py)).
