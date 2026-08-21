"""SSRF DNS-rebinding pin for outbound_http (_pin_target)."""

import socket

import pytest

from aimarket_hub import outbound_http as oh


def _fake_gai(ip):
    def gai(host, port, *a, **k):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    return gai


def test_pin_http_public_rewrites_to_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_gai("93.184.216.34"))
    target, headers, ext = oh._pin_target("http://example.com/x")
    assert target == "http://93.184.216.34/x"
    assert headers == {"Host": "example.com"}
    assert ext == {}  # no SNI for plain http


def test_pin_https_adds_sni(monkeypatch):
    if not oh._HTTPS_PIN_OK:
        pytest.skip("httpx too old to pin https safely")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_gai("93.184.216.34"))
    target, headers, ext = oh._pin_target("https://example.com/x")
    assert target == "https://93.184.216.34/x"
    assert headers == {"Host": "example.com"}
    assert ext == {"sni_hostname": "example.com"}  # cert validated against hostname


def test_pin_blocks_rebind_to_private(monkeypatch):
    # Hostname that (post-validation) resolves to a private IP must be rejected.
    monkeypatch.setattr(socket, "getaddrinfo", _fake_gai("10.1.2.3"))
    with pytest.raises(ValueError):
        oh._pin_target("http://rebind.example/latest/meta-data/")


def test_pin_allow_hosts_bypasses(monkeypatch):
    # A configured gateway / localhost is exempt (not pinned, not blocked).
    monkeypatch.setattr(socket, "getaddrinfo", _fake_gai("127.0.0.1"))
    target, headers, ext = oh._pin_target(
        "http://host.docker.internal:9000/invoke",
        allow_hosts={"host.docker.internal"},
    )
    assert target == "http://host.docker.internal:9000/invoke"
    assert headers == {} and ext == {}


def test_pin_literal_ip_unchanged():
    target, headers, ext = oh._pin_target("http://93.184.216.34:8080/x")
    assert target == "http://93.184.216.34:8080/x"
    assert headers == {} and ext == {}


def test_pin_preserves_port(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_gai("93.184.216.34"))
    target, _, _ = oh._pin_target("http://example.com:8443/x")
    assert target == "http://93.184.216.34:8443/x"


def test_invoke_allow_hosts_env(monkeypatch):
    monkeypatch.delenv("AIMARKET_ALLOW_LOCAL_PUBLISH", raising=False)
    monkeypatch.setenv("AIMARKET_INVOKE_HOST_GATEWAY", "host.docker.internal")
    assert oh._invoke_allow_hosts() == {"host.docker.internal"}
    monkeypatch.setenv("AIMARKET_ALLOW_LOCAL_PUBLISH", "1")
    assert oh._invoke_allow_hosts() == {"host.docker.internal", "127.0.0.1", "localhost", "::1"}
