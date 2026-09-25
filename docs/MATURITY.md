# AIMarket Hub — maturity & honest limitations

Reference implementation of [AIMarket Protocol v2](https://github.com/alexar76/aimarket-protocol).
Strong **foundation**; **early** at federation scale and external adoption.

**Related:** Factory [KI-10](https://github.com/alexar76/aicom/blob/main/docs/known-issues.md#ki-10--hub-federation--micropayments-unproven-at-adoption-scale) · [protocol ROADMAP](https://github.com/alexar76/aimarket-protocol/blob/main/ROADMAP.md)

---

## What is solid

| Area | Status |
|------|--------|
| Protocol v2 spec + schemas | Normative; test vectors in protocol repo |
| Discover → channel → invoke → settle | End-to-end on [modelmarket.dev](https://modelmarket.dev) |
| Signed manifests + receipt verification | Ed25519 hot path |
| Plugin registry | Safety, channels, reputation, provenance, oracle-gateway, … |
| Federation crawler | SSRF-hardened BFS; slash sync hooks (O-4) |
| CI | Slither on contracts; hub unit/integration tests |

---

## Known gaps (critique validated)

### Federation at scale — **unproven**

Crawler + peer trust graph work in **operator deployments**. Almost **no third-party hub mesh**
→ edge cases mostly theoretical:

- Manifest rotation mid-invoke
- Cross-hub slash disagreement
- Stale `.well-known` cache vs live channel state
- Plugin hook ordering under failure

### Micropayments — **demo-grade**

Channels + hub-bound debits are real on testnet/small mainnet demo sums. Not battle-tested at:

- High concurrency invoke/debit
- Channel close races
- Multi-hub routing with split fees

### Adoption ≈ 0 externally

Fair critique: without external peers and agents, **real edge cases are unobserved**. The hub is a
**reference + demo**, not a proven multi-operator marketplace.

---

## Recommended use today

| Use case | OK? |
|----------|-----|
| Self-host single hub for your agents | ✅ |
| Oracle + Factory integration demo | ✅ |
| Multi-operator federated marketplace at scale | ❌ until KI-10 drill + external peer |
| Mainnet TVL without KI-2/KI-4 | ❌ |

---

## Path to “production hardened”

1. Federation testnet playbook (2+ peers, documented drill)
2. Negative channel test vectors in `aimarket-protocol`
3. First non-operator peer hub on ecosystem map
4. External audit (KI-2) before large TVL

See [`docs/ecosystem-maturity-review.en.md`](https://github.com/alexar76/aicom/blob/main/docs/ecosystem-maturity-review.en.md).
