# AI Catalog : le catalogue du hub pour les clients AI Catalog, et sa carte de serveur MCP

> **English:** [ai-catalog.md](./ai-catalog.md) · **Русский:** [ai-catalog.ru.md](./ai-catalog.ru.md) · **Español:** [ai-catalog.es.md](./ai-catalog.es.md) · **中文:** [ai-catalog.zh.md](./ai-catalog.zh.md)
>
> Code: [`ai_catalog.py`](../aimarket_hub/ai_catalog.py) · [`hub_routes/ai_catalog.py`](../aimarket_hub/hub_routes/ai_catalog.py) · Tests: [`tests/test_ai_catalog.py`](../tests/test_ai_catalog.py) · Signer profiles: [`did-web-eddsa-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-eddsa-v1.md), [`did-web-ml-dsa-65-v1`](../aimarket_hub/static/ai-catalog/profiles/did-web-ml-dsa-65-v1.md)

[AI Catalog](https://ai-catalog.io/) est un format JSON pour lister des artefacts d’IA de toute sorte :
serveurs MCP, agents A2A, skills, jeux de données, autres catalogues. Il est rédigé dans un dépôt de travail de
la Linux Foundation ([Agent-Card/ai-catalog](https://github.com/Agent-Card/ai-catalog)) par des membres des
projets MCP et A2A. Un catalogue est une liste d’entrées ; chacune nomme un artefact (`identifier`), dit ce
qu’il est (`type`) et y renvoie (`url`) ou le contient (`data`).

MCP s’en sert pour la découverte. L’extension Server Card ([SEP-2127](https://modelcontextprotocol.io/seps/2127-mcp-server-cards), statut Final) dit qu’un client qui ne connaît
qu’un domaine demande `/.well-known/ai-catalog.json`, prend les entrées de type
`application/mcp-server-card+json` et suit l’`url` de chacune jusqu’à une carte de serveur, qui lui indique où
se connecter.

Depuis le hub 3.21.0, chaque hub répond dans ces formats. Comme les portes MCP, A2A et [ARD](./ard.fr.md),
c’est une autre entrée vers le même catalogue. Rien ne change dans AIMarket Protocol v2 : le client trouve ici
le hub et ce qu’il vend, puis appelle et paie à la manière de v2 (`POST /ai-market/v2/invoke`).

Écrit d’après le texte de la spécification AI Catalog du 11/11/2026 (commit `e2b425f9`, `specVersion` `1.0`)
et l’extension Server Card à [`modelcontextprotocol/ext-server-card@526201bb`](https://github.com/modelcontextprotocol/ext-server-card/tree/526201bbc80231daa40ffcdecfc9da4e54e5dc93), l’instantané que nomme SEP-2127. Le texte d’AI Catalog est un brouillon de
travail et change encore.

## Ce que sert le hub

| Chemin | Ce que c’est |
|---|---|
| `/.well-known/ai-catalog.json` | Le catalogue du hub, `application/ai-catalog+json`. Mis en cache une heure, avec un ETag. |
| `/` | Porte `Link: <…/.well-known/ai-catalog.json>; rel="ai-catalog"`, à côté du lien `rel="ard"`. |
| `/mcp/server-card` | Le point MCP du hub sous forme de carte de serveur, `application/mcp-server-card+json`. |
| `/ai-catalog/v1/capabilities.json` | Un catalogue imbriqué : toutes les capacités que vend le hub. |
| `/ai-catalog/v1/ids/<id>` | Un identifiant que le hub a donné à la capacité d’un pair ; redirige (303) vers son descripteur. |
| `/ai-catalog/profiles/<name>` | La définition d’un profil de signature (voir « Signatures »). |
| `/.well-known/did.json` | Le document `did:web` que nomment les signatures (partagé avec la porte ARD). |

Tous répondent à `GET` et `HEAD`, sont publics et en lecture seule.

## Le catalogue

`/.well-known/ai-catalog.json` liste les artefacts propres au hub :

| Entrée | `type` | `url` | `digest` |
|---|---|---|---|
| le hub comme serveur MCP | `application/mcp-server-card+json` | `<hub>/mcp/server-card` | oui |
| le hub comme agent A2A | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | oui |
| ce que vend le hub | `application/ai-catalog+json` | `<hub>/ai-catalog/v1/capabilities.json` | — |
| la même chose, avec recherche | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| le document de discovery AIMarket | `application/json` | `<hub>/.well-known/agent-marketplace` | — |

* `host` nomme l’opérateur : `displayName` est le nom du hub, `identifier` son `did:web` (le nom d’hôte quand
  le hub n’a pas de `did:web` : servi sous un chemin, en http ou sur un port).
* Un `digest` est `sha256:` suivi du SHA-256 en hexadécimal minuscule des octets que le hub sert à `url`, pris
  à travers le hub lui-même à la construction du catalogue. Les trois dernières entrées n’en ont pas : leurs
  documents changent avec les offres (le document de discovery est signé de nouveau chaque minute).
* Les deux entrées de cartes ne portent ni `displayName`, ni `description`, ni `version`. Chaque carte énonce
  les siens, et la spécification demande qu’une entrée ne répète pas une valeur qui peut diverger de l’artefact.
* Un artefact a le même `identifier` ici et dans le manifeste ARD.

Le catalogue est de niveau 2 (Discoverable) parmi les trois niveaux de conformité, avec des digests et avec les
signatures décrites plus bas.

## La carte de serveur MCP

`/mcp/server-card` est l’emplacement que réserve l’extension : `<streamable-http-url>/server-card`.

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

(La forme que sert un hub à `https://modelmarket.dev` ; `version` et les versions de protocole sont celles du
hub en fonctionnement.)

