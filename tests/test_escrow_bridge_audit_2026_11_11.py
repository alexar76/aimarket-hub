"""Regressions for the escrow-rail findings of the money-flow audit of 2026-11-11.

channels-1  a replayed receipt (e.g. with a wrong channel secret) abandoned the hub's own claim
escrow-1    a receiptId differing only in hex case reset the window's running total
escrow-2    withdrawing a call that a later total had superseded left the whole window
            uncollectable
federation-1 a federated call paid with credits still stored the buyer's escrow authorization
"""

from __future__ import annotations

import sqlite3

import pytest

from aimarket_hub.escrow_bridge import authorization, chain, mirror, signer as signer_mod, store
from aimarket_hub.escrow_bridge.eip712 import DebitAuthorization
from aimarket_hub.escrow_bridge.errors import AuthorizationRejected

pytest.importorskip("eth_account")

_ESCROW = "0xe7f1725e7734ce288f8367e1bb143e90bb3f0512"
_HUB = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
_TOKEN = "0x5fbdb2315678afecb367f032d93f642f64180aa3"
_ESCROW_CHANNEL = "0x" + "11" * 32
_CHAIN_ID = 8453
_ZERO = "0x" + "00" * 20
_DEPOSITOR_KEY = "0x" + "42" * 32
_NOW = 1_000_000
_DEADLINE = _NOW + 3600
_P = 1_000_000  # $1.00 in USDC base units


def _account():
    from eth_account import Account
    return Account.from_key(_DEPOSITOR_KEY)


@pytest.fixture
def st(tmp_path):
    s = store.AuthorizationStore(str(tmp_path / "bridge.db"))
    yield s
    s.close()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("AIMARKET_ESCROW_CONTRACT", _ESCROW)
    monkeypatch.setenv("AIMARKET_ESCROW_HUB_ADDRESS", _HUB)
    monkeypatch.setenv("AIMARKET_ESCROW_MAX_UNCOLLECTED_USD", "5.0")
    state = {"nonce": 0}

    def _read(cid, address=None):
        return chain.EscrowChannel(
            channel_id=_ESCROW_CHANNEL, depositor=_account().address.lower(), hub=_ZERO,
            token=_TOKEN, deposit_amount=5_000_000, balance=5_000_000, used_amount=0,
            expires_at=4_000_000_000, nonce=state["nonce"], status=chain.STATUS_OPEN,
        )

    monkeypatch.setattr(chain, "chain_id", lambda: _CHAIN_ID)
    monkeypatch.setattr(chain, "escrow_address", lambda: _ESCROW)
    monkeypatch.setattr(chain, "read_channel", _read)
    monkeypatch.setattr(chain, "receipt_already_used", lambda *a, **k: False)
    monkeypatch.setattr(chain, "simulate", lambda **k: {"ok": True, "error": "", "gas": 100_000})
    return state


def _payload(*, amount, receipt, nonce=0):
    from eth_account import Account
    from aimarket_hub.escrow_bridge import eip712

    auth = DebitAuthorization(channel_id=_ESCROW_CHANNEL, hub=_HUB, token=_TOKEN, amount=amount,
                              receipt_id=receipt, nonce=nonce, deadline=_DEADLINE)
    digest = eip712.debit_digest(auth, chain_id=_CHAIN_ID, verifying_contract=_ESCROW)
    sig = Account._sign_hash(digest, Account.from_key(_DEPOSITOR_KEY).key).signature.hex()
    return {**auth.as_message(), "signature": sig if sig.startswith("0x") else "0x" + sig}


