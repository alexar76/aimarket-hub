#!/usr/bin/env bash
# Publish hello-capability to a local Hub (UNI / dev).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_URL="${HUB_URL:-http://127.0.0.1:9083}"
PUBLISH_TOKEN="${AIMARKET_PUBLISH_TOKEN:-${AIMARKET_ADMIN_TOKEN:-}}"

if [[ -z "$PUBLISH_TOKEN" ]]; then
  echo "Set AIMARKET_PUBLISH_TOKEN or AIMARKET_ADMIN_TOKEN" >&2
  exit 1
fi

export CAPABILITY_BIND="${CAPABILITY_BIND:-127.0.0.1}"
export CAPABILITY_PORT="${CAPABILITY_PORT:-3456}"
export CAPABILITY_PUBLIC_HOST="${CAPABILITY_PUBLIC_HOST:-127.0.0.1}"

INVOKE_URL="$(python3 -c "import os; h=os.environ['CAPABILITY_PUBLIC_HOST']; p=os.environ['CAPABILITY_PORT']; print(f'http://{h}:{p}/invoke')")"

MANIFEST="$(python3 - "$DIR/capability.json" "$INVOKE_URL" <<'PY'
import json, sys
from pathlib import Path
cap = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cap["invoke_url"] = sys.argv[2]
print(json.dumps(cap, ensure_ascii=False))
PY
)"

echo "Publishing invoke_url=$INVOKE_URL → $HUB_URL"
curl -sf -X POST "$HUB_URL/ai-market/v2/supply/register" \
  -H "Authorization: Bearer $PUBLISH_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$MANIFEST" | python3 -m json.tool