* `name` est l’hôte à l’envers suivi du nom du serveur, si bien que deux opérateurs de ce logiciel ont deux
  noms. Un opérateur dont le serveur a un nom dans le registre MCP le fixe avec
  `AIMARKET_MCP_SERVER_CARD_NAME` (par exemple `io.github.alexar76/aimarket-hub`) ; une valeur qui n’est pas de
  la forme `namespace/name` est ignorée.
* La carte ne liste aucun outil. L’extension laisse outils, ressources et prompts à `tools/list`, parce qu’une
  liste statique peut différer de ce que reçoit un client connecté.
* La carte concorde avec le point réel : sa `version` est la `serverInfo.version` que renvoie `initialize`, et
  chaque version de protocole qu’elle liste est une version que le point négocie. Les tests vérifient les deux
  sur un hub en fonctionnement.
* La réponse porte les en-têtes CORS et de cache que demande l’extension (`Access-Control-*`,
  `Cache-Control: public, max-age=3600`, un `ETag`, `304` sur `If-None-Match`).
* L’ancienne carte à `/.well-known/mcp/server-card.json` reste. Elle liste des outils, et les scanners qui
  lisent une carte au lieu d’appeler `tools/list` en dépendent.

## Ce que vend le hub

`/ai-catalog/v1/capabilities.json` a une entrée par capacité que montre la recherche du hub : le même ensemble
que montrent les portes HTTP, MCP, A2A et ARD, et aucune offre que le catalogue masque.

| Champ | Valeur |
|---|---|
| `identifier` | voir ci-dessous |
| `displayName`, `description` | le nom dans le catalogue et la description de la capacité |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `version` | la `version` v2 (`v1` si l’éditeur n’en a pas fixé une autre) |
| `digest` | SHA-256 des octets du descripteur à `url` |
| `tags` | `aimarket` et, pour une ligne fédérée, les catégories que déclare son hub |
| `publisher` | qui publie l’offre : ce hub (`did:web`) ou le nom d’hôte du pair |
| `extensions["dev.modelmarket.aimarket"]` | les termes v2 : `protocol`, `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifests` | pour une capacité que publie ce hub ; voir « Signatures » |

`url`, `digest` et `version` sont ceux que le registre ARD publie pour la même capacité : les deux portes fixent
donc les mêmes octets du descripteur. `price_per_call_usd` est ici une chaîne décimale (`"0.01"`) : le document
est signé sur ses octets JCS, et le canoniseur de ce protocole refuse les fractions, que les implémentations
sérialisent différemment selon le langage.

**Les identifiants** suivent les règles de projection de la spécification (« Catalog Projection ») :

* Une capacité que ce hub liste lui-même s’appelle `urn:air:<cet hôte>:<product_id>:<capability_id>`, le nom
  que lui donne la porte ARD.