def _accept(st, payload, price_usd=1.0):
    return authorization.verify_and_store(
        payload=payload, ledger_channel_id="ch_abc", escrow_channel_id=_ESCROW_CHANNEL,
        expected_amount_usd=price_usd, expected_receipt_id=payload["receiptId"], authorizations=st,
        now=_NOW,
    )


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from aimarket_hub import channels as ch_mod

    path = str(tmp_path / "channels.db")
    led = ch_mod.ChannelLedger(db_path=path)
    led._running = False
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO channels (channel_id, balance_cents, original_deposit_cents, used_cents, "
            "wallet, status, opened_at, expires_at, secret_hash) VALUES "
            "('ch_abc', 500, 500, 0, '', 'open', '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z', ?)",
            (ch_mod._hash_secret("s3cret"),),
        )
    monkeypatch.setattr(ch_mod, "_DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return led


class _RecordingSigner(signer_mod.Signer):
    name = "recording"

    def __init__(self):
        self.sent = []

    @property
    def sender(self):
        return _HUB

    def submit(self, tx):
        self.sent.append(tx)
        return "0x" + "ee" * 32


def _units_in_calldata(data_hex: str) -> int:
    raw = bytes.fromhex(data_hex[2:])
    return int.from_bytes(raw[4 + 32:4 + 64], "big")


def test_a_replayed_receipt_never_abandons_the_stored_claim(st, wired):
    """channels-1: the same receipt again (a wrong secret skips the idempotency answer) is refused
    and the delivered call's claim stays live."""
    ra = "0x" + "a1" * 32
    _accept(st, _payload(amount=_P, receipt=ra))
    with pytest.raises(AuthorizationRejected, match="already stored"):
        _accept(st, _payload(amount=_P, receipt=ra))
    assert st.get(ra).status == store.PENDING
    assert st.active_at(_ESCROW_CHANNEL, 0).receipt_id == ra


def test_a_case_variant_receipt_is_the_same_receipt(st, wired):
    """escrow-1: 0xABAB… and 0xabab… are one bytes32; the variant is a replay, not a second call
    that signs only its own price."""
    upper = "0x" + "AB" * 32
    row = _accept(st, _payload(amount=_P, receipt=upper)).row
    assert row.receipt_id == upper.lower()
    with pytest.raises(AuthorizationRejected, match="already stored"):
        _accept(st, _payload(amount=_P, receipt="0x" + "ab" * 32))
    with pytest.raises(AuthorizationRejected, match="already stored"):
        _accept(st, _payload(amount=_P, receipt="0x" + "Ab" * 32))
    assert st.active_at(_ESCROW_CHANNEL, 0).amount_units == _P
    assert authorization.canonical_receipt_id("0x" + "AB" * 32) == "0x" + "ab" * 32
    for bad in ("0x1234", "ab" * 32, "0x" + "zz" * 32, "", None):
        assert authorization.canonical_receipt_id(bad) is None


def test_a_withdrawn_superseded_call_leaves_the_window_collectable(st, wired, ledger):
    """escrow-2: A is superseded by B (2P), then A fails. The next call signs the standing total
    (B + C = 2P, not 3P), replaces B's over-claim, and the mirror collects exactly what the ledger
    debited."""
    ra, rb, rc = "0x" + "a1" * 32, "0x" + "b2" * 32, "0x" + "c3" * 32
    _accept(st, _payload(amount=_P, receipt=ra))
    assert ledger.hold("ch_abc", 1.0, receipt_id=ra, secret="s3cret").get("ok")
    _accept(st, _payload(amount=2 * _P, receipt=rb))
    assert ledger.hold("ch_abc", 1.0, receipt_id=rb, secret="s3cret").get("ok")
    assert ledger.capture_hold(rb).get("ok")
    authorization.withdraw(ra, st, reason="the call was not delivered")
    assert ledger.release_hold(ra).get("ok")
    assert st.window_call_units(_ESCROW_CHANNEL, 0) == _P            # only B stands

    with pytest.raises(AuthorizationRejected, match="running total"):
        _accept(st, _payload(amount=3 * _P, receipt=rc))              # the old, poisoned total
    _accept(st, _payload(amount=2 * _P, receipt="0x" + "c4" * 32))   # B + C
    assert ledger.hold("ch_abc", 1.0, receipt_id="0x" + "c4" * 32, secret="s3cret").get("ok")
    assert ledger.capture_hold("0x" + "c4" * 32).get("ok")
    assert st.get(rb).status == store.ABANDONED and st.get(rb).last_error == store.SUPERSEDED_REASON

    signer = _RecordingSigner()
    m = mirror.Mirror(authorizations=st, signer=signer, require_enabled=False)
    m._dry_run = False
    m.run(now=_NOW)
    assert len(signer.sent) == 1
    assert _units_in_calldata(signer.sent[0].data) == 2 * _P


def test_rows_from_before_call_units_keep_the_old_rule(st, wired):
    """A store written before 2026-11-11 has call_units = 0: the live row's own total is carried."""
    ra = "0x" + "a1" * 32
    _accept(st, _payload(amount=_P, receipt=ra))
    st._conn.execute("UPDATE debit_authorizations SET call_units = 0")
    st._conn.commit()
    assert st.window_call_units(_ESCROW_CHANNEL, 0) is None
    _accept(st, _payload(amount=2 * _P, receipt="0x" + "b2" * 32))


# ── federation-1 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", ["sells", "resells"])
def test_a_federated_call_paid_with_credits_stores_no_escrow_authorization(tmp_path, monkeypatch, shape):
    from fastapi.testclient import TestClient

    import aimarket_hub.channels as channels_mod
    import aimarket_hub.escrow_bridge.authorization as bridge_auth
    import aimarket_hub.hub_routes.invoke as invoke_routes
    import aimarket_hub.outbound_http as ohttp
    from aimarket_hub.api import create_app
    from aimarket_hub.channels import ChannelLedger
    from aimarket_hub.config import HubConfig
    from aimarket_hub.database import HubDatabase
    from aimarket_hub.models import Capability, Peer
    from aimarket_hub.signing import Signer

    admin, peer_url, mcp = "fed-admin-token", "https://oracle-peer.example.com", \
        "https://oracle-peer.example.com/ai-market/mcp"

    class _Resp:
        def __init__(self, status_code=200, payload=None):
            self.status_code, self._p, self.text, self.headers = status_code, payload, "", {}

        def json(self):
            return self._p

    async def safe_get(url, *, timeout=10.0):
        return _Resp(200, {"mcp_endpoint": mcp})

    async def safe_post(url, *, json=None, headers=None, timeout=30.0, invoke=False, oracle_paid=False):
        return _Resp(200, {"ok": True, "result": {"v": 1},
                           "receipt": {"nonce": "orc_1", "latency_ms": 5, "price_usd": 0.05}})

    monkeypatch.setenv("AIFACTORY_CRYPTO_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_CREDITS_ENABLED", "1")
    monkeypatch.setenv("AIMARKET_CREDITS_FREE_GRANT_USD", "0")
    monkeypatch.setenv("AIMARKET_ALLOW_DEMO_CREDIT", "1")
    monkeypatch.setenv("AIMARKET_ADMIN_TOKEN", admin)
    monkeypatch.setenv("AIFACTORY_PUBLIC_URL", "http://factory.test")
    monkeypatch.setenv("AIMARKET_ROUTING_FEE_BPS", "1000")
    monkeypatch.setattr(channels_mod, "_ledger", ChannelLedger(db_path=str(tmp_path / "channels.db")))
    config = HubConfig()
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "key")
    db = HubDatabase(config.db_path)
    app = create_app(config=config, db=db, signer=Signer(config.signing_key_path))
    db.upsert_peer(Peer(url=peer_url, name=peer_url, capabilities_count=1,
                        well_known_url=f"{peer_url}/.well-known/ai-market.json", trusted=True))
    db.upsert_capability(Capability(capability_id="cap.x@v1", product_id="prod-x", name="x",
                                    price_per_call_usd=0.05, source_hub=peer_url, source_hub_name=peer_url,
                                    trust_score=0.5))
    if shape == "sells":
        monkeypatch.setattr(config, "sells_on_behalf_of", lambda _u: True)
    else:
        monkeypatch.setattr(config, "peer_api_key", lambda _u: "aimk_router_key_at_peer")
    monkeypatch.setattr(ohttp, "safe_get", safe_get)
    monkeypatch.setattr(ohttp, "safe_post", safe_post)
    monkeypatch.setattr(invoke_routes, "channel_escrow_binding", lambda _cid: "0x" + "cd" * 32)
    stored = []
    monkeypatch.setattr(bridge_auth, "verify_and_store", lambda **kw: stored.append(kw))

    with TestClient(app) as client:
        signup = client.post("/ai-market/v2/accounts", json={"label": "buyer"}).json()
        client.post(f"/ai-market/v2/accounts/{signup['account_id']}/credit",
                    json={"amount_usd": 1.0}, headers={"Authorization": f"Bearer {admin}"})
        opened = channels_mod._ledger.open(5.0, with_secret=True)["channel"]
        r = client.post("/ai-market/v2/invoke", json={
            "product_id": "prod-x", "capability_id": "cap.x@v1", "source_hub": peer_url, "input": {},
            "payment_authorization": {"receiptId": "0x" + "77" * 32, "amount": "55000", "nonce": 0},
        }, headers={"X-API-Key": signup["api_key"], "X-Payment-Channel": opened["channel_id"],
                    "X-Payment-Channel-Secret": opened["channel_secret"]})
        acct = client.get("/ai-market/v2/account", headers={"X-API-Key": signup["api_key"]}).json()

    assert r.status_code == 200, r.text
    assert stored == []                                   # credits paid: nothing claims the escrow
    expected = 0.05 if shape == "sells" else 0.055        # a resale now pays its routing fee
    assert acct["spent_usd"] == pytest.approx(expected)


