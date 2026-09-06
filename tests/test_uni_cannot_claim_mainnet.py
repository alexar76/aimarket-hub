"""A sealed-bubble hub must not advertise a real payment rail.

`payment_testnet` appears exactly once in the codebase — in
`/.well-known/ai-market.json`. That makes it the one field a crawler can use to tell a real
rail from a simulated one, and `uni.modelmarket.dev` was getting it wrong: it served
`payment_testnet: false` with `supported_chains: ["base"]` and the live hub's own name while
running on Anvil chain 31337 with virtual amounts. `deploy_uni_hub.sh` warns about exactly
that in its header; the env said mainnet and nothing checked it against the realm.
"""

from __future__ import annotations

import importlib

import pytest


def _fresh_config(monkeypatch, **env: str):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import aimarket_hub.config as cfg

    importlib.reload(cfg)
    return cfg


def test_uni_realm_advertises_testnet_even_when_the_env_says_mainnet(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        AIMARKET_CHAIN_REALM="uni",
        AIFACTORY_PAYMENT_TESTNET="0",
    )
    assert cfg.HubConfig().payment_testnet is True, (
        "a UNI deployment claimed mainnet — a crawler would index simulated amounts as real"
    )


@pytest.mark.parametrize("realm_value", ["uni", "bubble", "virtual"])
def test_every_alias_of_the_bubble_realm_is_covered(monkeypatch, realm_value):
    """`realm()` accepts three spellings; the guard must not key on one of them."""
    cfg = _fresh_config(
        monkeypatch,
        AIMARKET_CHAIN_REALM=realm_value,
        AIFACTORY_PAYMENT_TESTNET="0",
    )
    assert cfg.HubConfig().payment_testnet is True


def test_live_realm_still_honours_the_env(monkeypatch):
    """The guard must not quietly turn the real hub into a testnet advertisement."""
    cfg = _fresh_config(
        monkeypatch,
        AIMARKET_CHAIN_REALM="live",
        AIFACTORY_PAYMENT_TESTNET="0",
    )
    assert cfg.HubConfig().payment_testnet is False


def test_testnet_env_wins_in_both_realms(monkeypatch):
    for realm_value in ("live", "uni"):
        cfg = _fresh_config(
            monkeypatch,
            AIMARKET_CHAIN_REALM=realm_value,
            AIFACTORY_PAYMENT_TESTNET="1",
        )
        assert cfg.HubConfig().payment_testnet is True


def test_the_flag_is_advertisement_only(monkeypatch):
    """Guard the premise: if `payment_testnet` ever starts GATING a payment path, forcing it
    for UNI stops being a safe honesty fix and this needs revisiting."""
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / "aimarket_hub"
    uses = []
    for f in pkg.rglob("*.py"):
        for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if "payment_testnet" in line and "def _advertised" not in line:
                uses.append(f"{f.name}:{n}")
    # config.py declares it; api.py publishes it. Anything else means it acquired behaviour.
    unexpected = [u for u in uses if not u.startswith(("config.py", "api.py"))]
    assert not unexpected, (
        f"payment_testnet is now read outside config/api ({unexpected}) — check it does not "
        "gate a payment path before keeping the UNI override"
    )
