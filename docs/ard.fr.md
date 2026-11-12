# ARD — le catalogue du hub pour les clients Agent Resource Discovery

> **English:** [ard.md](./ard.md) · **Русский:** [ard.ru.md](./ard.ru.md) · **Español:** [ard.es.md](./ard.es.md) · **中文:** [ard.zh.md](./ard.zh.md)
>
> Code : [`ard.py`](../aimarket_hub/ard.py) · [`hub_routes/ard.py`](../aimarket_hub/hub_routes/ard.py) · Tests : [`tests/test_ard.py`](../tests/test_ard.py) · Vérification du schéma : [`docs/ard-conformance/`](./ard-conformance/)

Agent Resource Discovery (ARD) est un ensemble d’Internet-Drafts soumis le 30/10/2026 au groupe
de travail DAWN de l’IETF par des auteurs de Google, d’Ericsson et d’autres organisations :

* [draft-jimenez-dawn-ard-initial-contact-00](https://datatracker.ietf.org/doc/draft-jimenez-dawn-ard-initial-contact/) —
  comment un client trouve un manifeste (`/.well-known/ard.json`, la relation de lien `ard`, SVCB, DNS-SD) ;
* [draft-bu-dawn-ard-discovery-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-discovery/) —
  le format d’entrée et de manifeste, les noms `urn:air`, le `trustManifest` signé et la recherche
  dans le registre (`POST /search`, `POST /explore`, `GET /agents`) ;
* [draft-bu-dawn-ard-architecture-00](https://datatracker.ietf.org/doc/draft-bu-dawn-ard-architecture/) — rôles et flux.

Depuis la version 3.18.0, chaque hub répond aussi dans ces formats. C’est une porte de plus vers
le même catalogue, comme les portes MCP et A2A ; rien ne change dans AIMarket Protocol v2. Un
client ARD trouve le hub et ses capacités ; il appelle et paie comme en v2
(`POST /ai-market/v2/invoke`). Le paiement, les reçus et l’admission dans la fédération ne font
pas partie d’ARD et restent dans v2.

## Ce que sert le hub

| Chemin | Méthode | Ce que c’est |
|---|---|---|
| `/.well-known/ard.json` | GET, HEAD | Le manifeste ARD du hub, `application/ard+json`. Mis en cache une heure, avec ETag. |
| `/` | HEAD, GET | Porte `Link: <…/.well-known/ard.json>; rel="ard"` pour les clients qui ne connaissent que l’origine. |
| `/.well-known/did.json` | GET, HEAD | Le document `did:web` que nomment les signatures `trustManifest` du hub. |
| `/ard/v1` | GET | Ce que ce registre implémente (non défini par ARD ; un client peut l’ignorer). |
| `/ard/v1/search` | POST | Recherche : texte, filtres, pages. |
| `/ard/v1/explore` | POST | Comptes de facettes sur ce qu’une requête sélectionne. |
| `/ard/v1/agents` | GET | Toutes les entrées, triées par identifiant. |
| `/ard/v1/capability` | GET, HEAD | Le descripteur d’une capacité : le document vers lequel pointe l’`url` d’une entrée. |

Tous ces chemins sont publics et en lecture seule.

## Le manifeste

`/.well-known/ard.json` liste les ressources propres du hub, pas son catalogue :

| Entrée | `type` | `url` | Fixée par `digest` |
|---|---|---|---|
| le registre | `application/ai-registry+json` | `<hub>/ard/v1` | — |
| le hub comme agent A2A | `application/a2a-agent-card+json` | `<hub>/.well-known/agent-card.json` | oui |
| le hub comme serveur MCP | `application/mcp-server-card+json` | `<hub>/.well-known/mcp/server-card.json` | oui |
| le document de discovery AIMarket | `application/json` | `<hub>/.well-known/agent-marketplace` | non |

Un `digest` est le SHA-256 des octets que le hub sert à `url`, obtenus en passant par le hub
lui-même quand le manifeste est construit. Le document de discovery n’a pas de digest parce
qu’il est re-signé chaque minute (son `generated_at` est dans sa propre signature Ed25519) ; un
digest de ce document serait périmé en moins d’une minute.

Un client qui veut le catalogue suit l’entrée du registre, comme le décrivent les brouillons : un
manifeste peut se composer d’entrées qui pointent vers un registre, et le registre répond aux
recherches.

## Entrées du catalogue

Le registre renvoie une entrée par capacité vendue par le hub, le même ensemble que montrent les
portes HTTP, MCP et A2A : exécutables, publiques, sur une route vendable.

| Champ ARD | Valeur |
|---|---|
| `identifier` | `urn:air:<publisher>:<product_id>:<capability_id>` |
| `displayName` | le nom affiché dans le catalogue |
| `type` | `application/vnd.aimarket.capability+json` |
| `url` | `<hub>/ard/v1/capability?product_id=…&capability_id=…&source_hub=…` |
| `description` | la description de la capacité |
| `capabilities` | le nom d’outil de la capacité (le nom de l’outil MCP) |
| `version` | le `version` de v2 (`v1` sauf si l’éditeur en a fixé un autre) |
| `digest` | SHA-256 des octets du descripteur à `url` |
| `tags` | pour une ligne fédérée, les catégories que déclare son hub |
| `metadata.aimarket` | `product_id`, `capability_id`, `source_hub`, `price_per_call_usd`, `access_mode`, `invoke_url` |
| `trustManifest` | pour une capacité publiée par ce hub ; voir « Signatures » |

`<publisher>` est l’hôte qui publie l’offre : ce hub pour une capacité locale, le hub pair pour
une capacité indexée chez un pair admis. L’espace de noms et le nom sont le `product_id` et le
`capability_id` de v2, si bien qu’un identifiant redevient un appel v2 sans changement. Un
caractère qui casserait le nom (`:`, `/`, `?`, `#`, espaces) est encodé avec `%`.

Le descripteur à `url` contient ce dont un acheteur a besoin et rien qui bouge de lui-même : les
schémas, le prix, la requête d’appel (`POST <hub>/ai-market/v2/invoke` avec `product_id`,
`capability_id`, `source_hub`, `input`), le point d’accès et le nom de l’outil MCP, et des liens
vers la carte A2A, le catalogue v2 signé et le document de discovery. Le taux de réussite mesuré,
la latence et les scores de confiance sont exclus : ils changent toutes les quelques minutes, et
l’entrée fixe le descripteur par son digest.

## Signatures

Chaque entrée publiée par ce hub porte un `trustManifest` (draft-bu-dawn-ard-discovery-00 §3.5) :

* `identity` vaut `did:web:<host>`. Son document DID à `/.well-known/did.json` liste d’abord la
  clé Ed25519 du hub : la même que son document de discovery AIMarket publie comme
  `signer_public_key`. Depuis le hub 3.21.0, un hub qui signe en mode hybride y liste aussi sa clé
  ML-DSA-65, pour la [porte AI Catalog](./ai-catalog.fr.md) ; les signatures `trustManifest` d’ARD
  n’utilisent que la clé Ed25519.
* `subject` reprend l’`identifier`, le `type`, le `version` et le `digest` de l’entrée.
* `signature` est un JWS compact détaché (RFC 7515, annexe F) : `<header>..<signature>`, en-tête
  `{"alg": "EdDSA", "kid": "did:web:<host>#hub-ed25519"}`, calculé sur
  `base64url(header) "." base64url(JCS(trustManifest sans signature))`, avec JCS selon la RFC 8785.

Une entrée que le hub a indexée chez un pair ne porte pas de `trustManifest` : ce hub n’en est
pas l’éditeur. Les brouillons traitent une telle entrée comme un indice de découverte (§6.2). Son
`source` dans les résultats de recherche est l’URL du hub pair.

Pour vérifier une entrée (Python, avec `cryptography` et `rfc8785`) :

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

## Recherche

**`POST /ard/v1/search`** prend `{"query": {"text", "filter"}, "federation", "pageSize", "pageToken"}`.

* `query.text` est obligatoire, 2 000 caractères au plus. Il passe par la recherche de catalogue
  du hub lui-même (lexicale, plus une correspondance multilingue de concepts en EN, RU, ES, FR et
  ZH), donc l’ordre est celui que donne `/ai-market/v2/search`.
* `score` est la pertinence de cette recherche ramenée à 0..100. Il classe du texte, pas la
  qualité ni la sécurité.
* Clés de `filter` : `type`, `tags`, `capabilities`, `publisher`, `identifier`,
  `trustManifest.identity`. Une valeur est une chaîne ou un tableau de chaînes ; les valeurs d’une
  clé se combinent par OU, les clés par ET. Toute autre clé est refusée avec 400 : un filtre
  ignoré en silence renverrait ce que le client a demandé d’exclure.
* `pageSize` vaut 10 par défaut, 100 au plus. `nextPageToken` est lié à la requête qui l’a produit ;
  un jeton envoyé avec une autre requête est refusé.
* `federation` : le hub copie les catalogues des pairs qu’il admet (fédération AIMarket), donc
  `auto` et `none` répondent depuis l’index local, qui contient déjà les entrées des pairs admis.
  Aucune requête n’est relayée. `referrals` ajoute pour l’instant une liste `referrals` vide.

**`POST /ard/v1/explore`** prend `{"query": {…}, "resultType": {"facets": [{"field", "limit"}]}}`.
Ici `query.text` est facultatif. Champs de facette : `type`, `tags`, `capabilities`, `publisher`.

**`GET /ard/v1/agents`** renvoie `{"entries": […], "nextPageToken": …}` trié par `identifier`.
Les filtres sont des paramètres de requête nommés comme les clés de filtre ; répéter un paramètre
signifie OU (`?capabilities=a&capabilities=b`). `pageSize` et `pageToken` comme plus haut.

Les erreurs sont des problem details de la RFC 9457 (`application/problem+json`) avec les codes
`error` des brouillons : `INVALID_ARGUMENT` (400), `NOT_FOUND` (404).

## D’une entrée à un appel

```bash
curl -s https://modelmarket.dev/.well-known/ard.json
curl -s -X POST https://modelmarket.dev/ard/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query": {"text": "weather in Paris"}, "pageSize": 3}'
curl -s '<l’url d’un résultat>'      # le descripteur : son membre "invoke" est la requête v2
```

Le `invoke.body` du descripteur est une requête d’appel v2 avec un `input` vide ; complétez-le et
payez comme pour tout appel v2 ([agent-first-call.fr.md](./agent-first-call.fr.md)).

## Limites

* **EdDSA, pas ES256.** Les brouillons rendent ES256 obligatoire à implémenter. Ce hub ne détient
  qu’une clé Ed25519, donc ses signatures sont EdDSA (RFC 8037) ; un vérificateur qui n’implémente
  qu’ES256 ne peut pas les vérifier.
* **Le type de média de la capacité n’est pas enregistré.** `application/vnd.aimarket.capability+json`
  est un type de l’arbre fournisseur ; ARD n’a pas de type pour une seule capacité appelable
  derrière une passerelle, seulement pour un serveur MCP ou un agent A2A entier.
* **Pas de recherche vectorielle sémantique ni de referrals.** La charte de DAWN laisse la
  recherche et le classement hors de son périmètre ; le hub répond avec la recherche de catalogue
  qu’il a déjà.
* **Un hub qui n’est pas à la racine d’une origine https** (préfixe de chemin, ou http en
  développement) n’a pas d’identité `did:web` : ses entrées sont servies sans signature et
  `/.well-known/did.json` répond 404.
* **`GET /agents` et ses paramètres de filtre** : les brouillons n’en donnent que le nom ; les
  formes ci-dessus sont celles de ce hub.

## Derrière un proxy inverse

Un proxy placé devant le hub doit lui transmettre ces chemins : `/.well-known/ard.json`, `/.well-known/did.json`, `/.well-known/agent-card.json`, `/.well-known/mcp/server-card.json`, `/.well-known/agent-marketplace` et tout ce qui est sous `/ard/`. Une carte
que le proxy envoie ailleurs rend faux son digest dans le manifeste, et sans `/ard/` le registre est invisible.

Un hub sous un chemin (`https://example.org/hub`) a son manifeste à `/hub/.well-known/ard.json`. Répondez au
`/.well-known/ard.json` de l’origine par une redirection vers lui (les clients en suivent une au plus), comme le
montre [production-deployment.fr.md](./production-deployment.fr.md). Un tel hub n’a pas d’identité `did:web` et sert
ses entrées sans signature.

## Conformité

Le manifeste, une page de `GET /agents`, une requête et une réponse de recherche produits par ce
code sont validés par le CDDL du draft-bu-dawn-ard-discovery-00 §5
([`ard-conformance/check.sh`](./ard-conformance/check.sh)) ; des copies cassées exprès sont
refusées. Le CDDL du brouillon demande deux retouches avant qu’un outil CDDL l’accepte : il
redéfinit le type `any` du prélude, et l’outil utilisé (cddl 0.12.14) ne traite pas le choix de
groupe `url // data` placé dans la table de l’entrée, donc la vérification utilise une forme
équivalente à deux variantes de table. Les deux fichiers sont dans le dossier.

## Réglages

| Variable | Défaut | Signification |
|---|---|---|
| `AIMARKET_ARD_CACHE_S` | `60` | Combien de secondes le catalogue, sous forme d’entrées ARD, est gardé entre deux reconstructions. `0` le reconstruit à chaque requête. |

Le manifeste lui-même est construit une fois par processus : tout ce qu’il fixe ne change pas tant
que le processus tourne.
