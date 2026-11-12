# AI Catalog signer profile `did-web-ml-dsa-65-v1`

Profile identifier (compare exactly, case-sensitively):

    https://modelmarket.dev/ai-catalog/profiles/did-web-ml-dsa-65-v1

Version 1, 2026-11-12. Defined by the AIMarket Protocol (https://github.com/alexar76/aimarket-protocol).
Licence of this text: CC BY 4.0.

This is a signer profile for the AI Catalog specification (https://ai-catalog.io/), section
"Signature Object". That specification defines one profile, `did-web-v1`, which authenticates a
root `did:web` signer with an ES256 key. This profile is `did-web-v1` with one change: the
algorithm and the key type. It exists so that an AI Catalog can carry a post-quantum signature: AIMarket hubs sign their protocol documents with Ed25519 and ML-DSA-65 together, and this profile carries the second of the two into the catalogue.

Everything the AI Catalog specification requires of any signature applies unchanged: the Signature
object and its members, the signed payload (the containing object without its own `signature.jws`
and its own `additionalSignatures`, canonicalised with JCS, RFC 8785), the detached compact JWS
(RFC 7515, Appendix F), the protected `typ` (`ai-catalog-trust-manifest+jws` on a Trust Manifest,
`ai-catalog+jws` on a catalogue), signature acceptance and time, and safe fetching. A reader that
does not implement this profile treats a signature that names it as not verified and evaluates
other signatures independently, as the specification says ("Profile Selection").

## 1. Signer identity

`signature.signer` MUST be a root `did:web` DID: `did:web:` followed by a lowercase ASCII domain
name, with no port, no path, no IP address and no trailing dot.

## 2. Algorithm and JWS header

The JWS `alg` MUST be `ML-DSA-65`, as registered for JOSE by RFC 9964: ML-DSA-65 of FIPS 204, Algorithm 2 (pure ML-DSA, not HashML-DSA), with the empty context string. A verifier applying this profile MUST reject any other `alg`. Hedged and deterministic signing both produce valid signatures.

The protected header MUST contain `alg`, `typ` and `kid`. `kid` MUST be the exact `signer` value
followed by `#` and a non-empty fragment, with no path or query. The header MUST NOT contain `jku`,
`jwk`, `x5u`, `x5c`, `b64` or `crit`: a verifier takes key material only from the signer's DID
document.

## 3. Key discovery and authorisation

The verifier resolves `signer` with the `did:web` method, under the specification's safe-fetching
rules. Verification fails unless:

- the DID document's `id` equals `signer` exactly;
- exactly one verification method has an `id` equal to `kid`, and the document's `assertionMethod`
  authorises it (by reference or by containing it);
- that method's `controller` equals `signer`;
- that method has a `publicKeyJwk` with `kty` equal to `AKP`, `alg` equal to `ML-DSA-65` and the FIPS 204 public key (1,952 bytes), base64url-encoded, in `pub` (RFC 9964), and no private key material.

A key listed only under another relationship (`authentication`, `keyAgreement`) MUST NOT be
accepted. Verification is against the DID document as it is at verification time; this profile
defines no way to check a signature against a key that has since been removed.

## 4. Verification

Reconstruct the payload, build the JWS signing input from the stored protected header and the
base64url of the payload's JCS bytes, and verify the signature over it with the selected key.
Use FIPS 204 Algorithm 3 with the empty context string.

## 5. What a valid signature establishes

That the party controlling the web origin named in `signer` signed these contents with that key.

**On a Trust Manifest**, the signature is the publisher's endorsement of an entry's release only
when, in addition:

- `signature.signer` equals the manifest's `contributor`;
- the signed `subject` equals the entry's `identifier`, `type` and `digest`, and `version` is
  present in both with the same value or absent from both (the specification's "Entry Release
  Coverage"); the reader then checks the artefact's bytes against `entry.digest`;
- the entry's identifier is `urn:air:{publisher}:…` and `signer` equals `did:web:` followed by that
  `{publisher}` segment, compared exactly. This is the rule of the specification's `did:web`
  Publisher Profile: it proves control of the namespace, not reputation or safety.

**On a catalogue**, the signature covers the whole document as served (the specification's
"Catalog Snapshot Coverage"). It is the operator's signature only when `signer` equals the
catalogue's `host.identifier` and, for a catalogue fetched from `/.well-known/ai-catalog.json` or
from a URL that catalogue links to, equals `did:web:` followed by the host it was fetched from.
A catalogue republished elsewhere keeps a valid signature of its original operator and gains no
authority from the place it is republished.

## 6. Using the two AIMarket profiles together

AIMarket hubs sign each catalogue document with this profile when they hold an ML-DSA-65 key, and sign each entry they publish with `did-web-eddsa-v1` (one Trust Manifest per entry). One ML-DSA-65 signature is 3,309 bytes, so it is placed once on the document, which it covers whole, and not on every entry. A reader that wants protection against an attacker with a large quantum computer requires this snapshot signature on the catalogue it fetched and reads the entries of that snapshot only. A reader that requires both this signature and the entry's Ed25519 Trust Manifest is protected as long as either algorithm holds. A reader MUST NOT accept one of the two profiles in place of the other when its policy asks for both. A hub that has published an ML-DSA-65 key and then serves a catalogue without this signature has either lost the key or been replaced: a reader that saw the key before SHOULD refuse the unsigned catalogue.

## 7. Security considerations

A signature under this profile says who signed, not that the claims are true. A `did:web` identity
is as strong as the control of its domain and of the server answering `/.well-known/did.json`: a
party that takes over either can publish a new key and sign under the same name. The snapshot signature covers one document. A nested catalogue reached by `url` is a separate document with its own signature. The `digest` of a nested catalogue is not published, because it changes whenever an offer changes.
