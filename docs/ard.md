# ARD — the hub's catalogue for Agent Resource Discovery clients

> **Русский:** [ard.ru.md](./ard.ru.md) · **Español:** [ard.es.md](./ard.es.md) · **Français:** [ard.fr.md](./ard.fr.md) · **中文:** [ard.zh.md](./ard.zh.md)
>
> Code: [`ard.py`](../aimarket_hub/ard.py) · [`hub_routes/ard.py`](../aimarket_hub/hub_routes/ard.py) · Tests: [`tests/test_ard.py`](../tests/test_ard.py) · Schema check: [`docs/ard-conformance/`](./ard-conformance/)

Agent Resource Discovery (ARD) is a set of Internet-Drafts submitted to the IETF DAWN
working group on 2026-10-30 by authors from Google, Ericsson and others:

* [draft-jimenez-dawn-ard-initial-contact-00](https://datatracker.ietf.org/doc/draft-jimenez-dawn-ard-initial-contact/) —
  how a client finds a manifest (`/.well-known/ard.json`, the `ard` link relation, SVCB, DNS-SD);
* [draft-bu-dawn-ard-discovery-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-discovery/) —
  the entry and manifest format, the `urn:air` names, the signed `trustManifest`, and the
  registry lookup (`POST /search`, `POST /explore`, `GET /agents`);
* [draft-bu-dawn-ard-architecture-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-architecture/) — roles and flows.

Since hub 3.18.0 every hub also answers in these formats. It is another door into the same
catalogue, like the MCP and A2A doors: nothing about AIMarket Protocol v2 changes. An ARD
client finds the hub and its capabilities; it calls and pays the v2 way
(`POST /ai-market/v2/invoke`). Payment, receipts and federation admission are not part of
ARD and stay in v2.

## What the hub serves

| Path | Method | What it is |
|---|---|---|
| `/.well-known/ard.json` | GET, HEAD | The hub's ARD manifest, `application/ard+json`. Cacheable for an hour, with an ETag. |
| `/` | HEAD, GET | Carries `Link: <…/.well-known/ard.json>; rel="ard"`, for clients that know only the origin. |
| `/.well-known/did.json` | GET, HEAD | The `did:web` document that the hub's `trustManifest` signatures name. |
| `/ard/v1` | GET | What this registry implements (not defined by ARD; a client may ignore it). |
| `/ard/v1/search` | POST | Search: text, filters, pages. |
| `/ard/v1/explore` | POST | Facet counts over what a query selects. |
| `/ard/v1/agents` | GET | Every entry, ordered by identifier. |
| `/ard/v1/capability` | GET, HEAD | The descriptor of one capability: the document an entry's `url` points at. |

All of them are public and read-only.

## The manifest

`/.well-known/ard.json` lists the hub's own resources, not its catalogue:

| Entry | `type` | `url` | Pinned by `digest` |
|---|---|---|---|
| the registry | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| the hub as an A2A agent | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | yes |
| the hub as an MCP server | `application/mcp-server-card+json` | `<hub>/.well-known/mcp/server-card.json` | yes |
| the AIMarket discovery document | `application/json` | `<hub>/.well-known/agent-marketplace` | no |

A digest is the SHA-256 of the bytes the hub serves at `url`, taken through the hub itself
when the manifest is built. The discovery document has no digest because it is re-signed
every minute (its `generated_at` is inside its own Ed25519 signature); a digest of it would be
stale within the minute.

A client that wants the catalogue follows the registry entry, as the drafts describe: a
manifest may consist of entries that point at a registry, and the registry answers lookups.

## Catalogue entries

The registry returns one entry per capability the hub sells, the same set the HTTP, MCP and
A2A doors show: fulfillable, public, on a sellable route.

| ARD field | Value |
|---|---|
| `identifier` | `urn:air:<publisher>:<product_id>:<capability_id>` |
| `displayName` | the catalogue display name |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `description` | the capability's description |
| `capabilities` | the capability's tool name (the MCP tool name) |
| `version` | the v2 `version` (`v1` unless the publisher set one) |
| `digest` | SHA-256 of the descriptor bytes at `url` |
| `tags` | for a federated row, the categories its hub declares |
| `metadata.aimarket` | `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifest` | for a capability this hub publishes; see "Signatures" |

`<publisher>` is the host that publishes the listing: this hub for a local capability, the
peer hub for a capability indexed from an admitted peer. The namespace and name are the v2
`product_id` and `capability_id`, so an identifier maps back to a v2 invoke unchanged. A
character that would break the name (`:`, `/`, `?`, `#`, spaces) is percent-encoded.

The descriptor at `url` holds what a buyer needs and nothing that moves on its own: the
schemas, the price, the invoke request (`POST <hub>/ai-market/v2/invoke` with
`product_id`, `capability_id`, `source_hub`, `input`), the MCP endpoint and tool name, and
links to the A2A card, the signed v2 catalogue and the discovery document. Measured success
rates, latency and trust scores are left out: they change every few minutes, and the entry
pins the descriptor by digest.

## Signatures

Every entry this hub publishes carries a `trustManifest` (draft-bu-dawn-ard-discovery-00 §3.5):

* `identity` is `did:web:<host>`. Its DID document at `/.well-known/did.json` lists the
  hub's Ed25519 key first: the same key its AIMarket discovery document publishes as
  `signer_public_key`. Since hub 3.21.0 a hub that signs hybrid also lists its ML-DSA-65 key
  there, for the [AI Catalog door](./ai-catalog.md); ARD `trustManifest` signatures use the
  Ed25519 key only.
* `subject` repeats the entry's `identifier`, `type`, `version` and `digest`.
* `signature` is a detached compact JWS (RFC 7515, Appendix F): `<header>..<signature>`,
  header `{"alg": "EdDSA", "kid": "did:web:<host>#hub-ed25519"}`, computed over
  `base64url(header) "." base64url(JCS(trustManifest without signature))`, with JCS per RFC 8785.

An entry the hub indexed from a peer carries no `trustManifest`: this hub is not its
publisher. The drafts treat such an entry as a discovery hint (§6.2). Its `source` in search
results is the peer hub's URL.

To check an entry (Python, with `cryptography` and `rfc8785`):

```python
import base64, hashlib, json, urllib.request
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def get(url): return urllib.request.urlopen(url).read()

hub = "https://modelmarket.dev"
entry = json.loads(get(hub + "/.well-known/ard.json"))["entries"][1]
tm = entry["trustManifest"]
assert tm["identity"] == "did:web:modelmarket.dev"
assert tm["subject"] == {k: entry[k] for k in ("identifier", "type", "version", "digest") if k in entry}
assert entry["digest"] == "sha256:" + hashlib.sha256(get(entry["url"])).hexdigest()

jwk = json.loads(get(hub + "/.well-known/did.json"))["verificationMethod"][0]["publicKeyJwk"]
header, _, signature = tm["signature"].split(".")
body = {k: v for k, v in tm.items() if k != "signature"}
Ed25519PublicKey.from_public_bytes(unb64(jwk["x"])).verify(
    unb64(signature), (header + "." + b64(rfc8785.dumps(body))).encode())
```

## Lookup

**`POST /ard/v1/search`** takes `{"query": {"text", "filter"}, "federation", "pageSize", "pageToken"}`.

* `query.text` is required, up to 2,000 characters. It runs through the hub's own catalogue
  search (lexical and a multilingual concept match over EN, RU, ES, FR and ZH), so the order
  is the order `/ai-market/v2/search` gives.
* `score` is that search's relevance scaled to 0..100. It ranks text, not quality or safety.
* `filter` keys: `type`, `tags`, `capabilities`, `publisher`, `identifier`,
  `trustManifest.identity`. A value is a string or an array of strings; values of one key are
  OR, keys are AND. Any other key is refused with 400: a filter silently ignored would return
  what the caller asked to exclude.
* `pageSize` defaults to 10, at most 100. `nextPageToken` is bound to the query that made it;
  a token sent with another query is refused.
* `federation`: the hub copies the catalogues of the peers it admits (AIMarket federation), so
  `auto` and `none` both answer from the local index, which already holds the admitted peers'
  entries. No query is forwarded. `referrals` adds an empty `referrals` list for now.

**`POST /ard/v1/explore`** takes `{"query": {…}, "resultType": {"facets": [{"field", "limit"}]}}`.
`query.text` is optional here. Facet fields: `type`, `tags`, `capabilities`, `publisher`.

**`GET /ard/v1/agents`** returns `{"entries": […], "nextPageToken": …}` ordered by
`identifier`. Filters are query parameters named like the filter keys; repeat a parameter
for OR (`?capabilities=a&capabilities=b`). `pageSize` and `pageToken` as above.

Errors are RFC 9457 problem details (`application/problem+json`) with the drafts' `error`
codes: `INVALID_ARGUMENT` (400), `NOT_FOUND` (404).

## From an entry to a call

```bash
curl -s https://modelmarket.dev/.well-known/ard.json
curl -s -X POST https://modelmarket.dev/ard/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "weather in Paris"}, "pageSize": 3}'
curl -s '<the url of a result>'      # the descriptor: its "invoke" member is the v2 request
```

The descriptor's `invoke.body` is a v2 invoke request with an empty `input`; fill it in and
pay as for any v2 call ([agent-first-call.md](./agent-first-call.md)).

## Limits

* **EdDSA, not ES256.** The drafts make ES256 mandatory to implement. This hub holds only an
  Ed25519 key, so its signatures are EdDSA (RFC 8037); a verifier that implements only ES256
  cannot check them.
* **The capability media type is not registered.** `application/vnd.aimarket.capability+json`
  is a vendor type; ARD has no type for one callable capability behind a gateway, only for a
  whole MCP server or A2A agent.
* **No semantic vector search and no referrals.** DAWN's charter leaves search and ranking out
  of scope; the hub answers with the catalogue search it already has.
* **A hub not at the root of an https origin** (a path prefix, or plain http in development)
  has no `did:web` identity: its entries are served unsigned and `/.well-known/did.json`
  answers 404.
* **`GET /agents` and its filter parameters** are not specified by the drafts beyond their
  names; the shapes above are this hub's.

## Behind a reverse proxy

A proxy in front of the hub must pass these paths to it: `/.well-known/ard.json`, `/.well-known/did.json`, `/.well-known/agent-card.json`, `/.well-known/mcp/server-card.json`, `/.well-known/agent-marketplace` and everything under `/ard/`. A card the proxy
sends somewhere else makes the manifest's digest of it wrong, and a missing `/ard/` hides the registry.

A hub under a path (`https://example.org/hub`) has its manifest at `/hub/.well-known/ard.json`. Answer the
origin's `/.well-known/ard.json` with one redirect to it (clients follow at most one), as
[production-deployment.md](./production-deployment.md) shows. Such a hub has no `did:web` identity and serves its
entries unsigned.

## Conformance

The manifest, a `GET /agents` page, a search request and a search response produced by this
code validate against the CDDL of draft-bu-dawn-ard-discovery-00 §5
([`ard-conformance/check.sh`](./ard-conformance/check.sh); deliberately broken copies fail).
The draft's CDDL needs two edits before a CDDL tool accepts it: it redefines the prelude type
`any`, and the tool used (cddl 0.12.14) cannot process the `url // data` group choice inside
the entry map, so the check uses an equivalent form with two map alternatives. Both files are
in the folder.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AIMARKET_ARD_CACHE_S` | `60` | How long the catalogue, as ARD entries, is kept between rebuilds. `0` rebuilds on every request. |

The manifest itself is built once per process: every document it pins is fixed while the
process runs.
