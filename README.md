<!-- aicom-mirror-notice -->
> **📖 Read-only mirror.** `aimarket-hub` is published from the canonical AI-Factory monorepo.
> **Pull requests are not accepted** — any commit pushed here is overwritten by
> `scripts/mirror_satellites.sh` on the next sync.
> 🐞 Found a bug or have a request? Please **[open an issue](https://github.com/alexar76/aimarket-hub/issues)**.

# AIMarket Hub

<!-- aicom-readme-badges -->
<p align="center">
  <a href="https://github.com/alexar76/aimarket-hub/actions/workflows/ci.yml"><img src="docs/badges/ci.svg" alt="CI" /></a>
  <a href="https://github.com/alexar76/aimarket-hub/releases"><img src="docs/badges/release.svg" alt="Release" /></a>
  <a href="https://github.com/alexar76/aimarket-protocol"><img src="docs/badges/aimarket.svg" alt="Protocol" /></a>
  <a href="docs/badges/coverage.svg"><img src="docs/badges/coverage.svg" alt="Test coverage" /></a>
  <a href="LICENSE"><img src="docs/badges/license.svg" alt="License: Apache-2.0" /></a>
</p>
<!-- /aicom-readme-badges -->








> **Ecosystem:** [AICOM overview & live demos](https://modeldev.modelmarket.dev) · **Package version:** `3.0.0` (pyproject) · **Community:** [Discord · Pollux](https://discord.gg/aimarket) · [Telegram · Castor](https://t.me/just_for_agents)

**Federation hub for AI capability discovery, micropayment routing, and plugin-extensible invoke.**

Reference implementation of [AIMarket Protocol v2](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md). One HTTP surface to **search** a federated catalog, **open payment channels**, **invoke** capabilities with safety and compliance hooks, and **settle** on-chain — without custodial wallets.

| | |
|---|---|
| **Live hub** | [modelmarket.dev](https://modelmarket.dev) |
| **Well-known** | [/.well-known/ai-market.json](https://modelmarket.dev/.well-known/ai-market.json) |
| **Plugin demo** | [/plugins/demo](https://modelmarket.dev/plugins/demo) |
| **Widget demo** | [/widget/demo](https://modelmarket.dev/widget/demo) |
| **Plain-language value** | [docs/value.md](docs/value.md) |

## Demo

- **Live:** https://modelmarket.dev/
- **Docs:** https://github.com/alexar76/aimarket-hub/blob/main/README.md

---

## Table of contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Core API](#core-api)
- [Invoke lifecycle](#invoke-lifecycle)
- [Plugin ecosystem](#plugin-ecosystem)
- [Federation](#federation)
- [Payments](#payments)
- [Pay-on-Verified](#pay-on-verified)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Development](#development)
- [Security](#security)
- [Related projects](#related-projects)
- [License](#license)

---

## Overview

AIMarket Hub sits between **capability providers** (factory-shipped products, [**oracles**](https://github.com/alexar76/oracles), peer hubs, data-cap publishers) and **consumers** (Flutter desktop apps, agents, embeddable widgets, MCP clients).

**Problems it solves**

| Problem | Hub answer |
|---------|------------|
| Fragmented AI APIs | Federated search over `.well-known/ai-market.json` peers |
| Per-call payment friction | Pre-funded **channels** — one deposit, N micro-invokes, one settlement |
| Trust in anonymous sellers | **Reputation** scores + stake bonds (plugin) |
| Compliance & audit | **Provenance** receipts on every invoke (Ed25519 + W3C VC) |
| Unsafe prompts | **Safety** pre-check with signed rejection + refund |


### Zero-Trust Agent Discovery

**No human app-store reviewer.** Agents **find peers over federation, pass safety + attestation gates, and invoke only verified capabilities** — cryptographic trust replaces marketing trust.

| | |
|---|---|
| **What** | Federated `discover` → safety / reputation / TEE plugins → routed invoke |
| **Why** | Scales to millions of micro-capabilities; malicious listings can’t drain channels |
| **Deep dive** | [docs/killer-feature-zero-trust-discovery.md](docs/killer-feature-zero-trust-discovery.md) · [Ecosystem capabilities](../docs/killer-features.md) |

---

## Architecture

### System context

```mermaid
C4Context
  title AIMarket Hub — system context

  Person(consumer, "Consumer", "App, agent, or widget user")
  Person(provider, "Provider", "Lists capabilities on a hub")

  System(hub, "AIMarket Hub", "Search, route, invoke, settle")
  System_Ext(peers, "Peer hubs", "Federated catalogs")
  System_Ext(factory, "AI-Factory", "Shipped products → capabilities")
  System_Ext(chain, "Base L2", "USDT channels")

  Rel(consumer, hub, "discover · channel · invoke")
  Rel(provider, hub, "manifest · capabilities")
  Rel(hub, peers, "crawl · route")
  Rel(hub, factory, "import shipped products")
  Rel(hub, chain, "open/close channel")
```

### Container diagram (this repository)

```mermaid
flowchart TB
  subgraph hub_process["aimarket_hub (FastAPI)"]
    API["api.py — REST /ai-market/v2/*"]
    CRW["crawler.py — BFS federation"]
    DB["database.py — capability index"]
    CH["channels.py — ledger + settle"]
    SG["safety_gate.py"]
    PR["plugin.py — PluginRegistry"]
    FB["factory_bridge.py"]
    SIG["signing.py — Ed25519 manifests"]
  end

  subgraph plugins["plugins/ (entry_points)"]
    P1["safety · provenance · channels …"]
  end

  subgraph storage["Persistence"]
    SQL[("SQLite / PostgreSQL")]
  end

  API --> CRW
  API --> DB
  API --> CH
  API --> SG
  API --> PR
  API --> FB
  CRW --> DB
  FB --> DB
  DB --> SQL
  PR --> P1
  P1 -.->|"pre/post hooks"| API
```

### Factory import path

Shipped AI-Factory products are indexed as local capabilities on hub startup:

```mermaid
sequenceDiagram
  participant Factory as AI-Factory pipeline.db
  participant Loader as factory_products_loader
  participant Bridge as factory_bridge
  participant Index as database (SQLite)

  Note over Factory,Index: On hub startup or sync script
  Factory->>Loader: COMPLETED / DEPLOYED products
  Loader->>Bridge: normalize capabilities
  Bridge->>Index: upsert source_hub=local
  Index-->>Bridge: indexed count
```

Sync ops: [`../scripts/sync_pipeline_mirror_and_hub.py`](../scripts/sync_pipeline_mirror_and_hub.py)

---

## Repository layout

```
aimarket-hub/
├── aimarket_hub/           # Core package
│   ├── api.py              # HTTP routes (search, invoke, federation, plugins)
│   ├── crawler.py          # Peer discovery (SSRF-hardened BFS)
│   ├── database.py         # Capability + peer index
│   ├── channels.py         # Payment channel ledger
│   ├── plugin.py           # setuptools aimarket.plugins loader
│   ├── factory_bridge.py   # AI-Factory product import
│   ├── safety_gate.py      # Built-in safety fallback
│   └── …
├── plugins/                # Hub-local plugins (e.g. aimarket-provenance)
├── tests/                  # pytest suite
├── Dockerfile
├── LICENSE                 # Apache-2.0
├── CONTRIBUTORS.md
├── SECURITY.md
└── docs/
    └── value.md
```

**Sibling packages** (monorepo root [`plugins/`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/)): 15 plugins — **top-5 on PyPI** ([install guide](https://github.com/alexar76/aimarket-plugins/blob/main/plugins/docs/install.md)); full set bundled in Docker.

---

## Quick start

### Prerequisites

- Python **3.11+**
- Optional: Docker for container deploy

### Install & run

```bash
pip install aimarket-hub
# optional core plugins (TEE, channels, reputation, safety, MCP packager):
pip install "aimarket-hub[plugins]"
aimarket serve
# → http://localhost:9083
```

Verify discovery and search:

```bash
curl -s http://localhost:9083/.well-known/ai-market.json | jq .
curl -s "http://localhost:9083/ai-market/v2/search?intent=translate&budget=1" | jq .
curl -s http://localhost:9083/ai-market/v2/plugins | jq '.plugins | length'
```

### Publish a capability (community providers)

Third-party developers list an HTTP endpoint in the catalog and earn USDC when agents invoke it. **Production hubs** require stake, LUMEN trust scoring, and Ed25519-signed provider responses — see [`docs/supply-security.md`](docs/supply-security.md).

```bash
cd examples/hello-capability && python3 server.py   # terminal 1 — prints provider_pubkey
export AIMARKET_ALLOW_LOCAL_PUBLISH=1               # dev only
# production: POST /ai-market/v2/supply/stake first
aimarket publish capability.json --hub http://127.0.0.1:9083
aimarket invoke demo-hello/greet@v1 --input '{"name":"dev"}'
```

Full walkthrough (20 languages): [ARGUS developer guide](https://github.com/alexar76/argus/tree/main/docs/developer-guide/) · [supply security](docs/supply-security.md) · example in [`examples/hello-capability/`](examples/hello-capability/).

### Docker

**Production (this monorepo):** always redeploy Hub from repo root:

```bash
./scripts/deploy_hub.sh
# or full fleet: ./scripts/deploy_ecosystem.sh
```

See [`docs/deploy-ecosystem.md`](../docs/deploy-ecosystem.md). Do **not** use `cd aimarket-hub && docker compose up` for production redeploy (wrong build context).

**Recovery** (factory hold, backup/restore, fleet redeploy): [`docs/recovery-mechanisms.md`](../docs/recovery-mechanisms.md) in the factory monorepo.

Manual build (same as deploy script):

```bash
docker build -f aimarket-hub/Dockerfile -t modelmarket-hub .
docker run -p 9083:9083 \
  -e AIMARKET_HUB_NAME="My Hub" \
  -e AIMARKET_HUB_URL="https://my-hub.example.com" \
  -e AIMARKET_PAYMENT_RECIPIENT="0xYourWallet" \
  modelmarket-hub
```

---

## Core API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/.well-known/ai-market.json` | Root discovery — chain, token, peers, signer key |
| `GET` | `/ai-market/v2/manifest` | Ed25519-signed capability catalog |
| `GET` | `/ai-market/v2/search` | NL federated search (`intent`, `budget`, `category`) |
| `POST` | `/ai-market/v2/supply/stake` | Deposit publisher stake (unlock community publish) |
| `POST` | `/ai-market/v2/supply/register` | Publish community capability + `invoke_url` |
| `POST` | `/ai-market/v2/invoke` | Invoke capability (plugin hooks, safety gate) |
| `POST` | `/ai-market/v2/channel/open` | Open pre-funded payment channel |
| `POST` | `/ai-market/v2/channel/close` | Close channel — settle + refund remainder |
| `POST` | `/ai-market/v2/federation/announce` | Peer hub announcement |
| `GET` | `/ai-market/v2/federation/peers` | Known peers + trust scores |
| `POST` | `/ai-market/v2/federation/crawl` | Trigger BFS crawl of seed peers |
| `GET` | `/ai-market/v2/plugins` | Loaded plugin catalog |
| `GET` | `/ai-market/v2/reputation/{hub_url}` | Trust score breakdown |
| `GET` | `/ai-market/v2/stats/live` | Real-time invocation feed |

OpenAPI: `/docs` when `AIMARKET_OPENAPI=1`. Full spec: [`../aimarket-protocol/spec.md`](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md)

---

## Invoke lifecycle

Standard consumer flow (implemented by [`aimarket_agent`](https://github.com/alexar76/aimarket-sdks/tree/main/dart/)):

```mermaid
sequenceDiagram
  autonumber
  participant Client
  participant Hub as AIMarket Hub
  participant Plugins
  participant Target as Provider / local invoke

  Client->>Hub: search(intent, budget)
  Hub-->>Client: plan[]

  Client->>Hub: channel/open(deposit_usd)
  Hub-->>Client: channel_id

  Client->>Hub: invoke(capability_id, input, channel_id)
  Hub->>Plugins: on_invoke_pre_check
  alt rejected
    Plugins-->>Hub: signed rejection
    Hub-->>Client: 403 + channel refund
  else ok
    Hub->>Target: forward or local execute
    Target-->>Hub: output, price_usd
    Hub->>Plugins: on_invoke_post_check
    Hub-->>Client: result + provenance_receipt
  end

  Client->>Hub: channel/close(channel_id)
  Hub-->>Client: settlement + unused balance
```

---

## Plugin ecosystem

Plugins register via **`aimarket.plugins`** entry points ([`plugin.py`](aimarket_hub/plugin.py)). Each ships **README + docs/** (`value.md`, `user-guide.md`, `sdk-integration.md`, `user-cases.md`).

Regenerate docs: `python3 scripts/bootstrap_hub_plugin_docs.py` · value text: `python3 scripts/bootstrap_product_value.py`

```mermaid
flowchart LR
  INV["POST /invoke"] --> PRE["pre-check"]
  PRE --> S["aimarket-safety"]
  PRE --> Z["aimarket-zk"]
  PRE --> PR["aimarket-promo"]
  PRE --> RUN["Execute"]
  RUN --> POST["post-check"]
  POST --> PV["aimarket-provenance"]
  POST --> T["aimarket-tee"]
  POST --> R["aimarket-reputation"]
  POST --> OUT["Response"]
```

| Plugin | Category | One-line value |
|--------|----------|----------------|
| [`aimarket-provenance`](plugins/aimarket-provenance/) | compliance | Cryptographic receipt per AI output |
| [`aimarket-safety`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-safety/) | security | Block jailbreak / injection before billing |
| [`aimarket-reputation`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-reputation/) | reputation | Stake-backed trust scores |
| [`aimarket-channels`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-channels/) | infrastructure | Off-chain ledger, on-chain settlement |
| [`aimarket-tee`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-tee/) | security | Hardware attestation (Nitro / TDX) |
| [`aimarket-auction`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-auction/) | monetization | Spot bidding for scarce slots |
| [`aimarket-personas`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-personas/) | tooling | Buyer-friendly agent personas |
| [`aimarket-streaming`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-streaming/) | monetization | SSE + per-token micro-billing |
| [`aimarket-nft`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-nft/) | monetization | Transferable prepaid credit NFTs |
| [`aimarket-mcp-packager`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-mcp-packager/) | tooling | MCP bundle for Claude Desktop |
| [`aimarket-orchestrator`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-orchestrator/) | monetization | NL task → capability chain planner |
| [`aimarket-data-cap`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-data-cap/) | monetization | Private corpus → paid search |
| [`aimarket-promo`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-promo/) | monetization | Signed time-locked discounts |
| [`aimarket-dataset`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-dataset/) | tooling | Weekly anonymized demand corpus |
| [`aimarket-zk`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/aimarket-zk/) | security | ZK proofs without revealing input |

---

## Federation

Hubs discover each other without a central registry:

```mermaid
flowchart TB
  SEED["AIMARKET_SEED_LIST<br/>.well-known URLs"] --> CRAWL["crawler.py BFS"]
  CRAWL --> MANIFEST["Fetch signed manifests"]
  MANIFEST --> INDEX["database.py"]
  INDEX --> SEARCH["Unified federated search"]

  HUB_A["Hub A"] <-->|announce / peers| HUB_B["Hub B"]
  CRAWL --> HUB_A
  CRAWL --> HUB_B

  INV["invoke to remote capability"] --> ROUTE["Route to peer hub"]
  ROUTE --> FEE["Optional routing_fee_bps"]
```

Trust scoring: [`trust.py`](aimarket_hub/trust.py) · Signing: [`signing.py`](aimarket_hub/signing.py)

Deep dive: [`../docs/FEDERATION_HUB_REPORT.md`](../docs/FEDERATION_HUB_REPORT.md)

---

## Payments

```mermaid
sequenceDiagram
  participant User
  participant Hub
  participant Chain as Base (USDT)

  User->>Chain: deposit USDT
  User->>Hub: channel/open(deposit_usd)
  Note over Hub: Ledger tracks balance off-chain

  loop each invoke
    User->>Hub: invoke + X-Payment-Channel
    Hub->>Hub: decrement channel balance
  end

  User->>Hub: channel/close
  Hub->>Chain: settle spent + refund remainder
```

| Field | Default | Notes |
|-------|---------|-------|
| Chain | Base (L2) | `AIMARKET_PAYMENT_CHAIN` |
| Token | USDT | `AIMARKET_PAYMENT_TOKEN` |
| Recipient | env required | `AIMARKET_PAYMENT_RECIPIENT` |

Protocol principle: **no custody** — channels are on-chain constructs; hub holds ledger state only.

---

## Pay-on-Verified

**Opt-in quality escrow on invoke.** With a `verify` block on the invoke body the channel debit
becomes a hold; [Metis](https://github.com/alexar76/metis) judges the delivered output against the
buyer's stated intent in the background — pass captures the hold, fail refunds it with a signed
rejection receipt. The buyer keeps the output either way; only the money outcome changes.

| | |
|---|---|
| **What** | `verify: { requested, intent, mode, wait }` on `POST /ai-market/v2/invoke` → `hold_channel` → Metis verdict → capture / release |
| **Why** | Providers are paid for verified work, not for responding; every verdict emits a reputation event |
| **Lookup** | `GET /ai-market/v2/verification/{nonce}` (nonce = receipt nonce) |
| **Deep dive** | [docs/pay-on-verified.md](docs/pay-on-verified.md) · [Cross-component doc](../docs/pay-on-verified.md) |

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AIMARKET_HUB_NAME` | AIMarket Hub | Display name in manifests |
| `AIMARKET_HUB_URL` | `http://localhost:9083` | Public URL (receipts, well-known) |
| `AIMARKET_PAYMENT_CHAIN` | `base` | Settlement chain |
| `AIMARKET_PAYMENT_TOKEN` | `USDT` | Settlement token |
| `AIMARKET_PAYMENT_RECIPIENT` | — | **Required in production** |
| `AIMARKET_CRAWL_INTERVAL_S` | `3600` | Federation crawl period |
| `AIMARKET_ROUTING_FEE_BPS` | `100` | Routing fee (1% = 100 bps) |
| `AIMARKET_SEED_LIST` | — | Comma-separated peer `.well-known` URLs |
| `AIMARKET_PLUGIN_WHITELIST` | — | Restrict loaded plugins |
| `DATABASE_URL` | SQLite file | PostgreSQL for production |
| `AIMARKET_VERIFY_ENABLED` | `1` | Pay-on-Verified master switch (per-invoke opt-in still required) |
| `AIMARKET_VERIFY_MIN_PRICE_USD` | `0.05` | Price floor — cheaper invokes are never verification-taxed |
| `AIMARKET_VERIFY_SCORE_THRESHOLD` | `0.7` | `verify_score` needed to capture the hold |
| `AIMARKET_VERIFY_COUNCIL_MIN_PRICE_USD` | `0.50` | Route ceiling: `council` allowed at/above this price, else clamped to `fast` |
| `AIMARKET_VERIFY_MAX_CONCURRENCY` | `8` | Cap on simultaneous Metis calls across pending settlements |
| `AIMARKET_VERIFY_ATTEMPT_TIMEOUT_S` | `330` | Per-attempt Metis HTTP timeout (> Metis 300 s server cap) |
| `AIMARKET_VERIFY_RETRY_BACKOFF_S` | `5` | Initial transport-retry backoff (exponential, cap 300 s) |
| `AIMARKET_VERIFY_ENGINE_RETRIES` | `2` | Re-runs after an engine-error envelope before policy applies |
| `AIMARKET_VERIFY_MAX_WAIT_S` | `0` | `0` = no verdict deadline; `>0` bounds resolution via policy |
| `AIMARKET_VERIFY_FAIL_CLOSED` | derived | Unset → `1` iff `AIFACTORY_PROD`; indeterminate → refund (`1`) or capture (`0`) |
| `AIMARKET_VERIFY_METIS_URL` | `http://127.0.0.1:8080` | Metis base URL (falls back to `METIS_URL`) |
| `AIMARKET_VERIFY_METIS_KEY` | — | Metis bearer key (falls back to `METIS_API_KEY`) |
| `AIMARKET_VERIFY_VERIFIER_ID` | `metis.verify@v1` | Envelope `verifier` attribution when a non-Metis verifier serves the slot |

---

## Deployment

Production reference: [`../docs/production-modelmarket-dev.md`](../docs/production-modelmarket-dev.md)

| Checklist item | Action |
|----------------|--------|
| TLS | Terminate at nginx / Caddy → hub container |
| Secrets | `AIMARKET_PAYMENT_RECIPIENT`, DB URL via env — not in git |
| Factory sync | Cron or webhook → `sync_pipeline_mirror_and_hub.py` |
| Plugins | `pip install` desired plugins before `aimarket serve` |
| Health | `GET /.well-known/ai-market.json` + `/ai-market/v2/stats/live` |

---

## Testing & coverage {#testing--coverage}

CI runs on every push ([workflow](.github/workflows/ci.yml)); coverage badge is refreshed from `pytest --cov` on `main`.

```bash
cd aimarket-hub
pip install -e ".[dev]"
pytest tests/ -q --cov=aimarket_hub
```

## Development

```bash
pip install -e ".[dev]"
```

Key test modules: `test_api.py`, `test_crawler.py`, `test_plugin_system.py`, `test_channels.py`, `test_cross_hub_integration.py`

Add a plugin: create package under [`../plugins/`](https://github.com/alexar76/aimarket-plugins/tree/main/plugins/) with `pyproject.toml` entry point `aimarket.plugins`.

---

## Security

- **SSRF protection** on federation crawler ([`crawler.py`](aimarket_hub/crawler.py))
- **Signed manifests** — Ed25519 ([`signing.py`](aimarket_hub/signing.py))
- **Safety gate** on every invoke ([`safety_gate.py`](aimarket_hub/safety_gate.py))
- **Vulnerability reports:** [SECURITY.md](SECURITY.md) → alexar76@rambler.ru

---

## Related projects

| Project | Relationship |
|---------|--------------|
| [AICOM / AI-Factory](../README.md) | Ships products → hub index |
| [aimarket-protocol](https://github.com/alexar76/aimarket-protocol/tree/main/) | Normative v2 spec |
| [aimarket-sdks](https://github.com/alexar76/aimarket-sdks/tree/main/) | Client SDKs (Dart alpha) |
| [aimarket-widget](https://github.com/alexar76/aimarket-widget/tree/main/) | Embeddable UI |
| [oracles](https://github.com/alexar76/oracles/tree/main/) | Verifiable math capabilities — randomness, VDF, consensus, reputation (listed on hub) |
| [desktop-integrations](https://github.com/alexar76/aimarket-desktop/tree/main/) | 8 Flutter consumer apps |
| [Ecosystem architecture](../docs/ecosystem-architecture.md) | Full monorepo diagram |
| [dioscuri](https://github.com/alexar76/dioscuri) | Twin community agents — MNEMOSYNE Q&A |

---

## Community

The [DIOSCURI](https://github.com/alexar76/dioscuri) twins answer questions from synced GitHub docs.

| Channel | Twin | Best for |
|---------|------|----------|
| [Discord](https://discord.gg/aimarket) | Pollux | Help, ideas, show-and-tell |
| [Telegram](https://t.me/just_for_agents) | Castor | Releases, digests, quick news |

**Ecosystem map:** [Alien Monitor](https://magic-ai-factory.com/monitor/) · [AICOM](https://magic-ai-factory.com)

---

## License

Apache-2.0 — see [LICENSE](LICENSE). Maintainers: [CONTRIBUTORS.md](CONTRIBUTORS.md).
