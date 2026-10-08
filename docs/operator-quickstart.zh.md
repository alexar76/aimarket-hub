# 自己运营这个枢纽 (Hub)

[English](operator-quickstart.md) · [Русский](operator-quickstart.ru.md) · [Español](operator-quickstart.es.md) · [Français](operator-quickstart.fr.md) · [中文](operator-quickstart.zh.md)

> 术语遵循规范的[本地化术语表](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)。产品名称、协议标识符、URL、CLI 命令和环境变量一律不翻译。

你克隆了它、部署了它，自然会问：这对我有什么用？本页是从一个全新部署到一个真正能收钱的枢纽的最短路径——不需要区块链，不需要合约，不需要钱包，也不需要在任何地方开账户。

## 三分钟

```bash
pip install "aimarket-hub[pqc]"        # [pqc]：后量子签名（见下文）
export AIMARKET_CREDITS_ENABLED=1      # 支付轨道
export AIMARKET_ORACLE_FAMILY_URL=off  # 自己为发布者评分（见下文）
export AIMARKET_PQC=1                  # 混合签名：Ed25519 + ML-DSA-65
python -m aimarket_hub quickstart
python -m aimarket_hub serve
```

`quickstart` 做三件新枢纽自己做不到的事：

1. **上架一个真正属于你、真正可执行的 capability。** 否则，新目录要么是空的，要么由不属于你的对等节点组成。示例是一个*静态包*：存放在 `prompt_template` 中的 JSON 对象，调用处理器原样返回它，因此它不需要提供方、不需要模型，也不需要网络。设置 `invoke_url` 换成你自己的服务，或者直接编辑这个包。
2. **生成一个买家密钥**，附带少量初始余额，让你在给任何人看之前，先在自己的枢纽上完成一笔交易。
3. **打印两条 curl 命令**，用来完成这笔交易。

它还会告诉你枢纽将如何签名，以及密钥存放在哪里。

## 从第一次启动起就使用后量子签名

上面两行与 PQC 有关的命令作用不同：

- **`[pqc]` 让枢纽能看见它的对等节点。** 它安装 ML-DSA-65 验证器。后量子签名的校验以失败关闭方式进行，因此没有它的枢纽永远不会收录采用混合签名的对等节点——而所有 AIMarket 枢纽都采用混合签名。
- **`AIMARKET_PQC=1` 让枢纽进行混合签名。** 它的清单、发现文档和收据在 Ed25519 签名旁边附带 ML-DSA-65 签名；对等节点第一次看到你的后量子公钥时就会将其固定。

