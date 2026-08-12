"""SSRF-safe outbound HTTP for hub invoke and federation."""

from __future__ import annotations

import os
from ipaddress import ip_address
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


# httpx honours the request-level ``sni_hostname`` extension (driving both TLS SNI
# and certificate hostname validation) from 0.23 onward; only then can https be
# pinned to an IP without breaking cert validation. Older httpx → https left unpinned.
_HTTPS_PIN_OK = tuple(int(p) for p in (httpx.__version__.split(".") + ["0", "0"])[:2]) >= (0, 23)


def _pin_target(url: str, *, allow_hosts: set[str] | None = None) -> tuple[str, dict[str, str], dict[str, str]]:
    """Close the SSRF DNS-rebinding window: resolve the host to a validated IP ONCE
    and connect to that pinned IP, so the connection cannot be re-pointed to a
    private/metadata IP between validation and connect.

    Returns (target_url, extra_headers, extensions). The target host is rewritten to
    the pinned IP with the original ``Host`` header; for https a ``sni_hostname``
    extension keeps TLS SNI + certificate validation on the real hostname. Raises
    ValueError if any resolved address is on the blocklist (SSRF caught).

    Returns the url unchanged (no pinning) for literal-IP hosts, explicitly-allowed
    hosts (localhost / configured invoke gateway), non-http(s) schemes, https on an
    httpx too old to pin safely, or a resolution error — so it never regresses a
    legitimate path.
    """
    import socket

    parsed = urlparse(url)
    host = parsed.hostname
    if not host or parsed.scheme not in ("http", "https"):
        return url, {}, {}
    if allow_hosts and host in allow_hosts:
        return url, {}, {}  # intentionally-allowed localhost / host-gateway
    try:
        ip_address(host)
        return url, {}, {}  # already a literal IP (validated upstream)
    except ValueError:
        pass
    if parsed.scheme == "https" and not _HTTPS_PIN_OK:
        return url, {}, {}  # cannot pin https safely on this httpx version
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return url, {}, {}  # let the normal request path surface the resolution error
    safe_ip: str | None = None
    for _family, _, _, _, sockaddr in infos:
        try:
            addr = ip_address(sockaddr[0])
        except ValueError:
            continue
        blocked = any(addr in net for net in _crawler._BLOCKED_NETS)
        mapped = getattr(addr, "ipv4_mapped", None)
        if not blocked and mapped is not None:
            blocked = any(mapped in net for net in _crawler._BLOCKED_NETS)
        if blocked:
            raise ValueError(f"host {host} resolves to a blocked network ({sockaddr[0]})")
        if safe_ip is None:
            safe_ip = sockaddr[0]
    if safe_ip is None:
        return url, {}, {}
    netloc = f"[{safe_ip}]" if ":" in safe_ip else safe_ip
    if parsed.port:
        netloc += f":{parsed.port}"
    headers = {"Host": parsed.netloc}
    extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
    return urlunparse(parsed._replace(netloc=netloc)), headers, extensions


def _invoke_allow_hosts() -> set[str]:
    """Hosts the invoke path intentionally allows and must NOT pin/block: loopback
    when local publish is enabled, plus any configured host gateway."""
    allow: set[str] = set()
    if os.environ.get("AIMARKET_ALLOW_LOCAL_PUBLISH", "").strip() == "1":
        allow |= {"127.0.0.1", "localhost", "::1"}
    gateway = os.environ.get("AIMARKET_INVOKE_HOST_GATEWAY", "").strip()
    if gateway:
        allow.add(gateway)
    return allow


async def safe_get(url: str, *, timeout: float = 10.0) -> httpx.Response:
    assert_url_safe(url)
    target, pin_headers, ext = _pin_target(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(target, headers=pin_headers or None, extensions=ext or None)


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
    out_headers = dict(headers or {})
    # Pin the validated IP (http + https) to defeat DNS rebinding. On the invoke path
    # the intentionally-allowed localhost / host-gateway targets are exempted so dev
    # and Docker gateways keep working; every other target (federation + external
    # providers) is pinned.
    target, pin_headers, ext = _pin_target(
        target, allow_hosts=_invoke_allow_hosts() if invoke else None
    )
    out_headers.update(pin_headers)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.post(target, json=json, headers=out_headers, extensions=ext or None)
