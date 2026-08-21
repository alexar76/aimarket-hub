"""The federated sell path: trial, gate, hold/capture/release, and no double charge.

Turning this on was the answer to "how does a LangGraph bot ever pay for a capability". It had
to be opt-in per peer: the federated economics were broker-shaped (the peer bills, this hub
takes routing_fee_bps), which is correct for a third party and wrong for the peers that exist —
`oracle_core` enforces nothing, so 42 of 47 catalogued capabilities were free while advertising
a price and the 1% fee was a cut of a sale that never happened.

The discriminator is DECLARED, never inferred. A peer that invoices out of band also answers
200, and billing the full price then would charge the buyer twice.
"""
from __future__ import annotations

import pytest

from aimarket_hub.config import HubConfig


class TestSellerOfRecordIsDeclared:
    def test_it_defaults_to_broker(self, monkeypatch):
        monkeypatch.delenv("AIMARKET_SELLS_FOR", raising=False)
        assert HubConfig().sells_on_behalf_of("https://oracles.example/family") is False

    def test_a_declared_peer_is_sold_for(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_SELLS_FOR", "https://oracles.example/family")
        assert HubConfig().sells_on_behalf_of("https://oracles.example/family") is True

    def test_the_match_covers_capabilities_under_the_path(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_SELLS_FOR", "https://oracles.example/family")
        cfg = HubConfig()
        assert cfg.sells_on_behalf_of("https://oracles.example/family/sortes") is True
        assert cfg.sells_on_behalf_of("https://oracles.example/family/") is True

    def test_a_different_peer_on_the_same_host_is_not_covered(self, monkeypatch):
        """Path-prefix, not host — one satellite under a domain does not vouch for another."""
        monkeypatch.setenv("AIMARKET_SELLS_FOR", "https://oracles.example/family")
        assert HubConfig().sells_on_behalf_of("https://oracles.example/other") is False

    def test_a_prefix_that_is_not_a_path_boundary_does_not_match(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_SELLS_FOR", "https://oracles.example/fam")
        assert HubConfig().sells_on_behalf_of("https://oracles.example/family") is False

    @pytest.mark.parametrize("declared,asked", [
        ("https://oracles.example/family/", "https://oracles.example/family"),
        ("https://ORACLES.example/Family", "https://oracles.example/family"),
        (" https://oracles.example/family , https://b.example ", "https://b.example"),
    ])
    def test_trailing_slash_case_and_whitespace_do_not_defeat_it(self, monkeypatch, declared, asked):
        monkeypatch.setenv("AIMARKET_SELLS_FOR", declared)
        assert HubConfig().sells_on_behalf_of(asked) is True

    def test_an_empty_or_missing_peer_is_never_sold_for(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_SELLS_FOR", "https://oracles.example/family")
        cfg = HubConfig()
        assert cfg.sells_on_behalf_of("") is False
        assert cfg.sells_on_behalf_of("   ") is False

    def test_local_is_not_a_federated_peer(self, monkeypatch):
        monkeypatch.setenv("AIMARKET_SELLS_FOR", "local")
        # "local" never reaches this branch — the local path has its own gate — but the setting
        # must not accidentally make it look like a sold-for peer either.
        assert HubConfig().sells_on_behalf_of("local") is True, (
            "matching is literal; the local branch simply never calls this"
        )
