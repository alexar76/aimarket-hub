# AI Catalog：面向 AI Catalog 客户端的 hub 目录，以及 hub 的 MCP 服务器卡片

> **English:** [ai-catalog.md](./ai-catalog.md) · **Русский:** [ai-catalog.ru.md](./ai-catalog.ru.md) · **Español:** [ai-catalog.es.md](./ai-catalog.es.md) · **Français:** [ai-catalog.fr.md](./ai-catalog.fr.md)
>
> Code: [`ai_catalog.py`](../aimarket_hub/ai_catalog.py) · [`hub_routes/ai_catalog.py`](../aimarket_hub/hub_routes/ai_catalog.py) · Tests: [`tests/test_ai_catalog.py`](../tests/test_ai_catalog.py) · Signer profiles: [`did-web-eddsa-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-eddsa-v1.md), [`did-web-ml-dsa-65-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-ml-dsa-65-v1.md)

[AI Catalog](https://ai-catalog.io/) 是一种 JSON 格式，用来列出各类 AI 制品：MCP 服务器、A2A 代理、技能、
数据集以及其他目录。它由 MCP 和 A2A 项目的成员在 Linux Foundation 的工作仓库
（[Agent-Card/ai-catalog](https://github.com/Agent-Card/ai-catalog)）中编写。目录是一组条目；每个条目给出制品的
名称（`identifier`）和类型（`type`），并通过 `url` 指向制品，或通过 `data` 直接携带制品。

MCP 用它做发现。Server Card 扩展（[SEP-2127](https://modelcontextprotocol.io/seps/2127-mcp-server-cards)，状态为 Final）规定：只知道域名的客户端请求
`/.well-known/ai-catalog.json`，取出类型为 `application/mcp-server-card+json` 的条目，再按每个条目的 `url`
取得服务器卡片，卡片说明应连接到哪里。

自 hub 3.21.0 起，每个 hub 都以这些格式作答。与 MCP、A2A 和 [ARD](./ard.zh.md) 入口一样，它是通向同一份目录的
又一个入口。AIMarket Protocol v2 没有任何变化：客户端在这里找到 hub 及其出售的内容，调用和付费仍按 v2 进行
（`POST /ai-market/v2/invoke`）。

本文依据 2026-11-11 的 AI Catalog 规范文本（提交 `e2b425f9`，`specVersion` 为 `1.0`）以及 [`modelcontextprotocol/ext-server-card@526201bb`](https://github.com/modelcontextprotocol/ext-server-card/tree/526201bbc80231daa40ffcdecfc9da4e54e5dc93)
处的 Server Card 扩展（即 SEP-2127 指明的快照）。AI Catalog 文本仍是工作草案，还在变化。

## hub 提供什么

| 路径 | 内容 |
|---|---|
| `/.well-known/ai-catalog.json` | hub 的目录，`application/ai-catalog+json`。可缓存一小时，带 ETag。 |
| `/` | 带有 `Link: <…/.well-known/ai-catalog.json>; rel="ai-catalog"`，与 `rel="ard"` 链接并列。 |
| `/mcp/server-card` | hub 的 MCP 端点对应的服务器卡片，`application/mcp-server-card+json`。 |
| `/ai-catalog/v1/capabilities.json` | 嵌套目录：hub 出售的全部能力。 |
| `/ai-catalog/v1/ids/<id>` | hub 为对等方的能力分配的标识符；重定向（303）到其描述文档。 |
| `/ai-catalog/profiles/<name>` | 签名配置文件的定义（见“签名”）。 |
| `/.well-known/did.json` | 签名所指的 `did:web` 文档（与 ARD 入口共用）。 |

以上路径均响应 `GET` 和 `HEAD`，公开且只读。

## 目录

`/.well-known/ai-catalog.json` 列出 hub 自己的制品：

| 条目 | `type` | `url` | `digest` |
|---|---|---|---|
| 作为 MCP 服务器的 hub | `application/mcp-server-card+json` | `<hub>/mcp/server-card` | 有 |
| 作为 A2A 代理的 hub | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | 有 |
| hub 出售的内容 | `application/ai-catalog+json` | `<hub>/ai-catalog/v1/capabilities.json` | — |
| 同一内容，可搜索 | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| AIMarket discovery 文档 | `application/json` | `<hub>/.well-known/agent-marketplace` | — |

* `host` 指明运营者：`displayName` 是 hub 的名称，`identifier` 是它的 `did:web`（hub 没有 `did:web` 时为主机名，
  即 hub 部署在子路径下、使用 http 或使用非默认端口的情况）。
* `digest` 的形式为 `sha256:` 加上 hub 在 `url` 处所返回字节的 SHA-256（小写十六进制），在构建目录时经由 hub
  自身取得。后三个条目没有 `digest`：它们的文档随报价变化（discovery 文档每分钟重新签名）。
* 两个卡片条目不带 `displayName`、`description` 和 `version`。卡片自己给出这些值，规范要求条目不要重复可能与制品
  不一致的值。
* 同一制品在这里和在 ARD 清单中的 `identifier` 相同。

该目录符合规范三个一致性级别中的第 2 级（Discoverable），带有摘要以及下文所述的签名。

## MCP 服务器卡片

`/mcp/server-card` 是扩展保留的位置：`<streamable-http-url>/server-card`。

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

（这是位于 `https://modelmarket.dev` 的 hub 所返回的形式；`version` 和协议版本取自正在运行的 hub。）

* `name` 由倒序的主机名和服务器名组成，因此运行同一软件的两个运营者有两个名称。如果运营者的服务器在 MCP 注册表
  中有名称，可用 `AIMARKET_MCP_SERVER_CARD_NAME` 设置（例如 `io.github.alexar76/aimarket-hub`）；不符合
  `namespace/name` 形式的值会被忽略。
* 卡片不列出工具。扩展把工具、资源和提示词留给 `tools/list`，因为静态列表可能与已连接客户端实际得到的不同。
* 卡片与实际端点一致：其 `version` 等于 `initialize` 返回的 `serverInfo.version`，所列的每个协议版本都是端点
  能够协商的版本。测试在运行中的 hub 上检查这两点。
* 响应带有扩展要求的 CORS 和缓存头（`Access-Control-*`、`Cache-Control: public, max-age=3600`、`ETag`，
  对 `If-None-Match` 返回 `304`）。
* `/.well-known/mcp/server-card.json` 处的旧卡片保留。它列出工具，读取卡片而不调用 `tools/list` 的扫描器依赖它。

## hub 出售的内容

`/ai-catalog/v1/capabilities.json` 为 hub 自身搜索所展示的每项能力提供一个条目：与 HTTP、MCP、A2A 和 ARD 入口
展示的集合相同，目录隐藏的报价不会出现。

| 字段 | 值 |
|---|---|
| `identifier` | 见下文 |
| `displayName`、`description` | 目录中的显示名称和能力描述 |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `version` | v2 的 `version`（发布者未设置时为 `v1`） |
| `digest` | `url` 处描述文档字节的 SHA-256 |
| `tags` | `aimarket`；对联邦条目，还有其 hub 声明的类别 |
| `publisher` | 发布该报价的一方：本 hub（`did:web`）或对等方的主机名 |
| `extensions["dev.modelmarket.aimarket"]` | v2 条款：`protocol`、`product_id`、`capability_id`、`source_hub`、`price_per_call_usd`、`access_mode`、`invoke_url` |
| `trustManifests` | 本 hub 发布的能力才有；见“签名” |

`url`、`digest` 和 `version` 与 ARD 注册表为同一能力发布的值相同，因此两个入口固定的是同一份描述文档字节。
这里的 `price_per_call_usd` 是十进制字符串（`"0.01"`）：文档按其 JCS 字节签名，而本协议的规范化器拒绝小数，
因为不同语言的实现对小数的序列化并不一致。

**标识符**遵循规范的投影规则（“Catalog Projection”）：

* 本 hub 自己列出的能力命名为 `urn:air:<本主机>:<product_id>:<capability_id>`，与 ARD 入口给出的名称相同。
* 从已接纳的对等方复制来的能力由该对等方发布，本 hub 并未读取对等方自己为它定的标识符。规范规定，不是发布者的
  运营者 “MUST use a non-`urn:air` identifier under its own control”。这类条目命名为
  `<hub>/ai-catalog/v1/ids/<32 个十六进制字符>`：对同一对等方、产品和能力保持稳定，并且可解析（303 到描述文档）。
  来源写在条目中：`publisher` 给出对等方的主机名，扩展中有 `source_hub`、`product_id` 和 `capability_id`。

ARD 入口把同一联邦能力命名为 `urn:air:<对等方主机>:…`。两个入口在这里不同，是因为两份规范不同。

## 签名

规范只定义了一个签名配置文件 `did-web-v1`，它只接受 ES256（P-256）密钥。hub 持有 Ed25519 密钥，在混合签名时
还持有 ML-DSA-65 密钥；它没有 P-256 密钥。因此 hub 在规范自身的容器中、按本协议定义的两个配置文件签名。规范
允许这样做：在规范之外定义的配置文件以其作者控制的 URL 命名，未实现该配置文件的读者把相应签名视为未验证，并
独立评估其余内容（“Profile Selection”）。

| 配置文件 | 算法 | DID 文档中的密钥 | hub 在何处使用 |
|---|---|---|---|
| `https://modelmarket.dev/ai-catalog/profiles/did-web-eddsa-v1` | `EdDSA`，Ed25519（RFC 8037） | `#hub-ed25519`，`OKP` 类型的 JWK | hub 发布并以摘要固定的每个条目上的 Trust Manifest；没有 ML-DSA-65 密钥的 hub 的快照签名 |
| `https://modelmarket.dev/ai-catalog/profiles/did-web-ml-dsa-65-v1` | `ML-DSA-65`（FIPS 204；JOSE 注册见 RFC 9964） | `#hub-ml-dsa-65`，`AKP` 类型的 JWK | hub 混合签名时，每份目录文档的快照签名 |

除算法和密钥类型外，两者都与 `did-web-v1` 相同：签名者是不带端口的根 `did:web`，`kid` 是签名者的 DID 加上片段，
密钥列在 DID 文档的 `assertionMethod` 下并由签名者控制，JWS 头中不带密钥材料。每个 URL 都返回其定义。

* **条目的 Trust Manifest** 包含 `contributor`（hub 的 `did:web`）、`subject`（条目的 `identifier`、`type`、
  `digest` 和 `version`）以及 `signature`。它是发布者对该版本的背书：签名者等于 `did:web:` 加上条目
  `urn:air` 名称中的发布者段。从对等方复制来的条目没有它，因为本 hub 不是其发布者。
* **目录的快照签名**是顶层的 `signature`。它覆盖整份文档：每个条目、`host` 和嵌套的清单。
* 签名是分离式紧凑 JWS（RFC 7515 附录 F），作用于被签名对象去掉其自身 `signature.jws` 后的 JCS（RFC 8785）字节。
  受保护头为 `{"alg", "kid", "typ"}`，其中 `typ` 为 `ai-catalog-trust-manifest+jws` 或 `ai-catalog+jws`。
* 相同内容只签名一次：条目的清单按版本签，文档的快照按内容签。在报价变化之前，目录的字节和 ETag 保持不变。
* 没有根 `did:web` 的 hub（位于子路径下、使用 http 或带端口）发布的目录不带签名。

### 后量子

支持这种格式不会削弱 hub 的后量子保护，原因有三。

1. 没有任何东西被替换。v2 的 discovery 文档、清单和回执保留其混合签名（Ed25519 与 ML-DSA-65 并用）；对等方仍然
   据此固定两把密钥。
2. 目录带有同样的两层。同时要求 ML-DSA-65 快照签名和条目的 Ed25519 Trust Manifest 的读者，只要两种算法之一
   仍然安全就受到保护，这正是混合签名所提供的。能够伪造 Ed25519 的攻击者可以伪造清单，却无法为包含该清单的目录
   生成 ML-DSA-65 签名。
3. 没有回退。配置文件按其确切名称选定，从不推断，因此 Ed25519 签名不能满足要求 ML-DSA-65 的读者。这是规范的
   规则，`ai_catalog.signature_problem` 中的验证器执行这一规则。

不在其内的部分：只实现 `did-web-v1` 的读者把这些文档视为未签名（第 2 级）。一个 ML-DSA-65 签名占 3,309 字节，
因此它在每份文档上只放一次，而不是放在每个条目上；通过 `url` 到达的嵌套目录是另一份文档，带有自己的签名。

### 检查目录

下面是简化的检查，使用 Python 以及 `cryptography`、`rfc8785` 和（针对混合签名的 hub）`dilithium-py`。它验证第一个
条目的摘要、该条目的 Trust Manifest 以及目录的快照签名。完整的验证器还要执行配置文件定义中的规则（密钥位于
`assertionMethod` 下并由签名者控制、时间、头部成员）；`aimarket_hub.ai_catalog.signature_problem` 和
`endorsement_problem` 实现了这些规则。

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

测试按英文版中印出的原样，在两种 hub 上各运行一次这段代码
（`test_the_documented_check_runs_against_a_hub`）。

## 与 AIMarket 格式的关系

hub 自己的格式既没有被替换，也没有分叉。hub 提供的 AI Catalog 文档对任何客户端都是有效的 AI Catalog 文档，
AIMarket 的补充内容放在规范为补充内容开放的两个位置：

* 条目的 `extensions` 映射中，位于反向 DNS 键 `dev.modelmarket.aimarket` 之下，存放买方需要的条款（价格、访问
  模式、调用地址）；
* 以我们自己的配置文件名称做出的签名，放在规范的 Signature 对象中。

只了解 AI Catalog 的客户端读取条目并忽略这两者，规范正是这样要求的（“Unrecognized Members”、
“Profile Selection”）。了解 AIMarket 的客户端读取同一份文档，并得到条款和签名。带有密钥固定和已接纳对等方列表的
已签名 v2 discovery 文档，作为条目之一列出。

## 设置

| 变量 | 默认值 | 含义 |
|---|---|---|
| `AIMARKET_MCP_SERVER_CARD_NAME` | 未设置 | 运营者在 MCP 注册表中有名称时，卡片的 `name`。 |
| `AIMARKET_AI_CATALOG_CACHE_S` | `60` | 能力目录在根据 hub 搜索重建之前保留的秒数。 |
| `AIMARKET_PQC` | 见 `signing.py` | hub 是否混合签名；决定快照签名使用的配置文件。 |

## 限制

* 不是第 3 级（Trusted）：该级别指定 `did-web-v1`，而 hub 没有 ES256 密钥。只实现 `did-web-v1` 的读者把 hub 的
  签名视为未验证。
* AI Catalog 规范是工作草案。其信任部分在 2026 年 10 月和 11 月多次修改（ADR 0025 至 0029）。本入口遵循
  2026-11-11 的文本。
* 没有分页：能力目录是一份文档。规范没有定义分页；位于 `/ard/v1` 的 ARD 注册表对同一目录提供分页和搜索。
* 不链接对等 hub 的嵌套目录：对等方的能力在这里作为本 hub 的副本列出，使用本 hub 的标识符。
* hub 不读取其他站点的目录。HISTOR 会读取，用来查找服务器卡片（[`histor/histor/cards.py`](https://github.com/alexar76/histor/blob/main/histor/cards.py)）。
