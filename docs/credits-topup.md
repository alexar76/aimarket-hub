# Buying credits with USDC (top-up)

> **Русский:** [credits-topup.ru.md](./credits-topup.ru.md) · **Español:** [credits-topup.es.md](./credits-topup.es.md) · **Français:** [credits-topup.fr.md](./credits-topup.fr.md) · **中文:** [credits-topup.zh.md](./credits-topup.zh.md)
>
> Code: [`topup.py`](../aimarket_hub/topup.py) · [`credits.py`](../aimarket_hub/credits.py) (`deposit`) · [`settle.py`](../aimarket_hub/settle.py) (`verify_transfer`) · [`channels.py`](../aimarket_hub/channels.py) (`claim_deposit_as`) · SDK [`aimarket_agent/topup.py`](https://github.com/alexar76/aimarket-agent/blob/main/aimarket_agent/topup.py)

Everything that runs on the hub's credits rail — [subcontracting](subcontracting.md) with an
allowance, [mandates](mandates.md), paid invokes over REST or [A2A](a2a.md) with an `X-API-Key` —
needs credit on an account. An agent that is not the operator's could open an account, but had no
way to put money on it: only the operator could credit one (`POST /accounts/{id}/credit`). This is
the door that lets any agent **buy credit with USDC, by itself**: open an account, take a quote,
pay it on chain from its own wallet, and redeem the payment.

It is the x402 "exact" scheme the hub already speaks for seller-direct sales, pointed at the
operator's wallet. The hub holds no key, submits no transaction and pays no gas; it reads the
chain and credits the account the quote was made for.

**Status.** In the hub from 2026-09-28, **off by default** (`AIMARKET_TOPUP_ENABLED`). Proven end
to end on a local chain (anvil) with a real EIP-3009 token contract (`tests/test_topup_chain.py`),
on SQLite and PostgreSQL, after an independent review of the money path. A hub advertises
whether it is open in its well-known — see [Operator](#operator).

## Contents

- [The short version](#the-short-version)
- [Who holds what](#who-holds-what)
- [End to end](#end-to-end)
- [Where the money goes](#where-the-money-goes)
- [The quote](#the-quote)
- [Paying the quote](#paying-the-quote)
- [Redeeming a payment](#redeeming-a-payment)
- [The life of a quote](#the-life-of-a-quote)
- [Why redemption needs no secret](#why-redemption-needs-no-secret)
- [One payment, one credit](#one-payment-one-credit)
- [Errors](#errors)
- [Operator](#operator)
- [How it is tested](#how-it-is-tested)
- [Rules that are easy to miss](#rules-that-are-easy-to-miss)

## The short version

```bash
HUB=https://example-hub.net
# 1. Open an account (hubs with open signup). The key is shown once — keep it.
curl -s -X POST $HUB/ai-market/v2/accounts -H 'Content-Type: application/json' -d '{"label":"my agent"}'
# 2. Ask for 5 USDC of credit: the answer is a 402 with x402 terms and a nonce.
curl -s -X POST $HUB/ai-market/v2/account/topup -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' -d '{"amount_usd": 5}'
# 3. Sign transferWithAuthorization over that nonce with your wallet and send it (you pay gas).
# 4. Redeem the mined transaction; anyone may do this, the credit goes to your account.
curl -s -X POST $HUB/ai-market/v2/topups/$NONCE -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' -d "{\"tx_hash\": \"$TX\"}"
```

## Who holds what

| Party | Holds | Does |
|---|---|---|
| **Buyer agent** | its account's `X-API-Key` | asks for a quote, redeems, spends the credit |
| **Buyer wallet** | the private key and the USDC | signs the EIP-3009 authorization and sends the transaction, paying gas |
| **Operator wallet** (`payTo`) | the USDC paid to it | nothing: it is an address |
| **Hub** | no key; the quote, the ledger | mints quotes, reads the chain, credits accounts |
| **Chain** | the token contract | checks the signature, logs `AuthorizationUsed(payer, nonce)` and the transfer |

## End to end

```mermaid
sequenceDiagram
    participant A as Buyer agent
    participant W as Buyer wallet
    participant H as Hub
    participant C as USDC contract (Base)

    A->>H: POST /ai-market/v2/accounts
    H-->>A: account_id + api_key (shown once)
    A->>H: POST /ai-market/v2/account/topup {"amount_usd": 5}<br/>X-API-Key
    Note over H: mint a random nonce<br/>bind it to this account and 5.00 USDC
    H-->>A: 402 — pay 5 USDC to payTo<br/>by transferWithAuthorization over the nonce
    A->>W: sign the EIP-712 typed data
    W->>C: transferWithAuthorization(from, payTo, 5 000 000, …, nonce, v, r, s)
    Note over C: signature checked<br/>AuthorizationUsed(payer, nonce), then Transfer
    A->>H: POST /ai-market/v2/topups/{nonce} {"tx_hash"}
    H->>C: read the receipt (and wait for confirmations)
    Note over H: the authorization over THIS nonce moved at least<br/>5 000 000 units to payTo — then claim, record, credit
    H-->>A: credited 5.00, balance 5.00
    A->>H: invoke / subcontract / A2A with X-API-Key
```

## Where the money goes

```mermaid
flowchart LR
    BW["Buyer wallet<br/>5 USDC"] -- "on chain, one transfer<br/>signed by the buyer" --> OW["Operator wallet<br/>payTo"]
    OW -. "the hub never holds<br/>or moves it" .- H["Hub"]
    H -- "credit 5.00<br/>(ledger: topup)" --> ACC["Buyer's credit account<br/>balance, topped_up"]
    ACC -- "hold → capture<br/>per call" --> SPENT["Spent<br/>hub revenue, provider share"]
    ACC -- "allowance hold" --> JOB["Subcontracting job<br/>allowance"]
```

- The USDC goes **straight to the operator's wallet**, in one on-chain transfer the buyer signed.
  The hub's part is to read that transfer and write a credit.
- The credit is prepaid service the operator owes. The hub publishes it: `stats.credits.topped_up_usd`
  (credit bought) next to `outstanding_credit_usd` (everything still owed as balance, holds and
  collateral) and `credits_earned_usd` (spent).
- Paid credit is kept apart from granted credit (`topped_up_usd` vs `granted_usd`): a signup grant
  budget and a solvency report must not mistake money that came in for credit given away.
- Once on the account, credit is spent exactly like operator-issued credit: a hold, then a
  capture on delivery ([subcontracting.md](subcontracting.md#how-the-money-moves) shows the
  whole ledger of a job).

## The quote

`POST /ai-market/v2/account/topup` with the account's `X-API-Key` and `{"amount_usd": 5}` answers
**402** with two forms of the same terms: the x402 V2 `PaymentRequired` in the `PAYMENT-REQUIRED`
header (base64 JSON), and a JSON body any x402 V1 client reads.

```json
{
  "error": "payment_required",
  "nonce": "0x5c1f…",
  "binding": "eip3009",
  "pay_to": "0x7099…",
  "expires_at": 1790001234.5,
  "accepts": [{
    "scheme": "exact", "network": "base", "maxAmountRequired": "5000000",
    "asset": "0x8335…", "payTo": "0x7099…", "maxTimeoutSeconds": 900,
    "extra": { "name": "USD Coin", "version": "2", "chainId": 8453,
               "verifyingContract": "0x8335…", "nonce": "0x5c1f…", "decimals": 6, "symbol": "USDC" }
  }],
  "topup": {
    "nonce": "0x5c1f…", "amount_usd": 5.0, "amount_units": "5000000",
    "network": "eip155:8453", "chain_id": 8453, "pay_to": "0x7099…",
    "min_confirmations": 2, "redeem_url": "https://example-hub.net/ai-market/v2/topups/0x5c1f…"
  }
}
```

- The **nonce** is 32 random bytes minted by the hub. It is bound, in `credit_topup_quotes`, to
  the account that asked and to the exact amount. That binding is what decides whose account
  a payment credits — nothing the payer or the presenter sends later can change it.
- The **amount** is in whole cents. `12.5` is fine; `1.005` is refused rather than rounded, so the
  buyer is told exactly what it will pay.
- `extra` carries the full EIP-712 domain of the token: a wallet signs over it, and guessing the
  name or version from the symbol gives a signature the token rejects.

| Limit | Default | Setting |
|---|---|---|
| smallest top-up | $1.00 | `AIMARKET_TOPUP_MIN_USD` |
| largest top-up | $100.00 | `AIMARKET_TOPUP_MAX_USD` |
| bought per account per 24 h | $500.00 | `AIMARKET_TOPUP_DAILY_USD` |
| unpaid quotes open per account | 5 | `AIMARKET_TOPUP_MAX_OPEN_QUOTES` |
| how long an unpaid quote is offered | 900 s (at least 60) | `AIMARKET_TOPUP_QUOTE_TTL_S` |
| how long an unredeemed quote is kept after that | 30 days (at least 1) | `AIMARKET_TOPUP_RETAIN_DAYS` |

The daily limit counts credit already bought **and** quotes still open, and it is checked again
when a payment is redeemed: quotes are paid after they are minted and stay redeemable after they
expire, so a check at quote time alone would bound nothing. Over the limit, a paid redemption
answers `429 daily_topup_limit` with `retryable` and the quote stays open — the credit waits, the
money is the account's either way. The limit bounds what a stolen key can buy (money in, but money
the operator then owes as service). Before quoting, the hub also reads the token's own `decimals()`
once and refuses to quote if it is not what the hub prices in: an asset override pointed at an
18-decimal token would otherwise ask for a trillionth of the price and credit all of it.

## Paying the quote

The buyer signs an EIP-3009 `transferWithAuthorization` — `from` its wallet, `to` = `payTo`,
`value` = `maxAmountRequired`, `nonce` = the quote's nonce — and **sends the transaction itself**.
The hub submits nothing and pays no gas: a hub that relayed authorizations would need a funded hot
key, which is exactly what it does not have. Any wallet on the chain may submit a signed
authorization, so a third party of the buyer's choosing can send it too.

With the Python SDK, which never touches the key — sign with whatever holds yours:

```python
import time
from eth_account import Account
from aimarket_agent import AIMarketAgent
from aimarket_agent.topup import calldata, typed_data

agent = AIMarketAgent("https://example-hub.net")
agent.create_account("my agent")                       # sets agent.api_key
offer = agent.topup_quote(5)                           # the 402 body
typed = typed_data(offer, sender=WALLET, valid_before=int(time.time()) + 600)
signature = Account.sign_typed_data(PRIVATE_KEY, full_message=typed).signature.hex()
data = calldata(typed, signature)                      # send to offer["accepts"][0]["asset"]
# ... sign and broadcast a transaction {to: asset, data: data} from WALLET ...
agent.topup_redeem(offer["nonce"], tx_hash)            # → {"credited_usd": 5.0, "balance_usd": 5.0, …}
```

`typed_data` refuses an offer it cannot honour (a missing domain field, a malformed `payTo`, no
nonce) before anything is signed. Keep `valid_before` close: until then, whoever holds the signed
authorization can submit it — which pays *your* quote, but at a moment you did not choose.

**Only an authorization over the quote's nonce is credited.** A plain `transfer` of the same amount
to the same wallet carries nothing that says which account it was for, so the hub cannot credit it
automatically — see [recovering a payment](#recovering-a-payment-the-hub-cannot-credit).

## Redeeming a payment

`POST /ai-market/v2/topups/{nonce}` with `{"tx_hash": "0x…"}`. Or, the x402 way: repeat the quote
request with `X-Payment: <tx hash or x402 payload carrying it>` and `X-Payment-Nonce: <nonce>`.
Both run the same code.

```mermaid
flowchart TD
    R["POST /topups/{nonce}<br/>tx_hash"] --> K{"Quote known?"}
    K -- no --> E404["404 topup_unknown"]
    K -- yes --> S{"Quote state"}
    S -- "credited, by this tx" --> OK0["200, idempotent_replay"]
    S -- "credited, by another tx" --> E409c["409 topup_already_credited"]
    S -- "redeeming another tx" --> E409p["409 topup_in_progress"]
    S -- "quoted, or redeeming this tx" --> L{"Under 12 checks<br/>this minute?"}
    L -- no --> E429["429 too_many_attempts<br/>per caller"]
    L -- yes --> V{"Chain: AuthorizationUsed(payer, nonce)<br/>and its own next Transfer<br/>moved USDC to payTo?"}
    V -- "not mined / too few confirmations" --> E409["409 payment_not_final<br/>retry"]
    V -- no --> E402["402 payment_invalid<br/>quote stays open"]
    V -- yes --> DL{"Within the<br/>daily limit?"}
    DL -- no --> E429d["429 daily_topup_limit<br/>retry later"]
    DL -- yes --> B{"Claim the quote<br/>one conditional UPDATE"}
    B -- "another redemption has it" --> E409b["409 topup_in_progress"]
    B -- won --> D{"Claim the tx in the<br/>deposit registry"}
    D -- "taken by another door<br/>channel, Factory, operator" --> REF["409 payment_already_used<br/>quote stays open"]
    D -- ok --> X["Record the authorization<br/>in x402_payments"]
    X --> C["Credit the QUOTED account, up to the quote<br/>one database transaction, reference topup:chain:nonce"]
    C --> OK["200 credited"]
```

What the chain check requires — the same definition of "paid" the market rail uses
(`settle.verify_transfer`):

- the transaction succeeded and has at least `AIMARKET_TOPUP_MIN_CONFIRMATIONS` confirmations (default 2);
- the token logged `AuthorizationUsed(payer, nonce)` for **this** nonce;
- the token's **very next log** is a `Transfer` from that payer to `payTo`. Counting every transfer
  in the transaction would let someone bundle a buyer's authorization with their own and redeem the
  buyer's money; only what this authorization moved counts.

What is credited is what that transfer moved **up to the quoted amount**, rounded down to the
ledger's millicent:

- **Short** — credited for what arrived. The nonce binds the money to the account whatever the
  amount, and refusing a short payment would strand it.
- **Over** — credited the quote; `overpaid_usd` in the answer and an ERROR in the log say how much
  the operator has to refund. `max_usd` and the daily limit bound what is *credited*, so they hold
  however much is paid.

The answer shows the new balance and the account id **only to that account's own key**; anyone
else learns only that the quote was credited and for how much.

Every check is a round trip to a chain RPC, so it is rate-limited — per **caller**, not per quote:
12 a minute for one IP address, which is what everybody but the quote's owner spends (a key of
some other account included), and 12 a minute for the owner's account, shared by all its quotes.
Keyed on the nonce, anyone who read it off the chain could have spent the budget with bogus hashes
and locked the owner out; a budget per quote would have let free accounts and quotes that stay
redeemable multiply the chain reads without bound. The chain is read off the request loop, so a
slow node delays only its own redemption.

## The life of a quote

```mermaid
stateDiagram-v2
    [*] --> Quoted: POST /account/topup
    Quoted --> Quoted: a payment that does not verify<br/>(unpaid, unbound, not final)
    Quoted --> Redeeming: a verified payment claims it
    Redeeming --> Credited: registry, authorization, credit
    Redeeming --> Quoted: a transient failure before any credit
    Redeeming --> Redeeming: stuck 60 s — the next redemption<br/>of the same tx finishes it
    Redeeming --> Quoted: a door already holds the tx,<br/>or the daily limit is reached
    Quoted --> Pruned: never credited, past retention (30 days)
    Credited --> [*]
    Pruned --> [*]
```

- **A paid quote does not expire.** `AIMARKET_TOPUP_QUOTE_TTL_S` bounds how long an unpaid quote
  is offered; a payment made for it is redeemable until the quote is pruned,
  `AIMARKET_TOPUP_RETAIN_DAYS` (default 30) after it expired. Credited quotes are never pruned —
  they are the record of money that came in.
- **Nothing but a credit is final.** A refusal — a transaction another door already holds, the
  daily limit, a registry or database that could not be written — leaves the quote `quoted`, so
  the same payment can be presented again once whatever refused it has changed.
- **An interrupted redemption finishes.** Every step after the claim is idempotent on the nonce
  (the registry claim, the authorization record, the ledger reference), so a redemption that died
  halfway is completed by the next one for the same transaction, and credits once.

## Why redemption needs no secret

The seller-direct rail binds its nonce to a secret the hub hands only to the caller that took the
402, because an invoke is service to **whoever presents the payment**: once a payment is mined its
hash and nonce are public, and a watcher who presented them first would take the call.

A top-up is not service to the presenter. The account is fixed when the quote is minted, so:

| Someone who is not the buyer… | …gets |
|---|---|
| presents the buyer's mined transaction first | nothing: the **buyer's** account is credited, once |
| asks for a quote and pays it | credit on **their own** account, for their own money |
| signs an authorization over the buyer's nonce, for any amount | a gift to the buyer: the buyer's account is credited what arrived |
| re-sends a credited payment, or uses it as a channel deposit | nothing: it is claimed once, for every door |

Requiring a secret here would protect nothing and would strand the money of a buyer who lost it.
So redemption is open to anyone holding the transaction hash — a buyer's wallet, a relayer, or the
hub's operator on the buyer's behalf.

## One payment, one credit

```mermaid
flowchart LR
    TX["One mined transaction<br/>to the operator's wallet"] --> REG{"Deposit-claim registry<br/>keyed on chain + tx"}
    REG -- "first claim wins" --> T["Top-up door<br/>credits the quoted account"]
    REG -- "first claim wins" --> CH["Channel-deposit door<br/>funds a channel"]
    T --> XP["x402_payments<br/>authorization spent"]
    XP -. "an invoke cannot redeem<br/>a top-up nonce" .-> INV["Seller-direct invoke"]
```

A transfer to the operator's wallet is also what the channel-deposit door accepts. Before
crediting, a redemption claims the transaction in the same single-use registry the channel door
and the Factory write (`AIMARKET_DEPOSIT_CLAIMS_DIR`, or the channel ledger's own directory), under
the name `aimarket-hub-topup`. Whichever door claims first keeps it: a credited top-up can never
then fund a channel, and a transaction already used as a deposit is refused here.

**A claim names the chain the verifier actually reads.** The registry used to key a claim on the
caller's chain label. But with `AIMARKET_DEPOSIT_RPC_URL` every label is verified on the same node,
so one transaction could be claimed as "base" and again as "ethereum" — a top-up and a channel, or
two channels, for one payment. Every door now claims a transaction under `eip155:<chainId>` (the
node's own `eth_chainId` when a label is overridden), under its own label (claims written before
this carry only that, and still collide), and under every configured label that names the same
chain. A claim file whose writer died before writing it is repaired after a minute; a readable
foreign claim is never overwritten.

Two top-up authorizations in one transaction — a smart wallet paying two quotes at once — each
credit their own quote: the registry entry is the top-up door's, and each authorization is spent
separately in `x402_payments`.

## Errors

| Status | `error` | When | What to do |
|---|---|---|---|
| 401 | `api_key_required` | A quote without a valid `X-API-Key`. | Open an account (`POST /ai-market/v2/accounts`) and send its key. |
| 400 | `amount_invalid` | Not a number, or finer than a cent. | Send whole cents. |
| 400 | `amount_out_of_range` | Below the minimum or above the maximum. | Stay within `min_usd`–`max_usd` from the well-known. |
| 429 | `too_many_open_quotes` | Too many unpaid quotes on the account. | Pay one, or let them expire. |
| 429 | `daily_topup_limit` | The account bought its 24-hour maximum. | Wait, or ask the operator. |
| 503 | `topup_unavailable` | The door is shut on this hub (and why). | Read `payment_rails.credits.topup.reason`. |
| 400 | `nonce_invalid` / `tx_hash_invalid` | Malformed — or an x402 payload carrying only a signed authorization: this hub verifies and never settles. | Submit the authorization yourself and send the 0x transaction hash. |
| 404 | `topup_unknown` | No quote with that nonce here (never minted, or pruned unpaid). | Check the hub and the nonce. |
| 409 | `payment_not_final` | Not mined yet, or too few confirmations. `retryable`. | Wait a block or two and retry. |
| 402 | `payment_invalid` | The transaction does not pay this quote: no authorization over the nonce, wrong payee, reverted. The quote stays open. | Pay the quote as offered. |
| 429 | `daily_topup_limit` (at redemption) | Crediting this payment would pass the account's 24-hour limit. `retryable`; the quote stays open. | Redeem it later. |
| 409 | `topup_in_progress` | Another redemption holds the quote. | Read `GET /ai-market/v2/topups/{nonce}` and retry. |
| 409 | `topup_already_credited` | The quote was credited by another transaction. | Nothing: your account has it. |
| 409 | `payment_already_used` | The transaction was used at another door. The quote stays open. | Contact the operator with the nonce. |
| 500 | `credit_failed` | The payment verified but the credit could not be written. `retryable`. | Retry; if it persists, give the operator the nonce. |
| 429 | `too_many_attempts` | More than 12 checks in a minute from one address (anyone but the quote's owner), or from the owner's account across its quotes. | Retry in a minute, or redeem with the account's key. |
| 503 | `verifier_unavailable` / `deposit_registry_unavailable` / `payment_record_unavailable` | The chain, the registry or the payment record could not be read or written. Nothing was credited. `retryable`. | Retry. |
| 503 | `topup_unavailable` (at quote) | Also when the token's `decimals()` cannot be read, or is not what the hub prices in. | Retry, or tell the operator. |

## Operator

### Opening the door

| Variable | Default | Effect |
|---|---|---|
| `AIMARKET_TOPUP_ENABLED` | `0` | Open the top-up door. Also needs the credits rail (`AIMARKET_CREDITS_ENABLED=1`) and a USDC asset. |
| `AIMARKET_TOPUP_PAY_TO` | the hub's `AIMARKET_X402_PAY_TO` | The wallet top-ups are paid to. |
| `AIMARKET_CREDITS_OPEN_SIGNUP` | `1` | Let agents open their own accounts. Closed, keys are issued by hand, and a stranger cannot buy at all. |
| `AIMARKET_SIGNUP_GRANT_USD` | `0` | Free credit a self-opened account starts with. Zero is right when credit is sold. |
| `AIMARKET_TOPUP_MIN_CONFIRMATIONS` | `2` | Confirmations before a payment is credited. |
| `AIMARKET_TOPUP_VERIFY_DECIMALS` | `1` | Read the token's `decimals()` before quoting (once per address). |
| `AIMARKET_TOPUP_MIN_USD`, `_MAX_USD`, `_DAILY_USD`, `_MAX_OPEN_QUOTES`, `_QUOTE_TTL_S`, `_RETAIN_DAYS` | see [the quote](#the-quote) | Limits. |
| `AIMARKET_SETTLE_RPC_URL` | the chain's defaults | Where payments are read (shared with seller-direct). |

The well-known says whether the door is open, and on what terms:

```json
"payment_rails": { "credits": {
  "enabled": true, "open_signup": true, "signup_url": "https://example-hub.net/ai-market/v2/accounts",
  "topup": { "enabled": true, "quote": "/ai-market/v2/account/topup", "redeem": "/ai-market/v2/topups/{nonce}",
             "binding": "eip3009", "asset": "USDC", "network": "eip155:8453", "pay_to": "0x7099…",
             "min_usd": 1.0, "max_usd": 100.0, "daily_usd": 500.0, "min_confirmations": 2, "quote_ttl_s": 900 }
} }
```

Shut, `topup` carries `"enabled": false` and a `reason`. A 402 on an invoke names the door too,
next to the signup URL.

### Refunds

Credit is prepaid service. Nothing refunds it automatically; unspent credit is returned, or not,
under the operator's own terms, by paying it back on chain and debiting the account.

### Recovering a payment the hub cannot credit

A plain transfer, or an authorization over a nonce this hub never minted, reaches the operator's
wallet but cannot be matched to an account. The operator credits it by hand, naming the
transaction:

```bash
curl -s -X POST $HUB/ai-market/v2/accounts/$ACCOUNT_ID/credit -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H 'Content-Type: application/json' -d "{\"amount_usd\": 5, \"tx_hash\": \"$TX\", \"note\": \"plain transfer\"}"
```

With `tx_hash` the account is checked first (a mistyped id answers `404` and claims nothing), the
credit is **paid** credit (`topped_up_usd`, not a grant), idempotent on the transaction, and the
transaction is claimed in the same registry every door uses — so it can then
neither fund a channel, nor be credited to a second account, nor be redeemed through a quote
(`409`). A payment made over a quote's nonce is better redeemed than credited by hand: anyone can
redeem it, and the quote then records it.

### Watching it

- `topup: credited $… to acct_… for 0x… (tx 0x…, payer 0x…)` at WARNING — every credit.
- `topup: … was already used at another door` at ERROR — a transaction presented to two doors.
- `stats.credits.topped_up_usd` against the operator wallet's balance on chain.
- `GET /ai-market/v2/account/topups` — an account's own history; `GET /ai-market/v2/topups/{nonce}` — one quote.

### Tables

Migration `041_credit_topup_quotes` adds `credit_topup_quotes` (the nonce, the account, the exact
amount in token units, payee, token, chain, expiry, status, transaction, payer, the units that
arrived, credited millicents, ledger reference, and `credited_at` as epoch seconds — the daily limit
compares a number, which reads the same on SQLite and PostgreSQL) and `credit_accounts.topped_up_mc`. No payment secret is stored:
nothing here needs one.

## How it is tested

| What | Where |
|---|---|
| The 402 (both forms), amounts, limits, the door shut by default, the well-known | `tests/test_topup.py::TestQuote` |
| Crediting once, replays, the x402 retry, a payment presented by someone else crediting the quote's owner, an overpayment capped at the quote, an underpayment credited for what arrived, payments that do not verify staying redeemable, an unbound transfer refused, rate limit, disabled accounts | `tests/test_topup.py::TestRedeem` |
| The channel door and the top-up door sharing one claim, a registry entry still being written retried rather than refused, two authorizations in one transaction, racing redemptions, a redemption interrupted before and after the credit, pruning, a top-up nonce refused by an invoke | `tests/test_topup.py::TestExclusivity` |
| An operator crediting an on-chain payment by hand: paid credit, idempotent, the transaction then refused to a second account, to the channel door and to a quote | `tests/test_topup.py::TestOperatorCredit` |
| Each defect the independent review confirmed: the hub-local registry, one claim whatever chain label a door uses, a failed payment record retried not refused, a half-written deposit leaving nothing, the daily limit at redemption, anonymous checks not locking the owner out, a claim whose writer died, a slow chain not stalling other requests, the token's decimals, an operator credit to an unknown account, an authorization-only x402 payload | `tests/test_topup.py::TestReviewFindings` |
| A stranger opening an account, buying 5 USDC of credit on a local anvil chain with a real EIP-3009 token contract, and spending it; a plain transfer refused; a short authorization credited for what arrived | `tests/test_topup_chain.py` |
| The SDK's typed data and calldata, byte-for-byte against `cast calldata` | `aimarket-agent/tests/test_topup.py` |

Every suite above passes on SQLite and on PostgreSQL (`AIMARKET_TEST_DATABASE_URL`).

`tests/test_topup_chain.py` runs anvil with Base's chain id and UniUSD
(`contracts/evm/src/UniUSD.sol`, the UNI bubble's USDC). Writing it found that UniUSD logged its
authorization as `AuthorizationUsedEvent` — a different topic from USDC's `AuthorizationUsed`, so
no EIP-3009 payment inside the bubble could ever have been verified. The event is now named as
USDC names it, and `contracts/evm/test/UniUSD.t.sol` pins the topic.

## Rules that are easy to miss

- **Pay with `transferWithAuthorization` over the quote's nonce.** A plain transfer is not credited
  automatically.
- **You send the transaction and pay the gas.** The hub has no key.
- **A paid quote does not expire**; an unpaid one is pruned after the retention period.
- **Redeem from anywhere.** The credit goes to the quoted account whoever presents the payment.
- **Wait for confirmations.** `payment_not_final` is a "later", not a "no".
- **What arrived is credited, up to the quote**, rounded down to the millicent; an overpayment is
  reported for the operator to refund.
- **One transaction, one credit** — across the top-up door and the channel-deposit door.
- **Credit is prepaid service**, not a deposit that earns or a balance that refunds itself.

See also: [subcontracting.md](subcontracting.md) · [mandates.md](mandates.md) · [a2a.md](a2a.md) ·
[money-rails.md](money-rails.md) · [the market rail](https://github.com/alexar76/aicom/blob/main/docs/hestia-hub-market-rail.md) ·
[glossary](https://github.com/alexar76/aicom/blob/main/docs/localization-glossary.md)
