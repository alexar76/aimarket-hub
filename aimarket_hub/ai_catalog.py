"""AI Catalog: this hub's artefacts as ``application/ai-catalog+json`` documents.

AI Catalog (https://ai-catalog.io/, repository Agent-Card/ai-catalog, a Linux Foundation working
repository) is a typed, nestable JSON container for discovering AI artefacts: a list of entries,
each naming an artefact by ``identifier`` and ``type`` and either linking to it (``url``) or
carrying it (``data``). MCP's Server Card extension (SEP-2127, Final) hands domain-level discovery
to it: a client fetches ``/.well-known/ai-catalog.json``, picks the entries typed
``application/mcp-server-card+json`` and follows each ``url`` to a Server Card.

This module is a door, like aimarket_hub/ard.py: nothing here sells, prices or routes. It writes
three documents (docs/ai-catalog.md):

* the catalogue at ``/.well-known/ai-catalog.json``: this hub's MCP Server Card, its A2A Agent
  Card, its ARD registry, its AIMarket discovery document, and a nested catalogue of what it sells;
* the nested catalogue at ``/ai-catalog/v1/capabilities.json``: one entry per sellable capability,
  typed ``application/vnd.aimarket.capability+json`` and pointing at the same descriptor, with the
  same digest, the ARD registry publishes (a price in it is written as a decimal string);
* the MCP Server Card at ``<streamable-http-url>/server-card``, in the schema of the Server Card
  extension. The older card at ``/.well-known/mcp/server-card.json`` (it lists tools, for scanners
  that read it instead of calling ``tools/list``) stays where it is.

Written against the specification text of 2026-11-11 (Agent-Card/ai-catalog e2b425f9, ``specVersion``
"1.0") and the Server Card extension at modelcontextprotocol/ext-server-card 526201bb.

Conformance: Level 2 (Discoverable) with artefact digests, plus signatures in the specification's
own containers under two signer profiles this protocol defines (docs/ai-catalog.md, "Signatures"):

* every entry this hub publishes carries a Trust Manifest signed with the hub's Ed25519 key
  (profile ``…/did-web-eddsa-v1``);
* every catalogue document carries a snapshot signature: ML-DSA-65 (profile
  ``…/did-web-ml-dsa-65-v1``, RFC 9964) when the hub signs hybrid, Ed25519 otherwise.

A reader that requires both has the same two layers the v2 manifest's hybrid signature gives: a
forged Ed25519 manifest does not come with a valid ML-DSA-65 snapshot of the catalogue it sits in.
Not Level 3: that level names the ``did-web-v1`` profile, which takes an ES256 (P-256) key only,
and this hub holds none. A reader that knows only ``did-web-v1`` MUST treat a signature under
another profile as not verified and go on (specification, "Profile Selection"), so these
signatures add to what it sees and take nothing away.

Identifiers follow the specification's projection rules (section "Catalog Projection"):

* what this hub publishes (its own cards, a capability it lists itself) is named
  ``urn:air:<this host>:…``, the same name the ARD door gives it;
* a capability copied from an admitted peer is published by that peer, and this hub has not seen
  the peer's own identifier for it. An operator that is not the publisher "MUST use a non-``urn:air``
  identifier under its own control", so such an entry is named by a URL on this hub, and where it
  came from goes into the entry's extension.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import ipaddress
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from aimarket_hub import ard

SPEC_VERSION = "1.0"
MEDIA_TYPE = "application/ai-catalog+json"
WELL_KNOWN_PATH = "/.well-known/ai-catalog.json"
LINK_RELATION = "ai-catalog"

BASE_PATH = "/ai-catalog/v1"
CAPABILITIES_PATH = BASE_PATH + "/capabilities.json"
IDS_PATH = BASE_PATH + "/ids"

SERVER_CARD_TYPE = ard.MCP_CARD_TYPE
SERVER_CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
#: ``GET <streamable-http-url>/server-card`` is the location the extension reserves.
MCP_PATH = "/mcp"
SERVER_CARD_PATH = MCP_PATH + "/server-card"
A2A_CARD_PATH = "/.well-known/agent-card.json"
DISCOVERY_PATH = "/.well-known/agent-marketplace"

#: The extension key under which an entry carries its AIMarket Protocol v2 terms. Keys are a
#: reverse-DNS string or a URL (specification, "Format and Key Naming").
EXTENSION_KEY = "dev.modelmarket.aimarket"

#: Signer profiles (specification, "Profile Selection": a profile defined outside it is named by
#: an absolute URI under its author's control). Each URL serves the profile's definition.
PROFILES_PATH = "/ai-catalog/profiles"
PROFILE_BASE = "https://modelmarket.dev" + PROFILES_PATH
PROFILE_EDDSA = PROFILE_BASE + "/did-web-eddsa-v1"
PROFILE_ML_DSA_65 = PROFILE_BASE + "/did-web-ml-dsa-65-v1"
#: profile → (JWS ``alg``, the JWK members that make a key this profile accepts)
PROFILE_KEYS: dict[str, tuple[str, dict[str, str]]] = {
    PROFILE_EDDSA: ("EdDSA", {"kty": "OKP", "crv": "Ed25519"}),
    PROFILE_ML_DSA_65: ("ML-DSA-65", {"kty": "AKP", "alg": "ML-DSA-65"}),
}
TYP_TRUST_MANIFEST = "ai-catalog-trust-manifest+jws"
TYP_CATALOG = "ai-catalog+jws"
CLOCK_SKEW_S = 300

SERVER_CARD_DESCRIPTION = "Agent capability marketplace: search the catalogue, call priced and free tools, run pipelines."
REPOSITORY_URL = "https://github.com/alexar76/aimarket-hub"

_NAME_RE = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
_DIGEST_RE = re.compile(r"^sha(256|384|512):[0-9a-f]+$")
_DIGEST_HEX = {"sha256": 64, "sha384": 96, "sha512": 128}


# --- the MCP Server Card ---------------------------------------------------------------

def reverse_dns(host: str) -> str:
    """``modelmarket.dev`` → ``dev.modelmarket``. An IP address or a single label stays as it is."""
    host = (host or "").strip(".").lower()
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    return ".".join(reversed([label for label in host.split(".") if label])) or "localhost"


def server_card_name(hub_url: str, server_name: str, override: str = "") -> str:
    """The card's ``name``: ``<reverse-DNS of this host>/<server name>``.

    The name is in the namespace of the host that serves the card, so two operators of the same
    software get two names. An operator whose server has a name in the MCP Registry sets it with
    ``AIMARKET_MCP_SERVER_CARD_NAME``; a value that is not ``namespace/name`` is ignored.
    """
    if override and _NAME_RE.fullmatch(override) and 3 <= len(override) <= 200:
        return override
    namespace = re.sub(r"[^a-zA-Z0-9.-]", "-", reverse_dns(ard.publisher_host(hub_url) or "localhost"))
    return f"{namespace}/{re.sub(r'[^a-zA-Z0-9._-]', '-', server_name)}"[:200]


def server_card(hub_url: str, *, hub_name: str, server_name: str, server_version: str,
                protocol_versions: tuple[str, ...] | list[str], name_override: str = "",
                key_page: str = "") -> dict[str, Any]:
    """This hub's MCP endpoint as a Server Card (SEP-2127; schema server-card.schema.json v1).

    A card says where to connect and nothing about tools: the extension leaves tools, resources
    and prompts to ``tools/list`` on purpose, because a static list can differ from what a
    connected client is served.
    """
    base = hub_url.rstrip("/")
    obtain = f" Get one at {key_page}." if key_page else ""
    return {
        "$schema": SERVER_CARD_SCHEMA,
        "name": server_card_name(base, server_name, name_override),
        "version": server_version[:255],
        "title": f"{hub_name} MCP"[:100],
        "description": SERVER_CARD_DESCRIPTION,
        "websiteUrl": base + "/",
        "repository": {"url": REPOSITORY_URL, "source": "github"},
        "remotes": [{
            "type": "streamable-http",
            "url": base + MCP_PATH,
            "supportedProtocolVersions": list(protocol_versions),
            "headers": [{
                "name": "X-API-Key",
                "description": ("Optional. Pays priced calls from a prepaid balance once the free "
                                "trial is spent; free tools need no key." + obtain),
                "isRequired": False,
                "isSecret": True,
            }],
        }],
    }


# --- catalogue objects -----------------------------------------------------------------

def host_info(hub_url: str, hub_name: str) -> dict[str, Any]:
    """Host Info: who operates this catalogue. The identifier is the hub's ``did:web`` when it
    has one (an https origin root), the host name otherwise."""
    out: dict[str, Any] = {"displayName": hub_name}
    identifier = ard.did_web(hub_url) or ard.publisher_host(hub_url)
    if identifier:
        out["identifier"] = identifier
    return out


def publisher(identifier: str, display_name: str) -> dict[str, Any] | None:
    """A Publisher object, or None when there is no identifier to state."""
    if not identifier:
        return None
    return {
        "identifier": identifier,
        "displayName": display_name or identifier,
        "identityType": "did" if identifier.startswith("did:") else "dns",
    }


def hub_publisher(hub_url: str, hub_name: str) -> dict[str, Any] | None:
    return publisher(ard.did_web(hub_url) or ard.publisher_host(hub_url), hub_name)


def catalog(hub_url: str, hub_name: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"specVersion": SPEC_VERSION, "host": host_info(hub_url, hub_name), "entries": entries}


def root_entries(hub_url: str, hub_name: str, documents: dict[str, bytes]) -> list[dict[str, Any]]:
    """The entries of ``/.well-known/ai-catalog.json``.

    ``documents`` maps a card path this hub serves to the exact bytes it serves there. A card
    absent from it is left out, never listed without a digest. The two cards carry no
    ``displayName``, ``description`` or ``version``: each card states its own, and the
    specification asks an entry not to repeat a value that can drift from the artefact's.
    """
    base = hub_url.rstrip("/")
    host = ard.publisher_host(base) or "localhost"
    by = hub_publisher(base, hub_name)

    def published(entry: dict[str, Any]) -> dict[str, Any]:
        return {**entry, "publisher": by} if by else entry

    out: list[dict[str, Any]] = []
    cards = (
        (SERVER_CARD_PATH, SERVER_CARD_TYPE, ard.urn(host, "mcp", "gateway"), ["mcp", "marketplace", "aimarket"]),
        (A2A_CARD_PATH, ard.A2A_CARD_TYPE, ard.urn(host, "agent", "a2a"), ["a2a", "marketplace", "aimarket"]),
    )
    for path, media_type, identifier, tags in cards:
        data = documents.get(path)
        if data is None:
            continue
        out.append(published({
            "identifier": identifier,
            "type": media_type,
            "url": base + path,
            "tags": tags,
            "digest": ard.digest(data),
        }))
    out.append(published({
        "identifier": ard.urn(host, "catalog", "capabilities"),
        "displayName": f"{hub_name} capabilities",
        "type": MEDIA_TYPE,
        "url": base + CAPABILITIES_PATH,
        "description": (
            "Every capability this hub sells: its own and those of the peer hubs it admits. Each "
            "entry links to a descriptor with the AIMarket Protocol v2 terms of the call."
        ),
        "tags": ["aimarket", "capabilities"],
    }))
    out.append(published({
        "identifier": ard.urn(host, "registry", "catalogue"),
        "displayName": f"{hub_name} catalogue (ARD registry)",
        "type": ard.REGISTRY_TYPE,
        "url": base + ard.REGISTRY_PATH,
        "description": (
            "The same catalogue with search: Agent Resource Discovery lookup "
            "(POST /search, POST /explore, GET /agents)."
        ),
        "tags": ["aimarket", "registry"],
    }))
    out.append(published({
        "identifier": ard.urn(host, "index", "agent-marketplace"),
        "displayName": f"{hub_name} discovery document",
        "type": ard.DISCOVERY_DOCUMENT_TYPE,
        "url": base + DISCOVERY_PATH,
        # No digest: the document carries its own Ed25519 signature and is re-signed every minute.
        "description": (
            "AIMarket discovery document: the hub's signing key, its signed catalogue and its "
            "admitted peers. Signed by the hub and re-signed every minute, so not pinned by digest."
        ),
        "tags": ["aimarket", "discovery"],
    }))
    return out


def projected_identifier(hub_url: str, source_hub: str, product_id: str, capability_id: str) -> str:
    """The identifier this hub assigns to a capability it copies from a peer: a URL under this
    hub, stable for the same (peer, product, capability) and resolvable (it redirects to the
    descriptor)."""
    key = "\x1f".join((source_hub.rstrip("/"), product_id, capability_id)).encode("utf-8")
    return f"{hub_url.rstrip('/')}{IDS_PATH}/{hashlib.sha256(key).hexdigest()[:32]}"


def _without_fractions(value: Any) -> Any:
    """``value`` with every number that is not an integer written as a decimal string
    (``0.01`` → ``"0.01"``, ``1e-05`` → ``"0.00001"``).

    These documents are signed over their JCS bytes. RFC 8785 serialises a fraction the way
    ECMAScript does, which implementations in other languages get wrong often enough that this
    protocol's canonicaliser refuses fractions outright; a price is also better read as the
    decimal the seller wrote than as the nearest binary double.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value == int(value) and abs(value) < 2 ** 53:
            return int(value)
        return format(Decimal(repr(value)), "f")
    if isinstance(value, dict):
        return {str(k): _without_fractions(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_fractions(v) for v in value]
    return str(value)


def capability_entry(src: dict[str, Any], source: str, hub_url: str, hub_name: str) -> dict[str, Any]:
    """One sellable capability as a Catalog Entry, from the entry the ARD registry publishes.

    ``src`` is that ARD entry and ``source`` the hub that publishes the listing (this hub's URL
    for a capability it lists itself). The ``url`` and ``digest`` are the ARD entry's, so both
    doors pin the same descriptor bytes. The descriptor is this protocol's own format, which a
    generic client cannot read, so the entry carries ``displayName`` and ``description`` itself.
    """
    local = source.rstrip("/") == hub_url.rstrip("/")
    terms = _without_fractions(src["metadata"]["aimarket"])
    entry: dict[str, Any] = {
        "identifier": src["identifier"],
        "displayName": src["displayName"],
        "type": src["type"],
        "url": src["url"],
        "version": src["version"],
        "digest": src["digest"],
    }
    if src.get("description"):
        entry["description"] = src["description"]
    tags = sorted({*src.get("tags", []), "aimarket"})
    entry["tags"] = tags
    if local:
        by = hub_publisher(hub_url, hub_name)
    else:
        entry["identifier"] = projected_identifier(hub_url, source, terms["product_id"], terms["capability_id"])
        peer_host = ard.publisher_host(source)
        by = publisher(peer_host, peer_host)
    if by:
        entry["publisher"] = by
    entry["extensions"] = {EXTENSION_KEY: terms}
    return entry


# --- signatures -------------------------------------------------------------------------

def signed_payload(obj: dict[str, Any]) -> dict[str, Any]:
    """What a signature covers (specification, "Signed Payload and JWS Construction"): the
    containing object with only its own ``signature.jws`` and its own ``additionalSignatures``
    left out. Everything else stays, unknown members and nested signatures included."""
    payload = {k: v for k, v in obj.items() if k != "additionalSignatures"}
    if isinstance(payload.get("signature"), dict):
        payload["signature"] = {k: v for k, v in payload["signature"].items() if k != "jws"}
    return payload


def _signing_input(header64: str, obj: dict[str, Any]) -> bytes:
    return (header64 + "." + ard.b64url(ard.jcs(signed_payload(obj)))).encode("ascii")


def sign(obj: dict[str, Any], *, did: str, profile: str, kid: str, typ: str, issued_at: str,
         signature: Callable[[bytes], bytes]) -> dict[str, Any]:
    """``obj`` with a Signature object: a detached compact JWS (RFC 7515 Appendix F) over the
    JCS bytes of ``obj`` as it stands with that Signature object minus ``jws``."""
    out = {**obj, "signature": {"signer": did, "profile": profile, "issuedAt": issued_at}}
    header64 = ard.b64url(ard.jcs({"alg": PROFILE_KEYS[profile][0], "kid": kid, "typ": typ}))
    out["signature"]["jws"] = header64 + ".." + ard.b64url(signature(_signing_input(header64, out)))
    return out


def trust_manifest(entry: dict[str, Any], *, did: str, issued_at: str,
                   signature: Callable[[bytes], bytes]) -> dict[str, Any]:
    """This hub's endorsement of the release ``entry`` names: a Trust Manifest holding its
    contributor identity, the subject and an Ed25519 signature, nothing else. The subject repeats
    the entry's identifier, type, digest and (when the entry has one) version, which is what binds
    the signature to this release ("Entry Release Coverage")."""
    subject = {k: entry[k] for k in ("identifier", "type", "digest", "version") if k in entry}
    return sign({"contributor": did, "subject": subject}, did=did, profile=PROFILE_EDDSA,
                kid=ard.key_id(did), typ=TYP_TRUST_MANIFEST, issued_at=issued_at, signature=signature)


def sign_catalog(document: dict[str, Any], *, did: str, issued_at: str, ed25519: Callable[[bytes], bytes],
                 ml_dsa_65: Callable[[bytes], bytes] | None) -> dict[str, Any]:
    """``document`` with a snapshot signature over all of it ("Catalog Snapshot Coverage"):
    ML-DSA-65 when the hub holds that key, Ed25519 otherwise."""
    if ml_dsa_65 is not None:
        return sign(document, did=did, profile=PROFILE_ML_DSA_65, kid=ard.pq_key_id(did), typ=TYP_CATALOG,
                    issued_at=issued_at, signature=ml_dsa_65)
    return sign(document, did=did, profile=PROFILE_EDDSA, kid=ard.key_id(did), typ=TYP_CATALOG,
                issued_at=issued_at, signature=ed25519)


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def signature_problem(obj: Any, did_document: Any, *, typ: str, now: datetime | None = None) -> str:
    """Why the signature on ``obj`` is not a verified endorsement under one of this protocol's two
    profiles; "" when it is one. ``did_document`` is the signer's DID document as resolved by the
    caller (``did:web``, over https, with the specification's safe-fetching rules).

    The checks are those of the profile definitions, which follow the specification's ``did-web-v1``
    profile in everything but the algorithm: the profile is selected by its exact name and never
    inferred; the protected header names the algorithm of that profile, the ``typ`` of the
    containing object and a ``kid`` that is the signer's DID plus a fragment, and carries no key
    material; the key is the one verification method with that id, controlled by the signer and
    listed under ``assertionMethod``; the signature is checked over the reconstructed payload; the
    endorsement is not accepted before ``issuedAt`` or from ``expiresAt`` on.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("signature"), dict):
        return "no Signature object"
    sig = obj["signature"]
    signer, profile, token = sig.get("signer"), sig.get("profile"), sig.get("jws")
    if not (isinstance(signer, str) and isinstance(profile, str) and isinstance(token, str)):
        return "the Signature object needs signer, profile, issuedAt and jws"
    if profile not in PROFILE_KEYS:
        return f"profile {profile[:120]!r} is not one this verifier implements"
    alg, key_members = PROFILE_KEYS[profile]
    if not re.fullmatch(r"did:web:[a-z0-9]([a-z0-9.-]*[a-z0-9])?", signer):
        return "signer is not a root did:web DID without a port"
    issued, expires = _instant(sig.get("issuedAt")), sig.get("expiresAt")
    if issued is None:
        return "issuedAt is not an RFC 3339 timestamp"
    now = now or datetime.now(timezone.utc)
    if now + timedelta(seconds=CLOCK_SKEW_S) < issued:
        return "the endorsement is not valid yet"
    if expires is not None:
        until = _instant(expires)
        if until is None or until <= issued:
            return "expiresAt is not a later RFC 3339 timestamp"
        if now >= until:
            return "the endorsement has expired"
    parts = token.split(".")
    if len(parts) != 3 or parts[1] != "" or not parts[0] or not parts[2]:
        return "jws is not a detached compact JWS"
    try:
        header = json.loads(ard.b64url_decode(parts[0]))
        raw_signature = ard.b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError):
        return "jws does not decode"
    if not isinstance(header, dict) or header.get("alg") != alg:
        return f"the protected header must name alg {alg}"
    if header.get("typ") != typ:
        return f"the protected header must name typ {typ}"
    if {"jku", "jwk", "x5u", "x5c", "b64", "crit"} & set(header):
        return "the protected header carries a member this profile refuses"
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid.startswith(signer + "#") or len(kid) == len(signer) + 1:
        return "kid is not the signer's DID followed by a fragment"
    if not isinstance(did_document, dict) or did_document.get("id") != signer:
        return "the DID document is not the signer's"
    methods = [m for m in did_document.get("verificationMethod") or [] if isinstance(m, dict)]
    asserted = did_document.get("assertionMethod") or []
    authorised = [m for m in asserted if isinstance(m, dict) and m.get("id") == kid]
    if kid in asserted:
        authorised += [m for m in methods if m.get("id") == kid]
    if len(authorised) != 1:
        return "kid does not name exactly one assertion method of the signer"
    method = authorised[0]
    jwk = method.get("publicKeyJwk")
    if method.get("controller") != signer or not isinstance(jwk, dict):
        return "the verification method is not the signer's own key"
    if any(jwk.get(name) != value for name, value in key_members.items()) or {"d", "priv"} & set(jwk):
        return f"the key is not a public {alg} key"
    message = _signing_input(parts[0], obj)
    from aimarket_hub.signing import Signer

    try:
        if profile == PROFILE_EDDSA:
            public = base64.b64encode(ard.b64url_decode(str(jwk.get("x") or ""))).decode()
            valid = Signer.verify(public, base64.b64encode(raw_signature).decode(), message.decode("ascii"))
        else:
            public = base64.b64encode(ard.b64url_decode(str(jwk.get("pub") or ""))).decode()
            valid = Signer.verify_pq(public, message, raw_signature)
    except Exception:  # noqa: BLE001 - a key that does not decode verifies nothing
        valid = False
    return "" if valid else "the signature does not verify"


def endorsement_problem(entry: Any, manifest: Any, did_document: Any, *, now: datetime | None = None) -> str:
    """Why ``manifest`` is not the publisher's verified endorsement of ``entry``'s release; "" when
    it is. On top of the signature: the signer is the manifest's contributor; the signed subject
    equals the entry's identifier, type, digest and version ("Entry Release Coverage"); and the
    signer is ``did:web:`` plus the publisher segment of the entry's ``urn:air`` name, the rule of
    the specification's publisher profile. The caller still checks the artefact's bytes against
    ``entry.digest``."""
    if not isinstance(entry, dict) or not isinstance(manifest, dict):
        return "not an entry and a Trust Manifest"
    found = signature_problem(manifest, did_document, typ=TYP_TRUST_MANIFEST, now=now)
    if found:
        return found
    signer = manifest["signature"]["signer"]
    if manifest.get("contributor") != signer:
        return "the signer is not the manifest's contributor"
    subject = manifest.get("subject")
    if not isinstance(subject, dict) or not isinstance(entry.get("digest"), str):
        return "a signed manifest needs a subject, and the entry a digest"
    for name in ("identifier", "type", "digest"):
        if subject.get(name) != entry.get(name):
            return f"subject.{name} is not the entry's"
    if ("version" in subject) != ("version" in entry) or subject.get("version") != entry.get("version"):
        return "subject.version is not the entry's"
    if signer != "did:web:" + (ard.urn_publisher(entry["identifier"]) or "\x00"):
        return "the signer is not the publisher named in the entry's identifier"
    return ""


def unsigned(document: dict[str, Any]) -> dict[str, Any]:
    """``document`` without its snapshot signature: what decides whether it must be signed again."""
    out = copy.copy(document)
    out.pop("signature", None)
    return out


# --- checking a document ---------------------------------------------------------------

def problems(document: Any, *, _depth: int = 0) -> list[str]:
    """What in ``document`` breaks the specification's structure (its CDDL schema and the rules
    the prose adds to it); empty when nothing does. Used by the tests and by the route, which
    refuses to serve a catalogue this hub built wrong rather than publish it."""
    out: list[str] = []
    if not isinstance(document, dict):
        return ["the catalogue is not a JSON object"]
    version = document.get("specVersion")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version):
        out.append("specVersion must be a \"Major.Minor\" string")
    host = document.get("host")
    if host is not None and (not isinstance(host, dict) or not isinstance(host.get("displayName"), str)
                             or not host["displayName"]):
        out.append("host needs a displayName")
    root_signature = document.get("signature")
    if root_signature is not None and not (isinstance(root_signature, dict) and all(
            isinstance(root_signature.get(k), str) and root_signature[k]
            for k in ("signer", "profile", "issuedAt", "jws"))):
        out.append("signature needs signer, profile, issuedAt and jws")
    entries = document.get("entries")
    if not isinstance(entries, list):
        return out + ["entries must be an array"]
    seen: set[tuple[str, str | None]] = set()
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            out.append(f"{where} is not an object")
            continue
        identifier, kind = entry.get("identifier"), entry.get("type")
        if not isinstance(identifier, str) or not identifier:
            out.append(f"{where} needs an identifier")
        if not isinstance(kind, str) or not kind:
            out.append(f"{where} needs a type")
        if ("url" in entry) == ("data" in entry):
            out.append(f"{where} needs exactly one of url and data")
        elif "url" in entry and (not isinstance(entry["url"], str)
                                 or urlparse(entry["url"]).scheme not in ("https", "http")):
            out.append(f"{where}.url is not an http(s) URL")
        for name in ("displayName", "description", "version", "updatedAt"):
            if name in entry and not isinstance(entry[name], str):
                out.append(f"{where}.{name} must be a string")
        if "tags" in entry and not (isinstance(entry["tags"], list)
                                    and all(isinstance(t, str) for t in entry["tags"])):
            out.append(f"{where}.tags must be an array of strings")
        digest = entry.get("digest")
        if digest is not None:
            algorithm = digest.split(":", 1)[0] if isinstance(digest, str) else ""
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest) \
                    or len(digest.split(":", 1)[1]) != _DIGEST_HEX[algorithm]:
                out.append(f"{where}.digest is not algorithm:lowercase-hex with SHA-256 or stronger")
        by = entry.get("publisher")
        if by is not None and not (isinstance(by, dict) and isinstance(by.get("identifier"), str)
                                   and by["identifier"] and isinstance(by.get("displayName"), str)
                                   and by["displayName"]):
            out.append(f"{where}.publisher needs an identifier and a displayName")
        manifests = entry.get("trustManifests")
        if manifests is not None:
            if not isinstance(manifests, list):
                out.append(f"{where}.trustManifests must be an array")
            else:
                for number, manifest in enumerate(manifests):
                    out.extend(f"{where}.trustManifests[{number}]: {p}" for p in manifest_problems(manifest, entry))
        extensions = entry.get("extensions")
        if extensions is not None:
            if not isinstance(extensions, dict):
                out.append(f"{where}.extensions must be an object")
            else:
                for key in extensions:
                    if not (re.fullmatch(r"[a-z0-9-]+(\.[A-Za-z0-9_-]+)+", key)
                            or urlparse(key).scheme in ("https", "http")):
                        out.append(f"{where}.extensions key {key!r} is neither reverse-DNS nor a URL")
        if isinstance(identifier, str):
            version_key = entry.get("version") if isinstance(entry.get("version"), str) else None
            if (identifier, version_key) in seen:
                out.append(f"{where}: identifier {identifier!r} repeats"
                           + (f" with version {version_key!r}" if version_key else ""))
            seen.add((identifier, version_key))
        if kind == MEDIA_TYPE and isinstance(entry.get("data"), dict):
            if _depth >= 3:
                out.append(f"{where}: nested deeper than 4 catalogues")
            else:
                out.extend(f"{where}.data: {p}" for p in problems(entry["data"], _depth=_depth + 1))
    return out


def manifest_problems(manifest: Any, entry: dict[str, Any]) -> list[str]:
    """What in a Trust Manifest breaks the structure the specification gives it ("Manifest
    Validity", "Subject Object", "Signature Object"). It checks shape, not the signature."""
    if not isinstance(manifest, dict):
        return ["not an object"]
    out: list[str] = []
    contributor = manifest.get("contributor")
    if not isinstance(contributor, str) or not urlparse(contributor).scheme:
        out.append("contributor must be an absolute URI")
    substance = ("signature" in manifest or "trustSchema" in manifest or bool(manifest.get("attestations"))
                 or bool(manifest.get("provenance")) or bool(manifest.get("extensions")))
    if not substance:
        out.append("an empty manifest must be omitted")
    sig = manifest.get("signature")
    if sig is not None:
        if not isinstance(sig, dict) or not all(isinstance(sig.get(k), str) and sig[k]
                                                for k in ("signer", "profile", "issuedAt", "jws")):
            out.append("signature needs signer, profile, issuedAt and jws")
        elif sig["signer"] != contributor:
            out.append("signature.signer must equal contributor")
        subject = manifest.get("subject")
        if not isinstance(subject, dict):
            out.append("a signed manifest needs a subject")
        else:
            for name in ("identifier", "type", "digest"):
                if subject.get(name) != entry.get(name) or not isinstance(subject.get(name), str):
                    out.append(f"subject.{name} must equal the entry's {name}")
            if ("version" in subject) != ("version" in entry) or subject.get("version") != entry.get("version"):
                out.append("subject.version must be present exactly when the entry's is, and equal it")
    return out


def server_card_problems(card: Any) -> list[str]:
    """What in ``card`` breaks the Server Card schema (v1): required members, the name pattern,
    the length limits, the transport names and the closed set of top-level members."""
    if not isinstance(card, dict):
        return ["the card is not a JSON object"]
    out: list[str] = []
    allowed = {"$schema", "_meta", "description", "icons", "name", "remotes", "repository", "title",
               "version", "websiteUrl"}
    out.extend(f"unknown member {key!r}" for key in sorted(set(card) - allowed))
    if card.get("$schema") != SERVER_CARD_SCHEMA:
        out.append("$schema must be the v1 Server Card schema URL")
    name = card.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not 3 <= len(name) <= 200:
        out.append("name must be namespace/name, 3 to 200 characters")
    if not isinstance(card.get("version"), str) or len(card["version"]) > 255:
        out.append("version must be a string of at most 255 characters")
    description = card.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 100:
        out.append("description must be 1 to 100 characters")
    if "title" in card and not (isinstance(card["title"], str) and 1 <= len(card["title"]) <= 100):
        out.append("title must be 1 to 100 characters")
    for index, remote in enumerate(card.get("remotes") or []):
        if not isinstance(remote, dict) or remote.get("type") not in ("sse", "streamable-http"):
            out.append(f"remotes[{index}].type must be sse or streamable-http")
            continue
        if set(remote) - {"headers", "supportedProtocolVersions", "type", "url", "variables"}:
            out.append(f"remotes[{index}] has an unknown member")
        if not isinstance(remote.get("url"), str) or not re.fullmatch(
                r"(https?://[^\s]+|\{[a-zA-Z_][a-zA-Z0-9_]*\}[^\s]*)", remote["url"]):
            out.append(f"remotes[{index}].url is not an http(s) URL or a template")
        for header in remote.get("headers") or []:
            if not isinstance(header, dict) or not isinstance(header.get("name"), str):
                out.append(f"remotes[{index}].headers: every header needs a name")
    return out
