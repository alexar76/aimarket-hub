# AI Catalog — the hub's catalogue for AI Catalog clients, and its MCP Server Card

> **Русский:** [ai-catalog.ru.md](./ai-catalog.ru.md) · **Español:** [ai-catalog.es.md](./ai-catalog.es.md) · **Français:** [ai-catalog.fr.md](./ai-catalog.fr.md) · **中文:** [ai-catalog.zh.md](./ai-catalog.zh.md)
>
> Code: [`ai_catalog.py`](../aimarket_hub/ai_catalog.py) · [`hub_routes/ai_catalog.py`](../aimarket_hub/hub_routes/ai_catalog.py) · Tests: [`tests/test_ai_catalog.py`](../tests/test_ai_catalog.py) · Signer profiles: [`did-web-eddsa-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-eddsa-v1.md), [`did-web-ml-dsa-65-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-ml-dsa-65-v1.md)

[AI Catalog](https://ai-catalog.io/) is a JSON format for listing AI artefacts of any kind: MCP
servers, A2A agents, skills, datasets, other catalogues. It is written in a Linux Foundation
working repository ([Agent-Card/ai-catalog](https://github.com/Agent-Card/ai-catalog)) by people
from the MCP and A2A projects. A catalogue is a list of entries; each entry names an artefact
(`identifier`), says what it is (`type`) and either links to it (`url`) or carries it (`data`).

MCP uses it for discovery. The Server Card extension
([SEP-2127](https://modelcontextprotocol.io/seps/2127-mcp-server-cards), Final) says that a client
which knows only a domain fetches `/.well-known/ai-catalog.json`, takes the entries typed
`application/mcp-server-card+json` and follows each `url` to a Server Card, which tells it where
to connect.

Since hub 3.21.0 every hub answers in these formats. Like the MCP, A2A and
[ARD](./ard.md) doors, it is another way into the same catalogue. Nothing about AIMarket
Protocol v2 changes: a client finds the hub and what it sells here, and calls and pays the v2 way
(`POST /ai-market/v2/invoke`).

Written against the AI Catalog specification text of 2026-11-11 (commit `e2b425f9`,
`specVersion` `1.0`) and the Server Card extension at
[`modelcontextprotocol/ext-server-card@526201bb`](https://github.com/modelcontextprotocol/ext-server-card/tree/526201bbc80231daa40ffcdecfc9da4e54e5dc93),
the snapshot SEP-2127 names. The AI Catalog text is a working draft and still changes.

## What the hub serves

| Path | What it is |
|---|---|
| `/.well-known/ai-catalog.json` | The hub's catalogue, `application/ai-catalog+json`. Cacheable for an hour, with an ETag. |
| `/` | Carries `Link: <…/.well-known/ai-catalog.json>; rel="ai-catalog"`, beside the `rel="ard"` link. |
| `/mcp/server-card` | The hub's MCP endpoint as a Server Card, `application/mcp-server-card+json`. |
| `/ai-catalog/v1/capabilities.json` | A nested catalogue: every capability the hub sells. |
| `/ai-catalog/v1/ids/<id>` | An identifier the hub gave to a peer's capability; redirects (303) to its descriptor. |
| `/ai-catalog/profiles/<name>` | The definition of a signer profile (see "Signatures"). |
| `/.well-known/did.json` | The `did:web` document the signatures name (shared with the ARD door). |

All of them answer `GET` and `HEAD`, are public and read-only.

## The catalogue

`/.well-known/ai-catalog.json` lists the hub's own artefacts:

| Entry | `type` | `url` | `digest` |
|---|---|---|---|
| the hub as an MCP server | `application/mcp-server-card+json` | `<hub>/mcp/server-card` | yes |
| the hub as an A2A agent | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | yes |
| what the hub sells | `application/ai-catalog+json` | `<hub>/ai-catalog/v1/capabilities.json` | — |
| the same, with search | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| the AIMarket discovery document | `application/json` | `<hub>/.well-known/agent-marketplace` | — |

* `host` names the operator: `displayName` is the hub's name, `identifier` its `did:web` (the host
  name when the hub has no `did:web`: served under a path, over http or on a port).
* A `digest` is `sha256:` and the lowercase hex SHA-256 of the bytes the hub serves at `url`, taken
  through the hub itself when the catalogue is built. The last three entries have none: their
  documents change as offers change (the discovery document is re-signed every minute).
* The two card entries carry no `displayName`, `description` or `version`. Each card states its own,
  and the specification asks an entry not to repeat a value that can drift from the artefact's.
* An artefact has the same `identifier` here and in the ARD manifest.

The catalogue is Level 2 (Discoverable) of the specification's three conformance levels, with
digests and with the signatures described below.

## The MCP Server Card

`/mcp/server-card` is the place the extension reserves: `<streamable-http-url>/server-card`.

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
  "name": "dev.modelmarket/aimarket-hub-mcp",
  "version": "1.3.0",
  "title": "AIMarket Hub MCP",
  "description": "Agent capability marketplace: search the catalogue, call priced and free tools, run pipelines.",
  "websiteUrl": "https://modelmarket.dev/",
  "repository": {"url": "https://github.com/alexar76/aimarket-hub", "source": "github"},
  "remotes": [{
    "type": "streamable-http",
    "url": "https://modelmarket.dev/mcp",
    "supportedProtocolVersions": ["2025-06-18", "2025-03-26", "2024-11-05"],
    "headers": [{"name": "X-API-Key", "isRequired": false, "isSecret": true, "description": "…"}]
  }]
}
```

(The shape a hub at `https://modelmarket.dev` serves; `version` and the protocol versions are the
running hub's.)

* `name` is the host in reverse and the server's name, so two operators of this software have two
  names. An operator whose server has a name in the MCP Registry sets it with
  `AIMARKET_MCP_SERVER_CARD_NAME` (for example `io.github.alexar76/aimarket-hub`); a value that is not
  `namespace/name` is ignored.
* The card lists no tools. The extension leaves tools, resources and prompts to `tools/list`,
  because a static list can differ from what a connected client is served.
* The card agrees with the live endpoint: its `version` is the `serverInfo.version` that
  `initialize` returns, and every protocol version it lists is one the endpoint negotiates. The
  tests check both against a running hub.
* The response carries the CORS and cache headers the extension asks for (`Access-Control-*`,
  `Cache-Control: public, max-age=3600`, an `ETag`, `304` on `If-None-Match`).
* The older card at `/.well-known/mcp/server-card.json` stays. It lists tools, and scanners that
  read a card instead of calling `tools/list` rely on it.

## What the hub sells

`/ai-catalog/v1/capabilities.json` has one entry per capability the hub's own search shows: the
set the HTTP, MCP, A2A and ARD doors show, and no offer the catalogue hides.

| Field | Value |
|---|---|
| `identifier` | see below |
| `displayName`, `description` | the catalogue display name and the capability's description |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `version` | the v2 `version` (`v1` unless the publisher set one) |
| `digest` | SHA-256 of the descriptor bytes at `url` |
| `tags` | `aimarket`, and for a federated row the categories its hub declares |
| `publisher` | who publishes the listing: this hub (`did:web`), or the peer's host name |
| `extensions["dev.modelmarket.aimarket"]` | the v2 terms: `protocol`, `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifests` | for a capability this hub publishes; see "Signatures" |

The `url`, `digest` and `version` are the ones the ARD registry publishes for the same capability,
so both doors pin the same descriptor bytes. `price_per_call_usd` is a decimal string here
(`"0.01"`): the document is signed over its JCS bytes, and this protocol's canonicaliser refuses
fractions, which implementations in different languages serialise differently.

**Identifiers** follow the specification's projection rules ("Catalog Projection"):

* A capability this hub lists itself is `urn:air:<this host>:<product_id>:<capability_id>`, the
  name the ARD door gives it.
* A capability copied from an admitted peer is published by that peer, and this hub has not read
  the peer's own identifier for it. The specification says an operator that is not the publisher
  "MUST use a non-`urn:air` identifier under its own control". Such an entry is named
  `<hub>/ai-catalog/v1/ids/<32 hex characters>`: stable for the same peer, product and capability,
  and resolvable (303 to the descriptor). Where it came from is in the entry: `publisher` names
  the peer's host, and the extension holds `source_hub`, `product_id` and `capability_id`.

The ARD door names the same federated capability `urn:air:<peer host>:…`. The two doors differ
here because the two specifications do.

## Signatures

The specification defines one signer profile, `did-web-v1`, which takes an ES256 (P-256) key. A hub
holds an Ed25519 key and, when it signs hybrid, an ML-DSA-65 key; it has no P-256 key. So the hub
signs in the specification's own containers under two profiles this protocol defines. The
specification provides for that: a profile defined outside it is named by a URL under its author's
control, and a reader that does not implement a profile treats the signature as not verified and
evaluates the rest independently ("Profile Selection").

| Profile | Algorithm | Key in the DID document | Where the hub uses it |
|---|---|---|---|
| `https://modelmarket.dev/ai-catalog/profiles/did-web-eddsa-v1` | `EdDSA`, Ed25519 (RFC 8037) | `#hub-ed25519`, an `OKP` JWK | a Trust Manifest on every entry the hub publishes and pins by digest; the snapshot signature of a hub without an ML-DSA-65 key |
| `https://modelmarket.dev/ai-catalog/profiles/did-web-ml-dsa-65-v1` | `ML-DSA-65` (FIPS 204; JOSE registration in RFC 9964) | `#hub-ml-dsa-65`, an `AKP` JWK | the snapshot signature of every catalogue document, when the hub signs hybrid |

Both follow `did-web-v1` in everything but the algorithm and the key type: a root `did:web` signer
without a port, a `kid` that is the signer's DID plus a fragment, a key that the DID document lists
under `assertionMethod` and that the signer controls, no key material in the JWS header. Each URL
serves its definition.

* **An entry's Trust Manifest** holds `contributor` (the hub's `did:web`), `subject` (the entry's
  `identifier`, `type`, `digest` and `version`) and `signature`. It is the publisher's endorsement
  of that release: the signer is `did:web:` plus the publisher segment of the entry's `urn:air`
  name. An entry copied from a peer has none, because this hub is not its publisher.
* **A catalogue's snapshot signature** is the top-level `signature`. It covers the whole document:
  every entry, the host, the nested manifests.
* A signature is a detached compact JWS (RFC 7515, Appendix F) over the JCS (RFC 8785) bytes of the
  signed object without its own `signature.jws`. The protected header is `{"alg", "kid", "typ"}`
  with `typ` `ai-catalog-trust-manifest+jws` or `ai-catalog+jws`.
* The same content is signed once: an entry's manifest per release, a document's snapshot per
  content. A catalogue keeps its bytes and its ETag until an offer changes.
* A hub without a root `did:web` (under a path, over http, on a port) publishes its catalogues
  unsigned.

### Post-quantum

Supporting this format does not weaken the hub's post-quantum protection, for three reasons.

1. Nothing is replaced. The v2 discovery document, manifest and receipts keep their hybrid
   signatures (Ed25519 and ML-DSA-65 together); a peer still pins both keys from them.
2. The catalogue carries the same two layers. A reader that requires the ML-DSA-65 snapshot
   signature and the entry's Ed25519 Trust Manifest is protected as long as either algorithm
   holds, which is what a hybrid signature gives. An attacker who can forge Ed25519 can forge a
   manifest, and cannot produce the ML-DSA-65 signature of the catalogue that manifest sits in.
3. There is no fallback. A profile is selected by its exact name and never inferred, so an Ed25519
   signature does not answer a reader that asks for ML-DSA-65. This is the specification's rule and
   the verifier in `ai_catalog.signature_problem` applies it.

What stays outside it: a reader that implements only `did-web-v1` sees these documents as unsigned
(Level 2). One ML-DSA-65 signature is 3,309 bytes, so it is placed once per document and not on
every entry; a nested catalogue reached by `url` is a separate document with its own signature.

### Checking a catalogue

A reduced check, in Python with `cryptography`, `rfc8785` and (for a hub that signs hybrid)
`dilithium-py`. It verifies the digest of the first entry, that entry's Trust Manifest and the
catalogue's snapshot signature. A full verifier also applies the rules of the profile definitions
(the key is under `assertionMethod` and controlled by the signer, the times, the header members);
`aimarket_hub.ai_catalog.signature_problem` and `endorsement_problem` do.

```python
import base64, hashlib, json, urllib.request
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def get(url): return urllib.request.urlopen(url).read()

hub, did = "https://modelmarket.dev", "did:web:modelmarket.dev"
catalog = json.loads(get(hub + "/.well-known/ai-catalog.json"))
keys = {m["id"]: m["publicKeyJwk"] for m in json.loads(get(hub + "/.well-known/did.json"))["verificationMethod"]}

def signing_input(obj):
    """The protected header of a signed object, and the bytes its signature covers."""
    header = obj["signature"]["jws"].split(".")[0]
    payload = {**obj, "signature": {k: v for k, v in obj["signature"].items() if k != "jws"}}
    payload.pop("additionalSignatures", None)
    return json.loads(unb64(header)), (header + "." + b64(rfc8785.dumps(payload))).encode()

# 1. An entry: the digest pins the card, the Trust Manifest binds the release, Ed25519 signs it.
entry = catalog["entries"][0]
assert entry["digest"] == "sha256:" + hashlib.sha256(get(entry["url"])).hexdigest()
manifest = entry["trustManifests"][0]
assert manifest["contributor"] == manifest["signature"]["signer"] == did
assert manifest["subject"] == {k: entry[k] for k in ("identifier", "type", "digest", "version") if k in entry}
header, message = signing_input(manifest)
assert header == {"alg": "EdDSA", "kid": did + "#hub-ed25519", "typ": "ai-catalog-trust-manifest+jws"}
Ed25519PublicKey.from_public_bytes(unb64(keys[header["kid"]]["x"])).verify(
    unb64(manifest["signature"]["jws"].split(".")[2]), message)

# 2. The catalogue: one signature over the whole document, ML-DSA-65 when the hub signs hybrid.
header, message = signing_input(catalog)
assert header["typ"] == "ai-catalog+jws" and header["kid"].startswith(did + "#")
assert catalog["signature"]["signer"] == catalog["host"]["identifier"] == did
signature = unb64(catalog["signature"]["jws"].split(".")[2])
if header["alg"] == "ML-DSA-65":
    from dilithium_py.ml_dsa import ML_DSA_65
    assert ML_DSA_65.verify(unb64(keys[header["kid"]]["pub"]), message, signature)
else:
    Ed25519PublicKey.from_public_bytes(unb64(keys[header["kid"]]["x"])).verify(signature, message)
```

The tests run this block as it is printed here against a hub of each kind
(`test_the_documented_check_runs_against_a_hub`).

## How this sits beside the AIMarket format

The hub's own format is not replaced and not forked. An AI Catalog document this hub serves is a
valid AI Catalog document for any client, with the AIMarket additions in the two places the
specification opens for additions:

* the `extensions` map of an entry, under the reverse-DNS key `dev.modelmarket.aimarket`, for the
  terms a buyer needs (price, access mode, where to invoke);
* signatures under profile names of our own, in the specification's Signature objects.

A client that knows only AI Catalog reads the entries and ignores both, as the specification
tells it to ("Unrecognized Members", "Profile Selection"). A client that knows AIMarket reads the
same document and gets the terms and the signatures. The signed v2 discovery document, with its
key pinning and its list of admitted peers, is listed as one of the entries.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AIMARKET_MCP_SERVER_CARD_NAME` | unset | The Server Card's `name`, when the operator has one in the MCP Registry. |
| `AIMARKET_AI_CATALOG_CACHE_S` | `60` | Seconds the capabilities catalogue is kept before it is rebuilt from the hub's search. |
| `AIMARKET_PQC` | see `signing.py` | Whether the hub signs hybrid; decides the snapshot signature's profile. |

## Limits

* Not Level 3 (Trusted): that level names `did-web-v1`, and the hub has no ES256 key. A reader
  that implements only `did-web-v1` treats the hub's signatures as not verified.
* The AI Catalog specification is a working draft. Its trust section changed several times in
  October and November 2026 (ADR 0025 to 0029). The door follows the text of 2026-11-11.
* No paging: the capabilities catalogue is one document. The specification defines none; the ARD
  registry at `/ard/v1` pages and searches the same catalogue.
* A nested catalogue of a peer hub is not linked: a peer's capabilities are listed here as this
  hub's copies of them, under identifiers of this hub.
* The hub does not read other sites' catalogues. HISTOR does, to find Server Cards
  ([`histor/histor/cards.py`](https://github.com/alexar76/histor/blob/main/histor/cards.py)).