# ── collection that used to stall for good ───────────────────────────────────


def _raw_row(st, receipt, *, channel=_ESCROW_CHANNEL, amount=_P, deadline=_DEADLINE, escrow=_ESCROW):
    return st.record(receipt_id=receipt, ledger_channel="ch_abc", escrow_channel=channel, chain_id=_CHAIN_ID,
                     escrow_address=escrow, hub=_HUB, token=_TOKEN, depositor=_account().address.lower(),
                     amount_units=amount, nonce=0, deadline=deadline, signature="0x" + "00" * 65,
                     call_units=amount)


def test_stuck_rows_on_low_channel_ids_no_longer_fill_the_pass(st, wired, ledger, monkeypatch):
    """cross-cutting-2: the host sweep runs `submit --limit 5`, and rows were taken in channel-id
    order; five rows that never progress on low ids filled every pass. Rows are taken by deadline
    now, so a healthy row with the nearest deadline is reached and sent."""
    monkeypatch.setattr(chain, "read_channel", lambda *a, **k: chain.EscrowChannel(
        channel_id=_ESCROW_CHANNEL, depositor=_account().address.lower(), hub=_ZERO, token=_TOKEN,
        deposit_amount=5_000_000, balance=5_000_000, used_amount=0, expires_at=4_000_000_000, nonce=0,
        status=chain.STATUS_OPEN))
    for i in range(5):     # no ledger debit behind them: guard 2 blocks them on every pass
        _raw_row(st, "0x" + f"{i:02x}" * 32, channel="0x" + f"{i:02x}" * 32, deadline=_DEADLINE + 3000)
    healthy = "0x" + "fe" * 32
    _raw_row(st, healthy, channel="0x" + "ff" * 32)
    assert ledger.hold("ch_abc", 1.0, receipt_id=healthy, secret="s3cret").get("ok")
    assert ledger.capture_hold(healthy).get("ok")
    signer = _RecordingSigner()
    m = mirror.Mirror(authorizations=st, signer=signer, require_enabled=False)
    m._dry_run = False
    m.run(now=_NOW, limit=5)
    assert len(signer.sent) == 1 and st.get(healthy).status == store.SUBMITTED


