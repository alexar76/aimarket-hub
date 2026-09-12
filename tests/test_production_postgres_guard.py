"""Production storage cannot silently fall back from PostgreSQL to SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.sandbox_trials import SandboxTrialLedger


def _config(tmp_path: Path) -> HubConfig:
    config = HubConfig()
    config.db_path = str(tmp_path / "must-not-exist.db")
    config.signing_key_path = str(tmp_path / "key")
    config.database_url = ""
    return config


def test_production_app_refuses_missing_database_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="production Hub requires DATABASE_URL=postgresql"):
        create_app(config=_config(tmp_path))
    assert not (tmp_path / "must-not-exist.db").exists()


def test_production_app_refuses_non_postgres_database_url(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    config = _config(tmp_path)
    config.database_url = "sqlite:///tmp/not-production.db"
    with pytest.raises(RuntimeError, match="SQLite fallback is disabled"):
        create_app(config=config)


def test_production_trial_ledger_refuses_sqlite_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFACTORY_PROD", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="production sandbox trial ledger requires"):
        SandboxTrialLedger(db_path=str(tmp_path / "trials.db"))
    assert not (tmp_path / "trials.db").exists()


def test_development_trial_ledger_keeps_zero_dependency_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFACTORY_PROD", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "trials.db"
    ledger = SandboxTrialLedger(db_path=str(path))
    assert ledger.quota("visitor_12345")["remaining"] >= 0
    assert path.exists()
