"""Every hub response carries the baseline browser headers, whatever nginx is in front.

The apex served /start (key minting, USDC top-up) with no header at all: frameable by any
site, no nosniff, no HSTS. /widget/ is the one surface made to be embedded.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aimarket_hub.api import create_app
from aimarket_hub.config import HubConfig
from aimarket_hub.database import HubDatabase
from aimarket_hub.signing import Signer


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmp:
        config = HubConfig()
        config.db_path = str(Path(tmp) / "t.db")
        config.signing_key_path = str(Path(tmp) / "k")
        app = create_app(config=config, db=HubDatabase(Path(tmp) / "t.db"), signer=Signer(Path(tmp) / "k"))
        with TestClient(app) as c:
            yield c


@pytest.mark.parametrize("path", ["/start", "/.well-known/ai-market.json", "/no-such-page"])
def test_pages_refuse_framing_and_sniffing(client, path):
    r = client.get(path)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_widget_stays_embeddable(client):
    r = client.get("/widget/live-stream.html")
    assert r.status_code == 200
    assert "X-Frame-Options" not in r.headers
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_hsts_only_behind_https(client):
    assert "Strict-Transport-Security" not in client.get("/start").headers
    r = client.get("/start", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"