def test_an_expired_row_on_an_address_without_a_contract_is_abandoned(st, wired, monkeypatch):
    """L-1 (seen live on the UNI hub): the replay-flag read fails forever on an address with no
    contract, and that guard ran before the deadline guard, so the row stayed pending forever."""
    gone = "0xf4fe699cceece5e016521dda25a4b8641248b624"
    _raw_row(st, "0x" + "d1" * 32, escrow=gone)

    def unreadable(*a, **k):
        raise chain.ChainUnavailable("usedReceipts returned 0 bytes")

    monkeypatch.setattr(chain, "receipt_already_used", unreadable)
    monkeypatch.setattr(chain, "has_no_code", lambda address: address.lower() == gone)
    m = mirror.Mirror(authorizations=st, signer=_RecordingSigner(), require_enabled=False)
    m.run(now=_NOW)
    assert st.get("0x" + "d1" * 32).status == store.PENDING        # not expired: wait, as before
    m.run(now=_DEADLINE + 1)
    assert st.get("0x" + "d1" * 32).status == store.ABANDONED


def test_a_row_above_the_per_pass_ceiling_goes_out_alone(st, wired, ledger, monkeypatch):
    """escrow-6 / cross-cutting-3: a row above AIMARKET_ESCROW_MAX_USD_PER_PASS was skipped on
    every pass until it expired. It is sent as the first and only submission of a pass."""
    monkeypatch.setenv("AIMARKET_ESCROW_MAX_USD_PER_PASS", "0.5")
    big, small = "0x" + "b1" * 32, "0x" + "b2" * 32
    _raw_row(st, big, deadline=_DEADLINE - 100)
    _raw_row(st, small, channel="0x" + "22" * 32, amount=100_000)
    for receipt, usd in ((big, 1.0), (small, 0.1)):
        assert ledger.hold("ch_abc", usd, receipt_id=receipt, secret="s3cret").get("ok")
        assert ledger.capture_hold(receipt).get("ok")
    signer = _RecordingSigner()
    m = mirror.Mirror(authorizations=st, signer=signer, require_enabled=False)
    m._dry_run = False
    m.run(now=_NOW)
    assert st.get(big).status == store.SUBMITTED and st.get(small).status != store.SUBMITTED
    assert len(signer.sent) == 1


