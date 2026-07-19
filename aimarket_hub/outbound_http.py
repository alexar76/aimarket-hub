"""SSRF-safe outbound HTTP for hub invoke and federation."""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

import httpx

from aimarket_hub import crawler as _crawler


def _url_is_safe(url: str) -> bool:
    # Dynamic delegation, NOT `from crawler import _url_is_safe`: an import-time
    # binding freezes whichever function crawler held when THIS module was first
    # imported, so a monkeypatch on crawler._url_is_safe would apply only for
    # some import orders (the test_cross_hub_integration vs
    # test_invoke_host_gateway collection-order flake).
    return _crawler._url_is_safe(url)


def resolve_invoke_url(url: str) -> str:
    """Rewrite localhost invoke_url so Hub in Docker can reach host-side providers.

    Publishers still register ``http://127.0.0.1:PORT/invoke`` (dev manifest).
    When ``AIMARKET_INVOKE_HOST_GATEWAY`` is set (e.g. ``host.docker.internal``),
    outbound calls use the gateway hostname instead.
    """
    gateway = os.environ.get("AIMARKET_INVOKE_HOST_GATEWAY", "").strip()
    if not gateway or not url:
        return url
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return url
    port = parsed.port
    netloc = f"{gateway}:{port}" if port else gateway
    return urlunparse(parsed._replace(netloc=netloc))


def invoke_url_is_safe(url: str) -> bool:
    """SSRF check for provider invoke_url (allows configured host gateway)."""
    parsed = urlparse(url)
    if os.environ.get("AIMARKET_ALLOW_LOCAL_PUBLISH", "").strip() == "1":
        if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
            return url.startswith(("http://", "https://")) and "\r" not in url and "\n" not in url
    gateway = os.environ.get("AIMARKET_INVOKE_HOST_GATEWAY", "").strip()
    if gateway:
        host = parsed.hostname
        if host == gateway:
            return url.startswith(("http://", "https://")) and "\r" not in url and "\n" not in url
    return _url_is_safe(url)


def assert_url_safe(url: str) -> None:
    if not _url_is_safe(url):
        raise ValueError(f"unsafe outbound URL: {url}")


def assert_invoke_url_safe(url: str) -> None:
    if not invoke_url_is_safe(url):
        raise ValueError(f"unsafe invoke URL: {url}")


async def safe_get(url: str, *, timeout: float = 10.0) -> httpx.Response:
    assert_url_safe(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(url)


async def safe_post(
    url: str,
    *,
    json: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    invoke: bool = False,
) -> httpx.Response:
    target = resolve_invoke_url(url) if invoke else url
    if invoke:
        assert_invoke_url_safe(target)
    else:
        assert_url_safe(target)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.post(target, json=json, headers=headers or {})
