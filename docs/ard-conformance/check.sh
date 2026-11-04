#!/usr/bin/env bash
# Validate the hub's ARD samples against the ARD CDDL (draft-bu-dawn-ard-discovery-00 §5).
# Needs the CDDL tool: `gem install --user-install cddl` (tested with cddl 0.12.14).
# Each sample must validate, and each control (a deliberately broken copy) must not.
set -uo pipefail
cd "$(dirname "$0")"
command -v cddl >/dev/null || { echo "cddl not found: gem install --user-install cddl" >&2; exit 2; }

fail=0
check() {  # check <start rule> <json file> <expected: valid|invalid>
  local rules
  rules=$(mktemp)
  sed "s/^start = ard-manifest/start = $1/" ard-checkable.cddl > "$rules"
  if cddl "$rules" validate "$2" >/dev/null 2>&1; then got=valid; else got=invalid; fi
  rm -f "$rules"
  if [[ $got == "$3" ]]; then echo "ok    $1  $2  ($got)"; else echo "FAIL  $1  $2  (expected $3, got $got)"; fail=1; fi
}

check ard-manifest samples/manifest.json valid
check ard-manifest samples/agents.json valid
check search-request samples/search-request.json valid
check search-response samples/search-response.json valid

controls=$(mktemp -d)
python3 - "$controls" <<'EOF'
import json, sys
out = sys.argv[1]
m = json.load(open("samples/manifest.json"))
m["entries"][0].pop("url")
json.dump(m, open(f"{out}/manifest-no-url.json", "w"))
s = json.load(open("samples/search-response.json"))
s["results"][0]["score"] = 101
json.dump(s, open(f"{out}/search-score-101.json", "w"))
EOF
check ard-manifest "$controls/manifest-no-url.json" invalid
check search-response "$controls/search-score-101.json" invalid
rm -rf "$controls"

# The draft's own CDDL, unchanged, does not compile (it redefines the prelude's `any`).
if cddl ard-draft-bu-dawn-ard-discovery-00.cddl validate samples/search-request.json >/dev/null 2>&1; then
  echo "note  the draft's CDDL now compiles as published"
else
  echo "note  the draft's CDDL as published does not compile (see README)"
fi
exit $fail
