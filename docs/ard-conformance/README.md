# ARD schema check

Checks that what the hub serves in ARD formats ([`../ard.md`](../ard.md)) matches the CDDL of
draft-bu-dawn-ard-discovery-00 §5.

| File | What it is |
|---|---|
| `ard-draft-bu-dawn-ard-discovery-00.cddl` | §5 of the draft as published, page breaks removed. |
| `ard-checkable.cddl` | The same rules in a form the CDDL tool accepts (see below). |
| `samples/` | The manifest, a `GET /agents` page and a search response as https://modelmarket.dev served them on 2026-11-04 after the rollout, and a search request. The same responses from hub.attestedmemory.net and independentai.network/hub (unsigned) validated too. |
| `check.sh` | Validates each sample, and checks that two broken copies (an entry without `url`, a `score` of 101) are refused. |

```bash
gem install --user-install cddl      # tested with cddl 0.12.14
./check.sh
```

Two edits separate `ard-checkable.cddl` from the draft:

1. The draft defines `any`, `any-cbor`, `any-json` and `null` at the end. `any` and `null` are
   already in the CDDL prelude (RFC 8610, Appendix D), and the tool refuses the duplicate
   (`Duplicate rule definition any`). The definitions are removed; the prelude's are used.
2. The entry's `content-delivery = ( url: tstr // data: {…} )` group choice, placed inside the
   entry map next to other members, stops the tool with an internal error. The check writes the
   entry as two map alternatives, one with `url` and one with `data`, with the same members
   otherwise. With the open `* tstr => any` member, neither form can refuse an entry that has
   both `url` and `data`, which the draft's prose forbids (§3.3); tests/test_ard.py checks that.

The CDDL is a code component of an IETF Internet-Draft; the IETF Trust Legal Provisions allow
its extraction under the Revised BSD License.
