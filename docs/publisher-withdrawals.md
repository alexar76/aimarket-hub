# Publisher earnings and Base USDC withdrawals

For calls paid with hub credits, capture and the publisher's share commit in one transaction. For a channel, the capture commits a publisher obligation in the same database transaction as the debit. It freezes the publisher account, the amount and the payout receipt. A worker retries incomplete obligations at hub startup and every minute. The credits ledger deduplicates on publisher and payout receipt, so a crash between crediting and acknowledging an obligation leaves one credit.

## Available to withdraw

An eligible sale adds both service credit and a withdrawable amount to the publisher's account. The backing comes from a verified Base USDC top-up spent on the call or a verified, transfer-funded Base USDC channel. A mixed credit payment accrues its backed share proportionally in whole millicents. Credit charges use backed top-up principal first and then backed publisher proceeds. Holds reduce spendable balance, and captured charges consume the backing. A grant, ordinary credit refund, card top-up, or publisher credit without funding provenance cannot increase the withdrawable amount. Escrow channel accrual and historical balances without recorded backing remain service credit.

`GET /ai-market/v2/account/withdrawals` shows the account's available amount, payout wallet and withdrawal history. Open reservations remain in the hub's outstanding liabilities until resolved.

## Publisher API

Authenticate every request with the publisher account's `X-API-Key`.

1. Send `POST /ai-market/v2/account/withdrawals/quote` with `request_id`, `amount_usdc`, and `recipient`. Use a unique request ID for each withdrawal. Send the amount as a decimal string with up to five decimal places, such as `"0.07001"`.
2. Sign the returned `message` from the recipient's externally owned wallet using EIP-191 `personal_sign`. The message binds the hub, account, request ID, token, chain, sender, amount and recipient. The quote is valid for ten minutes.
3. Send `POST /ai-market/v2/account/withdrawals` with the same fields plus `issued_at` and `signature`. The first successful request fixes the account's payout wallet. Enrollment uses the account key and a signature from that wallet. A repeated request ID with the same terms returns the original withdrawal, and a different amount or recipient conflicts.
4. Read `GET /ai-market/v2/account/withdrawals/{withdrawal_id}` for the status and transaction hash. `DELETE` on that URL releases a reservation before signing. A signed withdrawal stays reserved until a finalized receipt resolves it.

## Operator worker

Configure `AIMARKET_PUBLISHER_PAYOUT_FROM` on the hub and the operator worker. Fund this dedicated Base account with canonical USDC and ETH for fees. Give it its own transaction nonce sequence, separate from the channel, top-up, relay and other spending wallets. Keep `AIMARKET_PUBLISHER_PAYOUT_KEY_FILE` on the worker only, in a regular file readable only by its owner. The worker and hub need access to the same database. The `aimarket-hub[escrow]` extra supplies the EVM signing dependency.

```sh
python -m aimarket_hub.publisher_withdrawal_cli list
python -m aimarket_hub.publisher_withdrawal_cli run-once
python -m aimarket_hub.publisher_withdrawal_cli reconcile <withdrawal_id>
```

Schedule `run-once` on the operator host to service the queue. It signs one request or retries the sender's in-flight transfer. `send <withdrawal_id>` services a specific request. A worker persists the nonce, signed transaction and expected hash before calling the RPC. A restart or an ambiguous broadcast retries the same bytes. The hub reconciles finalized receipts every minute without access to the signing key.

`paid` requires a canonical finalized Base receipt for the reserved hash and an exact canonical USDC transfer from the configured sender to the reserved recipient. A finalized revert restores the balance once. A missing or conflicting receipt leaves funds reserved for reconciliation.
