# Running this hub for yourself

[English](operator-quickstart.md) · [Русский](operator-quickstart.ru.md) · [Español](operator-quickstart.es.md) · [Français](operator-quickstart.fr.md) · [中文](operator-quickstart.zh.md)

> Terminology follows the canonical [localization glossary](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md). Product names, protocol identifiers, URLs, CLI commands and environment variables are never translated.

You cloned it, you deployed it, and the fair question is what you get out of it. This page
is the shortest path from a fresh deployment to a hub that can actually take money — no
chain, no contract, no wallet, no account with anyone.

## Three minutes

```bash
pip install "aimarket-hub[pqc]"        # [pqc]: post-quantum signatures (below)
export AIMARKET_CREDITS_ENABLED=1      # the payment rail
export AIMARKET_ORACLE_FAMILY_URL=off  # score publishers yourself (see below)
export AIMARKET_PQC=1                  # sign hybrid: Ed25519 + ML-DSA-65
python -m aimarket_hub quickstart
python -m aimarket_hub serve
```

`quickstart` does three things a fresh hub could not do on its own:

1. **Lists a capability that is really yours and really executable.** A fresh catalogue is
   otherwise either empty or made of peers you do not own. The sample is a *static pack* —
   a JSON object stored in `prompt_template`, returned verbatim by the invoke handler — so
   it needs no provider, no model and no network. Replace it with your own service by
   setting `invoke_url`, or edit the pack.
2. **Mints a buyer key** with a small starting balance, so you can complete a sale against
   your own hub before you show it to anyone.
3. **Prints the two curls** that make that sale happen.

It also says how the hub will sign, and where its keys are.

## Post-quantum signatures from the first start

The two PQC lines above do different jobs:

- **`[pqc]` lets the hub see its peers.** It installs the ML-DSA-65 verifier. Checking a
  post-quantum signature fails closed, so a hub without it never indexes a peer that signs
  hybrid — and the AIMarket hubs all do.
- **`AIMARKET_PQC=1` makes the hub sign hybrid.** Its manifest, discovery document and receipts
  carry an ML-DSA-65 signature next to the Ed25519 one, and peers pin your post-quantum key the
  first time they see it.