def test_a_window_above_the_daily_ceiling_is_refused_at_admission(st, wired, monkeypatch):
    """cross-cutting-3: one debit per window and at most AIMARKET_ESCROW_MAX_USD_PER_DAY a day —
    a bigger window would be served and could never be collected."""
    monkeypatch.setenv("AIMARKET_ESCROW_MAX_USD_PER_DAY", "0.5")
    with pytest.raises(AuthorizationRejected, match="collects in a day"):
        _accept(st, _payload(amount=_P, receipt="0x" + "e1" * 32))


def test_a_full_daily_ceiling_refuses_new_escrow_calls(st, wired, monkeypatch):
    """escrow-6: with the day's ceiling already sent elsewhere, a new call was served and then
    expired unsent. It is refused at admission now, and served again once there is room."""
    monkeypatch.setenv("AIMARKET_ESCROW_MAX_USD_PER_DAY", "1.5")
    other = "0x" + "77" * 32
    st.record(receipt_id=other, ledger_channel="ch_x", escrow_channel="0x" + "99" * 32,
              chain_id=_CHAIN_ID, escrow_address=_ESCROW, hub=_HUB, token=_TOKEN,
              depositor="0x" + "aa" * 20, amount_units=_P, nonce=0, deadline=_DEADLINE,
              signature="0x" + "ab" * 65)
    st.mark_submitted(other, "0x" + "ee" * 32)            # $1 of today's $1.50 already sent
    with pytest.raises(AuthorizationRejected, match="collection for the day is full"):
        _accept(st, _payload(amount=_P, receipt="0x" + "a7" * 32))
    _accept(st, _payload(amount=_P // 2, receipt="0x" + "a8" * 32), price_usd=0.5)
