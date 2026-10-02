#!/usr/bin/env bash
# Publish weather.witness@v1 on a hub. The OPERATOR runs this, once the provider is serving
# behind its public invoke_url — see README.md, "Going live". Nothing calls it on its own.
#
#   AIMARKET_ADMIN_TOKEN=... ./publish.sh                    # modelmarket.dev, public invoke_url
#   HUB_URL=https://uni.modelmarket.dev \
#   WITNESS_INVOKE_URL=http://172.17.0.1:9475/invoke \
#   WITNESS_HEALTH_URL=http://172.17.0.1:9475/healthz ./publish.sh   # the UNI rehearsal
#
# The provider_pubkey is read from the RUNNING provider's /healthz, not from a key file:
# a manifest naming a key the live process does not sign with makes every invoke answer 502
# "invalid provider response signature", and this is the one place both can be compared.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_URL="${HUB_URL:-https://modelmarket.dev}"
HUB_URL="${HUB_URL%/}"
INVOKE_URL="${WITNESS_INVOKE_URL:-${HUB_URL}/providers/weather-witness/invoke}"
HEALTH_URL="${WITNESS_HEALTH_URL:-${INVOKE_URL%/invoke}/healthz}"
TOKEN="${AIMARKET_ADMIN_TOKEN:-${AIMARKET_PUBLISH_TOKEN:-}}"

if [[ -z "$TOKEN" ]]; then
  echo "Set AIMARKET_ADMIN_TOKEN (or a publisher token for aicom-weather-witness)" >&2
  exit 1
fi

echo "Checking the provider at $HEALTH_URL"
HEALTH="$(curl -sS --fail --max-time 10 "$HEALTH_URL")" || {
  echo "The provider does not answer at $HEALTH_URL — start it first (README: systemd unit, nginx)." >&2
  exit 1
}

MANIFEST="$(python3 - "$DIR/capability.json" "$INVOKE_URL" "$HUB_URL" "$HEALTH" <<'PY'
import json, os, re, sys
from pathlib import Path

cap = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
health = json.loads(sys.argv[4])
if health.get("capability_id") != cap["capability_id"]:
    sys.exit(f"the provider at that URL serves {health.get('capability_id')!r}, not {cap['capability_id']}")
if health.get("hub", "").rstrip("/") != sys.argv[3]:
    # Its job tokens would come from one hub while it verifies them against another: every
    # call would be refused as "issued by another hub".
    sys.exit(f"the provider verifies tokens from {health.get('hub')!r}, not {sys.argv[3]} — "
             "fix WITNESS_HUB_URL in its environment")
payout = os.environ.get("WITNESS_PAYOUT_ADDRESS", "").strip()
if payout:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", payout) or int(payout[2:], 16) == 0:
        sys.exit("WITNESS_PAYOUT_ADDRESS must be a nonzero EVM address")
    cap["payout_address"] = payout
cap["invoke_url"] = sys.argv[2]
cap["provider_pubkey"] = health["provider_pubkey"]
print(json.dumps(cap, ensure_ascii=False))
PY
)"

echo "Publishing weather.witness@v1 (invoke_url=$INVOKE_URL) on $HUB_URL"
# The bearer goes to curl on a config stream, never on its command line, where any user on
# the host could read it from the process list.
curl -sS --fail-with-body -X POST "$HUB_URL/ai-market/v2/supply/register" \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN") \
  -H "Content-Type: application/json" \
  --data-binary @- <<<"$MANIFEST" | python3 -m json.tool
