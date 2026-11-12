# AI Catalog — каталог хаба для клиентов AI Catalog и его карточка MCP-сервера

> **English:** [ai-catalog.md](./ai-catalog.md) · **Español:** [ai-catalog.es.md](./ai-catalog.es.md) · **Français:** [ai-catalog.fr.md](./ai-catalog.fr.md) · **中文:** [ai-catalog.zh.md](./ai-catalog.zh.md)
>
> Code: [`ai_catalog.py`](../aimarket_hub/ai_catalog.py) · [`hub_routes/ai_catalog.py`](../aimarket_hub/hub_routes/ai_catalog.py) · Tests: [`tests/test_ai_catalog.py`](../tests/test_ai_catalog.py) · Signer profiles: [`did-web-eddsa-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-eddsa-v1.md), [`did-web-ml-dsa-65-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-ml-dsa-65-v1.md)

[AI Catalog](https://ai-catalog.io/) — формат JSON для перечня ИИ-артефактов любого вида: MCP-серверов,
A2A-агентов, навыков, наборов данных, других каталогов. Его пишут в рабочем репозитории Linux Foundation
([Agent-Card/ai-catalog](https://github.com/Agent-Card/ai-catalog)) участники проектов MCP и A2A. Каталог —
это список записей; каждая называет артефакт (`identifier`), говорит, что это (`type`), и либо ссылается на
него (`url`), либо несёт его в себе (`data`).

MCP использует этот формат для обнаружения. Расширение Server Card
([SEP-2127](https://modelcontextprotocol.io/seps/2127-mcp-server-cards), статус Final) говорит: клиент,
который знает только домен, запрашивает `/.well-known/ai-catalog.json`, берёт записи с типом
`application/mcp-server-card+json` и по `url` каждой получает карточку сервера, где сказано, куда
подключаться.

С версии 3.21.0 каждый хаб отвечает в этих форматах. Как двери MCP, A2A и [ARD](./ard.ru.md), это ещё
один вход в тот же каталог. В AIMarket Protocol v2 ничего не меняется: клиент находит здесь хаб и то, что
он продаёт, а вызывает и платит по v2 (`POST /ai-market/v2/invoke`).

Написано по тексту спецификации AI Catalog от 11.11.2026 (коммит `e2b425f9`, `specVersion` `1.0`) и по
расширению Server Card в состоянии
[`modelcontextprotocol/ext-server-card@526201bb`](https://github.com/modelcontextprotocol/ext-server-card/tree/526201bbc80231daa40ffcdecfc9da4e54e5dc93),
которое называет SEP-2127. Текст AI Catalog — рабочий черновик и ещё меняется.

## Что отдаёт хаб

| Путь | Что это |
|---|---|
| `/.well-known/ai-catalog.json` | Каталог хаба, `application/ai-catalog+json`. Кэшируется на час, с ETag. |
| `/` | Несёт `Link: <…/.well-known/ai-catalog.json>; rel="ai-catalog"` рядом со ссылкой `rel="ard"`. |
| `/mcp/server-card` | MCP-вход хаба как карточка сервера, `application/mcp-server-card+json`. |
| `/ai-catalog/v1/capabilities.json` | Вложенный каталог: все возможности, которые хаб продаёт. |
| `/ai-catalog/v1/ids/<id>` | Идентификатор, который хаб дал возможности соседа; перенаправляет (303) на её описание. |
| `/ai-catalog/profiles/<name>` | Определение профиля подписи (см. «Подписи»). |
| `/.well-known/did.json` | Документ `did:web`, на который ссылаются подписи (общий с дверью ARD). |

Все отвечают на `GET` и `HEAD`, публичны и только для чтения.

## Каталог

`/.well-known/ai-catalog.json` перечисляет собственные артефакты хаба:

| Запись | `type` | `url` | `digest` |
|---|---|---|---|
| хаб как MCP-сервер | `application/mcp-server-card+json` | `<hub>/mcp/server-card` | да |
| хаб как A2A-агент | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | да |
| что хаб продаёт | `application/ai-catalog+json` | `<hub>/ai-catalog/v1/capabilities.json` | — |
| то же, с поиском | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| discovery-документ AIMarket | `application/json` | `<hub>/.well-known/agent-marketplace` | — |

* `host` называет оператора: `displayName` — имя хаба, `identifier` — его `did:web` (имя хоста, если
  `did:web` у хаба нет: он отдаётся из подкаталога, по http или на нестандартном порту).
* `digest` — это `sha256:` и SHA-256 в нижнем регистре от байтов, которые хаб отдаёт по `url`; они берутся
  через сам хаб при сборке каталога. У трёх последних записей его нет: их документы меняются вместе с
  предложениями (discovery-документ переподписывается каждую минуту).
* У двух записей с карточками нет `displayName`, `description` и `version`. Каждая карточка называет их
  сама, а спецификация просит не повторять в записи значение, которое может разойтись с артефактом.
* У артефакта один и тот же `identifier` здесь и в манифесте ARD.

Каталог соответствует уровню 2 (Discoverable) из трёх уровней спецификации, с дайджестами и с подписями,
описанными ниже.

## Карточка MCP-сервера

`/mcp/server-card` — место, которое резервирует расширение: `<streamable-http-url>/server-card`.

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

(Так выглядит карточка хаба на `https://modelmarket.dev`; `version` и версии протокола — те, что у
работающего хаба.)

* `name` — хост в обратном порядке и имя сервера, поэтому у двух операторов этой программы два разных
  имени. Оператор, у чьего сервера есть имя в реестре MCP, задаёт его переменной
  `AIMARKET_MCP_SERVER_CARD_NAME` (например, `io.github.alexar76/aimarket-hub`); значение не вида
  `namespace/name` игнорируется.
* Инструментов в карточке нет. Расширение оставляет инструменты, ресурсы и промпты запросу `tools/list`:
  статичный список может расходиться с тем, что получает подключённый клиент.
* Карточка согласована с работающим входом: её `version` равна `serverInfo.version` из ответа
  `initialize`, и каждую названную версию протокола вход действительно согласует. Тесты проверяют оба
  утверждения на работающем хабе.
* Ответ несёт заголовки CORS и кэширования, которых требует расширение (`Access-Control-*`,
  `Cache-Control: public, max-age=3600`, `ETag`, `304` на `If-None-Match`).
* Прежняя карточка по `/.well-known/mcp/server-card.json` остаётся. В ней есть список инструментов, и на неё
  опираются сканеры, которые читают карточку вместо вызова `tools/list`.

## Что хаб продаёт

В `/ai-catalog/v1/capabilities.json` по одной записи на каждую возможность, которую показывает собственный
поиск хаба: тот же набор, что в дверях HTTP, MCP, A2A и ARD, и ни одного предложения, которое каталог
скрывает.

| Поле | Значение |
|---|---|
| `identifier` | см. ниже |
| `displayName`, `description` | имя в каталоге и описание возможности |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `version` | `version` из v2 (`v1`, если издатель не задал свою) |
| `digest` | SHA-256 байтов описания по `url` |
| `tags` | `aimarket`, а для записи от соседа — категории, которые объявляет его хаб |
| `publisher` | кто публикует предложение: этот хаб (`did:web`) или имя хоста соседа |
| `extensions["dev.modelmarket.aimarket"]` | условия v2: `protocol`, `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifests` | у возможности, которую публикует этот хаб; см. «Подписи» |

`url`, `digest` и `version` — те же, что реестр ARD публикует для той же возможности, поэтому обе двери
закрепляют одни и те же байты описания. `price_per_call_usd` здесь — десятичная строка (`"0.01"`): документ
подписывается по байтам JCS, а канонизатор этого протокола не принимает дробные числа, потому что реализации
на разных языках записывают их по-разному.

**Идентификаторы** следуют правилам проекции из спецификации («Catalog Projection»):

* Возможность, которую хаб публикует сам, называется `urn:air:<этот хост>:<product_id>:<capability_id>` —
  тем же именем, что в двери ARD.
* Возможность, скопированную у допущенного соседа, публикует сосед, и собственного идентификатора соседа
  для неё этот хаб не читал. Спецификация говорит: оператор, который не является издателем, «MUST use a
  non-`urn:air` identifier under its own control». Такая запись называется
  `<hub>/ai-catalog/v1/ids/<32 шестнадцатеричных символа>`: имя постоянно для тех же соседа, продукта и
  возможности и разрешается (303 на описание). Откуда запись, сказано в ней самой: `publisher` называет хост
  соседа, а в расширении лежат `source_hub`, `product_id` и `capability_id`.

Дверь ARD называет ту же возможность соседа `urn:air:<хост соседа>:…`. Двери расходятся здесь потому, что
расходятся две спецификации.

## Подписи

Спецификация определяет один профиль подписи, `did-web-v1`, и он принимает только ключ ES256 (P-256). У хаба
ключ Ed25519 и, когда он подписывает гибридно, ключ ML-DSA-65; ключа P-256 у него нет. Поэтому хаб
подписывает в контейнерах самой спецификации под двумя профилями, которые определяет этот протокол.
Спецификация это предусматривает: профиль, определённый вне её, называется URL-адресом, которым владеет его
автор, а читатель, не реализующий профиль, считает такую подпись непроверенной и оценивает остальное
независимо («Profile Selection»).

| Профиль | Алгоритм | Ключ в DID-документе | Где хаб его применяет |
|---|---|---|---|
| `https://modelmarket.dev/ai-catalog/profiles/did-web-eddsa-v1` | `EdDSA`, Ed25519 (RFC 8037) | `#hub-ed25519`, JWK типа `OKP` | Trust Manifest у каждой записи, которую хаб публикует и закрепляет дайджестом; подпись всего документа у хаба без ключа ML-DSA-65 |
| `https://modelmarket.dev/ai-catalog/profiles/did-web-ml-dsa-65-v1` | `ML-DSA-65` (FIPS 204; регистрация для JOSE — RFC 9964) | `#hub-ml-dsa-65`, JWK типа `AKP` | подпись каждого документа каталога, когда хаб подписывает гибридно |

Оба повторяют `did-web-v1` во всём, кроме алгоритма и типа ключа: подписант — корневой `did:web` без порта,
`kid` — DID подписанта плюс фрагмент, ключ перечислен в DID-документе под `assertionMethod` и принадлежит
подписанту, ключевого материала в заголовке JWS нет. По каждому URL отдаётся определение профиля.

* **Trust Manifest записи** содержит `contributor` (`did:web` хаба), `subject` (`identifier`, `type`,
  `digest` и `version` записи) и `signature`. Это подтверждение издателем именно этого выпуска: подписант
  равен `did:web:` плюс сегмент издателя из имени `urn:air` записи. У записи, скопированной у соседа, его
  нет: этот хаб ей не издатель.
* **Подпись документа** — это `signature` верхнего уровня. Она покрывает весь документ: каждую запись,
  `host`, вложенные манифесты.
* Подпись — отделённый компактный JWS (RFC 7515, приложение F) над байтами JCS (RFC 8785) подписываемого
  объекта без его собственного `signature.jws`. Защищённый заголовок — `{"alg", "kid", "typ"}`, где `typ`
  равен `ai-catalog-trust-manifest+jws` или `ai-catalog+jws`.
* Одно и то же содержимое подписывается один раз: манифест записи — на выпуск, подпись документа — на
  содержимое. Каталог сохраняет свои байты и ETag, пока не изменится предложение.
* Хаб без корневого `did:web` (в подкаталоге, по http, на порту) публикует каталоги без подписей.

### Постквантовая защита

Поддержка этого формата не ослабляет постквантовую защиту хаба, по трём причинам.

1. Ничего не заменяется. Discovery-документ v2, манифест и квитанции сохраняют гибридные подписи (Ed25519 и
   ML-DSA-65 вместе); сосед по-прежнему закрепляет по ним оба ключа.
2. Каталог несёт те же два слоя. Читатель, который требует и подпись документа ML-DSA-65, и Ed25519-манифест
   записи, защищён, пока держится хотя бы один алгоритм, — то же даёт гибридная подпись. Тот, кто умеет
   подделывать Ed25519, подделает манифест, но не сможет выпустить подпись ML-DSA-65 для каталога, в котором
   этот манифест лежит.
3. Отката нет. Профиль выбирается по точному имени и никогда не выводится из других полей, поэтому подпись
   Ed25519 не отвечает читателю, который просит ML-DSA-65. Это правило спецификации, и проверка в
   `ai_catalog.signature_problem` его применяет.

Что остаётся за рамками: читатель, который реализует только `did-web-v1`, видит эти документы как
неподписанные (уровень 2). Одна подпись ML-DSA-65 занимает 3 309 байт, поэтому она ставится один раз на
документ, а не на каждую запись; вложенный каталог, достижимый по `url`, — отдельный документ со своей
подписью.

### Как проверить каталог

Сокращённая проверка на Python с `cryptography`, `rfc8785` и (для хаба с гибридной подписью)
`dilithium-py`. Она проверяет дайджест первой записи, её Trust Manifest и подпись документа. Полная
проверка применяет ещё правила из определений профилей (ключ под `assertionMethod` и принадлежит подписанту,
время, члены заголовка); это делают `aimarket_hub.ai_catalog.signature_problem` и `endorsement_problem`.

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

Тесты исполняют этот блок в том виде, как он напечатан в английской версии, на хабе каждого вида
(`test_the_documented_check_runs_against_a_hub`).

## Как это соотносится с форматом AIMarket

Собственный формат хаба не заменяется и не раздваивается. Документ AI Catalog, который отдаёт хаб, —
корректный документ AI Catalog для любого клиента, а дополнения AIMarket лежат в двух местах, которые
спецификация открывает для дополнений:

* в `extensions` записи, под ключом `dev.modelmarket.aimarket` (обратная запись домена), — условия, нужные
  покупателю (цена, режим доступа, куда вызывать);
* в подписях под нашими именами профилей, в объектах Signature самой спецификации.

Клиент, который знает только AI Catalog, читает записи и пропускает то и другое, как велит ему спецификация
(«Unrecognized Members», «Profile Selection»). Клиент, который знает AIMarket, читает тот же документ и
получает условия и подписи. Подписанный discovery-документ v2 с закреплением ключей и списком допущенных
соседей указан среди записей.

## Настройки

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `AIMARKET_MCP_SERVER_CARD_NAME` | не задана | `name` карточки, если у оператора есть имя в реестре MCP. |
| `AIMARKET_AI_CATALOG_CACHE_S` | `60` | Сколько секунд хранится каталог возможностей до пересборки из поиска хаба. |
| `AIMARKET_PQC` | см. `signing.py` | Подписывает ли хаб гибридно; от этого зависит профиль подписи документа. |

## Ограничения

* Не уровень 3 (Trusted): этот уровень называет `did-web-v1`, а ключа ES256 у хаба нет. Читатель, который
  реализует только `did-web-v1`, считает подписи хаба непроверенными.
* Спецификация AI Catalog — рабочий черновик. Её раздел о доверии менялся несколько раз в октябре и ноябре
  2026 года (ADR 0025–0029). Дверь следует тексту от 11.11.2026.
* Страниц нет: каталог возможностей — один документ. Спецификация их не определяет; тот же каталог
  постранично и с поиском отдаёт реестр ARD по `/ard/v1`.
* Вложенный каталог хаба-соседа не подключается: возможности соседа перечислены здесь как копии этого хаба,
  под идентификаторами этого хаба.
* Хаб не читает чужие каталоги. Их читает HISTOR, чтобы находить карточки серверов
  ([`histor/histor/cards.py`](https://github.com/alexar76/histor/blob/main/histor/cards.py)).
