# ARD — каталог хаба для клиентов Agent Resource Discovery

> **English:** [ard.md](./ard.md) · **Español:** [ard.es.md](./ard.es.md) · **Français:** [ard.fr.md](./ard.fr.md) · **中文:** [ard.zh.md](./ard.zh.md)
>
> Код: [`ard.py`](../aimarket_hub/ard.py) · [`hub_routes/ard.py`](../aimarket_hub/hub_routes/ard.py) · Тесты: [`tests/test_ard.py`](../tests/test_ard.py) · Проверка по схеме: [`docs/ard-conformance/`](./ard-conformance/)

Agent Resource Discovery (ARD) — набор Internet-Draft, поданных 30.10.2026 в рабочую группу
IETF DAWN авторами из Google, Ericsson и других организаций:

* [draft-jimenez-dawn-ard-initial-contact-00](https://datatracker.ietf.org/doc/draft-jimenez-dawn-ard-initial-contact/) —
  как клиент находит манифест (`/.well-known/ard.json`, link relation `ard`, SVCB, DNS-SD);
* [draft-bu-dawn-ard-discovery-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-discovery/) —
  формат записи и манифеста, имена `urn:air`, подписанный `trustManifest` и поиск по реестру
  (`POST /search`, `POST /explore`, `GET /agents`);
* [draft-bu-dawn-ard-architecture-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-architecture/) — роли и сценарии.

С версии 3.18.0 каждый хаб отвечает и в этих форматах. Это ещё одна дверь в тот же каталог,
как двери MCP и A2A; в AIMarket Protocol v2 ничего не меняется. Клиент ARD находит хаб и его
возможности, а вызывает и платит по v2 (`POST /ai-market/v2/invoke`). Оплаты, квитанций и
допуска пиров в федерацию в ARD нет, они остаются в v2.

## Что отдаёт хаб

| Путь | Метод | Что это |
|---|---|---|
| `/.well-known/ard.json` | GET, HEAD | ARD-манифест хаба, `application/ard+json`. Кэшируется на час, с ETag. |
| `/` | HEAD, GET | Заголовок `Link: <…/.well-known/ard.json>; rel="ard"` для клиентов, которые знают только origin. |
| `/.well-known/did.json` | GET, HEAD | Документ `did:web`, на который ссылаются подписи `trustManifest` хаба. |
| `/ard/v1` | GET | Что реализует этот реестр (в ARD не определено, клиент может не читать). |
| `/ard/v1/search` | POST | Поиск: текст, фильтры, страницы. |
| `/ard/v1/explore` | POST | Подсчёт фасетов по тому, что выбрал запрос. |
| `/ard/v1/agents` | GET | Все записи по порядку идентификаторов. |
| `/ard/v1/capability` | GET, HEAD | Описание одной возможности — документ, на который указывает `url` записи. |

Все пути публичные и только для чтения.

## Манифест

`/.well-known/ard.json` перечисляет собственные ресурсы хаба, а не каталог:

| Запись | `type` | `url` | Закреплена `digest` |
|---|---|---|---|
| реестр | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| хаб как агент A2A | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | да |
| хаб как сервер MCP | `application/mcp-server-card+json` | `<hub>/.well-known/mcp/server-card.json` | да |
| discovery-документ AIMarket | `application/json` | `<hub>/.well-known/agent-marketplace` | нет |

`digest` — SHA-256 тех байтов, которые хаб отдаёт по `url`; хаб получает их через самого себя,
когда собирает манифест. У discovery-документа хеша нет: он переподписывается каждую минуту
(`generated_at` входит в его собственную подпись Ed25519), и хеш устарел бы в течение минуты.

За каталогом клиент идёт по записи реестра, как описано в драфтах: манифест может состоять из
записей, указывающих на реестр, а на поисковые запросы отвечает реестр.

## Записи каталога

Реестр отдаёт по одной записи на каждую возможность, которую продаёт хаб, — тот же набор, что
показывают двери HTTP, MCP и A2A: исполнимые, публичные, с продаваемым маршрутом.

| Поле ARD | Значение |
|---|---|
| `identifier` | `urn:air:<publisher>:<product_id>:<capability_id>` |
| `displayName` | отображаемое имя в каталоге |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `description` | описание возможности |
| `capabilities` | имя инструмента возможности (имя инструмента MCP) |
| `version` | `version` из v2 (`v1`, если издатель не задал другую) |
| `digest` | SHA-256 байтов описания по `url` |
| `tags` | для федеративной записи — категории, которые объявляет её хаб |
| `metadata.aimarket` | `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifest` | для возможности, которую публикует этот хаб; см. «Подписи» |

`<publisher>` — хост, который публикует запись: этот хаб для локальной возможности, хаб-пир для
возможности, проиндексированной у допущенного пира. Пространство имён и имя — это `product_id` и
`capability_id` из v2, поэтому идентификатор без изменений превращается обратно в вызов v2.
Символы, которые сломали бы имя (`:`, `/`, `?`, `#`, пробелы), кодируются через `%`.

Описание по `url` содержит то, что нужно покупателю, и ничего, что меняется само по себе:
схемы, цену, запрос вызова (`POST <hub>/ai-market/v2/invoke` с `product_id`, `capability_id`,
`source_hub`, `input`), endpoint и имя инструмента MCP, ссылки на карточку A2A, подписанный
каталог v2 и discovery-документ. Измеренная доля успешных вызовов, задержка и оценки доверия не
включены: они меняются каждые несколько минут, а запись закрепляет описание хешем.

## Подписи

У каждой записи, которую публикует этот хаб, есть `trustManifest` (draft-bu-dawn-ard-discovery-00 §3.5):

* `identity` — `did:web:<host>`. В его DID-документе `/.well-known/did.json` первым идёт
  ключ Ed25519 хаба, тот же, что discovery-документ AIMarket публикует как
  `signer_public_key`. С версии 3.21.0 хаб с гибридной подписью перечисляет там и свой ключ
  ML-DSA-65 — для [двери AI Catalog](./ai-catalog.ru.md); подписи `trustManifest` в ARD делаются
  только ключом Ed25519.
* `subject` повторяет `identifier`, `type`, `version` и `digest` записи.
* `signature` — отделённый компактный JWS (RFC 7515, приложение F): `<header>..<signature>`,
  заголовок `{"alg": "EdDSA", "kid": "did:web:<host>#hub-ed25519"}`, подпись вычислена над
  `base64url(header) "." base64url(JCS(trustManifest без signature))`, JCS по RFC 8785.

У записи, которую хаб проиндексировал у пира, `trustManifest` нет: этот хаб не её издатель.
Драфты считают такую запись подсказкой для обнаружения (§6.2). Её `source` в результатах поиска —
URL хаба-пира.

Проверка записи (Python, `cryptography` и `rfc8785`):

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

## Поиск

**`POST /ard/v1/search`** принимает `{"query": {"text", "filter"}, "federation", "pageSize", "pageToken"}`.

* `query.text` обязателен, до 2 000 символов. Он проходит через собственный поиск каталога хаба
  (лексический плюс многоязычное совпадение понятий на EN, RU, ES, FR и ZH), поэтому порядок тот
  же, что даёт `/ai-market/v2/search`.
* `score` — релевантность этого поиска, приведённая к 0..100. Она ранжирует текст, а не качество
  или безопасность.
* Ключи `filter`: `type`, `tags`, `capabilities`, `publisher`, `identifier`,
  `trustManifest.identity`. Значение — строка или массив строк; значения одного ключа
  объединяются через ИЛИ, ключи — через И. Любой другой ключ отклоняется с 400: молча
  проигнорированный фильтр вернул бы то, что клиент просил исключить.
* `pageSize` по умолчанию 10, не больше 100. `nextPageToken` привязан к запросу, который его
  выдал; токен с другим запросом отклоняется.
* `federation`: хаб копирует каталоги допущенных пиров (федерация AIMarket), поэтому `auto` и
  `none` отвечают из локального индекса, в котором записи допущенных пиров уже есть. Запросы
  никуда не пересылаются. `referrals` пока добавляет пустой список `referrals`.

**`POST /ard/v1/explore`** принимает `{"query": {…}, "resultType": {"facets": [{"field", "limit"}]}}`.
Здесь `query.text` необязателен. Поля фасетов: `type`, `tags`, `capabilities`, `publisher`.

**`GET /ard/v1/agents`** возвращает `{"entries": […], "nextPageToken": …}` по порядку
`identifier`. Фильтры — параметры запроса с именами ключей фильтра; повтор параметра означает ИЛИ
(`?capabilities=a&capabilities=b`). `pageSize` и `pageToken` — как выше.

Ошибки — problem details по RFC 9457 (`application/problem+json`) с кодами `error` из драфтов:
`INVALID_ARGUMENT` (400), `NOT_FOUND` (404).

## От записи к вызову

```bash
curl -s https://modelmarket.dev/.well-known/ard.json
curl -s -X POST https://modelmarket.dev/ard/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "weather in Paris"}, "pageSize": 3}'
curl -s '<url одного из результатов>'      # описание: его поле "invoke" — запрос v2
```

`invoke.body` в описании — запрос вызова v2 с пустым `input`; заполните его и оплатите, как любой
вызов v2 ([agent-first-call.ru.md](./agent-first-call.ru.md)).

## Ограничения

* **EdDSA, а не ES256.** Драфты делают ES256 обязательным для реализации. У хаба есть только ключ
  Ed25519, поэтому его подписи — EdDSA (RFC 8037); верификатор, который умеет только ES256, их
  не проверит.
* **Медиатип возможности не зарегистрирован.** `application/vnd.aimarket.capability+json` —
  тип из vendor-дерева; в ARD нет типа для одной вызываемой возможности за шлюзом, только для
  целого сервера MCP или агента A2A.
* **Нет семантического векторного поиска и нет referrals.** Устав DAWN выносит поиск и
  ранжирование за рамки группы; хаб отвечает тем поиском по каталогу, который у него уже есть.
* **Хаб не в корне https-origin** (с префиксом пути или по http при разработке) не имеет
  `did:web`: его записи отдаются без подписи, а `/.well-known/did.json` отвечает 404.
* **`GET /agents` и его параметры фильтра** драфты задают только по имени; форма ответа выше —
  решение этого хаба.

## За обратным прокси

Прокси перед хабом должен передавать ему эти пути: `/.well-known/ard.json`, `/.well-known/did.json`, `/.well-known/agent-card.json`, `/.well-known/mcp/server-card.json`, `/.well-known/agent-marketplace` и всё под `/ard/`. Если прокси отправит карточку
куда-то ещё, её хеш в манифесте не совпадёт, а без `/ard/` реестр не виден.

У хаба под путём (`https://example.org/hub`) манифест лежит по адресу `/hub/.well-known/ard.json`. На
`/.well-known/ard.json` в корне origin отвечайте одним редиректом туда (клиент следует не более чем за одним), как
показано в [production-deployment.ru.md](./production-deployment.ru.md). У такого хаба нет `did:web`, и записи он
отдаёт без подписи.

## Соответствие схеме

Манифест, страница `GET /agents`, запрос и ответ поиска, которые выдаёт этот код, проходят
проверку по CDDL из draft-bu-dawn-ard-discovery-00 §5
([`ard-conformance/check.sh`](./ard-conformance/check.sh)); намеренно испорченные копии не
проходят. CDDL из драфта требует двух правок, прежде чем инструмент его примет: он заново
определяет тип прелюдии `any`, а инструмент (cddl 0.12.14) не обрабатывает выбор групп
`url // data` внутри карты записи, поэтому проверка использует эквивалентную форму с двумя
вариантами карты. Оба файла лежат в папке.

## Настройки

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `AIMARKET_ARD_CACHE_S` | `60` | Сколько секунд каталог в виде записей ARD живёт до пересборки. `0` — пересборка на каждый запрос. |

Сам манифест собирается один раз за жизнь процесса: всё, что он закрепляет, не меняется, пока
процесс работает.