后量子密钥是与经典密钥并列的第二个文件：`<AIMARKET_SIGNING_KEY_PATH>_mldsa`，默认是 `data/hub_signing_key_mldsa`。两者要一起备份，并放在同一个卷上。一旦丢失 ML-DSA 文件，每个固定过它的对等节点都会拒绝你的枢纽，直到它重新固定（`POST /federation/peers/repin`）。背景说明：[后量子迁移](https://github.com/alexar76/aicom/blob/main/docs/pqc-migration.zh.md)。

## 你如何收款

有两条轨道，可以只用一条，也可以两条都用。

| | 积分（`X-API-Key`） | 通道（`X-Payment-Channel`） |
|---|---|---|
| 配置 | 一个环境变量 | 你在 Base 上部署的托管合约 + 5 项预检设置 |
| 最小计费金额 | $0.00001 | $0.01（整美分，向上取整） |
| 谁持有资金 | 你，预付 | 你，预付（链上存款） |
| 买家需要什么 | 一个 HTTP 客户端 | 一个有余额的钱包和一个 typed data 签名 |

**积分**是在 `AIFACTORY_CRYPTO_ENABLED=0`（默认值）下就能工作的轨道。开启后，上架价格就意味着真钱：付费 capability 以 `402` 应答并列出它接受的轨道；带有效 `X-API-Key` 的调用会在提供方运行前预留价款，成功时扣款，任何失败都会退回。

```
POST /ai-market/v2/accounts                    # 买家生成密钥（有频率限制）
GET  /ai-market/v2/account                     # 买家余额
GET  /ai-market/v2/account/ledger              # 其自有资金的每一笔变动
POST /ai-market/v2/accounts/{id}/credit        # 你为其充值  (admin bearer)
POST /ai-market/v2/accounts/{id}/status        # 你停用某个账户  (admin bearer)
GET  /ai-market/v2/stats/live                  # summary.credits：已赚取与应付
```

资金只能通过 `/credit` 进入，而这个路由只属于你。无论你用什么方式收款——发票、网站结账、你亲眼看到到账的稳定币转账——都是在钱到手之后再调用 `/credit`。枢纽不会假装自己收到了什么。

### 你承担的责任

预付余额是客户放在你账本里的钱。正因如此，`stats/live` 把 `outstanding_credit_usd` 和 `credits_earned_usd` 放在一起公布：前者是负债，后者是收入，分不清两者的枢纽迟早会把其中一个当成另一个花掉。枢纽本身无法转出任何价值，所以退款由你来做。

### 可调参数

| 变量 | 默认值 | 含义 |
|---|---|---|
| `AIMARKET_CREDITS_ENABLED` | `0` | 轨道本身 |
| `AIMARKET_CREDITS_OPEN_SIGNUP` | `1` | 陌生人能否自行生成密钥 |
| `AIMARKET_CREDITS_FREE_GRANT_USD` | `0.05` | 自助生成密钥的初始余额 |
| `AIMARKET_CREDITS_SIGNUPS_PER_HOUR` | `5` | 每个地址自助注册的上限 |
| `AIMARKET_ECOSYSTEM_FILE` | 数据库旁的 `ecosystem.json` | 哪些账户、服务器和钱包属于你自己的生态：它们的调用是 SELF，而不是外部需求；见[流量类别](traffic-classes.zh.md) |
| `AIMARKET_OPERATOR_ACCOUNTS` | 空 | 该文件中 `self.accounts` 的旧式简写（逗号分隔的账户 id）；会合并进去 |
| `AIMARKET_ROUTING_FEE_BPS` | `100` | 你代理其他枢纽的 capability 时的抽成 |
| `AIMARKET_PQC` | 未设置 = 自动 | 未设置时：新枢纽使用混合签名（Ed25519 + ML-DSA-65），已有 ML-DSA 密钥的枢纽继续用它签名，仅 Ed25519 的枢纽保持不变；`1` / `0` 显式指定 |

## 让别人在你的枢纽上发布

积分账户是一个身份，而不只是一个钱包，所以别人也通过它在你这里上架 capability——不用改 `.env`，不用重启，不用区块链。

```bash
curl -X POST $HUB/ai-market/v2/accounts                      # 他们拿到密钥
curl -X POST $HUB/ai-market/v2/accounts/<id>/credit \
     -H "Authorization: Bearer $ADMIN" -d '{"amount_usd": 30}'   # 你收下他们的钱
curl -X POST $HUB/ai-market/v2/supply/stake \
     -H "X-API-Key: <key>" -d '{"amount_usd": 25}'               # 他们缴纳保证金
curl -X POST $HUB/ai-market/v2/supply/register \
     -H "X-API-Key: <key>" -d @manifest.json                     # 他们上架
```

一个密钥只代表一个发布者：它自己的账户。它不能替别人缴纳保证金，也不能以别人的名义发布——共享的发布令牌从来防不住这一点。

保证金是从他们余额中真实扣除的款项，因此是你持有并可以罚没的钱。它被记录为一种独立的抵押类型，区别于开发用的占位值，并且能通过生产环境的保证金检查。链上路径依然存在，依然要求经过验证的 `tx_hash`；但如果枢纽的镜像不带存款验证器，就不必再在“没有抵押”和“无法使用的检查”之间二选一。

发布者信任：设置 `AIMARKET_ORACLE_FAMILY_URL=off` 时没有可查询的预言机，因此发布者获得文档规定的中性初始分，信任检查不适用。如果需要评分，请把它指向你自己运行的 LUMEN 实例。

## 向你的卖家付款

当某个 capability 的发布者在这里有积分账户时，每笔完成的交易都会自动分账：`AIMARKET_PUBLISHER_SHARE_BPS`（默认 70%）记入发布者账户，其余归你。这是枢纽以前从未有过的卖家收益通道：通道账本无法转出价值，唯一的义务表退款给存款人而不是提供方。在此之前，卖家可以上架、被调用，却仍得请你手动转给他十分之一美分。

`stats/live` 如实报告分账：`credits_earned_usd` 是总额（买家花了多少），`publisher_payouts_usd` 是付给卖家的部分，`operator_net_usd` 是你留下的部分。失败的调用不给任何人付钱——付款与向买家扣款走的是同一次扣款。

## 代理其他枢纽

当你把一次调用路由给一个你并未为其销售的对等节点时，路由费会**在向对等节点提出任何请求之前预留**，用的是买家正在使用的那条轨道。没有付款的调用方会收到写明费用的 `402`；拒绝服务的对等节点不会让买家花一分钱；收费低于其公布价格的对等节点按较低金额结算。如果你不想对代理收费，设置 `AIMARKET_ROUTING_FEE_BPS=0`——这就是关闭方式，而不是缺了某个请求头。

基于托管的通道无法授权路由费（没有针对它的签名授权），因此这种组合会被拒绝，而不是记成你永远收不到的收入。走这条路径的买家应使用积分账户。

### 转售收费的对等节点

只有当你能付钱给对方时，代理才会产生资金流动。在对等节点那里持有一个积分账户，并配置它：

```bash
export AIMARKET_PEER_API_KEYS="https://peer.example=aimk_your_key_there"
```

这样，路由到该对等节点的调用就是一次转售：向你的买家收取目录价加你的费用，你从你在对方那里的账户付款给对等节点，费用就是你的利润。没有密钥时，对等节点的 `402` 会到达一个在那里没有账户、无法处理它的买家——这就是为什么线上联邦里一直只有免费的对等节点。

对等枢纽通过它们的 `/ai-market/v2/invoke` 调用，而不是通过 `mcp_endpoint`：那个端点在 SSE 上使用 MCP JSON-RPC，会在 `text/event-stream` 响应体里对路由过来的信封回复 `Method not found`。在修复之前，枢纽之间的路由是不可能的。

## 接受 x402 付款

目录中的销售直接付给卖家。402 写明该列表的 `payout_address` 和一个发票 `nonce`，只有它的 JSON 响应体中带有 `payment_secret`，且 `nonce = sha256(payment_secret)`。买家通过针对该 nonce 签名的 EIP-3009 `transferWithAuthorization` 在链上付款到这个钱包，然后带上 `X-Payment: <tx hash>`（或 `PAYMENT-SIGNATURE`）、`X-Payment-Nonce` 和 `X-Payment-Secret` 重试。秘密证明付款的正是这位买家：交易及其 nonce 一旦上链就是公开的。枢纽验证这笔转账，从不持有资金（`aimarket_hub/settle.py`）：只有该授权所移动的转账才算付款，而且每个授权都是一项独立的主张，所以一笔交易里的两个授权可以购买两次调用。在联邦调用中，只有当枢纽在同一请求上完成了结算，付款才会随你的对等节点密钥一起传递，并使用它自己的 nonce、保留秘密。对该对等节点没有密钥、也没有结算任何东西的枢纽只是中继，会把买家的付款请求头（包括秘密）原样转发。每笔付款都是绑定的（直接转账给卖家会被拒绝；`AIMARKET_SETTLE_REQUIRE_BINDING=0` 会被忽略），因此为旧版枢纽编写、只发送 `X-Payment` 和 `X-Payment-Nonce` 的买家工具会被拒绝，直到它也发送秘密。MCP 网关以 `x_payment_secret` 接收它，A2A 桥会自行提交。迁移 `040` 增加了保存它的发票列，并且 `X-Payment-Secret` 在 CORS 允许列表中，因此来自 `AIMARKET_CORS_ORIGINS` 来源的浏览器客户端也能兑付。`AIMARKET_X402_ACCEPT=0` 保持仅发现模式。

积分和支付通道仍然只是预付的便利手段。持有钱包的发布者并不是通过它们收款的。

## 让陌生人找到你

始终开着两扇观察之门，两者都通向隔离，而不是立即信任：一个无需认证的 `POST /federation/announce`，以及当对等节点抓取你并表明身份时的双向发现。无论哪种方式，对等节点都会进入 `pending` 状态，且 `trusted=false`。随后会自动运行一次沙箱测试；结果为 `pass` 时无需点击 Approve 即可收录。fail 和 review 会在 `/operator` 中保持待处理。另有一张预览表保存待处理对等节点声称提供的内容；这些行永远不会进入搜索或路由，因为它们根本不在 `capabilities` 表中。见 `docs/join-the-federation.md`。

## 独立运营

有两个默认值指向参考部署，你很可能两个都不想要：

- **发布者信任。** `AIMARKET_ORACLE_FAMILY_URL` 默认指向另一个运营者的 LUMEN 实例。保留它，你能否发布就取决于对方是否在线：在它不可达时发布的 capability 会以无评分状态保存，而它的每次调用都会返回 502。`off` 表示这个枢纽没有信任预言机：发布者获得文档规定的中性初始分，检查不适用。如果你自己运行了实例，就指向它。
- **联邦种子。** 自带的种子列表是参考枢纽自己的卫星服务。它们是可供浏览的目录，而不是属于你的供给；见 `AIMARKET_SEED_LIST`。

## 命名

代码采用 Apache-2.0 许可（生态中其他部分为 MIT），你可以商业化运营、自定费率并全部归你所有；没有任何分成会流向别处。这里不授予商标许可，所以不要把你的部署称为“AIMarket”，也不要暗示获得认可。“Implements the AIMarket Protocol”和“interoperates with <peer>”是准确且可以使用的表述。任何人都不能宣称“certified”或“conformant”——目前还没有一致性测试套件。
