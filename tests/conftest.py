"""Hub test defaults.

Channel credit is now fail-closed unless production (on-chain verify) or an explicit dev
opt-in. The suite exercises the dev/demo path, so allow demo credit by default — individual
tests that assert the prod or fail-closed behaviour override it via monkeypatch.
"""
import os

import pytest

os.environ.setdefault("AIMARKET_ALLOW_DEMO_CREDIT", "1")


@pytest.fixture(autouse=True)
def isolated_deposit_claims(tmp_path_factory, monkeypatch):
    """Give every test its own single-use deposit registry.

    The registry is deliberately GLOBAL in production — one record both settlement doors
    (this ledger and the factory's v1 channel path) write before crediting, which is the
    only thing that can make one on-chain deposit exclusive across two independent
    ledgers. That global namespace is exactly what tests must not share: the suite reuses
    fixed hashes like ``0xdeposit1`` across cases, so without isolation one test's claim
    rejects the next test's open and the failure lands far from its cause.

    Tests that assert registry behaviour itself (an unavailable directory, a claim already
    taken by the other door) override this with their own ``monkeypatch.setenv``, which
    wins because it runs after the fixture.
    """
    monkeypatch.setenv(
        "AIMARKET_DEPOSIT_CLAIMS_DIR", str(tmp_path_factory.mktemp("deposit_claims"))
    )
