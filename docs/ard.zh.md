# ARD — 面向 Agent Resource Discovery 客户端的 hub 目录

> **English:** [ard.md](./ard.md) · **Русский:** [ard.ru.md](./ard.ru.md) · **Español:** [ard.es.md](./ard.es.md) · **Français:** [ard.fr.md](./ard.fr.md)
>
> 代码：[`ard.py`](../aimarket_hub/ard.py) · [`hub_routes/ard.py`](../aimarket_hub/hub_routes/ard.py) · 测试：[`tests/test_ard.py`](../tests/test_ard.py) · 模式校验：[`docs/ard-conformance/`](./ard-conformance/)

Agent Resource Discovery（ARD）是 Google、Ericsson 等机构的作者于 2026-10-30 提交给 IETF DAWN
工作组的一组 Internet-Draft：

* [draft-jimenez-dawn-ard-initial-contact-00](https://datatracker.ietf.org/doc/draft-jimenez-dawn-ard-initial-contact/) ——
  客户端如何找到 manifest（`/.well-known/ard.json`、`ard` 链接关系、SVCB、DNS-SD）；
* [draft-bu-dawn-ard-discovery-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-discovery/) ——
  条目与 manifest 的格式、`urn:air` 名称、带签名的 `trustManifest`，以及注册表查询
  （`POST /search`、`POST /explore`、`GET /agents`）；
* [draft-bu-dawn-ard-architecture-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-architecture/) —— 角色与流程。

从 3.18.0 版起，每个 hub 也用这些格式作答。它是通往同一个目录的又一扇门，与 MCP 和 A2A 两扇门
一样；AIMarket Protocol v2 没有任何变化。ARD 客户端找到 hub 及其能力，调用和付款仍按 v2 进行
（`POST /ai-market/v2/invoke`）。付款、收据和联邦准入不属于 ARD，仍留在 v2 中。

## hub 提供的内容

| 路径 | 方法 | 说明 |
|---|---|---|
| `/.well-known/ard.json` | GET, HEAD | hub 的 ARD manifest，`application/ard+json`。可缓存一小时，带 ETag。 |
| `/` | HEAD, GET | 带 `Link: <…/.well-known/ard.json>; rel="ard"`，供只知道 origin 的客户端使用。 |
| `/.well-known/did.json` | GET, HEAD | hub 的 `trustManifest` 签名所指向的 `did:web` 文档。 |
| `/ard/v1` | GET | 本注册表实现了什么（ARD 未定义，客户端可以忽略）。 |
| `/ard/v1/search` | POST | 搜索：文本、过滤、分页。 |
| `/ard/v1/explore` | POST | 对查询选中的条目做分面计数。 |
| `/ard/v1/agents` | GET | 全部条目，按标识符排序。 |
| `/ard/v1/capability` | GET, HEAD | 单个能力的描述：条目 `url` 所指向的文档。 |

以上路径全部公开、只读。

## manifest

`/.well-known/ard.json` 列出的是 hub 自身的资源，而不是目录：

| 条目 | `type` | `url` | 用 `digest` 固定 |
|---|---|---|---|
| 注册表 | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| 作为 A2A agent 的 hub | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | 是 |
| 作为 MCP server 的 hub | `application/mcp-server-card+json` | `<hub>/.well-known/mcp/server-card.json` | 是 |
| AIMarket discovery 文档 | `application/json` | `<hub>/.well-known/agent-marketplace` | 否 |

`digest` 是 hub 在 `url` 处实际返回的字节的 SHA-256，构建 manifest 时通过 hub 自身取得。
discovery 文档没有 digest，因为它每分钟重新签名一次（其 `generated_at` 在它自己的 Ed25519 签名
之内），对它取的 digest 不到一分钟就会过时。

想要目录的客户端沿着注册表条目继续，正如草案所述：manifest 可以由指向注册表的条目组成，由注册表
回答查询。

## 目录条目

注册表为 hub 出售的每个能力返回一个条目，与 HTTP、MCP、A2A 三扇门展示的集合相同：可履约、公开、
路由可售。

| ARD 字段 | 取值 |
|---|---|
| `identifier` | `urn:air:<publisher>:<product_id>:<capability_id>` |
| `displayName` | 目录中的显示名称 |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `description` | 能力的说明 |
| `capabilities` | 能力的工具名（即 MCP 工具名） |
| `version` | v2 的 `version`（发布者未另行设置时为 `v1`） |
| `digest` | `url` 处描述字节的 SHA-256 |
| `tags` | 联邦条目使用其来源 hub 声明的分类 |
| `metadata.aimarket` | `product_id`、`capability_id`、`source_hub`、`price_per_call_usd`、`access_mode`、`invoke_url` |
| `trustManifest` | 本 hub 发布的能力才有；见“签名” |

`<publisher>` 是发布该条目的主机：本地能力为本 hub，从已准入对等 hub 索引来的能力为该对等 hub。
命名空间和名称就是 v2 的 `product_id` 和 `capability_id`，因此标识符可以原样还原成一次 v2 调用。
会破坏名称结构的字符（`:`、`/`、`?`、`#`、空格）用 `%` 编码。

`url` 处的描述只包含买方需要的、不会自行变化的内容：模式、价格、调用请求
（`POST <hub>/ai-market/v2/invoke`，带 `product_id`、`capability_id`、`source_hub`、`input`）、
MCP 端点和工具名，以及指向 A2A 卡片、已签名的 v2 目录和 discovery 文档的链接。实测成功率、延迟和
信任分不包括在内：它们几分钟就会变化，而条目用 digest 固定了描述。

## 签名

本 hub 发布的每个条目都带有 `trustManifest`（draft-bu-dawn-ard-discovery-00 §3.5）：

* `identity` 为 `did:web:<host>`。其 DID 文档 `/.well-known/did.json` 首先列出 hub 的
  Ed25519 密钥，也就是其 AIMarket discovery 文档以 `signer_public_key` 公布的同一把密钥。自 hub 3.21.0 起，
  混合签名的 hub 还会在其中列出 ML-DSA-65 密钥，供 [AI Catalog 入口](./ai-catalog.zh.md)使用；ARD 的
  `trustManifest` 签名只使用 Ed25519 密钥。
* `subject` 重复条目的 `identifier`、`type`、`version` 和 `digest`。
* `signature` 是分离式紧凑 JWS（RFC 7515 附录 F）：`<header>..<signature>`，头部为
  `{"alg": "EdDSA", "kid": "did:web:<host>#hub-ed25519"}`，签名计算对象为
  `base64url(header) "." base64url(JCS(去掉 signature 的 trustManifest))`，JCS 按 RFC 8785。

hub 从对等方索引来的条目不带 `trustManifest`：本 hub 不是它的发布者。草案把这样的条目视为发现
提示（§6.2）。它在搜索结果中的 `source` 是对等 hub 的 URL。

校验一个条目（Python，使用 `cryptography` 和 `rfc8785`）：

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

## 查询

**`POST /ard/v1/search`** 接收 `{"query": {"text", "filter"}, "federation", "pageSize", "pageToken"}`。

* `query.text` 必填，最多 2,000 个字符。它经过 hub 自己的目录搜索（词法匹配加上 EN、RU、ES、FR、ZH
  多语言概念匹配），所以顺序与 `/ai-market/v2/search` 给出的一致。
* `score` 是该搜索的相关度换算到 0..100。它对文本排序，不代表质量或安全性。
* `filter` 的键：`type`、`tags`、`capabilities`、`publisher`、`identifier`、
  `trustManifest.identity`。值为字符串或字符串数组；同一键的多个值按“或”组合，不同键按“与”组合。
  其他键一律以 400 拒绝：被悄悄忽略的过滤条件会返回调用方要求排除的条目。
* `pageSize` 默认 10，最大 100。`nextPageToken` 与生成它的查询绑定；随其他查询发送的令牌会被拒绝。
* `federation`：hub 会复制它准入的对等方的目录（AIMarket 联邦），因此 `auto` 和 `none` 都从本地
  索引作答，该索引已经包含已准入对等方的条目。不会转发任何查询。`referrals` 目前只附加一个空的
  `referrals` 列表。

**`POST /ard/v1/explore`** 接收 `{"query": {…}, "resultType": {"facets": [{"field", "limit"}]}}`。
这里 `query.text` 可选。分面字段：`type`、`tags`、`capabilities`、`publisher`。

**`GET /ard/v1/agents`** 返回按 `identifier` 排序的 `{"entries": […], "nextPageToken": …}`。过滤条件
是与过滤键同名的查询参数；同一参数重复出现表示“或”（`?capabilities=a&capabilities=b`）。`pageSize`
和 `pageToken` 同上。

错误采用 RFC 9457 problem details（`application/problem+json`），`error` 码取自草案：
`INVALID_ARGUMENT`（400）、`NOT_FOUND`（404）。

## 从条目到调用

```bash
curl -s https://modelmarket.dev/.well-known/ard.json
curl -s -X POST https://modelmarket.dev/ard/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "weather in Paris"}, "pageSize": 3}'
curl -s '<某个结果的 url>'      # 描述：其中的 "invoke" 成员就是 v2 请求
```

描述中的 `invoke.body` 是 `input` 为空的 v2 调用请求；填好它，并像任何 v2 调用一样付款
（[agent-first-call.zh.md](./agent-first-call.zh.md)）。

## 局限

* **EdDSA，而非 ES256。** 草案把 ES256 定为必须实现。本 hub 只持有 Ed25519 密钥，因此签名为
  EdDSA（RFC 8037）；只实现 ES256 的验证方无法校验它们。
* **能力的媒体类型未注册。** `application/vnd.aimarket.capability+json` 属于厂商树类型；ARD 没有
  “网关后面的单个可调用能力”的类型，只有整个 MCP server 或 A2A agent 的类型。
* **没有语义向量搜索，也没有 referrals。** DAWN 章程把搜索和排序排除在范围之外；hub 使用它已有的
  目录搜索作答。
* **不在 https origin 根路径上的 hub**（带路径前缀，或开发时用 http）没有 `did:web` 身份：其条目不带
  签名，`/.well-known/did.json` 返回 404。
* **`GET /agents` 及其过滤参数** 草案只给出了名称；上面的格式是本 hub 的做法。

## 位于反向代理之后

hub 前面的代理必须把这些路径转给 hub：`/.well-known/ard.json`, `/.well-known/did.json`, `/.well-known/agent-card.json`, `/.well-known/mcp/server-card.json`, `/.well-known/agent-marketplace`，以及 `/ard/` 下的全部路径。代理若把某张卡片转到别处，manifest 中
它的 digest 就会对不上；缺少 `/ard/` 则注册表不可见。

挂在路径下的 hub（`https://example.org/hub`）的 manifest 位于 `/hub/.well-known/ard.json`。请让 origin 的
`/.well-known/ard.json` 用一次重定向指向它（客户端最多跟随一次），做法见
[production-deployment.zh.md](./production-deployment.zh.md)。这样的 hub 没有 `did:web` 身份，其条目不带签名。

## 一致性

本代码生成的 manifest、一页 `GET /agents`、一个搜索请求和一个搜索响应，都通过了
draft-bu-dawn-ard-discovery-00 §5 中 CDDL 的校验（[`ard-conformance/check.sh`](./ard-conformance/check.sh)）；
故意改坏的副本不能通过。草案中的 CDDL 需要改两处，CDDL 工具才肯接受：它重新定义了预置类型 `any`；
所用工具（cddl 0.12.14）无法处理条目映射内部的组选择 `url // data`，因此校验使用了一个等价写法，即两个
映射备选。两份文件都在该目录中。

## 设置

| 变量 | 默认值 | 含义 |
|---|---|---|
| `AIMARKET_ARD_CACHE_S` | `60` | 以 ARD 条目形式保存的目录在两次重建之间保留多少秒。`0` 表示每次请求都重建。 |

manifest 本身每个进程只构建一次：它所固定的内容在进程运行期间不会变化。
