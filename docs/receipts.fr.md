# Ce que prouve une réponse d'invoke, et comment le vérifier

[EN](receipts.md) · [RU](receipts.ru.md) · [ES](receipts.es.md) · [ZH](receipts.zh.md)

Une réponse livrée de `POST /ai-market/v2/invoke` porte jusqu'à trois objets signés. Chacun engage sur des choses différentes.

| Objet | Signé par | Engage sur |
|---|---|---|
| `receipt` | le hub qui a exécuté la capacité (le fournisseur pour un appel routé) | nonce, produit, capacité, prix, heure, succès, latence ([spécification §7.3.3](https://github.com/alexar76/aimarket-protocol/blob/main/spec.md)) ; pour un refus, aussi le motif et le remboursement (§7.3.4) |
| `receipt_binding` | le hub qui vous a répondu | l'`input` de votre requête, le `result` ou l'`output` de la réponse, votre propre nonce et le reçu auquel il se rattache (§7.3.9) |
| `provenance_receipt` | le hub qui vous a répondu (AWR/2, `eddsa-jcs-2022`) | empreintes d'entrée et de sortie, hub fournisseur, prix une fois réglé et, pour un appel routé, le reçu du fournisseur par empreinte |

## Envoyez votre propre nonce

```bash
curl -s https://modelmarket.dev/ai-market/v2/invoke \
  -H 'Content-Type: application/json' -H 'X-Client-Nonce: my-task-0001' \
  -d '{"product_id":"…","capability_id":"…","input":{…}}'
```

La valeur (8 à 128 caractères parmi `A-Z a-z 0-9 . _ -`) revient dans `X-Client-Nonce-Echo` et dans le champ signé `receipt_binding.client_nonce`. Un hub ne peut pas vous remettre une liaison signée plus tôt pour un appel identique : elle ne contiendrait pas votre nonce. `X-Agent-Nonce`, l'en-tête lu par les reçus AFTA de TensorFeed, est accepté avec le même sens. Une valeur mal formée est refusée en 400 avant tout travail ou débit.

## Vérifiez vous-même

`input_sha256` et `output_sha256` sont le SHA-256 d'un JSON à clés triées, séparateurs compacts et caractères non ASCII tels quels :

```python
import hashlib, json
h = lambda v: hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
b = answer["receipt_binding"]
assert b["input_sha256"] == h(my_input)
assert b["output_sha256"] == h(answer[b["output_member"]])
assert b["client_nonce"] == "my-task-0001"
assert b["receipt_nonce"] == answer["receipt"]["nonce"]
```

La signature est Ed25519 sur `receipt_binding:1|` suivi du SHA-256 hexadécimal des neuf champs `v, hub, receipt_nonce, receipt_signer, input_sha256, output_member, output_sha256, client_nonce, timestamp`, sérialisés de la même façon. Vérifiez-la avec la clé que le hub publie dans `/.well-known/ai-market.json` (`signer_public_key`), pas avec celle que porte la signature. L'exemple normatif est [`receipt-binding-signed.json`](https://github.com/alexar76/aimarket-protocol/blob/main/test-vectors/receipt-binding-signed.json).

L'attestation de provenance nomme ce que couvre son `outputDigest` dans le champ signé `outputDigestOf` (`result` ou `output`). Pour un appel routé, elle ajoute `gatewayReceiptDigest`, l'empreinte du `receipt` du fournisseur. Les deux se recalculent à partir de la réponse que vous détenez, selon la sérialisation nommée dans `payloadSerialization`.

## Ou demandez au hub

```bash
curl -s https://modelmarket.dev/ai-market/v2/receipts/verify -H 'Content-Type: application/json' \
  -d '{"receipt": …, "response": <la réponse entière>, "input": {…}, "client_nonce": "my-task-0001"}'
```

Le hub vérifie le reçu avec ses propres clés ou avec la clé épinglée pour un pair actif de la fédération ; le reçu du fournisseur d'une réponse routée se vérifie donc aussi, quand le reçu nomme une clé épinglée ou quand vous envoyez la réponse (ou son `receipt_binding`) avec lui ; les reçus d'un hub ne nomment pas sa clé. Il n'utilise jamais une clé tirée de la requête. Avec `response`, `input` et `client_nonce`, il vérifie aussi la liaison : signature, hub, reçu, entrée, sortie et nonce, chacun séparément. `GET /ai-market/v2/p/provenance/verify/{receipt_id}` vérifie l'attestation de provenance.

## Limites

- **Nouvelles tentatives.** Votre nonce fait partie de la requête pour `Idempotency-Key` et pour un appel avec escrow : une nouvelle tentative avec le même corps et le même nonce reçoit la première réponse avec sa liaison ; un autre nonce sous la même clé est refusé (422 `idempotency_key_reused`). Réutilisez le nonce quand vous réessayez.
- **Nombres.** Les empreintes écrivent les nombres comme Python (`21.0`, `1e-07`). Si votre analyseur JSON transforme `21.0` en `21`, ne recalculez pas sur ce qu'il resérialise : envoyez le texte du corps de la réponse tel que reçu, sans l'analyser, comme `response` à `/receipts/verify` (ci-dessus), pour que le hub l'analyse comme il l'a haché.
- **Pas encore couvert.** Une réponse escrow reprise après un encaissement interrompu revient sans liaison, et les artefacts des tâches A2A n'en portent pas. Une réponse sans `result` ni `output` n'en a pas.
- Pour un appel routé dont le reçu ne nomme aucune clé, `receipt_signer` est la clé que le hub qui répond a épinglée pour le fournisseur appelé ; s'il n'en connaît aucune, c'est `null`, pas sa propre clé.

## Pourquoi

Le 30/10/2026, nous avons vérifié avec un seul script les reçus AFTA signés de [TensorFeed](https://tensorfeed.ai) et les nôtres. Les leurs liaient la requête, la réponse et le nonce de l'appelant ; le nôtre, rien de cela. Notre attestation de provenance routée hachait aussi une réponse qui continuait de grandir ensuite, si bien qu'aucun acheteur ne pouvait la recalculer. Les deux sont corrigés ici ; la comparaison se trouve dans `docs/evidence/tensorfeed-receipts-2026-10-30/` du monorepo.
