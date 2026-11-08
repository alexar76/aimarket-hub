"""Persistent, operator-owned rejections shared by all federation entry points.

The policy sits beside the database so replacing a package does not forget a rejection.
It names peer URLs and provider invoke resources, never a whole public host. Ban
comparison folds common HTTP routing aliases; it is not a universal URL identity rule.
"""
from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
import socket
import string
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)
_MAX_POLICY_BYTES = 1_048_576
MAX_FEDERATION_URL_BYTES = 4096
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_Policy = tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...],
                tuple[tuple[str, str, str], ...], bool]
_DISCOVERY_SUFFIXES = (
    "/.well-known/agent-marketplace",
    "/.well-known/ai-market.json",
    "/.well-known/aimarket/hub.json",
    "/.well-known/ai-market/hub.json",
    "/ai-market/manifest",
    "/ai-market/v2/manifest",
)


def _canonical_path(path: str) -> str:
    # HTTP clients remove literal dot segments; servers commonly decode unreserved
    # escapes before routing. Reserved escapes such as %2F remain distinct.
    # HTTP servers/proxies commonly merge literal repeated slashes. Encoded %2F
    # remains reserved: this policy never turns an encoded delimiter into a path.
    path = re.sub(r"/{2,}", "/", path)
    path = re.sub(r"%([0-9a-fA-F]{2})", lambda m: (
        chr(int(m[1], 16)) if chr(int(m[1], 16)) in _UNRESERVED else "%" + m[1].upper()
    ), path)
    parts = path.split("/")
    out: list[str] = []
    for index, part in enumerate(parts):
        if part == ".":
            if index == len(parts) - 1:
                out.append("")
        elif part == "..":
            if out and not (len(out) == 1 and out[0] == ""):
                out.pop()
            if index == len(parts) - 1:
                out.append("")
        else:
            out.append(part)
    return "/".join(out).rstrip("/")


def _canonical_host(host: str) -> str:
    """Match the network host without DNS or any network access.

    The socket resolver accepts historical numeric IPv4 forms (integer, hex,
    octal and shortened dotted forms). ``inet_aton`` parses them locally.
    """
    host = host.lower().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        pass
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except (OSError, ValueError):
            return host
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped if mapped is not None else address)


def normalize_url(url: str, *, discovery: bool = False) -> str:
    value = str(url or "").strip().rstrip("/")
    # Some historical query-bearing announcements had discovery appended after the
    # query. Strip only known discovery suffixes, before parsing, to cover those too.
    if discovery:
        for suffix in _DISCOVERY_SUFFIXES:
            if value.endswith(suffix):
                value = value[:-len(suffix)].rstrip("/")
                break
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value
        host = _canonical_host(parsed.hostname or "")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)):
            host += f":{port}"
        # Credentials can change HTTP authentication, never the network endpoint's
        # identity. Adding userinfo must not resurrect an operator-rejected host.
        path = _canonical_path(parsed.path)
        if discovery:
            for suffix in _DISCOVERY_SUFFIXES:
                if path.endswith(suffix):
                    path = path[:-len(suffix)].rstrip("/")
                    break
        normalized = urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))
        if discovery:
            for suffix in _DISCOVERY_SUFFIXES:
                if normalized.endswith(suffix):
                    return normalized[:-len(suffix)].rstrip("/")
        return normalized
    except (TypeError, ValueError):
        return value


def _matches(value: str, rule: str, *, discovery: bool = False) -> bool:
    # A no-query tombstone names the resource for all query variants. A tombstone
    # that explicitly contains a query (e.g. a redirect test) remains scoped to it.
    if "?" not in rule:
        value = value.partition("?")[0]
    return value == rule or bool(discovery and (
        value.startswith(rule + "/.well-known/")
        or value.startswith(rule + "/ai-market/manifest/")
        or value.startswith(rule + "/ai-market/v2/manifest/")
    ))


def policy_path(db_path: str | Path | None = None) -> Path:
    explicit = os.environ.get("AIMARKET_PEER_REJECTIONS_FILE", "").strip()
    if explicit:
        return Path(explicit)
    database = db_path or os.environ.get("AIMARKET_DB_PATH", "data/hub.db")
    return Path(database).parent / "peer-rejections.json"


