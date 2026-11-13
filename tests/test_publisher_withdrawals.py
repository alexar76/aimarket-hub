"""Publisher earnings and withdrawals against the real credit ledger and Hub routes."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import Barrier

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from aimarket_hub import credits, publisher_withdrawal_cli, settle
from aimarket_hub.database import HubDatabase
from aimarket_hub.models import Capability
from aimarket_hub.publisher_withdrawal_chain import PayoutChainError, SignedPayout, USDC_ADDRESS
from aimarket_hub.publisher_withdrawals import PublisherWithdrawals, WithdrawalError
from tests._mandate_kit import PG_URL, _reset_postgres, hub
from tests.test_topup import PAY_TO, Chain, tx

PATH = "/ai-market/v2/account/withdrawals"
SENDER = "0x" + "a7" * 20
RECIPIENT = "0x" + "b8" * 20
TX = "0x" + "19" * 32
PAYOUT = SignedPayout(raw_tx="0x02abcd", tx_hash=TX)


@pytest.fixture
def open_database(tmp_path):
    if PG_URL:
        _reset_postgres(PG_URL)
    return partial(HubDatabase, tmp_path / "hub.db", database_url=PG_URL)


def sale(ledger, buyer, publisher, receipt, *, price=0.40, share=0.28):
    hold = ledger.hold(buyer, price, receipt)
    assert "error" not in hold, hold
    payout = credits.PublisherShare(publisher, share, receipt, "capability sale")
    assert ledger.capture_hold(receipt, publisher_share=payout)["captured_usd"] == price
    assert ledger.capture_hold(receipt, publisher_share=payout)["already"] == "captured"


def backed_sale(ledger):
    buyer = ledger.create_account()["account_id"]
    seller = ledger.create_account()
    ledger.deposit(buyer, 40_000, reference="verified-usdc-top-up", base_usdc=True)
    sale(ledger, buyer, seller["account_id"], "backed-sale")
    return seller


def test_only_backed_sale_proceeds_can_be_withdrawn_and_spent_proceeds_stay_spent(open_database):
    db = open_database()
    try:
        ledger = credits.CreditsLedger(db._conn)
        store = PublisherWithdrawals(ledger)
        buyer = ledger.create_account(grant_usd=1.0)["account_id"]
        seller = ledger.create_account()["account_id"]
        ledger.deposit(buyer, 20_000, reference="verified-usdc-top-up", base_usdc=True)
        ledger.deposit(buyer, 40_000, reference="stripe:test-card-top-up")

        sale(ledger, buyer, seller, "partly-backed")
        sale(ledger, buyer, seller, "unbacked")
        assert ledger.balance(seller) == ledger.earnings(seller) == 0.56
        assert store.summary(seller, 20)["withdrawable_usdc"] == 0.14
        ledger.debit(seller, 0.09, "publisher-spend")
        ledger.refund(seller, 0.09, "operator credit adjustment")
        ledger.grant(seller, 1.0)
        assert store.summary(seller, 20)["withdrawable_usdc"] == 0.05
        accrued = [row for row in ledger.recent(seller) if row["kind"] == "publisher_cash_accrual"]
        assert [(row["receipt_id"], row["amount_usd"]) for row in accrued] == [("partly-backed", 0.14)]
        with pytest.raises(WithdrawalError, match="insufficient withdrawable"):
            store.reserve(account=seller, request_id="unbacked-withdrawal", amount_mc=5001,
                          recipient=RECIPIENT, sender=SENDER)
    finally:
        db.close()


def signed_request(client, headers, wallet, *, request_id="publisher-request-01", amount="0.07001"):
    body = {"request_id": request_id, "amount_usdc": amount, "recipient": wallet.address}
    response = client.post(f"{PATH}/quote", headers=headers, json=body)
    assert response.status_code == 200, response.text
    quote = response.json()
    message = encode_defunct(text=quote["message"])
    return {**body, "issued_at": quote["issued_at"], "signature": wallet.sign_message(message).signature.hex()}


def test_withdrawal_api_checks_wallet_and_account_and_reserves_once(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path, AIMARKET_PUBLISHER_PAYOUT_FROM=SENDER) as (client, db):
        ledger = credits.CreditsLedger(db._conn)
        seller = backed_sale(ledger)
        seller_headers = {"X-API-Key": seller["api_key"]}
        other = ledger.create_account()
        other_headers = {"X-API-Key": other["api_key"]}
        wallet = Account.create()
        other_wallet = Account.create()
        request = signed_request(client, seller_headers, wallet)

        assert client.post(PATH, json=request).status_code == 401
        assert client.post(PATH, headers=other_headers, json=request).status_code == 403
        assert client.post(PATH, headers=seller_headers,
                           json={**request, "recipient": other_wallet.address}).status_code == 403
        assert ledger.balance(seller["account_id"]) == 0.28

        reserved = client.post(PATH, headers=seller_headers, json=request)
        assert reserved.status_code == 202, reserved.text
        row = reserved.json()
        assert row["status"] == "requested"
        assert row["recipient"] == wallet.address.lower() and row["sender"] == SENDER
        assert row["amount_atomic"] == "70010" and row["chain_id"] == 8453 and row["token"] == USDC_ADDRESS
        assert "raw_tx" not in row

        again = client.post(PATH, headers=seller_headers, json=request)
        assert again.status_code in (200, 202), again.text
        assert again.json()["withdrawal_id"] == row["withdrawal_id"]
        changed = signed_request(client, seller_headers, wallet, amount="0.07002")
        assert client.post(PATH, headers=seller_headers, json=changed).status_code == 409
        replacement = signed_request(client, seller_headers, other_wallet, request_id="publisher-request-02")
        assert client.post(PATH, headers=seller_headers, json=replacement).status_code == 409

        status_url = f"{PATH}/{row['withdrawal_id']}"
        assert client.get(status_url, headers=seller_headers).json()["status"] == "requested"
        assert client.get(status_url, headers=other_headers).status_code == 404
        assert client.get(PATH, headers=other_headers).json()["withdrawals"] == []
        summary = client.get(PATH, headers=seller_headers).json()
        assert summary["withdrawable_usdc"] == ledger.balance(seller["account_id"]) == 0.20999
        assert [item["withdrawal_id"] for item in summary["withdrawals"]] == [row["withdrawal_id"]]
        assert ledger.stats()["publisher_withdrawal_pending_usd"] == 0.07001
        assert ledger.stats()["outstanding_credit_usd"] == 0.28


def test_verified_base_topup_and_paid_invoke_make_publisher_proceeds_withdrawable(monkeypatch, tmp_path):
    chain = Chain()
    monkeypatch.setattr(settle, "verify_transfer", chain.verify)
    with hub(monkeypatch, tmp_path, AIMARKET_PUBLISHER_PAYOUT_FROM=SENDER, AIMARKET_PUBLISHER_SHARE_BPS="7000",
             AIMARKET_TOPUP_ENABLED="1", AIMARKET_TOPUP_VERIFY_DECIMALS="0", AIMARKET_X402_CHAIN="base",
             AIMARKET_X402_PAY_TO=PAY_TO) as (client, db):
        ledger = credits.CreditsLedger(db._conn)
        buyer = ledger.create_account()
        seller = ledger.create_account()
        buyer_headers = {"X-API-Key": buyer["api_key"]}
        seller_headers = {"X-API-Key": seller["api_key"]}
        db.upsert_capability(Capability(
            capability_id="publisher.echo@v1", product_id="publisher-echo", name="Publisher echo",
            description="Test publisher payout", price_per_call_usd=0.40, source_hub="local",
            publisher_id=seller["account_id"], prompt_template='{"answer":"echo","ok":true}',
        ))

        quote = client.post("/ai-market/v2/account/topup", headers=buyer_headers, json={"amount_usd": 2})
        assert quote.status_code == 402, quote.text
        nonce = quote.json()["nonce"]
        chain.pays(tx(7), nonce, 2_000_000)
        for _ in range(2):
            redeemed = client.post(f"/ai-market/v2/topups/{nonce}", json={"tx_hash": tx(7)})
            assert redeemed.status_code == 200, redeemed.text

        result = client.post("/ai-market/v2/invoke", headers=buyer_headers, json={
            "product_id": "publisher-echo", "capability_id": "publisher.echo@v1",
            "source_hub": "local", "input": {},
        })
        assert result.status_code == 200, result.text
        assert ledger.balance(buyer["account_id"]) == 1.60
        assert client.get(PATH, headers=seller_headers).json()["withdrawable_usdc"] == 0.28
        publisher_ledger = ledger.recent(seller["account_id"])
        assert {item["kind"] for item in publisher_ledger} == {"payout", "publisher_cash_accrual"}
        assert {item["receipt_id"] for item in publisher_ledger} == {result.json()["receipt"]["nonce"]}

        body = signed_request(client, seller_headers, Account.create(), amount="0.28")
        reserved = client.post(PATH, headers=seller_headers, json=body)
        assert reserved.status_code == 202 and reserved.json()["amount_atomic"] == "280000", reserved.text
        assert client.get(PATH, headers=seller_headers).json()["withdrawable_usdc"] == 0


def test_only_owner_can_cancel_before_signing_and_reservation_returns_once(monkeypatch, tmp_path):
    with hub(monkeypatch, tmp_path, AIMARKET_PUBLISHER_PAYOUT_FROM=SENDER) as (client, db):
        ledger = credits.CreditsLedger(db._conn)
        store = PublisherWithdrawals(ledger)
        seller = backed_sale(ledger)
        headers = {"X-API-Key": seller["api_key"]}
        other_headers = {"X-API-Key": ledger.create_account()["api_key"]}
        wallet = Account.create()
        body = signed_request(client, headers, wallet)
        response = client.post(PATH, headers=headers, json=body)
        assert response.status_code == 202, response.text
        withdrawal_id = response.json()["withdrawal_id"]
        url = f"{PATH}/{withdrawal_id}"
        assert client.delete(url).status_code == 401
        assert client.delete(url, headers=other_headers).status_code == 404
        for _ in range(2):
            cancelled = client.delete(url, headers=headers)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
        replay = client.post(PATH, headers=headers, json=body)
        assert replay.json()["status"] == "cancelled"
        assert ledger.stats()["publisher_withdrawal_pending_usd"] == 0
        assert ledger.balance(seller["account_id"]) == 0.28
        entries = ledger.recent(seller["account_id"])
        assert sum(row["kind"] == "publisher_withdraw_cancelled" for row in entries) == 1

        next_body = signed_request(client, headers, wallet, request_id="publisher-request-02")
        signed_id = client.post(PATH, headers=headers, json=next_body).json()["withdrawal_id"]
        assert store.save_signed(store.get(signed_id), 13, PAYOUT)["status"] == "signed"
        assert client.delete(f"{PATH}/{signed_id}", headers=headers).status_code == 409
        assert ledger.stats()["publisher_withdrawal_pending_usd"] == 0.07001
        assert ledger.balance(seller["account_id"]) == 0.20999


class FakePayoutChain:
    sender = SENDER

    def __init__(self, store, withdrawal_id):
        self.store = store
        self.withdrawal_id = withdrawal_id
        self.prepared = 0
        self.sent = []
        self.verdict = "pending"

    def pending_nonce(self):
        return 13

    def prepare(self, *, recipient, amount_units, nonce, private_key):
        assert (recipient, amount_units, nonce) == (RECIPIENT, 70_010, 13)
        self.prepared += 1
        return PAYOUT

    def reconcile(self, *, tx_hash, recipient, amount_units):
        assert (tx_hash, recipient, amount_units) == (TX, RECIPIENT, 70_010)
        return self.verdict

    def broadcast(self, payout, *, recipient, amount_units, nonce):
        assert (recipient, amount_units, nonce) == (RECIPIENT, 70_010, 13)
        row = self.store.get(self.withdrawal_id)
        assert row["status"] in ("signed", "broadcast")
        assert (row["tx_hash"], row["raw_tx"]) == (payout.tx_hash, payout.raw_tx)
        self.sent.append(payout)
        if len(self.sent) == 1:
            raise PayoutChainError("connection lost after sending")
        return payout.tx_hash


@pytest.mark.parametrize("verdict, final_balance", [("paid", 0.20999), ("reverted", 0.28)])
def test_signed_payout_survives_restart_and_finalizes_once(open_database, verdict, final_balance):
    db = open_database()
    try:
        ledger = credits.CreditsLedger(db._conn)
        account = backed_sale(ledger)["account_id"]
        store = PublisherWithdrawals(ledger)
        withdrawal_id = store.reserve(account=account, request_id="publisher-request-01", amount_mc=7001,
                                      recipient=RECIPIENT, sender=SENDER)["withdrawal_id"]
        chain = FakePayoutChain(store, withdrawal_id)
        first = store.process(withdrawal_id, chain, private_key="unused-test-key", broadcast=True)
        assert first["status"] == "signed" and first["broadcast_unconfirmed"] is True
    finally:
        db.close()

    reopened = open_database()
    try:
        ledger = credits.CreditsLedger(reopened._conn)
        store = PublisherWithdrawals(ledger)
        chain.store = store
        second = store.process(withdrawal_id, chain, broadcast=True)
        assert second["status"] == "broadcast"
        assert chain.prepared == 1 and chain.sent == [PAYOUT, PAYOUT]

        chain.verdict = "mismatch"
        with pytest.raises(WithdrawalError, match="does not match"):
            store.process(withdrawal_id, chain, broadcast=True)
        assert ledger.stats()["publisher_withdrawal_pending_usd"] == 0.07001
        assert ledger.balance(account) == 0.20999 and len(chain.sent) == 2

        chain.verdict = verdict
        done = store.process(withdrawal_id, chain)
        assert (done["status"], done["tx_hash"], done["raw_tx"]) == (verdict, TX, "")
        assert store.process(withdrawal_id, chain, broadcast=True)["status"] == verdict
        assert ledger.stats()["publisher_withdrawal_pending_usd"] == 0
        assert ledger.stats()["outstanding_credit_usd"] == final_balance
        assert store.summary(account, 20)["withdrawable_usdc"] == ledger.balance(account) == final_balance
        assert sum(row["kind"] == f"publisher_withdraw_{verdict}" for row in ledger.recent(account)) == 1
    finally:
        reopened.close()


def test_operator_run_once_recovers_signed_payout_before_queued_work(open_database, tmp_path, monkeypatch, capsys):
    db = open_database()
    try:
        ledger = credits.CreditsLedger(db._conn)
        store = PublisherWithdrawals(ledger)
        account = backed_sale(ledger)["account_id"]
        first = store.reserve(account=account, request_id="publisher-request-01", amount_mc=7001,
                              recipient=RECIPIENT, sender=SENDER)["withdrawal_id"]
        chain = FakePayoutChain(store, first)
        key_path = tmp_path / "payout-key"
        key_path.write_text("43" * 32, encoding="ascii")
        key_path.chmod(0o600)
        monkeypatch.setenv("AIMARKET_DB_PATH", str(tmp_path / "hub.db"))
        monkeypatch.setenv("DATABASE_URL", PG_URL)
        monkeypatch.setenv("AIMARKET_PUBLISHER_PAYOUT_FROM", SENDER)
        monkeypatch.setenv("AIMARKET_PUBLISHER_PAYOUT_KEY_FILE", str(key_path))
        monkeypatch.setattr(publisher_withdrawal_cli, "PublisherWithdrawalChain", lambda sender: chain)
        monkeypatch.setattr(sys, "argv", ["publisher_withdrawal_cli", "run-once"])

        publisher_withdrawal_cli.main()
        queued = json.loads(capsys.readouterr().out)
        assert (queued["status"], queued["tx_hash"], queued["broadcast_unconfirmed"]) == ("signed", TX, True)
        assert "raw_tx" not in queued
        second = store.reserve(account=account, request_id="publisher-request-02", amount_mc=7001,
                               recipient=RECIPIENT, sender=SENDER)["withdrawal_id"]
        key_path.unlink()
        chain.verdict = "paid"

        publisher_withdrawal_cli.main()
        reconciled = json.loads(capsys.readouterr().out)
        assert (reconciled["status"], reconciled["withdrawal_id"]) == ("paid", first)
        assert store.get(second)["status"] == "requested"
        assert chain.prepared == 1 and chain.sent == [PAYOUT]
    finally:
        db.close()


def test_concurrent_reservations_cannot_overdraw_one_publisher(open_database):
    left = open_database()
    right = open_database()
    try:
        account = backed_sale(credits.CreditsLedger(left._conn))["account_id"]
        barrier = Barrier(2)

        def reserve(db, request_id):
            store = PublisherWithdrawals(credits.CreditsLedger(db._conn))
            barrier.wait(timeout=5)
            try:
                return store.reserve(account=account, request_id=request_id, amount_mc=20_000,
                                     recipient=RECIPIENT, sender=SENDER)["status"]
            except WithdrawalError as exc:
                assert exc.status == 409 and "insufficient withdrawable" in str(exc)
                return "insufficient"

        with ThreadPoolExecutor(max_workers=2) as workers:
            requests = [workers.submit(reserve, db, f"publisher-request-{i}") for i, db in enumerate((left, right))]
            assert {result.result(timeout=10) for result in requests} == {"requested", "insufficient"}
        ledger = credits.CreditsLedger(left._conn)
        assert ledger.balance(account) == 0.08
        assert ledger.stats()["publisher_withdrawal_pending_usd"] == 0.20
    finally:
        right.close()
        left.close()