The post-quantum key is a second file next to the classical one:
`<AIMARKET_SIGNING_KEY_PATH>_mldsa`, by default `data/hub_signing_key_mldsa`. Back up both
together and keep them on one volume. Lose the ML-DSA file and every peer that pinned it rejects
your hub until it re-pins (`POST /federation/peers/repin`). Background:
[post-quantum migration](https://github.com/alexar76/aicom/blob/main/docs/pqc-migration.md).

## How you get paid

There are two rails and you can run either or both.

| | Credits (`X-API-Key`) | Channels (`X-Payment-Channel`) |
|---|---|---|
| Setup | one env var | escrow contract you deploy on Base + 5 preflight settings |
| Smallest billable amount | $0.00001 | $0.01 (whole cents, rounded up) |
| Who holds the money | you, prepaid | you, prepaid (on-chain deposit) |
| Buyer needs | an HTTP client | a funded wallet and a typed-data signature |

**Credits** is the rail that works with `AIFACTORY_CRYPTO_ENABLED=0`, which is the default.
Turning it on makes a listed price mean money: a paid capability answers `402` with the
rails it accepts, and an invoke carrying a valid `X-API-Key` reserves the price before the
provider runs, captures it on success and hands it back on any failure.

```
POST /ai-market/v2/accounts                    # a buyer mints a key (rate-limited)
GET  /ai-market/v2/account                     # the buyer's balance
GET  /ai-market/v2/account/ledger              # every movement on their own money
POST /ai-market/v2/accounts/{id}/credit        # you top them up  (admin bearer)
POST /ai-market/v2/accounts/{id}/status        # you disable one  (admin bearer)
GET  /ai-market/v2/stats/live                  # summary.credits — earned vs. owed
```

Money enters only through `/credit`, and that route is yours alone. Whatever you use to
get paid — an invoice, a checkout, a stablecoin transfer you watched land — you call
`/credit` once you have the money. The hub does not pretend to have collected anything.

### What you are taking on

A prepaid balance is your customer's money sitting in your ledger. `stats/live` publishes
`outstanding_credit_usd` next to `credits_earned_usd` for exactly that reason: the first is
a liability, the second is revenue, and a hub that cannot tell them apart eventually spends
one thinking it is the other. Nothing in the hub can send value, so refunds are something
you do.

### Knobs

| Variable | Default | Meaning |
|---|---|---|
| `AIMARKET_CREDITS_ENABLED` | `0` | the rail itself |
| `AIMARKET_CREDITS_OPEN_SIGNUP` | `1` | may a stranger mint their own key |
| `AIMARKET_CREDITS_FREE_GRANT_USD` | `0.05` | starting balance for a self-minted key |
| `AIMARKET_CREDITS_SIGNUPS_PER_HOUR` | `5` | per-address cap on self-serve signup |
| `AIMARKET_ECOSYSTEM_FILE` | `ecosystem.json` next to the database | which accounts, servers and wallets are your own ecosystem: their calls are SELF, not external demand — see [traffic classes](traffic-classes.md) |
| `AIMARKET_OPERATOR_ACCOUNTS` | empty | older shortcut for `self.accounts` in that file (comma-separated account ids); merged into it |
| `AIMARKET_ROUTING_FEE_BPS` | `100` | your cut when you broker another hub's capability |
| `AIMARKET_PQC` | unset = auto | unset: a new hub signs hybrid (Ed25519 + ML-DSA-65), a hub that holds an ML-DSA key keeps signing with it, an Ed25519-only hub stays so; `1` / `0` force it |

## Letting other people publish on your hub

A credit account is an identity, not just a wallet, so it is also how somebody lists a
capability with you — no `.env` edit, no restart, no chain.

```bash
curl -X POST $HUB/ai-market/v2/accounts                      # they get a key
curl -X POST $HUB/ai-market/v2/accounts/<id>/credit \
     -H "Authorization: Bearer $ADMIN" -d '{"amount_usd": 30}'   # you take their money
curl -X POST $HUB/ai-market/v2/supply/stake \
     -H "X-API-Key: <key>" -d '{"amount_usd": 25}'               # they post collateral
curl -X POST $HUB/ai-market/v2/supply/register \
     -H "X-API-Key: <key>" -d @manifest.json                     # they list
```

The key speaks for exactly one publisher: its own account id. It cannot stake for, or
publish as, anybody else — which the shared publish token never could prevent.

The stake is a real debit against their balance, so it is money you are holding and can
slash. It is recorded as its own kind of collateral, distinct from the dev sentinel, and it
satisfies the production stake gate — the on-chain path still exists and still demands a
verified `tx_hash`, but a hub whose image does not ship the deposit verifier is no longer
forced to choose between "no collateral" and "an unusable gate".

Publisher trust: with `AIMARKET_ORACLE_FAMILY_URL=off` there is no oracle to consult, so
publishers get the documented neutral bootstrap and the trust gate does not apply. Point it
at a LUMEN instance you run if you want scoring.

## Paying your sellers

When a capability's publisher holds a credit account here, every completed sale splits
automatically: `AIMARKET_PUBLISHER_SHARE_BPS` (70% by default) is credited to them, the rest
is yours. That is the seller-earnings route the hub never had — the channel ledger cannot
send value, and the only obligations table refunds depositors rather than providers, so
before this a seller could list, be invoked, and still have to ask you to wire them a tenth
of a cent by hand.

`stats/live` reports the split honestly: `credits_earned_usd` is gross (what buyers spent),
`publisher_payouts_usd` is what went to sellers, and `operator_net_usd` is what you kept.
A failed call pays nobody — the payout runs on the same capture that charges the buyer.

## Brokering other hubs

When you route an invoke to a peer you did not sell for, the routing fee is **reserved
before the peer is asked to do anything**, on whichever rail the buyer is using. A caller
with no payment gets a `402` naming the fee; a peer that refuses costs the buyer nothing;
a peer that charges less than its published price is billed the smaller amount. If you do
not want to charge for brokering, set `AIMARKET_ROUTING_FEE_BPS=0` — that is the opt-out,
not a missing header.

An escrow-backed channel cannot authorize a routing fee (there is no signed authorization
for it), so that combination is refused rather than accrued as revenue you could never
collect. Buyers on that path should use a credit account.

### Reselling a peer that charges

Brokering only moves money if you can pay the other side. Hold a credit account at the peer
and name it:

```bash
export AIMARKET_PEER_API_KEYS="https://peer.example=aimk_your_key_there"
```

Now a routed call to that peer is a resale: your buyer is charged the catalogued price plus
your fee, you pay the peer out of your account there, and the fee is your margin. Without a
key the peer's `402` reaches a buyer who has no account there and cannot act on it, which is
why the live federation only ever contained peers that charge nothing.

Peer hubs are reached at their `/ai-market/v2/invoke`, not at their `mcp_endpoint` — that
endpoint speaks MCP JSON-RPC over SSE and answers a routed envelope with `Method not found`
inside a `text/event-stream` body. Hub-to-hub routing was impossible until that was fixed.

## Taking x402 payments

A catalogue sale is seller-direct. The 402 names the listing's `payout_address` and an
invoice `nonce`, and its JSON body alone carries a `payment_secret` with
`nonce = sha256(payment_secret)`. The buyer pays that wallet on-chain with an EIP-3009
`transferWithAuthorization` signed over the nonce, then retries with `X-Payment: <tx hash>`
(or `PAYMENT-SIGNATURE`), `X-Payment-Nonce` and `X-Payment-Secret`. The secret is what
shows the buyer is the one who paid: the transaction and its nonce are public once mined.
The hub verifies the transfer and never holds the money (`aimarket_hub/settle.py`): only
the transfer that authorization moved pays, and each authorization is its own claim, so
two in one transaction buy two calls. On a federated call, a payment travels next to your
peer key only when the hub settled it on that request, naming its own nonce and keeping the
secret. A hub with no key for that peer that settled nothing is a relay and passes the
buyer's payment headers through, the secret included. Every payment is bound (a plain
transfer to the seller is refused; `AIMARKET_SETTLE_REQUIRE_BINDING=0` is ignored), so buyer
tooling written against an earlier hub, which sent `X-Payment` and `X-Payment-Nonce` alone, is
refused until it sends the secret too; the MCP gateway takes it as `x_payment_secret`, and
the A2A bridge presents it itself. Migration `040` adds the invoice column that keeps it, and
`X-Payment-Secret` is in the CORS allow-list, so a browser client on an
`AIMARKET_CORS_ORIGINS` origin can redeem too. `AIMARKET_X402_ACCEPT=0` keeps discovery-only
mode.

