"""Hub capital pricing for Pulse Terminal (ACEX Phase 2)."""

from __future__ import annotations

import sys
from pathlib import Path


# acex/ lives at monorepo root (sibling of aimarket-hub) or at /app/acex in Hub image.
def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[1]):
        if (base / "acex").is_dir():
            return base
    return here.parents[2]


_REPO_ROOT = _repo_root()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from acex.integrations.pricing import build_pricing_snapshot
from aimarket_hub.database import HubDatabase


def hub_capital_pricing(
    db: HubDatabase,
    *,
    chain: str = "any",
    listing_id: str | None = None,
    limit: int = 50,
) -> dict:
    caps = db.list_capabilities(limit=1000)

    # Overlay live ACEX Agent IPO state (CapShares + distributed revenue) by product.
    overlay: dict = {}
    audit_overlay: dict = {}
    try:
        from aimarket_hub import acex_ipo

        for st in acex_ipo.list_listings(limit=500):
            if not st.get("error"):
                overlay[st["listing_id"]] = st
    except Exception:
        overlay = {}

    try:
        from aimarket_hub import acex_audit

        for st in acex_audit.list_audit_states(limit=500):
            if not st.get("error"):
                audit_overlay[st["listing_id"]] = st
    except Exception:
        audit_overlay = {}

    return build_pricing_snapshot(
        caps,
        chain=chain,
        listing_id=listing_id,
        limit=limit,
        ipo_overlay=overlay,
        audit_overlay=audit_overlay,
    )