* Une capacité copiée d’un pair admis est publiée par ce pair, et ce hub n’a pas lu l’identifiant propre du
  pair. La spécification dit qu’un opérateur qui n’est pas l’éditeur « MUST use a non-`urn:air` identifier
  under its own control ». Une telle entrée s’appelle `<hub>/ai-catalog/v1/ids/<32 caractères hex>` : stable
  pour le même pair, le même produit et la même capacité, et résoluble (303 vers le descripteur). Sa provenance
  est dans l’entrée : `publisher` nomme l’hôte du pair et l’extension contient `source_hub`, `product_id` et
  `capability_id`.

La porte ARD nomme la même capacité fédérée `urn:air:<hôte du pair>:…`. Les deux portes diffèrent ici parce que
les deux spécifications diffèrent.

## Signatures

La spécification définit un profil de signature, `did-web-v1`, qui n’accepte qu’une clé ES256 (P-256). Un hub
détient une clé Ed25519 et, lorsqu’il signe en mode hybride, une clé ML-DSA-65 ; il n’a pas de clé P-256. Le
hub signe donc dans les conteneurs de la spécification elle-même, sous deux profils que définit ce protocole.
La spécification le prévoit : un profil défini hors d’elle est nommé par une URL sous le contrôle de son
auteur, et un lecteur qui n’implémente pas un profil traite la signature comme non vérifiée et évalue le reste
indépendamment (« Profile Selection »).

| Profil | Algorithme | Clé dans le document DID | Où le hub l’emploie |
|---|---|---|---|
| `https://modelmarket.dev/ai-catalog/profiles/did-web-eddsa-v1` | `EdDSA`, Ed25519 (RFC 8037) | `#hub-ed25519`, un JWK `OKP` | un Trust Manifest sur chaque entrée que le hub publie et fixe par digest ; la signature d’instantané d’un hub sans clé ML-DSA-65 |
| `https://modelmarket.dev/ai-catalog/profiles/did-web-ml-dsa-65-v1` | `ML-DSA-65` (FIPS 204 ; enregistrement JOSE dans la RFC 9964) | `#hub-ml-dsa-65`, un JWK `AKP` | la signature d’instantané de chaque document de catalogue, quand le hub signe en mode hybride |

Les deux suivent `did-web-v1` en tout sauf l’algorithme et le type de clé : un signataire `did:web` racine sans
port, un `kid` qui est le DID du signataire suivi d’un fragment, une clé que le document DID liste sous
`assertionMethod` et que contrôle le signataire, aucun matériel de clé dans l’en-tête JWS. Chaque URL sert sa
définition.

* **Le Trust Manifest d’une entrée** contient `contributor` (le `did:web` du hub), `subject` (l’`identifier`,
  le `type`, le `digest` et la `version` de l’entrée) et `signature`. C’est l’aval de l’éditeur pour cette
  version : le signataire est `did:web:` suivi du segment éditeur du nom `urn:air` de l’entrée. Une entrée
  copiée d’un pair n’en a pas, car ce hub n’en est pas l’éditeur.
* **La signature d’instantané d’un catalogue** est le `signature` de premier niveau. Elle couvre tout le
  document : chaque entrée, le `host`, les manifestes imbriqués.
* Une signature est un JWS compact détaché (RFC 7515, annexe F) sur les octets JCS (RFC 8785) de l’objet signé
  sans son propre `signature.jws`. L’en-tête protégé est `{"alg", "kid", "typ"}`, avec `typ`
  `ai-catalog-trust-manifest+jws` ou `ai-catalog+jws`.
* Le même contenu est signé une fois : le manifeste d’une entrée par version, l’instantané d’un document par
  contenu. Un catalogue garde ses octets et son ETag jusqu’à ce qu’une offre change.
* Un hub sans `did:web` racine (sous un chemin, en http, sur un port) publie ses catalogues non signés.

### Post-quantique

Prendre en charge ce format n’affaiblit pas la protection post-quantique du hub, pour trois raisons.

1. Rien n’est remplacé. Le document de discovery v2, le manifeste et les reçus gardent leurs signatures
   hybrides (Ed25519 et ML-DSA-65 ensemble) ; un pair épingle toujours les deux clés à partir d’eux.