@lru_cache(maxsize=32)
def _read_policy(path: str, stamp: tuple[int, int, int]) -> _Policy:
    try:
        with open(path, "rb") as stream:
            raw = stream.read(_MAX_POLICY_BYTES + 1)
        if len(raw) > _MAX_POLICY_BYTES:
            raise ValueError("policy exceeds size limit")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("policy requires version 1")
        lists = []
        for key in ("peer_urls", "invoke_urls"):
            entries = document.get(key)
            if not isinstance(entries, list) or any(
                not isinstance(item, str) or not item.strip().startswith(("http://", "https://"))
                for item in entries
            ):
                raise ValueError(f"policy requires a URL list: {key}")
            lists.append(tuple(normalize_url(item, discovery=key == "peer_urls") for item in entries))
        keys = document.get("capability_keys", [])
        if not isinstance(keys, list) or any(
            not isinstance(key, dict)
            or not isinstance(key.get("product_id"), str) or not key["product_id"].strip()
            or not isinstance(key.get("capability_id"), str) or not key["capability_id"].strip()
            for key in keys
        ):
            raise ValueError("capability_keys requires exact product_id/capability_id pairs")
        pairs = tuple((key["product_id"], key["capability_id"]) for key in keys)
        scoped = document.get("capability_sources", [])
        if not isinstance(scoped, list) or any(
            not isinstance(key, dict)
            or any(not isinstance(key.get(name), str) or not key[name].strip()
                   for name in ("product_id", "capability_id", "source_hub"))
            or not (key["source_hub"] == "local"
                    or key["source_hub"].startswith(("http://", "https://")))
            for key in scoped
        ):
            raise ValueError("capability_sources requires exact product/capability/source entries")
        sources = tuple((key["product_id"], key["capability_id"],
                         normalize_url(key["source_hub"], discovery=True)) for key in scoped)
        return lists[0], lists[1], pairs, sources, False
    except (OSError, ValueError, TypeError):
        logger.error("Peer rejection policy cannot be read or validated; federation admission is blocked")
        return (), (), (), (), True


def _policy(db_path: str | Path | None) -> _Policy:
    path = policy_path(db_path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        # Existing deployments have no file. An explicitly selected missing file is a
        # broken operator configuration and must not silently erase all rejections.
        return (), (), (), (), bool(os.environ.get("AIMARKET_PEER_REJECTIONS_FILE", "").strip())
    except OSError:
        return (), (), (), (), True
    return _read_policy(str(path.resolve()), (stat.st_mtime_ns, stat.st_size, stat.st_ino))


def peer_is_rejected(url: str, *, db_path: str | Path | None = None) -> bool:
    # Gossip is not a Pydantic AnnounceRequest. Apply a finite URL budget here
    # too, before persistence or DNS, so a bounded row count also bounds storage.
    try:
        if len(str(url or "").encode("utf-8")) > MAX_FEDERATION_URL_BYTES:
            return True
    except UnicodeError:
        return True
    peers, _invokes, _keys, _sources, invalid = _policy(db_path)
    if invalid:
        return True
    value = normalize_url(url, discovery=True)
    return any(_matches(value, peer, discovery=True) for peer in peers)


def invoke_is_rejected(url: str, *, db_path: str | Path | None = None) -> bool:
    _peers, invokes, _keys, _sources, invalid = _policy(db_path)
    return invalid or any(_matches(normalize_url(url), rule) for rule in invokes)


def capability_is_rejected(product_id: str, capability_id: str, *, source_hub: str = "",
                           db_path: str | Path | None = None) -> bool:
    """Block a proven synthetic listing even when a relay hides its invoke address."""
    _peers, _invokes, keys, sources, invalid = _policy(db_path)
    return invalid or (product_id, capability_id) in keys or bool(source_hub and any(
        product_id == product and capability_id == capability
        and _matches(normalize_url(source_hub, discovery=True), origin, discovery=True)
        for product, capability, origin in sources
    ))
