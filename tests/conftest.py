"""Hub test defaults.

Channel credit is now fail-closed unless production (on-chain verify) or an explicit dev
opt-in. The suite exercises the dev/demo path, so allow demo credit by default — individual
tests that assert the prod or fail-closed behaviour override it via monkeypatch.
"""
import os

os.environ.setdefault("AIMARKET_ALLOW_DEMO_CREDIT", "1")