Credits and payment channels remain prepaid conveniences. They are not how a
wallet publisher is paid.

## Letting strangers find you

Two observation doors are always available and both lead to quarantine rather than
immediate trust: an unauthenticated `POST /federation/announce`, and reciprocal discovery
when a peer crawls you and identifies itself. Either way the peer lands `pending` and
`trusted=false`. A sandbox assay then runs automatically; a `pass` indexes without an
Approve click. Fail and review stay pending for `/operator`. A separate preview table
holds what a pending peer claims to offer; those rows can never reach search or routing
because they are not in the `capabilities` table at all. See `docs/join-the-federation.md`.

## Standing on your own

Two defaults point at the reference deployment, and you probably want neither:

- **Publisher trust.** `AIMARKET_ORACLE_FAMILY_URL` defaults to another operator's LUMEN
  instance. Leave it and your ability to publish depends on their uptime — a capability
  published while it is unreachable is stored unscored and every invoke of it answers 502.
  `off` says this hub has no trust oracle: publishers get the documented neutral bootstrap
  and the gate does not apply. Point it at your own instance if you run one.
- **Federation seeds.** The shipped seed list is the reference hub's own satellites. They
  are a catalogue to browse, not supply you own — see `AIMARKET_SEED_LIST`.

## Naming

The code is Apache-2.0 (MIT elsewhere in the ecosystem) and you may run it commercially,
set your own fees and keep all of them; nothing routes a cut anywhere. There is no
trademark licence, so do not call your deployment "AIMarket" or imply endorsement.
"Implements the AIMarket Protocol" and "interoperates with <peer>" are accurate and fine.
Nobody may claim "certified" or "conformant" — there is no conformance suite yet.
