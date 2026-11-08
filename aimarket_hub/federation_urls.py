"""Cheap federation URL preflight; this is never permission to open a socket.

Metadata parsing and the crawl frontier run on the event loop. DNS validation
belongs to the outbound transport, which checks every answer and pins the
selected public address before connecting. A syntactically valid DNS name may
therefore pass here and still be refused by that transport.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

from aimarket_hub.peer_rejections import MAX_FEDERATION_URL_BYTES

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.I)
_TUNNELS = (ipaddress.ip_network("64:ff9b::/96"),
            ipaddress.ip_network("2002::/16"))


def url_has_public_syntax(url: str) -> bool:
    """Reject malformed/local literals without resolving any DNS name.

    Callers must still use the pinned safe HTTP transport for every request.
    ``socket.inet_aton`` below is a local parser, not a resolver; it catches
    integer, hex, octal and shortened IPv4 spellings accepted by libc.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        if len(url.encode("utf-8")) > MAX_FEDERATION_URL_BYTES:
            return False
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if (not host or "@" in parsed.netloc or parsed.username is not None
                or parsed.password is not None or port == 0 or "%" in host):
            return False
        host = host.lower().rstrip(".")
        if not host:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
            except (OSError, ValueError):
                address = None
        if address is not None:
            if not address.is_global or address.is_multicast:
                return False
            if address.version == 6 and any(address in net for net in _TUNNELS):
                return False
            mapped = getattr(address, "ipv4_mapped", None)
            return mapped is None or (mapped.is_global and not mapped.is_multicast)
        host = host.encode("idna").decode("ascii")
        if (len(host) > 253 or "." not in host
                or host.endswith((".localhost", ".local", ".internal"))):
            return False
        labels = host.split(".")
        # All-numeric labels that failed the numeric parser are malformed IPs,
        # not names to hand to a platform-specific resolver.
        if all(part.isdigit() for part in labels):
            return False
        return all(_LABEL.fullmatch(label) for label in labels)
    except (ValueError, UnicodeError, TypeError):
        return False