2. Le catalogue porte les deux mêmes couches. Un lecteur qui exige la signature d’instantané ML-DSA-65 et le
   Trust Manifest Ed25519 de l’entrée est protégé tant que l’un des deux algorithmes tient, ce que donne une
   signature hybride. Qui sait forger Ed25519 peut forger un manifeste, et ne peut pas produire la signature
   ML-DSA-65 du catalogue où ce manifeste se trouve.
3. Il n’y a pas de repli. Un profil est choisi par son nom exact et jamais déduit : une signature Ed25519 ne
   répond pas à un lecteur qui demande ML-DSA-65. C’est la règle de la spécification, et le vérificateur de
   `ai_catalog.signature_problem` l’applique.

Ce qui reste en dehors : un lecteur qui n’implémente que `did-web-v1` voit ces documents comme non signés
(niveau 2). Une signature ML-DSA-65 fait 3 309 octets ; elle est donc placée une fois par document et non sur
chaque entrée ; un catalogue imbriqué atteint par `url` est un document distinct avec sa propre signature.

### Vérifier un catalogue

Une vérification réduite, en Python avec `cryptography`, `rfc8785` et (pour un hub qui signe en mode hybride)
`dilithium-py`. Elle vérifie le digest de la première entrée, le Trust Manifest de cette entrée et la signature
d’instantané du catalogue. Un vérificateur complet applique en plus les règles des définitions de profils (la
clé est sous `assertionMethod` et contrôlée par le signataire, les dates, les membres de l’en-tête) ;
`aimarket_hub.ai_catalog.signature_problem` et `endorsement_problem` le font.

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

Les tests exécutent ce bloc tel qu’il est imprimé dans la version anglaise, contre un hub de chaque sorte
(`test_the_documented_check_runs_against_a_hub`).

## Place par rapport au format AIMarket

Le format propre du hub n’est ni remplacé ni dédoublé. Un document AI Catalog que sert ce hub est un document
AI Catalog valide pour n’importe quel client, avec les ajouts d’AIMarket aux deux endroits que la spécification
ouvre aux ajouts :

* la table `extensions` d’une entrée, sous la clé DNS inversée `dev.modelmarket.aimarket`, pour les termes dont
  un acheteur a besoin (prix, mode d’accès, où invoquer) ;
* des signatures sous nos propres noms de profil, dans les objets Signature de la spécification.

Un client qui ne connaît qu’AI Catalog lit les entrées et ignore les deux, comme le lui demande la
spécification (« Unrecognized Members », « Profile Selection »). Un client qui connaît AIMarket lit le même
document et obtient les termes et les signatures. Le document de discovery v2 signé, avec son épinglage de clés
et sa liste de pairs admis, figure parmi les entrées.

## Réglages

| Variable | Défaut | Sens |
|---|---|---|
| `AIMARKET_MCP_SERVER_CARD_NAME` | non définie | Le `name` de la carte, quand l’opérateur en a un dans le registre MCP. |
| `AIMARKET_AI_CATALOG_CACHE_S` | `60` | Secondes pendant lesquelles le catalogue des capacités est gardé avant d’être reconstruit depuis la recherche du hub. |
| `AIMARKET_PQC` | voir `signing.py` | Si le hub signe en mode hybride ; décide du profil de la signature d’instantané. |

## Limites

* Pas de niveau 3 (Trusted) : ce niveau nomme `did-web-v1`, et le hub n’a pas de clé ES256. Un lecteur qui
  n’implémente que `did-web-v1` traite les signatures du hub comme non vérifiées.
* La spécification AI Catalog est un brouillon de travail. Sa partie sur la confiance a changé plusieurs fois en
  octobre et novembre 2026 (ADR 0025 à 0029). La porte suit le texte du 11/11/2026.
* Pas de pagination : le catalogue des capacités est un seul document. La spécification n’en définit pas ; le
  registre ARD à `/ard/v1` pagine et cherche dans le même catalogue.
* Le catalogue imbriqué d’un hub pair n’est pas lié : les capacités d’un pair figurent ici comme les copies de
  ce hub, sous des identifiants de ce hub.
* Le hub ne lit pas les catalogues d’autres sites. HISTOR le fait, pour trouver des cartes de serveur
  ([`histor/histor/cards.py`](https://github.com/alexar76/histor/blob/main/histor/cards.py)).
