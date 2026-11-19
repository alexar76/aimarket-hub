"""AID v2 endpoint proof on /mcp (aimarket_hub/aid_pka.py).

Checked with a verifier written here from the text of AID v2.1.1 Appendix B, sharing no code with
the signer: it derives the keyid from ``k``, parses ``Signature-Input`` itself, rebuilds the RFC 9421
signature base from its own request and verifies Ed25519 with ``cryptography``.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from aimarket_hub import aid_pka
from aimarket_hub.signing import Signer

HUB = "https://hub.example.com"
DOMAIN = "hub.example.com"


# ── an independent verifier (AID Appendix B.6 / B.7) ───────


def _thumbprint(k: str) -> str:
    raw = ('{"crv":"Ed25519","kty":"OKP","x":"' + k + '"}').encode()
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()


def verify(response, *, k: str, nonce: str, method: str, target_uri: str, domain: str | None,
           now: int | None = None) -> bool:
    """True when ``response`` is a valid aid-pka-v2 proof for this request; False otherwise.
    Returns whether the proof is domain-bound through ``verify.bound``."""
    verify.bound = False
    sig_input = response.headers.get("signature-input", "")
    sig = response.headers.get("signature", "")
    if response.headers.get("cache-control") != "no-store":
        return False
    m = re.fullmatch(r'aid-pka=(\((?:[^()]*)\)(?:;[a-z]+=(?:"[^"]*"|\d+))*)', sig_input)
    s = re.fullmatch(r"aid-pka=:([A-Za-z0-9+/=]+):", sig)
    if not m or not s:
        return False
    params_value = m.group(1)
    inner = re.match(r"\(([^()]*)\)", params_value).group(1).split(" ")
    params = dict(re.findall(r';([a-z]+)=("[^"]*"|\d+)', params_value))
    base4 = ['"@method";req', '"@target-uri";req', '"@authority";req', '"@status"']
    bound4 = ['"@method";req', '"@target-uri";req', '"@authority";req', '"aid-domain";req', '"@status"']
    if inner == bound4:
        if domain is None:
            return False  # a client that sent no AID-Domain rejects a bound proof (fail closed)
        verify.bound = True
    elif inner != base4:
        return False
    if params.get("tag") != '"aid-pka-v2"' or params.get("alg", "").lower() != '"ed25519"':
        return False
    if params.get("keyid") != f'"{_thumbprint(k)}"' or params.get("nonce") != f'"{nonce}"':
        return False
    created, expires = int(params["created"]), int(params["expires"])
    if not (expires > created and expires - created <= 300):
        return False
    if now is not None and not (created - 5 <= now <= expires):
        return False
    authority = re.match(r"https?://([^/?#]+)", target_uri).group(1).lower()
    lines = [f'"@method";req: {method}', f'"@target-uri";req: {target_uri}', f'"@authority";req: {authority}']
    if verify.bound:
        lines.append(f'"aid-domain";req: {domain}')
    lines += [f'"@status": {response.status_code}', f'"@signature-params": {params_value}']
    key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(k + "=" * (-len(k) % 4)))
    try:
        key.verify(base64.b64decode(s.group(1)), "\n".join(lines).encode())
    except InvalidSignature:
        return False
    return True


def _nonce() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _ask(nonce: str, *, bound: bool, kid: str = "x") -> dict[str, str]:
    covered = '"@method";req "@target-uri";req "@authority";req ' + ('"aid-domain";req ' if bound else "") + '"@status"'
    value = (f'aid-pka=({covered});created;expires;keyid="{kid}";alg="ed25519";'
             f'nonce="{nonce}";tag="aid-pka-v2"')
    return {"Accept-Signature": value, "Cache-Control": "no-store"}


# ── a small app with the middleware ────────────────────────


@pytest.fixture
def signer(tmp_path):
    return Signer(tmp_path / "hub.key")


def _app(signer, *, domains=frozenset({DOMAIN}), hub_url=HUB, clock=None):
    app = FastAPI()

    @app.get("/mcp")
    async def info():
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "max-age=60"})

    @app.post("/mcp")
    async def rpc():
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {}})

    @app.get("/other")
    async def other():
        return {"ok": True}

    kwargs = {"clock": clock} if clock else {}
    app.add_middleware(aid_pka.AidPkaSigner, key=aid_pka.private_key(signer), hub_url=hub_url,
                       domains=domains, **kwargs)
    return TestClient(app, base_url="http://127.0.0.1:8080")


# ── the key ─────────────────────────────────────────────────


def test_key_is_derived_stable_and_not_the_hub_key(signer, tmp_path):
    k = aid_pka.public_k(aid_pka.private_key(signer))
    assert len(base64.urlsafe_b64decode(k + "=")) == 32 and "=" not in k
    assert k == aid_pka.public_k(aid_pka.private_key(Signer(tmp_path / "hub.key")))  # same seed, same key
    hub_raw = base64.b64decode(signer.public_key_b64)
    assert base64.urlsafe_b64decode(k + "=") != hub_raw
    assert aid_pka.keyid(k) == _thumbprint(k)
    other = Signer(tmp_path / "other.key")
    assert aid_pka.public_k(aid_pka.private_key(other)) != k


def test_keyid_matches_rfc7638_example_shape():
    # RFC 8037 Appendix A.1 public key; its RFC 7638 thumbprint is given in RFC 8037 A.3.
    k = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
    assert aid_pka.keyid(k) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


def test_txt_record_shape(signer):
    k = aid_pka.public_k(aid_pka.private_key(signer))
    rec = aid_pka.txt_record("https://modelmarket.dev/mcp", k, desc="AIMarket hub: search and call agent tools")
    assert rec == ("v=aid2;u=https://modelmarket.dev/mcp;p=mcp;a=none;"
                   f"s=AIMarket hub: search and call agent tools;k={k}")
    assert len(rec.encode()) < 255


# ── the proof ───────────────────────────────────────────────


def test_unbound_proof_verifies(signer):
    client = _app(signer)
    k = aid_pka.public_k(aid_pka.private_key(signer))
    n = _nonce()
    r = client.get("/mcp", headers=_ask(n, bound=False))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=None)
    assert verify.bound is False
    assert r.headers["cache-control"] == "no-store"  # replaces the route's max-age


def test_domain_bound_proof_verifies(signer):
    client = _app(signer)
    k = aid_pka.public_k(aid_pka.private_key(signer))
    n = _nonce()
    r = client.get("/mcp", headers={**_ask(n, bound=True), "AID-Domain": DOMAIN})
    assert verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=DOMAIN)
    assert verify.bound is True
    # The same proof does not verify for another domain or another nonce.
    assert not verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain="evil.example")
    assert not verify(r, k=k, nonce=_nonce(), method="GET", target_uri=f"{HUB}/mcp", domain=DOMAIN)
    # Nor for the external URL of somebody else, nor under another key.
    assert not verify(r, k=k, nonce=n, method="GET", target_uri="https://evil.example/mcp", domain=DOMAIN)
    other_k = aid_pka.public_k(aid_pka.private_key(Signer(signer.key_path.parent / "x.key")))
    assert not verify(r, k=other_k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=DOMAIN)


def test_post_and_query_and_status_are_signed(signer):
    client = _app(signer)
    k = aid_pka.public_k(aid_pka.private_key(signer))
    n = _nonce()
    r = client.post("/mcp?probe=1&x=a%20b", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=_ask(n, bound=False))
    assert r.status_code == 200
    assert verify(r, k=k, nonce=n, method="POST", target_uri=f"{HUB}/mcp?probe=1&x=a%20b", domain=None)
    assert not verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp?probe=1&x=a%20b", domain=None)
    n2 = _nonce()
    r2 = client.delete("/mcp", headers=_ask(n2, bound=False))  # 405 from the app: still a proof (B.4)
    assert r2.status_code == 405
    assert verify(r2, k=k, nonce=n2, method="DELETE", target_uri=f"{HUB}/mcp", domain=None)


def test_unserved_domain_gets_403_without_signature(signer):
    client = _app(signer)
    for bad in ("evil.example", "Hub.Example.com", "hub.example.com.", "hub.example.com:443"):
        r = client.get("/mcp", headers={**_ask(_nonce(), bound=True), "AID-Domain": bad})
        assert r.status_code == 403, bad
        assert "signature-input" not in r.headers and "signature" not in r.headers
        assert r.headers["cache-control"] == "no-store"


def test_extra_domains_from_the_environment(signer, monkeypatch):
    monkeypatch.setenv("AIMARKET_AID_DOMAINS", "agents.example.org, Other.example ")
    app = FastAPI()

    @app.get("/mcp")
    async def info():
        return {"status": "ok"}

    k = aid_pka.install(app, signer=signer, hub_url=HUB)
    client = TestClient(app)
    for domain in (DOMAIN, "agents.example.org", "other.example"):
        n = _nonce()
        r = client.get("/mcp", headers={**_ask(n, bound=True), "AID-Domain": domain})
        assert verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=domain), domain


def test_switched_off(signer, monkeypatch):
    monkeypatch.setenv("AIMARKET_AID_PKA", "0")
    app = FastAPI()
    assert aid_pka.install(app, signer=signer, hub_url=HUB) is None


@pytest.mark.parametrize("headers", [
    {},
    {"Accept-Signature": 'sig1=("@method");nonce="abcdefghijklmnopqrstuvwxyz";tag="aid-pka-v2"'},
    {"Accept-Signature": 'aid-pka=("@method";req);nonce="abcdefghijklmnopqrstuvwxyz";tag="other"'},
    {"Accept-Signature": 'aid-pka=("@method";req);tag="aid-pka-v2"'},
    {"Accept-Signature": 'aid-pka=("@method";req);nonce="short";tag="aid-pka-v2"'},
    {"Accept-Signature": 'aid-pka=("@method";req);nonce="abcdefghijklmnop\\"qrstuvwxyz";tag="aid-pka-v2"'},
    {"Accept-Signature": 'aid-pka=("@method";req);nonce=abcdefghijklmnopqrstuvwxyz;tag="aid-pka-v2"'},
])
def test_no_or_unusable_request_means_no_signature(signer, headers):
    client = _app(signer)
    r = client.get("/mcp", headers=headers)
    assert r.status_code == 200
    assert "signature" not in r.headers and "signature-input" not in r.headers
    assert r.headers["cache-control"] == "max-age=60"  # untouched


def test_other_paths_are_untouched(signer):
    client = _app(signer)
    r = client.get("/other", headers=_ask(_nonce(), bound=False))
    assert "signature" not in r.headers


def test_dictionary_with_other_members_and_split_fields(signer):
    client = _app(signer)
    k = aid_pka.public_k(aid_pka.private_key(signer))
    n = _nonce()
    value = (f'sig-b=("@status");keyid="other", aid-pka=("@method";req "@target-uri";req "@authority";req '
             f'"@status");created;expires;keyid="k";alg="ed25519";nonce="{n}";tag="aid-pka-v2"')
    r = client.get("/mcp", headers={"Accept-Signature": value})
    assert verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=None)


def test_freshness_window(signer):
    client = _app(signer, clock=lambda: 1_800_000_000)
    k = aid_pka.public_k(aid_pka.private_key(signer))
    n = _nonce()
    r = client.get("/mcp", headers=_ask(n, bound=False))
    sig_input = r.headers["signature-input"]
    assert ";created=1800000000;expires=1800000060;" in sig_input
    assert verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=None, now=1_800_000_030)
    assert not verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=None, now=1_800_000_061)


def test_public_origin():
    assert aid_pka.public_origin("https://ModelMarket.dev/") == ("https", "modelmarket.dev", "")
    assert aid_pka.public_origin("https://example.net:443/hub/") == ("https", "example.net", "/hub")
    assert aid_pka.public_origin("http://127.0.0.1:8080") == ("http", "127.0.0.1:8080", "")


def test_base_path_hub(signer):
    client = _app(signer, hub_url="https://example.net/hub", domains=frozenset({"example.net"}))
    k = aid_pka.public_k(aid_pka.private_key(signer))
    n = _nonce()
    r = client.get("/mcp", headers=_ask(n, bound=False))
    assert verify(r, k=k, nonce=n, method="GET", target_uri="https://example.net/hub/mcp", domain=None)


# ── the real hub app ────────────────────────────────────────


def test_hub_app_signs_mcp(monkeypatch, tmp_path):
    from aimarket_hub.api import create_app
    from aimarket_hub.config import HubConfig

    monkeypatch.setenv("AIMARKET_AUTO_CRAWL", "0")
    monkeypatch.setenv("AIMARKET_FEDERATION_ASSAY", "0")
    config = HubConfig()
    config.hub_url = HUB
    config.hub_name = "Example Hub"
    config.db_path = str(tmp_path / "hub.db")
    config.signing_key_path = str(tmp_path / "hub.key")
    app = create_app(config)
    k = aid_pka.public_k(aid_pka.private_key(Signer(config.signing_key_path)))
    with TestClient(app) as client:
        n = _nonce()
        r = client.get("/mcp", headers={**_ask(n, bound=True), "AID-Domain": DOMAIN})
        assert r.status_code == 200 and r.json().get("status") == "ok"
        assert verify(r, k=k, nonce=n, method="GET", target_uri=f"{HUB}/mcp", domain=DOMAIN)
        assert verify.bound is True
        n2 = _nonce()
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        r2 = client.post("/mcp", json=init, headers={**_ask(n2, bound=False), "Accept": "application/json, text/event-stream"})
        assert r2.status_code == 200
        assert verify(r2, k=k, nonce=n2, method="POST", target_uri=f"{HUB}/mcp", domain=None)
        plain = client.get("/mcp")
        assert "signature" not in plain.headers
